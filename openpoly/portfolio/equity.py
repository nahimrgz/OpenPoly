"""Equity-curve reconstruction — folds the position ledger against sampled
order books into a realized + unrealized P&L time series.

Pure read side: no state, reads ``position`` and ``order_book_snapshot``.
``build_equity_curve`` recomputes from scratch per call, so it must stay linear
in the snapshot count: the book sampler writes ~10k rows per token per week and
the Overview tab polls this every few seconds. The best bid is extracted in
SQL (no per-row JSON parsing in Python), marks are looked up by bisection, and
the snapshot part of the time axis is thinned to ``MAX_POINTS`` — a chart needs
a few hundred points, not one per sample. The budget is split evenly across
positions, so a short trade keeps its shape next to a long, densely sampled
one. Open/close events and "now" are always kept, so the summary is exact.

Unrealized P&L marks each open position at the level-1 bid of its token's most
recent snapshot at-or-before the evaluated time (hold-last). That is the price
the exit executor would actually sell into — an honest "if I closed now" mark.
"""

from __future__ import annotations

import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import OrderBookSnapshot, PositionRow

# Upper bound on snapshot-derived points in the curve (events are added on top),
# shared evenly across positions.
MAX_POINTS = 500


@dataclass(frozen=True)
class EquityPoint:
    """Wallet equity at one instant. ``equity == realized + unrealized``."""

    ts: float  # epoch seconds, UTC
    equity: float
    realized: float
    unrealized: float


@dataclass(frozen=True)
class EquityCurve:
    """The full curve plus a summary snapshot (the last point's values)."""

    points: tuple[EquityPoint, ...]  # ascending by ts, de-duplicated
    realized: float
    unrealized: float
    total: float
    open_positions: int


def _mark_at(marks: tuple[list[float], list[float]], t: float) -> float | None:
    """Most recent bid at-or-before ``t`` (hold-last), or None if no mark
    exists yet at ``t``."""
    stamps, bids = marks
    i = bisect_right(stamps, t)
    return bids[i - 1] if i else None


def _thin(timestamps: list[float], limit: int) -> list[float]:
    """At most ``limit`` evenly strided entries of an ascending list, keeping
    the last one so the curve reaches the newest sample."""
    if len(timestamps) <= limit:
        return timestamps
    step = -(-len(timestamps) // limit)  # ceil division
    thinned = timestamps[::step]
    thinned[-1] = timestamps[-1]
    return thinned


def build_equity_curve(session_factory: sessionmaker[Session]) -> EquityCurve:
    """Reconstruct the realized + unrealized equity time series."""
    now = time.time()
    with session_factory() as session:
        positions = list(session.execute(select(PositionRow)).scalars().all())
        if not positions:
            return EquityCurve((), 0.0, 0.0, 0.0, 0)

        # Per-token level-1 bid series as parallel (timestamps, bids) lists,
        # ascending by recorded_at. Empty books yield NULL and are dropped.
        best_bid = func.json_extract(OrderBookSnapshot.bids_json, "$[0][0]")
        marks: dict[str, tuple[list[float], list[float]]] = {}
        for token_id in {p.token_id for p in positions}:
            rows = session.execute(
                select(OrderBookSnapshot.recorded_at, best_bid)
                .where(OrderBookSnapshot.token_id == token_id, best_bid.is_not(None))
                .order_by(OrderBookSnapshot.recorded_at)
            ).all()
            marks[token_id] = ([r[0] for r in rows], [float(r[1]) for r in rows])

    # Time axis: every open/close event + now, plus each position's in-window
    # snapshots thinned to its share of the point budget.
    events: set[float] = {now}
    in_window: set[float] = set()
    per_position = max(1, MAX_POINTS // len(positions))
    for p in positions:
        events.add(p.opened_at)
        if p.closed_at is not None:
            events.add(p.closed_at)
        end = p.closed_at if p.closed_at is not None else now
        stamps = marks.get(p.token_id, ([], []))[0]
        window = stamps[bisect_left(stamps, p.opened_at) : bisect_right(stamps, end)]
        in_window.update(_thin(window, per_position))
    timeline = sorted(events | in_window)

    points: list[EquityPoint] = []
    for t in timeline:
        realized = 0.0
        unrealized = 0.0
        for p in positions:
            if p.closed_at is not None and p.closed_at <= t:
                realized += p.realized_pnl or 0.0
            elif p.opened_at <= t:
                mark = _mark_at(marks.get(p.token_id, ([], [])), t)
                if mark is None:
                    mark = p.avg_entry_price
                unrealized += (mark - p.avg_entry_price) * p.qty
        points.append(
            EquityPoint(
                ts=t, equity=realized + unrealized, realized=realized, unrealized=unrealized
            )
        )

    last = points[-1]
    return EquityCurve(
        points=tuple(points),
        realized=last.realized,
        unrealized=last.unrealized,
        total=last.equity,
        open_positions=sum(1 for p in positions if p.status == "open"),
    )

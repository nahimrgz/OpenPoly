"""Shadow picks: what the ``min_confidence`` filter turned down.

The analyzer skips a pick whose confidence is below ``min_confidence``. Such a
pick never reaches entry, so the ledger alone cannot say whether the filter
discards losers or winners. This module keeps that record:

- ``choose_shadow_side`` prices a discarded pick the way entry v1 would:
  the side with the larger edge (fair − ask) among the sides v1 could quote —
  a level-1 ask above 0 and a depth-guarded bid (``_read_quote``).
- ``ShadowStore.record`` persists it (``shadow_pick``). The orchestrator calls
  it off the event loop, from the cached books, with no extra fetch.
- ``shadow_report`` marks each stored pick at fixed horizons against the
  persisted order-book snapshots and groups the results by confidence, next to
  the real closed positions grouped by their entry confidence. It backs
  ``GET /api/analytics/shadow`` and ``scripts/shadow_report.py``.

The return of a pick at a horizon is ``(bid − ask) / ask``: buy at the ask the
entry would have paid, mark at the depth-guarded bid of the first snapshot
taken at or after the horizon. It is a mark-to-market, not a settled outcome.
Snapshot retention (``order_book_retention_days``, 7 by default) bounds how
far back a horizon can still be measured. Clocks are UTC.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import OrderBookSnapshot, ShadowPickRow
from openpoly.markets.models import OrderBook
from openpoly.markets.pricing import first_level_with_size

HORIZONS_HOURS: tuple[int, ...] = (6, 24, 48)


@dataclass(frozen=True)
class ShadowQuote:
    """The side entry would have bought and its quote. All None when neither
    side had a usable ask."""

    side: str | None = None
    token_id: str | None = None
    ask: float | None = None
    bid: float | None = None
    edge: float | None = None


def choose_shadow_side(
    p_yes: float,
    *,
    yes: tuple[str | None, OrderBook | None],
    no: tuple[str | None, OrderBook | None],
) -> ShadowQuote:
    """The side with the larger edge, mirroring entry v1's side choice.

    ``yes`` / ``no`` are ``(token_id, cached book)``. A side counts only where
    entry v1's ``_read_quote`` would quote it: a token, a book with asks and
    bids, a level-1 ask above 0 (the price v1 pays) and a depth-guarded bid
    (first bid level with ≥ 5 shares). Ties go to YES, as in v1. Gates that
    come after the side choice (spread, edge, portfolio) are not applied: the
    shadow records what the pick was, not whether every gate would pass."""
    best: ShadowQuote | None = None
    for side, (token, book), fair in (("yes", yes, p_yes), ("no", no, 1.0 - p_yes)):
        if token is None or book is None or not book.asks or not book.bids:
            continue
        ask = book.asks[0][0]
        bid = first_level_with_size(book.bids)
        if ask <= 0.0 or bid is None:
            continue
        edge = fair - ask
        if best is None or best.edge is None or edge > best.edge:
            best = ShadowQuote(side=side, token_id=token, ask=ask, bid=bid, edge=edge)
    return best or ShadowQuote()


class ShadowStore:
    """Writes ``shadow_pick`` rows. Without a session factory (the DB is not
    up yet) it records nothing: shadow bookkeeping must never break the
    pipeline."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._factory = session_factory

    def configure(self, session_factory: sessionmaker[Session] | None) -> None:
        self._factory = session_factory

    def record(
        self,
        *,
        ts: float,
        news_id: str | None,
        market_id: str,
        p_model: float,
        confidence: str,
        quote: ShadowQuote | None,
    ) -> None:
        if self._factory is None:
            return
        q = quote or ShadowQuote()
        row = ShadowPickRow(
            ts=ts,
            news_id=news_id,
            market_id=market_id,
            p_model=p_model,
            confidence=confidence,
            side=q.side,
            token_id=q.token_id,
            ask=q.ask,
            bid=q.bid,
            edge=q.edge,
        )
        with self._factory() as session:
            session.add(row)
            session.commit()


# Process-wide store; the FastAPI lifespan configures it once the DB is up.
shadow_store = ShadowStore()


# ---------- report ----------


class _ClosedPosition(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def status(self) -> str: ...
    @property
    def entry_confidence(self) -> str | None: ...
    @property
    def realized_pnl(self) -> float | None: ...


def _bid_of(bids_json: str) -> float | None:
    try:
        levels = [(float(p), float(s)) for p, s in json.loads(bids_json)]
    except (ValueError, TypeError):
        return None
    return first_level_with_size(levels)


def _summary(returns: list[float]) -> dict[str, Any]:
    n = len(returns)
    return {
        "count": n,
        "mean_return": sum(returns) / n if n else None,
        "win_rate": sum(1 for r in returns if r > 0) / n if n else None,
    }


def _mark_at(session: Session, token: str, at: float) -> float | None:
    """Depth-guarded bid of the first snapshot of ``token`` at or after ``at``."""
    stmt = (
        select(OrderBookSnapshot.bids_json)
        .where(OrderBookSnapshot.token_id == token, OrderBookSnapshot.recorded_at >= at)
        .order_by(OrderBookSnapshot.recorded_at)
        .limit(1)
    )
    row = session.execute(stmt).first()
    return _bid_of(row[0]) if row is not None else None


def _latest_mark(session: Session, token: str, after: float) -> float | None:
    stmt = (
        select(OrderBookSnapshot.bids_json)
        .where(OrderBookSnapshot.token_id == token, OrderBookSnapshot.recorded_at > after)
        .order_by(OrderBookSnapshot.recorded_at.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    return _bid_of(row[0]) if row is not None else None


def shadow_report(
    session_factory: sessionmaker[Session],
    *,
    positions: Iterable[_ClosedPosition],
    cost_basis: Mapping[int, float],
    now: float | None = None,
) -> dict[str, Any]:
    """Shadow picks marked at each horizon, grouped by confidence, plus the
    real closed positions grouped by entry confidence.

    Per confidence and horizon: ``count`` / ``mean_return`` / ``win_rate`` of
    the picks that could be marked, ``pending`` (horizon not reached yet) and
    ``no_data`` (no quote at decision time, or no snapshot at or after the
    horizon — e.g. pruned, or the market left the catalog). ``latest`` marks
    at the newest snapshot after the pick.
    """
    now = time.time() if now is None else now
    keys = [f"{h}h" for h in HORIZONS_HOURS] + ["latest"]
    groups: dict[str, dict[str, Any]] = {}
    with session_factory() as session:
        picks = session.execute(select(ShadowPickRow).order_by(ShadowPickRow.ts)).scalars().all()
        for pick in picks:
            g = groups.setdefault(
                pick.confidence,
                {
                    "picks": 0,
                    "_r": {k: [] for k in keys},
                    "_p": dict.fromkeys(keys, 0),
                    "_n": dict.fromkeys(keys, 0),
                },
            )
            g["picks"] += 1
            if pick.token_id is None or not pick.ask:
                for k in keys:
                    g["_n"][k] += 1
                continue
            for h in HORIZONS_HOURS:
                k = f"{h}h"
                at = pick.ts + h * 3600.0
                if at > now:
                    g["_p"][k] += 1
                    continue
                mark = _mark_at(session, pick.token_id, at)
                if mark is None:
                    g["_n"][k] += 1
                else:
                    g["_r"][k].append((mark - pick.ask) / pick.ask)
            latest = _latest_mark(session, pick.token_id, pick.ts)
            if latest is None:
                g["_n"]["latest"] += 1
            else:
                g["_r"]["latest"].append((latest - pick.ask) / pick.ask)

    shadow = {
        conf: {
            "picks": g["picks"],
            "horizons": {
                k: {**_summary(g["_r"][k]), "pending": g["_p"][k], "no_data": g["_n"][k]}
                for k in keys
            },
        }
        for conf, g in groups.items()
    }

    real_returns: dict[str, list[float]] = {}
    for p in positions:
        cost = cost_basis.get(p.id)
        if p.status != "closed" or p.realized_pnl is None or not cost:
            continue
        real_returns.setdefault(p.entry_confidence or "unknown", []).append(p.realized_pnl / cost)

    return {
        "generated_at": now,
        "horizons_hours": list(HORIZONS_HOURS),
        "shadow": shadow,
        "real": {conf: _summary(rs) for conf, rs in real_returns.items()},
    }

"""Portfolio endpoints — ``GET /api/positions``, ``GET /api/fills`` (read side)
and ``POST /api/positions/{id}/close`` (manual close).

The ``fill`` ledger is the source of truth; ``position`` is its materialized
projection. Reads are newest-first, bounded by ``limit``. The manual close
routes one open position through ``executor.execute_sell`` (close_reason
``manual``) — the same fill path the ExitMonitor uses, and therefore under the
same single-writer discipline: both close routes consult (and hold) the
in-flight claim in ``runtime.closing_registry``.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy.orm import Session, sessionmaker

from openpoly.api.security import require_api_token
from openpoly.analytics.calibration import calibration_report
from openpoly.db.engine import get_session_factory
from openpoly.execution import executor
from openpoly.portfolio import PortfolioStore
from openpoly.portfolio.equity import build_equity_curve
from openpoly.runtime.closing_registry import clear_closing, is_closing, mark_closing

router = APIRouter(prefix="/api", tags=["portfolio"])

LIMIT_DEFAULT = 100
LIMIT_MAX = 500
# Calibration wants the whole trading history, not a page of it — the report is
# only meaningful at n ≥ 100 per bucket. Still bounded: this is a per-request
# scan of the position projection, not a cursor.
CALIBRATION_LIMIT = 5000


def get_portfolio_store() -> PortfolioStore:
    """Default dependency — a PortfolioStore on the process engine.
    Overridable via ``app.dependency_overrides`` in tests."""
    return PortfolioStore(get_session_factory())


def _clamp(limit: int) -> int:
    return max(1, min(limit, LIMIT_MAX))


@router.get("/positions")
def list_positions(
    limit: int = LIMIT_DEFAULT,
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Recent positions (open + closed), newest first.

    Each row is augmented with ``market_question`` + ``analyzer_decisions``
    (same shape and fallback semantics as ``/positions/{id}`` — see that
    route's docstring). Cost: N catalog scans (O(~50) each) + N+1 SQLite
    queries for news_id + N analyzer_log scans (O(~200) each); at the
    LIMIT_DEFAULT=100 cap, well under 10ms total. Card-style UI relies on
    these being available list-wide so it can render question / rationale
    without fanning out to /positions/{id} per row.
    """
    rows = store.list_positions(_clamp(limit))
    positions: list[dict[str, Any]] = []
    for record in rows:
        body = asdict(record)
        body["market_question"] = _lookup_market_question(record.condition_id)
        news_id = store.news_id_for_position(record.id)
        body["analyzer_decisions"] = _lookup_analyzer_decisions(news_id)
        positions.append(body)
    return {"positions": positions}


@router.get("/fills")
def list_fills(
    limit: int = LIMIT_DEFAULT,
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Recent fills — the ledger tail, newest first."""
    rows = store.list_fills(_clamp(limit))
    return {"fills": [asdict(f) for f in rows]}


@router.post("/positions/{position_id}/close", dependencies=[Depends(require_api_token)])
async def close_position(
    position_id: int,
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Manually close one open position at the level-1 bid (close_reason
    ``manual``). 404 if no such position; 409 if it is already closed, or if
    the exit monitor already has a sell in flight for it. The response body is
    the ``ExecResult`` — ``filled`` is False (with a ``skip_reason``) when the
    order book has no bid liquidity right now.

    A fill is capped by the level-1 bid's depth, so ``filled`` alone does not
    mean the position is gone: the body carries ``partial`` (and, when partial,
    ``remaining_qty``) so "sold 10 of 25, 15 still on the book" is not reported
    as a completed exit.

    Async, and it never awaits between the open-position lookup and the
    synchronous ``execute_sell`` — so the close is atomic with respect to the
    ExitMonitor tick on the same event loop.

    "Still open in the DB" is not by itself proof that nobody is selling this
    position: the exit monitor's ``execute_sell`` runs in a worker thread and
    the row stays ``open`` for the seconds the on-chain order takes. Closing it
    from here in that window is a second on-chain sell of tokens that are
    already gone, so an in-flight claim is a 409 — and this route takes the
    same claim for the duration of its own sell, so the settlement and
    reconciliation monitors leave it alone too.
    """
    held = next(
        (p for p in store.get_open_positions() if p.position_id == position_id),
        None,
    )
    if held is None:
        record = store.get_position(position_id)
        if record is None:
            raise HTTPException(status_code=404, detail="position not found")
        raise HTTPException(
            status_code=409,
            detail=f"position {position_id} is {record.status}, not open",
        )
    if is_closing(position_id):
        raise HTTPException(status_code=409, detail="exit_in_flight")
    mark_closing(position_id)
    try:
        result = executor.execute_sell(held, close_reason="manual", ts=time.time(), trigger=None)
    finally:
        clear_closing(position_id)
    body = asdict(result)
    if result.filled:
        # "Still open" is the authoritative test, not ``qty < held.qty``: a
        # sell leaving a sub-0.01 residue closes the row outright (see
        # ``PortfolioStore.record_sell``), and that is a completed exit.
        remaining = _remaining_qty(store, position_id)
        body["partial"] = remaining is not None
        if remaining is not None:
            body["remaining_qty"] = remaining
    return body


def _remaining_qty(store: PortfolioStore, position_id: int) -> float | None:
    """Open qty left on ``position_id`` after a sell, or None when it is flat."""
    record = store.get_position(position_id)
    if record is None or record.status != "open":
        return None
    return record.qty


@router.post("/positions/close-all", dependencies=[Depends(require_api_token)])
async def close_all_positions(
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Bulk-close every currently-open position via the same level-1 bid path
    as the single-close route. Routes each ``execute_sell`` independently:
    one position's failure (e.g. ``no_bid_liquidity``) does not abort the
    others. Always returns 200 with a per-position result list — the caller
    decides what to do with the residuals.

    Same atomicity story as ``close_position``: the open snapshot is taken
    once at the top and each ``execute_sell`` is synchronous; no await
    interleaves between them and the ExitMonitor tick. Positions the exit
    monitor is already selling (see ``closing_registry``) are skipped with
    ``exit_in_flight`` and reported in ``details`` rather than sold twice; each
    position this route does sell is claimed for the duration.

    A per-position ``ok`` means **flat**, not "the order went through": a sell
    capped by the level-1 bid's depth leaves the rest of the position on the
    book, and that is counted under ``partial``, never under ``filled``. Bulk
    close is the button an operator presses to be out of the market, so a
    summary that counted a half-sold position as closed would be answering a
    different question than the one being asked.
    """
    opens = store.get_open_positions()
    if not opens:
        return {
            "attempted": 0,
            "filled": 0,
            "partial": 0,
            "skipped": 0,
            "errored": 0,
            "details": [],
        }

    now = time.time()
    details: list[dict[str, Any]] = []
    filled = partial = skipped = errored = 0
    for held in opens:
        entry: dict[str, Any] = {
            "position_id": held.position_id,
            "market_id": held.market_id,
            "side": held.side,
        }
        if is_closing(held.position_id):
            entry["ok"] = False
            entry["skip_reason"] = "exit_in_flight"
            skipped += 1
            details.append(entry)
            continue
        mark_closing(held.position_id)
        try:
            result = executor.execute_sell(held, close_reason="manual", ts=now, trigger=None)
        except Exception as exc:  # noqa: BLE001 — isolate per-position failure
            entry["ok"] = False
            entry["error"] = repr(exc)[:200]
            errored += 1
        else:
            if result.filled:
                entry["price"] = result.price
                entry["qty"] = result.qty
                remaining = _remaining_qty(store, held.position_id)
                entry["partial"] = remaining is not None
                entry["ok"] = remaining is None
                if remaining is None:
                    filled += 1
                else:
                    entry["remaining_qty"] = remaining
                    partial += 1
            else:
                entry["ok"] = False
                entry["skip_reason"] = result.skip_reason
                skipped += 1
        finally:
            clear_closing(held.position_id)
        details.append(entry)
    return {
        "attempted": len(opens),
        "filled": filled,
        "partial": partial,
        "skipped": skipped,
        "errored": errored,
        "details": details,
    }


@router.get("/positions/{position_id}")
def get_position_by_id(
    position_id: int,
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """One position (open or closed) by id. 404 if no such position.

    Augments the raw PositionRecord with two best-effort lookups so the
    PositionDetail UI doesn't have to fan out to additional endpoints:

    - ``market_question``: catalog lookup by condition_id. ``None`` when
      the market is no longer catalogued (filtered out or resolved). UI
      falls back to displaying the condition_id.
    - ``analyzer_decisions``: list (newest-first) of every ``verdict=ok``
      analyzer call whose ``news_id`` matches this position's news_id.
      Each element carries rationale / p_model / confidence / ts. Empty
      list when the analyzer_log ring has evicted the original call
      (common for positions older than ~200 news events).
    """
    record = store.get_position(position_id)
    if record is None:
        raise HTTPException(status_code=404, detail="position not found")
    body = asdict(record)
    body["market_question"] = _lookup_market_question(record.condition_id)
    # PositionRecord doesn't carry news_id (it lives on the BUY fill row).
    # Look it up via the store + then scan analyzer_log.
    news_id = store.news_id_for_position(position_id)
    body["analyzer_decisions"] = _lookup_analyzer_decisions(news_id)
    return body


def _lookup_market_question(condition_id: str) -> str | None:
    """Resolve PositionRecord.condition_id → Market.question via the live
    catalog. Best-effort: returns ``None`` when the market is no longer
    catalogued (filtered or resolved). Frontend renders condition_id
    truncation as fallback."""
    from openpoly.markets.manager import manager as market_source_manager

    market = market_source_manager.store.get_by_condition(condition_id)
    return market.question if market is not None else None


def _lookup_analyzer_decisions(news_id: str | None) -> list[dict[str, Any]]:
    """All ``verdict=ok`` analyzer calls whose news_id matches, newest first.

    Scans the in-memory analyzer_log ring (default ~200 entries). Returns
    empty list when:
    - ``news_id`` is None (paper / manual position with no news linkage)
    - The matching call has been evicted from the ring (long-held positions)
    - The analyzer hit only errored or skipped on this news_id

    Returned dicts are flattened to UI-friendly shape: rationale, p_model,
    confidence, ts (no internal AnalyzerCall fields like
    news_content_preview / latency_ms / urgency — those are noise on the
    PositionDetail panel)."""
    if news_id is None:
        return []
    from openpoly.runtime.section_log import analyzer_log

    matches: list[dict[str, Any]] = []
    # analyzer_log.entries() returns oldest-first; reverse for newest-first.
    for entry in reversed(analyzer_log.entries()):
        if entry.verdict != "ok":
            continue
        if entry.news_id != news_id:
            continue
        matches.append(
            {
                "rationale": entry.rationale,
                "p_model": entry.p_model,
                "confidence": entry.confidence,
                "ts": entry.ts,
            }
        )
    return matches


@router.get("/analytics/calibration")
def get_calibration(
    store: PortfolioStore = Depends(get_portfolio_store),
) -> dict[str, Any]:
    """Is ``p_model`` calibrated? Closed positions bucketed by the model's
    probability for the side they held, with each bucket's win rate and mean
    realized return (see ``openpoly.analytics.calibration``).

    Read-only and derived per request — nothing here is persisted. Read it as:
    a bucket whose ``win_rate`` sits near its own midpoint, with ``count`` of
    at least ~100, is a bucket whose probability means something. That is the
    precondition for turning ``size_edge_multiplier_max`` above 1.0.
    """
    positions = store.list_positions(CALIBRATION_LIMIT)
    buckets = calibration_report(
        positions,
        store.buy_cost_basis([p.id for p in positions]),
    )
    return {
        "buckets": [asdict(b) for b in buckets],
        "sample_size": sum(b.count for b in buckets),
    }


@router.get("/portfolio/equity")
def get_equity_curve(
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Equity curve — realized + unrealized P&L over time, marked at the
    level-1 bid. Reconstructed per request from the position ledger + sampled
    order books; see ``openpoly.portfolio.equity``."""
    curve = build_equity_curve(factory)
    return {
        "points": [asdict(p) for p in curve.points],
        "summary": {
            "realized": curve.realized,
            "unrealized": curve.unrealized,
            "total": curve.total,
            "open_positions": curve.open_positions,
        },
    }

"""Tests for ExitMonitor — the timer-driven close loop.

The exit section is the real (pure) ThresholdExitV0 — TP / SL is driven by the
order-book bid price. The executor and portfolio are fakes, so the monitor's
own orchestration (mark → run → route → log) is tested in isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.tables import OrderBookSnapshot
from openpoly.execution import ExecResult
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import MarketStore
from openpoly.portfolio import HeldPosition, PortfolioStore
from openpoly.portfolio.models import PositionRecord
from openpoly.runtime.closing_registry import is_closing
from openpoly.runtime.exit_monitor import ExitMonitor
from openpoly.runtime.reconciliation_monitor import ReconciliationMonitor
from openpoly.runtime.section_log import exit_log
from openpoly.sections.exit.threshold_v0 import (
    ThresholdExitConfig,
    ThresholdExitV0,
)


@pytest.fixture(autouse=True)
def _isolate():
    """Fresh market catalog + exit log per test."""
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    exit_log.reset()
    yield
    market_source_manager.store = saved
    exit_log.reset()


def _held(
    position_id: int,
    token_id: str,
    *,
    avg: float = 0.40,
    market_id: str = "m1",
    side: str = "yes",
    qty: float = 20.0,
) -> HeldPosition:
    return HeldPosition(
        position_id=position_id,
        market_id=market_id,
        side=side,  # type: ignore[arg-type]
        token_id=token_id,
        condition_id=f"0x{market_id}",
        qty=qty,
        avg_entry_price=avg,
        opened_at=1.0,
    )


def _book(token_id: str, bid: float) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(bid, 100.0)],
        asks=[(bid + 0.02, 100.0)],
    )


class _FakePortfolio:
    def __init__(self, positions: list[HeldPosition]) -> None:
        self._positions = positions

    def get_open_positions(self) -> list[HeldPosition]:
        return list(self._positions)

    def get_position(self, position_id: int) -> PositionRecord | None:
        """The monitor re-reads a position's status before claiming it for
        sale — every position handed to this fake stays open."""
        for p in self._positions:
            if p.position_id == position_id:
                return PositionRecord(
                    id=p.position_id,
                    market_id=p.market_id,
                    side=p.side,
                    token_id=p.token_id,
                    condition_id=p.condition_id,
                    qty=p.qty,
                    avg_entry_price=p.avg_entry_price,
                    status="open",
                    opened_at=p.opened_at,
                    closed_at=None,
                    close_reason=None,
                    realized_pnl=None,
                )
        return None


class _FakeExecutor:
    """Records execute_sell calls; returns a canned ExecResult or raises."""

    def __init__(
        self,
        *,
        result: ExecResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[dict[str, object]] = []

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason: str,
        ts: float,
        trigger: str | None,
    ) -> ExecResult:
        self.calls.append(
            {
                "position_id": position.position_id,
                "close_reason": close_reason,
                "trigger": trigger,
            }
        )
        if self._exc is not None:
            raise self._exc
        return self._result or ExecResult.ok(
            price=0.55, qty=position.qty, position_id=position.position_id
        )


def _monitor(portfolio: _FakePortfolio, executor: _FakeExecutor) -> ExitMonitor:
    # take_profit ships off (the trailing lock is the primary exit path), but
    # these tests drive the monitor's own plumbing — mark → run → route → log —
    # through a take-profit close, so they opt the ceiling back in explicitly.
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True)),
        executor=executor,
        tick_interval_seconds=3600,
    )
    m.configure(portfolio)  # type: ignore[arg-type]
    return m


# ---------- close paths ----------


async def test_take_profit_triggers_execute_sell() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    ex = _FakeExecutor()  # default result: ExecResult.ok(price=0.55)
    await _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)._tick_once()
    # (0.55 - 0.40) / 0.40 = 0.375 ≥ 0.20 → take_profit
    assert ex.calls == [
        {
            "position_id": 1,
            "close_reason": "take_profit",
            "trigger": "take_profit",
        }
    ]
    e = exit_log.entries()[0]
    assert e.verdict == "ok"
    assert e.trigger == "take_profit"
    assert e.fill_price == 0.55
    assert e.realized_pnl == pytest.approx((0.55 - 0.40) * 20.0)


async def test_stop_loss_triggers_execute_sell() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.30)])
    ex = _FakeExecutor(result=ExecResult.ok(price=0.30, qty=20.0, position_id=1))
    await _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)._tick_once()
    # (0.30 - 0.40) / 0.40 = -0.25 ≤ -0.15 → stop_loss
    assert ex.calls[0]["close_reason"] == "stop_loss"
    assert exit_log.entries()[0].trigger == "stop_loss"


async def test_within_thresholds_holds() -> None:
    # v18: a within-threshold hold writes NO log entry (the ring keeps only
    # ok / error closes); tick telemetry records the position was evaluated.
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    await m._tick_once()
    assert ex.calls == []  # nothing closed
    assert exit_log.entries() == []  # no skip entry
    assert m.open_positions == 1
    assert m.blocked == 0
    assert m.last_tick_at is not None


async def test_no_order_book_blocked() -> None:
    # v18: no order book → can't evaluate → counted as blocked, no log entry.
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t-missing")]), ex)
    await m._tick_once()
    assert ex.calls == []
    assert exit_log.entries() == []
    assert m.open_positions == 1
    assert m.blocked == 1


async def test_empty_bids_blocked() -> None:
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[], asks=[(0.5, 100.0)])]
    )
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1")]), ex)
    await m._tick_once()
    assert ex.calls == []
    assert exit_log.entries() == []
    assert m.blocked == 1


async def test_tick_telemetry_open_and_blocked() -> None:
    # One evaluable (held within thresholds) + one blocked (no order book) →
    # open=2, blocked=1, and still zero log entries.
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor()
    positions = [_held(1, "t1", avg=0.40), _held(2, "t-missing", market_id="m2")]
    m = _monitor(_FakePortfolio(positions), ex)
    await m._tick_once()
    assert m.open_positions == 2
    assert m.blocked == 1
    assert exit_log.entries() == []
    assert m.last_tick_at is not None


# ---------- error handling ----------


async def test_execute_sell_raises_logged_as_error_sweep_continues() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55), _book("t2", bid=0.55)])
    ex = _FakeExecutor(exc=ValueError("position already closed"))
    positions = [
        _held(1, "t1", avg=0.40),
        _held(2, "t2", avg=0.40, market_id="m2"),
    ]
    await _monitor(_FakePortfolio(positions), ex)._tick_once()
    # Both TP-trigger → execute_sell raises on both → both logged error;
    # the first error did not abort the sweep.
    entries = exit_log.entries()
    assert [e.verdict for e in entries] == ["error", "error"]
    assert {e.position_id for e in entries} == {1, 2}


async def test_execute_sell_not_filled_logged_as_error() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    ex = _FakeExecutor(result=ExecResult.skip("no_bid_liquidity"))
    await _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)._tick_once()
    e = exit_log.entries()[0]
    assert e.verdict == "error"
    assert e.error is not None
    assert "no_bid_liquidity" in e.error


# ---------- no-op paths ----------


async def test_empty_positions_noop() -> None:
    ex = _FakeExecutor()
    await _monitor(_FakePortfolio([]), ex)._tick_once()
    assert exit_log.entries() == []
    assert ex.calls == []


async def test_not_configured_noop() -> None:
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=_FakeExecutor(),
    )
    await m._tick_once()  # no configure() — portfolio is None
    assert exit_log.entries() == []


# ---------- loop lifecycle ----------


async def test_tick_loop_start_stop() -> None:
    m = _monitor(_FakePortfolio([]), _FakeExecutor())
    assert m.state == "stopped"
    await m.start()
    assert m.state == "running"
    await asyncio.sleep(0)  # let the loop run one iteration
    await m.stop()
    assert m.state == "stopped"


async def test_stop_before_start_is_safe() -> None:
    m = _monitor(_FakePortfolio([]), _FakeExecutor())
    await m.stop()  # never started
    assert m.state == "stopped"


# ---------- peak tracking ----------


async def test_peak_persists_across_ticks_and_triggers_drawdown() -> None:
    # TP set very high so peak_drawdown is the *only* close trigger that can
    # fire over the price path 0.46 → 0.56 → 0.50 with entry 0.40. The peak
    # has to reach 0.52 to arm the trailing lock (30% of the $8 cost basis).
    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(
            ThresholdExitConfig(
                take_profit_pct=0.50,
                stop_loss_pct=0.50,
                peak_drawdown_pct=0.12,
            )
        ),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    portfolio = _FakePortfolio([_held(1, "t1", avg=0.40)])
    monitor.configure(portfolio)  # type: ignore[arg-type]
    store = market_source_manager.store

    # Tick 1: bid 0.46 → +15%, no trigger; peak seeded at 0.46.
    # v18: a hold writes no log entry — assert via the peak instead.
    store.set_order_books([_book("t1", bid=0.46)])
    await monitor._tick_once()
    assert monitor._peak[1] == pytest.approx(0.46)
    assert exit_log.entries() == []

    # Tick 2: bid climbs to 0.56; peak follows and the lock arms
    # (peak gain 0.16 × 20 = $3.20 ≥ the $2.40 floor).
    store.set_order_books([_book("t1", bid=0.56)])
    await monitor._tick_once()
    assert monitor._peak[1] == pytest.approx(0.56)
    assert exit_log.entries() == []

    # Tick 3: bid retreats to 0.50; peak stays at 0.56. Retrace 0.06 clears the
    # trailing distance max(2 ticks, 0.02 spread, 0.12 × 0.16) = 0.02 → close.
    store.set_order_books([_book("t1", bid=0.50)])
    await monitor._tick_once()
    last = exit_log.entries()[-1]
    assert last.verdict == "ok"
    assert last.trigger == "peak_drawdown"
    assert last.peak_price == pytest.approx(0.56)
    # On a successful close the peak entry is dropped.
    assert 1 not in monitor._peak


async def test_peak_tracked_on_hold() -> None:
    # v18: held within thresholds writes no entry, but the peak is still
    # tracked in-memory for the drawdown trigger.
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    await m._tick_once()
    assert exit_log.entries() == []
    assert m._peak[1] == pytest.approx(0.41)


async def test_bootstrap_peaks_rebuilds_from_snapshots(tmp_path) -> None:
    db_path = tmp_path / "openpoly_peak.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    sf = make_session_factory(engine)

    pf = PortfolioStore(sf)
    held = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )

    # Three snapshots after opened_at; peak bid = 0.55. One snapshot *before*
    # opened_at with a higher bid that must be ignored.
    with sf() as session:
        session.add_all(
            [
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=99.0,  # before open — ignored
                    bids_json=json.dumps([[0.99, 100]]),
                    asks_json=json.dumps([[1.0, 100]]),
                ),
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=110.0,
                    bids_json=json.dumps([[0.45, 100]]),
                    asks_json=json.dumps([[0.46, 100]]),
                ),
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=120.0,
                    bids_json=json.dumps([[0.55, 100]]),
                    asks_json=json.dumps([[0.56, 100]]),
                ),
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=130.0,
                    bids_json=json.dumps([[0.50, 100]]),
                    asks_json=json.dumps([[0.51, 100]]),
                ),
            ]
        )
        session.commit()

    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    monitor.configure(pf)
    monitor.bootstrap_peaks(sf)
    assert monitor._peak[held.position_id] == pytest.approx(0.55)


async def test_bootstrap_peaks_no_snapshot_falls_back_to_entry(tmp_path) -> None:
    db_path = tmp_path / "openpoly_peak2.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    sf = make_session_factory(engine)

    pf = PortfolioStore(sf)
    held = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )

    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    monitor.configure(pf)
    monitor.bootstrap_peaks(sf)
    # No snapshots after opened_at → peak defaults to avg_entry_price (0.40).
    assert monitor._peak[held.position_id] == pytest.approx(0.40)


# ---------- mark sanity (depth-guarded bid) ----------


async def test_thin_l1_bid_blocks_the_position_and_never_marks_at_the_mid() -> None:
    # A 1-share resting bid at 0.40 is a dust order, not a price — and the mid
    # is not a price the position can be sold at either: both executors sell
    # into the raw level-1 bid. Entry 0.45 with a 0.40 dust bid and a far 0.72
    # ask has a 0.56 mid, i.e. +24% — marking there would fire take_profit and
    # sell at 0.40, a loss. The position is blocked instead.
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.40, 1.0)], asks=[(0.72, 50.0)])]
    )
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.45)]), ex)
    await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert 1 not in m._peak


async def test_unmarkable_position_is_logged_once_until_the_state_changes() -> None:
    # A blocked position is invisible in the tick counters alone (they only
    # carry the last sweep), so the first tick that cannot mark it writes one
    # exit_log row. Repeat ticks in the same state stay silent — the ring must
    # not evict the ok / error closes.
    thin = OrderBook(token_id="t1", ts=1.0, bids=[(0.40, 1.0)], asks=[(0.72, 50.0)])
    market_source_manager.store.set_order_books([thin])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.45)]), ex)

    await m._tick_once()
    entries = exit_log.entries()
    assert len(entries) == 1
    assert entries[0].verdict == "skip"
    assert entries[0].reason == "no_executable_bid"
    assert entries[0].position_id == 1

    await m._tick_once()
    assert len(exit_log.entries()) == 1  # same state → no second row

    # The book recovers, the position is marked again, then goes thin once
    # more: that is a new occurrence and is logged again.
    market_source_manager.store.set_order_books([_book("t1", bid=0.46)])
    await m._tick_once()
    assert len(exit_log.entries()) == 1
    market_source_manager.store.set_order_books([thin])
    await m._tick_once()
    assert len(exit_log.entries()) == 2
    assert exit_log.entries()[-1].reason == "no_executable_bid"


async def test_mark_walks_to_first_bid_level_meeting_min_size() -> None:
    # L1 is a 1-share probe; the first level with real depth is 0.54, which is
    # where the position could actually be sold.
    market_source_manager.store.set_order_books(
        [
            OrderBook(
                token_id="t1",
                ts=1.0,
                bids=[(0.55, 1.0), (0.54, 50.0)],
                asks=[(0.60, 100.0)],
            )
        ]
    )
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.50)]), ex)
    await m._tick_once()
    assert ex.calls == []  # +8% on a 0.50 entry — held
    assert m._peak[1] == pytest.approx(0.54)


async def test_deep_l1_bid_is_used_as_is() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    await m._tick_once()
    assert m._peak[1] == pytest.approx(0.41)


async def test_thin_bid_with_no_ask_counts_as_blocked() -> None:
    # No depth-qualified bid → the position cannot be marked at a price it
    # could actually be sold at, so it is reported blocked rather than closed.
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.40, 1.0)], asks=[])]
    )
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.50)]), ex)
    await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert exit_log.entries()[-1].reason == "no_executable_bid"


async def test_min_mark_bid_size_is_configurable() -> None:
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.30, 1.0)], asks=[(0.52, 50.0)])]
    )
    ex = _FakeExecutor()
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=ex,
        tick_interval_seconds=3600,
        min_mark_bid_size=1.0,  # accept the 1-share bid
    )
    m.configure(_FakePortfolio([_held(1, "t1", avg=0.40)]))  # type: ignore[arg-type]
    await m._tick_once()
    assert ex.calls[0]["close_reason"] == "stop_loss"


async def test_marked_position_carries_spread_from_the_book() -> None:
    captured: list[object] = []

    class _Recorder:
        def run(self, input):  # noqa: ANN001, ANN201
            captured.append(input.payload)
            return ThresholdExitV0(ThresholdExitConfig()).run(input)

    market_source_manager.store.set_order_books(
        [
            OrderBook(
                token_id="t1",
                ts=1.0,
                bids=[(0.50, 100.0)],
                asks=[(0.53, 100.0)],
            )
        ]
    )
    m = ExitMonitor(
        exit_section=_Recorder(),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    m.configure(_FakePortfolio([_held(1, "t1", avg=0.48)]))  # type: ignore[arg-type]
    await m._tick_once()
    marked = captured[0]
    assert marked.spread == pytest.approx(0.03)
    assert marked.tick_size is None


async def test_bootstrap_peaks_ignores_thin_snapshot_bids(tmp_path) -> None:
    db_path = tmp_path / "openpoly_peak_thin.db"
    engine = make_engine(f"sqlite:///{db_path}")
    init_db(engine)
    sf = make_session_factory(engine)

    pf = PortfolioStore(sf)
    held = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )
    with sf() as session:
        session.add_all(
            [
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=110.0,
                    bids_json=json.dumps([[0.50, 100]]),
                    asks_json=json.dumps([[0.51, 100]]),
                ),
                # A 1-share spike bid over a real 0.60 book: must not inflate
                # the peak, or the very first live tick looks like a drawdown.
                OrderBookSnapshot(
                    token_id="t1",
                    recorded_at=120.0,
                    bids_json=json.dumps([[0.90, 1], [0.60, 100]]),
                    asks_json=json.dumps([[0.91, 100]]),
                ),
            ]
        )
        session.commit()

    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=_FakeExecutor(),
        tick_interval_seconds=3600,
    )
    monitor.configure(pf)
    monitor.bootstrap_peaks(sf)
    # The 0.90 dust bid is rejected and the walk lands on the 0.60 level that
    # actually had depth, so that — not 0.90 — becomes the peak.
    assert monitor._peak[held.position_id] == pytest.approx(0.60)


# ---------- observe_price push hook ----------


async def test_observe_price_lifts_peak_between_ticks() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.46)])
    ex = _FakeExecutor()
    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(
            ThresholdExitConfig(take_profit_enabled=False, stop_loss_pct=0.50)
        ),
        executor=ex,
        tick_interval_seconds=3600,
    )
    monitor.configure(_FakePortfolio([_held(1, "t1", avg=0.40)]))  # type: ignore[arg-type]
    await monitor._tick_once()
    assert monitor._peak[1] == pytest.approx(0.46)

    # Between ticks the book sampler observes a spike the 120s tick would miss.
    monitor.observe_price("t1", 0.56)
    assert monitor._peak[1] == pytest.approx(0.56)
    # A lower observation never lowers the peak.
    monitor.observe_price("t1", 0.52)
    assert monitor._peak[1] == pytest.approx(0.56)

    # Next tick sees 0.50 and closes against the observed peak, not 0.46.
    market_source_manager.store.set_order_books([_book("t1", bid=0.50)])
    await monitor._tick_once()
    assert exit_log.entries()[-1].trigger == "peak_drawdown"
    assert exit_log.entries()[-1].peak_price == pytest.approx(0.56)


async def test_observe_price_ignores_unwatched_tokens() -> None:
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    m.observe_price("t-unknown", 0.99)
    assert m._peak == {}


async def test_observe_book_applies_the_depth_guard() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.46)])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    await m._tick_once()
    # A 1-share spike bid at 0.90 over a real 0.60 book must not become the
    # peak — the walk lands on the first level with depth.
    m.observe_book(
        OrderBook(
            token_id="t1",
            ts=2.0,
            bids=[(0.90, 1.0), (0.60, 100.0)],
            asks=[(0.91, 100.0)],
        )
    )
    assert m._peak[1] == pytest.approx(0.60)
    m.observe_book(OrderBook(token_id="t1", ts=3.0, bids=[(0.95, 50.0)], asks=[(0.96, 100.0)]))
    assert m._peak[1] == pytest.approx(0.95)


async def test_observe_book_thin_ladder_with_wide_ask_does_not_raise_the_peak() -> None:
    # Every bid level is dust and the ask is far away, so the mid (0.66) is way
    # above anything the position could be sold at. A peak raised to a
    # non-executable price makes the trailing lock measure a retrace from a
    # price that never existed — the observation must be dropped entirely.
    market_source_manager.store.set_order_books([_book("t1", bid=0.46)])
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), _FakeExecutor())
    await m._tick_once()
    m.observe_book(
        OrderBook(
            token_id="t1",
            ts=2.0,
            bids=[(0.44, 1.0), (0.43, 2.0)],
            asks=[(0.88, 100.0)],
        )
    )
    assert m._peak[1] == pytest.approx(0.46)


async def test_observe_book_with_no_bids_is_a_noop() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.46)])
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), _FakeExecutor())
    await m._tick_once()
    m.observe_book(OrderBook(token_id="t1", ts=2.0, bids=[], asks=[]))
    assert m._peak[1] == pytest.approx(0.46)


# ---------- blocking I/O must not stall the event loop ----------


async def test_execute_sell_runs_off_the_event_loop() -> None:
    """The live executor sleeps seconds inside execute_sell (CTF cache polling
    + close-persist retries). It must run in a worker thread, or every other
    runtime task — WS reconnects, market polls — stalls behind it."""
    import time as _time

    class _SlowExecutor(_FakeExecutor):
        def execute_sell(self, position, *, close_reason, ts, trigger):  # noqa: ANN001, ANN201
            _time.sleep(0.3)
            return super().execute_sell(position, close_reason=close_reason, ts=ts, trigger=trigger)

    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), _SlowExecutor())

    beats = 0

    async def _heartbeat() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.01)
            beats += 1

    hb = asyncio.create_task(_heartbeat())
    try:
        await m._tick_once()
    finally:
        hb.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await hb
    # ~30 beats are possible in 0.3s; anything above a handful proves the loop
    # kept running while execute_sell slept.
    assert beats >= 10, f"event loop stalled: only {beats} heartbeats"


async def test_closed_position_stops_being_observed() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40)]), ex)
    await m._tick_once()  # +37.5% → take_profit → closed
    assert ex.calls
    assert m._peak == {}
    # A book delivered before the next sweep must not resurrect the peak.
    m.observe_book(_book("t1", bid=0.60))
    assert m._peak == {}


# ---------- in-flight sell guard ----------


class _SleepingExecutor(_FakeExecutor):
    """Executor whose sell blocks the worker thread long enough for another
    monitor's tick to interleave on the event loop."""

    def __init__(self, seconds: float = 0.3, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self._seconds = seconds

    def execute_sell(self, position, *, close_reason, ts, trigger):  # noqa: ANN001, ANN201
        import time as _time

        _time.sleep(self._seconds)
        return super().execute_sell(position, close_reason=close_reason, ts=ts, trigger=trigger)


def _portfolio_with_open_position(tmp_path, name: str):  # noqa: ANN001, ANN201
    engine = make_engine(f"sqlite:///{tmp_path}/{name}.db")
    init_db(engine)
    pf = PortfolioStore(make_session_factory(engine))
    held = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )
    return pf, held


async def test_position_is_registered_as_closing_while_the_sell_is_in_flight(tmp_path) -> None:
    # execute_sell runs in a worker thread, so the event loop is free while the
    # on-chain sell is still open. Any other monitor that closes positions must
    # be able to see that this one is mid-flight.
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    pf, held = _portfolio_with_open_position(tmp_path, "closing_flag")
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True)),
        executor=_SleepingExecutor(0.3),
        tick_interval_seconds=3600,
    )
    m.configure(pf)

    tick = asyncio.create_task(m._tick_once())
    await asyncio.sleep(0.05)
    assert is_closing(held.position_id)
    await tick
    assert not is_closing(held.position_id)


async def test_closing_flag_is_cleared_when_the_sell_raises(tmp_path) -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    pf, held = _portfolio_with_open_position(tmp_path, "closing_raise")
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True)),
        executor=_SleepingExecutor(0.05, exc=ValueError("boom")),
        tick_interval_seconds=3600,
    )
    m.configure(pf)
    await m._tick_once()
    assert exit_log.entries()[-1].verdict == "error"
    assert not is_closing(held.position_id)


async def test_reconciliation_does_not_close_a_position_being_sold(tmp_path) -> None:
    # The race this prevents: the exit monitor yields the loop inside
    # to_thread, reconciliation sees the (empty) on-chain holdings, closes the
    # same position id, and the real fill can no longer be persisted.
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    pf, held = _portfolio_with_open_position(tmp_path, "recon_race")
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True)),
        executor=_SleepingExecutor(0.3),
        tick_interval_seconds=3600,
    )
    m.configure(pf)

    async def _no_holdings() -> set[tuple[str, str]]:
        return set()

    rm = ReconciliationMonitor(holdings_fetcher=_no_holdings, grace_seconds=0)
    rm.configure(pf)

    tick = asyncio.create_task(m._tick_once())
    await asyncio.sleep(0.05)
    await rm._tick_once()
    record = pf.get_position(held.position_id)
    assert record is not None
    assert record.status == "open"  # reconciliation deferred to the next tick
    await tick


async def test_stop_waits_for_an_in_flight_sell_to_record_its_close() -> None:
    """stop() cancels the tick loop, but a sell already handed to a worker
    thread cannot be cancelled — it completes on-chain and in the DB. If the
    bookkeeping after the await is dropped, that close leaves no exit_log entry
    and the position keeps a stale peak. stop() must drain it."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True)),
        executor=_SleepingExecutor(0.2),
        tick_interval_seconds=3600,
    )
    m.configure(_FakePortfolio([_held(1, "t1", avg=0.40)]))  # type: ignore[arg-type]
    await m.start()
    await asyncio.sleep(0.05)  # the tick is inside the sell
    assert is_closing(1)

    await m.stop()
    assert m.state == "stopped"
    entries = exit_log.entries()
    assert [e.verdict for e in entries] == ["ok"]
    assert entries[0].trigger == "take_profit"
    assert entries[0].fill_price == 0.55
    assert m._peak == {}  # peak cleanup ran
    assert not is_closing(1)


async def test_stop_is_safe_when_no_sell_is_in_flight() -> None:
    m = _monitor(_FakePortfolio([]), _FakeExecutor())
    await m.start()
    await asyncio.sleep(0)
    await m.stop()
    assert m.state == "stopped"


# ---------- stale-snapshot guard ----------


def _portfolio_with_two_open_positions(tmp_path, name: str):  # noqa: ANN001, ANN201
    engine = make_engine(f"sqlite:///{tmp_path}/{name}.db")
    init_db(engine)
    pf = PortfolioStore(make_session_factory(engine))
    first = pf.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n1",
    )
    second = pf.open_position(
        market_id="m2",
        side="yes",
        token_id="t2",
        condition_id="0xm2",
        qty=20.0,
        price=0.40,
        ts=100.0,
        news_id="n2",
    )
    return pf, first, second


async def test_position_closed_mid_sweep_is_not_sold_from_the_stale_snapshot(tmp_path) -> None:
    """The sweep's open-position list is read once, then the first sell hands
    the loop back for seconds. Another monitor can close a *different* position
    in that window; selling it from the stale snapshot hits an already-closed
    row (paper: ValueError → a spurious ``error`` row; live: a real on-chain
    sell that can never be persisted). Re-read the row before claiming it."""
    import threading
    import time as _time

    market_source_manager.store.set_order_books([_book("t1", bid=0.55), _book("t2", bid=0.55)])
    pf, first, second = _portfolio_with_two_open_positions(tmp_path, "stale_snapshot")

    selling = threading.Event()

    class _SignallingExecutor(_FakeExecutor):
        """Blocks the worker thread on the first position's sell and tells the
        event loop it is in flight, so the race is deterministic."""

        def execute_sell(self, position, *, close_reason, ts, trigger):  # noqa: ANN001, ANN201
            if position.position_id == first.position_id:
                selling.set()
                _time.sleep(0.3)
            return super().execute_sell(position, close_reason=close_reason, ts=ts, trigger=trigger)

    ex = _SignallingExecutor()
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True)),
        executor=ex,
        tick_interval_seconds=3600,
    )
    m.configure(pf)

    async def _close_the_other_position() -> None:
        while not selling.is_set():
            await asyncio.sleep(0.01)
        pf.close_position(second.position_id, sell_price=0.50, ts=200.0, close_reason="settlement")

    closer = asyncio.create_task(_close_the_other_position())
    try:
        await asyncio.wait_for(m._tick_once(), timeout=5.0)
    finally:
        closer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await closer

    assert [c["position_id"] for c in ex.calls] == [first.position_id]
    skips = [e for e in exit_log.entries() if e.verdict == "skip"]
    assert [(e.position_id, e.reason) for e in skips] == [
        (second.position_id, "position_no_longer_open")
    ]


# ---------- sub-one-share remainder (dust) ----------


async def test_dust_position_is_skipped_once_and_never_sold() -> None:
    """A remainder below one share is not a placeable order: the executors
    skip it and the row stays open until settlement. Evaluating it every tick
    therefore produced a CloseIntent -> an unfilled sell -> an ``error`` row,
    every 120s forever, which evicts the real closes from the 200-entry ring
    and inflates the error counter. The monitor must not evaluate it at all:
    one ``skip`` row per position, no sell, and it is not ``blocked``."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    ex = _FakeExecutor()
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40, qty=0.6)]), ex)

    for _ in range(3):
        await m._tick_once()

    assert ex.calls == []
    entries = exit_log.entries()
    assert len(entries) == 1
    assert entries[0].verdict == "skip"
    assert entries[0].reason == "dust_remainder"
    assert entries[0].position_id == 1
    assert [e for e in entries if e.verdict == "error"] == []
    assert m.open_positions == 1
    assert m.blocked == 0


async def test_sellable_position_is_unaffected_by_the_dust_guard() -> None:
    """Six shares is a placeable order — the dust guard must not touch it."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    ex = _FakeExecutor(result=ExecResult.ok(price=0.55, qty=6.0, position_id=1))
    m = _monitor(_FakePortfolio([_held(1, "t1", avg=0.40, qty=6.0)]), ex)

    await m._tick_once()

    assert [c["position_id"] for c in ex.calls] == [1]
    entries = exit_log.entries()
    assert [e.verdict for e in entries] == ["ok"]
    assert entries[0].trigger == "take_profit"


async def test_dust_marker_is_pruned_when_the_position_stops_being_open() -> None:
    """The dedup set is pruned against the current open list every tick, like
    ``_unmarkable`` — it must not grow across the process lifetime."""
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    ex = _FakeExecutor()
    pf = _FakePortfolio([_held(1, "t1", avg=0.40, qty=0.6)])
    m = _monitor(pf, ex)

    await m._tick_once()
    assert m._dust == {1}

    pf._positions = []
    await m._tick_once()
    assert m._dust == set()

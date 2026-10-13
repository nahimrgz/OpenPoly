"""Regression tests for the second max review of fix/audit-2026-10 (exit,
store and live-sell findings S1–S6, S10, S12–S14, S15).

No test here patches the process-wide ``time`` module: the exit monitor takes
an injected clock, and the live executor's sleeps are rebound in its own
module namespace by ``no_sleep``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from py_clob_client_v2.exceptions import PolyException

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.execution import ExecResult, PaperExecutor
from openpoly.execution import live_executor as le_mod
from openpoly.execution.live_executor import LiveExecutor
from openpoly.markets.manager import (
    GONE_BOOK_RECHECK_SECONDS,
    MarketSourceManager,
)
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import MarketStore, book_clock
from openpoly.portfolio import PortfolioStore
from openpoly.runtime.exit_monitor import ExitMonitor
from openpoly.runtime.section_log import exit_log
from openpoly.sections._base import SectionInput
from openpoly.sections.exit.threshold_v0 import (
    CloseIntent,
    MarkedPosition,
    ThresholdExitConfig,
    ThresholdExitV0,
)
from openpoly.sections.exit.threshold_v1 import (
    THIN_NO_ASK_CONFIRM_TICKS,
    ThresholdExitConfigV1,
    ThresholdExitV1,
)
from tests.test_exit_monitor import (  # noqa: F401 — autouse fixture
    _FakeExecutor,
    _FakePortfolio,
    _held,
    _isolate,
)
from tests.test_live_executor import (  # noqa: F401 — autouse fixtures
    _FakeClob,
    _isolate_market_store,
    _market,
    _populate,
    _zero_fill_response,
    no_sleep,
)
from tests.test_live_executor import _held as _live_held


class _Clock:
    """Injected monitor clock."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _monitor(
    ex: Any,
    *,
    section: Any = None,
    source: MarketSourceManager | None = None,
    clock: _Clock | None = None,
    portfolio: Any = None,
) -> ExitMonitor:
    m = ExitMonitor(
        exit_section=section if section is not None else ThresholdExitV1(ThresholdExitConfigV1()),
        executor=ex,
        tick_interval_seconds=10,
        book_source=source,
        clock=clock if clock is not None else _Clock(),
    )
    m.configure(portfolio or _FakePortfolio([_held(1, "t1", avg=0.50)]))
    return m


def _set(*, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
    """A new fetch of token t1's book (stamped now)."""
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=bids, asks=asks)]
    )


def _reasons() -> list[str | None]:
    return [e.reason for e in exit_log.entries()]


@pytest.fixture
def portfolio(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


# ---------- S1: no sweep down to a stink bid ----------


@pytest.mark.parametrize("section", [ThresholdExitV0(ThresholdExitConfig()), None])
async def test_s1_a_stink_bid_on_a_bid_pull_does_not_fire_a_stop(section: Any) -> None:
    """Bid pull: only dust at 0.45, a deep 0.10 × 1000 stink bid. Marking at
    0.10 fired stop_loss and swept every level down to it."""
    _set(bids=[(0.45, 2.0), (0.10, 1000.0)], asks=[(0.46, 100.0)])
    ex = _FakeExecutor()
    m = _monitor(ex, section=section)
    for _ in range(3):
        _set(bids=[(0.45, 2.0), (0.10, 1000.0)], asks=[(0.46, 100.0)])
        await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert _reasons() == ["no_executable_bid"]


async def test_s1_a_normal_close_floors_at_the_best_bid() -> None:
    """take_profit on a dust top over real size: the floor is level 1."""
    section = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True))
    _set(bids=[(0.62, 2.0), (0.61, 100.0)], asks=[(0.63, 100.0)])
    ex = _FakeExecutor()
    await _monitor(ex, section=section)._tick_once()
    assert [c["trigger"] for c in ex.calls] == ["take_profit"]
    assert ex.floors == [pytest.approx(0.62)]


async def test_s1_a_normal_paper_stop_sells_level_one_only(portfolio) -> None:
    held = portfolio.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        price=0.50,
        qty=20.0,
        ts=100.0,
        news_id="n",
    )
    _set(bids=[(0.34, 2.0), (0.33, 100.0)], asks=[(0.35, 100.0)])
    m = _monitor(PaperExecutor(portfolio), portfolio=portfolio)
    await m._tick_once()
    entries = exit_log.entries()
    assert [(e.verdict, e.trigger) for e in entries] == [("ok", "stop_loss")]
    assert entries[0].fill_price == pytest.approx(0.34)
    rec = portfolio.get_position(held.position_id)
    assert rec is not None and rec.status == "open" and rec.qty == pytest.approx(18.0)


async def test_s1_a_confirmed_thin_hard_stop_floors_past_the_hard_stop() -> None:
    _set(bids=[(0.30, 2.0)], asks=[(0.32, 50.0)])
    ex = _FakeExecutor()
    await _monitor(ex)._tick_once()
    assert [c["trigger"] for c in ex.calls] == ["stop_loss"]
    # entry × (1 − hard_stop_loss_pct − 0.10) = 0.50 × 0.60
    assert ex.floors == [pytest.approx(0.30)]


# ---------- S2: never a sub-tick price ----------


def test_s2_the_thin_floor_is_never_below_one_tick() -> None:
    pos = MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.015,
        qty=1000.0,
        current_price=0.005,
        peak_price=0.015,
        ask=0.006,
        thin=True,
    )
    out = ThresholdExitV1(ThresholdExitConfigV1()).run(SectionInput(tick_type="hard", payload=pos))
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.price == pytest.approx(0.01)


def test_s2_live_never_signs_a_sell_below_one_tick(portfolio) -> None:
    m = _market("m1")
    _populate(m, OrderBook(token_id=m.yes_token_id, ts=1.0, bids=[(0.02, 100.0)], asks=[]))
    held = _live_held(portfolio, m, qty=10.0)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xS",
            "status": "matched",
            "makingAmount": "10",
            "takingAmount": "0.2",
        }
    )
    LiveExecutor(portfolio=portfolio, clob_client=clob).execute_sell(
        held, close_reason="stop_loss", ts=200.0, floor_price=0.005
    )
    assert clob.posted[0]["order_args"].price == pytest.approx(0.01)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_s2_a_price_validation_error_is_a_deterministic_rejection(portfolio, side: str) -> None:
    m = _market("m1")
    _populate(
        m,
        OrderBook(token_id=m.yes_token_id, ts=1.0, bids=[(0.40, 100.0)], asks=[(0.41, 100.0)]),
    )
    clob = _FakeClob(exception=PolyException("invalid price (0.005), min: 0.01 - max: 0.99"))
    le = LiveExecutor(portfolio=portfolio, clob_client=clob)
    if side == "buy":
        from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

        r = le.execute_buy(
            OrderIntent(market_id="m1", side="yes", price=0.41, qty=10.0), news_id=None, ts=1.0
        )
    else:
        held = _live_held(portfolio, m, qty=10.0)
        r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is False
    assert r.skip_reason is not None and r.skip_reason.startswith("live_rejected:invalid price")
    reads_after_post = clob.balance_reads
    assert reads_after_post == 1  # the pre-order baseline only — no lost-order confirm


# ---------- S3: a probe ask does not confirm a thin hard stop ----------


async def test_s3_a_dust_ask_does_not_confirm_the_thin_hard_stop() -> None:
    _set(bids=[(0.20, 2.0)], asks=[(0.30, 1.0), (0.49, 100.0)])
    ex = _FakeExecutor()
    m = _monitor(ex)
    await m._tick_once()
    assert ex.calls == []
    assert _reasons() == ["no_executable_bid"]


async def test_s3_the_section_still_gets_the_raw_level_one_ask_on_a_normal_mark() -> None:
    seen: list[MarkedPosition] = []

    class _Spy(ThresholdExitV1):
        def run(self, input: SectionInput):  # noqa: A002, ANN201
            seen.append(input.payload)
            return super().run(input)

    _set(bids=[(0.48, 100.0)], asks=[(0.49, 1.0), (0.55, 100.0)])
    await _monitor(_FakeExecutor(), section=_Spy(ThresholdExitConfigV1()))._tick_once()
    assert seen[0].ask == pytest.approx(0.49)


# ---------- S4: the no-ask streak counts fetches, not ticks ----------


async def test_s4_a_frozen_snapshot_cannot_complete_the_streak() -> None:
    _set(bids=[(0.20, 2.0)], asks=[])
    ex = _FakeExecutor()
    m = _monitor(ex)
    for _ in range(THIN_NO_ASK_CONFIRM_TICKS + 3):
        await m._tick_once()
    assert ex.calls == []


async def test_s4_repeat_reads_between_fetches_neither_add_nor_reset() -> None:
    ex = _FakeExecutor()
    m = _monitor(ex)
    for _ in range(THIN_NO_ASK_CONFIRM_TICKS - 1):
        _set(bids=[(0.20, 2.0)], asks=[])
        await m._tick_once()
        await m._tick_once()  # same fetch again
    assert ex.calls == []
    _set(bids=[(0.20, 2.0)], asks=[])
    await m._tick_once()
    assert [c["trigger"] for c in ex.calls] == ["stop_loss"]


# ---------- S5: live proceeds never overstated ----------

_DEEP = [(0.55, 4.0), (0.50, 4.0), (0.40, 50.0)]


def _deep_sell(portfolio, response: dict[str, Any], order_status: Any):  # noqa: ANN202
    m = _market("m1")
    _populate(m, OrderBook(token_id=m.yes_token_id, ts=1.0, bids=list(_DEEP), asks=[]))
    held = _live_held(portfolio, m, qty=10.0)
    clob = _FakeClob(order_response=response, order_status=order_status)
    le = LiveExecutor(portfolio=portfolio, clob_client=clob)
    return le.execute_sell(held, close_reason="stop_loss", ts=200.0, floor_price=0.40)


def test_s5_a_rested_order_that_filled_later_books_the_limit(portfolio) -> None:
    r = _deep_sell(
        portfolio,
        _zero_fill_response("live", "0x1"),
        {"size_matched": "10", "status": "MATCHED"},
    )
    assert r.filled is True and r.qty == pytest.approx(10.0)
    # Maker at our 0.40 limit — not the 0.50 VWAP of levels it never took.
    assert r.price == pytest.approx(0.40)


def test_s5_a_raced_remainder_is_priced_at_the_limit(portfolio) -> None:
    r = _deep_sell(
        portfolio,
        {
            "success": True,
            "orderID": "0x1",
            "status": "matched",
            "makingAmount": "4",
            "takingAmount": "2.2",  # 4 crossed at 0.55
        },
        {"size_matched": "10", "status": "MATCHED"},
    )
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx((2.2 + 6 * 0.40) / 10)


def test_s5_a_full_cross_still_books_the_venue_amounts(portfolio) -> None:
    r = _deep_sell(
        portfolio,
        {
            "success": True,
            "orderID": "0x1",
            "status": "matched",
            "makingAmount": "10",
            "takingAmount": "4.7",
        },
        {"size_matched": "10", "status": "MATCHED"},
    )
    assert r.price == pytest.approx(0.47)


# ---------- S6: no new SELL while ours may still rest ----------


def test_s6_live_posts_no_new_sell_while_one_may_rest(portfolio, monkeypatch) -> None:
    # One settle attempt, so the refused cancel ends the settle without sleeping.
    monkeypatch.setattr(le_mod, "_SETTLE_ATTEMPTS", 1)
    m = _market("m1")
    _populate(m, OrderBook(token_id=m.yes_token_id, ts=1.0, bids=[(0.40, 100.0)], asks=[]))
    held = _live_held(portfolio, m, qty=10.0)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0x1",
            "status": "matched",
            "makingAmount": "4",
            "takingAmount": "1.6",
        },
        cancel_responses=[{"canceled": [], "not_canceled": {"0x1": "pending"}}],
        order_status={"size_matched": "4", "status": "LIVE"},
    )
    le = LiveExecutor(portfolio=portfolio, clob_client=clob)
    first = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert first.filled is True and first.resting_order_id == "0x1"
    rest = portfolio.get_open_positions()[0]

    second = le.execute_sell(rest, close_reason="stop_loss", ts=210.0)
    assert second.filled is False
    assert second.skip_reason == "resting_order_pending"
    assert second.resting_order_id == "0x1"
    assert len(clob.posted) == 1  # nothing new signed

    # The venue finally takes the cancel: the next attempt sells again.
    clob._cancel_responses = [{"canceled": ["0x1"], "not_canceled": {}}]
    clob._order_status = [{"size_matched": "4", "status": "CANCELED"}]
    clob._response = {
        "success": True,
        "orderID": "0x2",
        "status": "matched",
        "makingAmount": "6",
        "takingAmount": "2.4",
    }
    third = le.execute_sell(rest, close_reason="stop_loss", ts=220.0)
    assert third.filled is True
    assert len(clob.posted) == 2


async def test_s6_the_monitor_holds_a_resting_order_and_logs_it_once() -> None:
    _set(bids=[(0.30, 100.0)], asks=[(0.31, 100.0)])
    clock = _Clock()
    ex = _FakeExecutor(result=ExecResult.skip("live_cancel_failed", resting_order_id="0x1"))
    m = _monitor(ex, clock=clock)
    for _ in range(6):
        await m._tick_once()
        clock.now += 200.0
    assert m.blocked == 1
    assert m._states[1].resting_order_id == "0x1"
    assert [(e.verdict, e.reason) for e in exit_log.entries()] == [
        ("skip", "resting_order_pending")
    ]


# ---------- S10: ensure_fresh ages at return time ----------


async def test_s10_ages_are_measured_after_the_gather() -> None:
    async def fetch(token_id: str) -> OrderBook:
        await asyncio.sleep(0.3)
        return OrderBook(token_id=token_id, ts=1.0, bids=[(0.4, 10.0)], asks=[(0.41, 10.0)])

    mgr = MarketSourceManager(book_fetcher=fetch)
    # Fresh enough at the call (0.1s) — not refreshed — but 0.4s old on return.
    mgr.store.put_order_book(
        OrderBook(token_id="ok", ts=1.0, bids=[(0.4, 10.0)], asks=[]),
        fetched_at=book_clock() - 0.1,
    )
    ages = await mgr.ensure_fresh(["ok", "slow"], 0.2)
    assert ages["ok"] is not None and ages["ok"] >= 0.35
    assert ages["slow"] is not None and ages["slow"] < 0.2


# ---------- S12: the gone mark is timestamped ----------


def test_s12_a_book_received_before_the_404_does_not_clear_it() -> None:
    s = MarketStore()
    book = OrderBook(token_id="t", ts=1.0, bids=[(0.4, 10.0)], asks=[])
    s.mark_book_gone("t", at=100.0)
    s.set_order_books([book], fetched_at=95.0, received_at={"t": 99.0})
    assert s.is_book_gone("t") and s.get_order_book("t") is None
    assert s.put_order_book(book, fetched_at=99.5) is False
    assert s.is_book_gone("t")
    # Received after the mark: a live book again.
    s.set_order_books([book], fetched_at=100.5, received_at={"t": 101.0})
    assert not s.is_book_gone("t") and s.get_order_book("t") is book


# ---------- S13: backoff on the confirmed decision ----------


class _Source(MarketSourceManager):
    def __init__(self) -> None:
        self.next_book: OrderBook | None = None

        async def fetch(token_id: str) -> OrderBook:
            assert self.next_book is not None
            return self.next_book

        super().__init__(book_fetcher=fetch)
        self.store = market_source_manager.store


async def test_s13_a_stale_trigger_flip_does_not_wipe_the_backoff() -> None:
    clock = _Clock()
    source = _Source()
    section = ThresholdExitV1(ThresholdExitConfigV1(take_profit_enabled=True))
    collapsed = OrderBook(token_id="t1", ts=1.0, bids=[(0.30, 100.0)], asks=[(0.31, 100.0)])
    source.next_book = collapsed
    source.store.put_order_book(collapsed)
    ex = _FakeExecutor(result=ExecResult.skip("ctf_cache_not_synced"))
    m = _monitor(ex, section=section, source=source, clock=clock)
    await m._tick_once()
    assert len(ex.calls) == 1
    # Inside the backoff, a stale cached book reads take_profit; the fresh one
    # still says stop_loss at the same floor: no retry yet.
    clock.now += 5.0
    stale_store = MarketStore()
    market_source_manager.store = source.store = stale_store  # restored by _isolate
    stale_store.put_order_book(
        OrderBook(token_id="t1", ts=1.0, bids=[(0.70, 100.0)], asks=[(0.71, 100.0)]),
        fetched_at=book_clock() - 60.0,
    )
    await m._tick_once()
    assert len(ex.calls) == 1
    assert m._states[1].backoff is not None


async def test_s13_a_lower_floor_retries_at_once_without_doubling() -> None:
    clock = _Clock()
    ex = _FakeExecutor(result=ExecResult.skip("ctf_cache_not_synced"))
    _set(bids=[(0.30, 100.0)], asks=[(0.31, 100.0)])
    m = _monitor(ex, clock=clock)
    await m._tick_once()
    clock.now += 10.0
    _set(bids=[(0.30, 100.0)], asks=[(0.31, 100.0)])
    await m._tick_once()  # same floor, retried after one tick → doubled to 20s
    assert len(ex.calls) == 2
    assert m._states[1].backoff is not None and m._states[1].backoff.delay == 20.0
    clock.now += 1.0
    _set(bids=[(0.28, 100.0)], asks=[(0.29, 100.0)])
    await m._tick_once()  # the market moved lower: retried now
    assert len(ex.calls) == 3
    assert ex.floors[-1] == pytest.approx(0.28)
    assert m._states[1].backoff.delay == 10.0  # reset, not 40


# ---------- S14: gone tokens are not refetched every 10s ----------


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://clob.example/book")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


async def test_s14_the_held_loop_backs_off_gone_tokens(caplog) -> None:
    calls: list[str] = []

    async def fetch(token_id: str) -> OrderBook:
        calls.append(token_id)
        raise _status_error(404)

    mgr = MarketSourceManager(book_fetcher=fetch)
    mgr.set_portfolio_store(_FakePortfolio([_held(1, "g")]))
    with caplog.at_level(logging.WARNING, logger="openpoly.markets.manager"):
        await mgr._refresh_held_books_once()
        await mgr._refresh_held_books_once()
        await mgr._refresh_held_books_once()
    assert calls == ["g"]
    # Recheck once the mark is old enough; a repeat 404 is not logged again.
    mgr.store.mark_book_gone("g", at=book_clock() - GONE_BOOK_RECHECK_SECONDS - 1.0)
    with caplog.at_level(logging.WARNING, logger="openpoly.markets.manager"):
        await mgr._refresh_held_books_once()
    assert calls == ["g", "g"]
    gone_logs = [r for r in caplog.records if "is gone" in r.getMessage()]
    assert len(gone_logs) == 1


# ---------- S15: a dropped book is held visibly ----------


async def test_s15_a_dropped_book_is_logged_once_as_no_order_book() -> None:
    ex = _FakeExecutor()
    m = _monitor(ex)
    for _ in range(3):
        await m._tick_once()
    assert m.blocked == 1
    assert _reasons() == ["no_order_book"]

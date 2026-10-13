"""Exit monitor holds, backoff and the thin-book hard stop.

* A held token whose market resolved (CLOB /book 404) or whose book stopped
  refreshing is never closed off its last quote: blocked, logged once as
  ``market_closed_await_settlement`` / ``stale_order_book``.
* A dust level-1 bid alone does not fire v1's thin hard stop: the ask must
  confirm the collapse, or — with no ask — it must persist for 3 ticks.
* A confirmed thin hard stop sweeps below the dust bid, so a sub-share level 1
  does not make every sell skip.
* A close that keeps failing backs off per position and logs its error once
  per (position, reason); it counts as blocked.
"""

from __future__ import annotations

import time

import httpx
import pytest

from openpoly.execution import ExecResult
from openpoly.execution import PaperExecutor
from openpoly.markets.manager import MarketSourceManager
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import KEPT_BOOK_MAX_AGE_SECONDS, book_clock
from openpoly.runtime.exit_monitor import CLOSE_BACKOFF_MAX_SECONDS, ExitMonitor
from openpoly.runtime.section_log import exit_log
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


class _Clock:
    """Injected monitor clock — tests never patch the process-wide ``time``."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _v1_monitor(
    ex: _FakeExecutor,
    *,
    source: MarketSourceManager | None = None,
    clock: _Clock | None = None,
) -> ExitMonitor:
    m = ExitMonitor(
        exit_section=ThresholdExitV1(ThresholdExitConfigV1()),
        executor=ex,
        tick_interval_seconds=10,
        book_source=source,
        clock=clock if clock is not None else _Clock(),
    )
    m.configure(_FakePortfolio([_held(1, "t1", avg=0.50)]))  # type: ignore[arg-type]
    return m


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://clob.example/book")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


class _GoneSource(MarketSourceManager):
    """A real manager over the monitor's store whose /book answers 404."""

    def __init__(self, code: int = 404) -> None:
        async def fetch(token_id: str) -> OrderBook:
            raise _status_error(code)

        super().__init__(book_fetcher=fetch)
        self.store = market_source_manager.store


def _reasons() -> list[str | None]:
    return [e.reason for e in exit_log.entries()]


# ---------- resolved / stale books ----------


async def test_a_404_on_a_held_token_holds_for_settlement_instead_of_filling() -> None:
    """Regression: the resolved market's last book was kept forever and a stop
    fired off it booked a phantom paper fill at the stale price."""
    source = _GoneSource()
    # A collapsed, 60s-old book: the stop fires, confirm-before-close refetches.
    source.store.put_order_book(
        OrderBook(token_id="t1", ts=1.0, bids=[(0.20, 100.0)], asks=[(0.21, 100.0)]),
        fetched_at=book_clock() - 60.0,
    )
    ex = _FakeExecutor()
    m = _v1_monitor(ex, source=source)
    await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert source.store.get_order_book("t1") is None  # evicted on the 404
    assert _reasons() == ["market_closed_await_settlement"]
    # Next ticks: still held, logged once.
    await m._tick_once()
    await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert _reasons() == ["market_closed_await_settlement"]


async def test_a_non_404_failure_does_not_evict_the_book() -> None:
    source = _GoneSource(code=503)
    source.store.put_order_book(
        OrderBook(token_id="t1", ts=1.0, bids=[(0.45, 100.0)], asks=[(0.46, 100.0)])
    )
    await source.refresh_order_book("t1")
    assert source.store.get_order_book("t1") is not None
    assert not source.store.is_book_gone("t1")


async def test_a_book_that_stopped_refreshing_is_not_closed_at() -> None:
    source = _GoneSource(code=503)  # transient failures only
    source.store.put_order_book(
        OrderBook(token_id="t1", ts=1.0, bids=[(0.20, 100.0)], asks=[(0.21, 100.0)]),
        fetched_at=book_clock() - (KEPT_BOOK_MAX_AGE_SECONDS + 30.0),
    )
    ex = _FakeExecutor()
    m = _v1_monitor(ex, source=source)
    await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert _reasons() == ["stale_order_book"]


def test_kept_held_book_is_dropped_once_it_stops_refreshing() -> None:
    store = market_source_manager.store
    store.put_order_book(OrderBook(token_id="held", ts=1.0, bids=[], asks=[]), fetched_at=1_000.0)
    store.set_order_books([], fetched_at=1_000.0 + KEPT_BOOK_MAX_AGE_SECONDS - 1, keep={"held"})
    assert store.get_order_book("held") is not None
    store.set_order_books([], fetched_at=1_000.0 + KEPT_BOOK_MAX_AGE_SECONDS + 1, keep={"held"})
    assert store.get_order_book("held") is None


async def test_empty_catalog_sample_keeps_held_books() -> None:
    """Regression: the empty-catalog branch dropped every held token's book."""
    mgr = MarketSourceManager(book_fetcher=lambda t: None)  # type: ignore[arg-type]
    mgr.store.put_order_book(OrderBook(token_id="held", ts=1.0, bids=[], asks=[]))
    mgr._held_tokens = frozenset({"held"})
    assert await mgr._sample_books_once() == 0
    assert mgr.store.get_order_book("held") is not None


# ---------- paper sell / manual close refuse dead books ----------


@pytest.fixture
def paper(tmp_path):
    from openpoly.db.engine import init_db, make_engine, make_session_factory
    from openpoly.portfolio import PortfolioStore

    engine = make_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    store = PortfolioStore(make_session_factory(engine))
    yield PaperExecutor(store), store
    engine.dispose()


def _open(store):  # noqa: ANN001, ANN202
    return store.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )


def test_paper_sell_refuses_a_book_that_stopped_refreshing(paper) -> None:
    ex, store = paper
    held = _open(store)
    market_source_manager.store.put_order_book(
        OrderBook(token_id="t1", ts=1.0, bids=[(0.55, 100.0)], asks=[(0.56, 100.0)]),
        fetched_at=book_clock() - (KEPT_BOOK_MAX_AGE_SECONDS + 1.0),
    )
    r = ex.execute_sell(held, close_reason="manual", ts=200.0)
    assert r.filled is False and r.skip_reason == "stale_order_book"
    assert store.get_position(held.position_id).status == "open"


def test_paper_sell_refuses_a_gone_market(paper) -> None:
    ex, store = paper
    held = _open(store)
    market_source_manager.store.put_order_book(
        OrderBook(token_id="t1", ts=1.0, bids=[(0.55, 100.0)], asks=[(0.56, 100.0)])
    )
    market_source_manager.store.mark_book_gone("t1")
    r = ex.execute_sell(held, close_reason="manual", ts=200.0)
    assert r.filled is False and r.skip_reason == "market_closed_await_settlement"


def test_paper_sell_on_a_fresh_book_still_fills(paper) -> None:
    ex, store = paper
    held = _open(store)
    market_source_manager.store.put_order_book(
        OrderBook(token_id="t1", ts=1.0, bids=[(0.55, 100.0)], asks=[(0.56, 100.0)])
    )
    r = ex.execute_sell(held, close_reason="manual", ts=200.0)
    assert r.filled is True and r.price == pytest.approx(0.55)


# ---------- thin hard stop: confirmation + deeper floor ----------


async def test_dust_bid_with_healthy_asks_does_not_fire_the_thin_hard_stop() -> None:
    """Regression: a momentary bid pull left only a dust bid at level 1 while
    the asks stayed healthy, and the hard stop sold into it."""
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.20, 2.0)], asks=[(0.49, 100.0)])]
    )
    ex = _FakeExecutor()
    m = _v1_monitor(ex)
    for _ in range(5):
        await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert _reasons() == ["no_executable_bid"]


async def test_thin_collapse_with_no_ask_fires_only_after_it_persists() -> None:
    def fetch() -> None:  # a new fetch of the same collapsed book
        market_source_manager.store.set_order_books(
            [OrderBook(token_id="t1", ts=1.0, bids=[(0.20, 2.0)], asks=[])]
        )

    ex = _FakeExecutor()
    m = _v1_monitor(ex)
    for _ in range(THIN_NO_ASK_CONFIRM_TICKS - 1):
        fetch()
        await m._tick_once()
        assert ex.calls == []
    fetch()
    await m._tick_once()
    assert [c["trigger"] for c in ex.calls] == ["stop_loss"]


async def test_thin_no_ask_streak_resets_when_the_book_recovers() -> None:
    store = market_source_manager.store
    thin = OrderBook(token_id="t1", ts=1.0, bids=[(0.20, 2.0)], asks=[])
    ex = _FakeExecutor()
    m = _v1_monitor(ex)
    for _ in range(THIN_NO_ASK_CONFIRM_TICKS - 1):
        store.set_order_books([thin])
        await m._tick_once()
    store.set_order_books([OrderBook(token_id="t1", ts=1.0, bids=[(0.48, 50.0)], asks=[])])
    await m._tick_once()
    store.set_order_books([thin])
    await m._tick_once()
    assert ex.calls == []


async def test_confirmed_thin_stop_sweeps_past_a_sub_share_level_one(tmp_path) -> None:
    """Regression: the floor was pinned to the raw level-1 bid; with <1 share
    there the paper sell skipped every tick (an error row each time) and the
    position never closed."""
    from openpoly.db.engine import init_db, make_engine, make_session_factory
    from openpoly.portfolio import PortfolioStore

    engine = make_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    store = PortfolioStore(make_session_factory(engine))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        price=0.50,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    market_source_manager.store.set_order_books(
        [
            OrderBook(
                token_id="t1",
                ts=1.0,
                bids=[(0.34, 0.5), (0.32, 3.0), (0.31, 4.0), (0.29, 4.0)],
                asks=[(0.33, 50.0)],  # a real ask confirms the collapse (-34%)
            )
        ]
    )
    m = ExitMonitor(
        exit_section=ThresholdExitV1(ThresholdExitConfigV1()),
        executor=PaperExecutor(store),
        tick_interval_seconds=10,
    )
    m.configure(store)
    await m._tick_once()
    entries = exit_log.entries()
    assert [e.verdict for e in entries] == ["ok"], entries
    # Floor 0.50 × (1 - 0.30 - 0.10) = 0.30: 0.5 @ .34 + 3 @ .32 + 4 @ .31
    # swept, the 0.29 level below the floor is not.
    assert entries[0].fill_price == pytest.approx((0.5 * 0.34 + 3 * 0.32 + 4 * 0.31) / 7.5)
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.qty == pytest.approx(2.5)
    engine.dispose()


# ---------- failing close: backoff, blocked, log once ----------


async def test_failing_close_backs_off_counts_blocked_and_logs_once() -> None:
    clock = _Clock()
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.30, 100.0)], asks=[(0.31, 100.0)])]
    )
    ex = _FakeExecutor(result=ExecResult.skip("ctf_cache_not_synced"))
    m = _v1_monitor(ex, clock=clock)

    await m._tick_once()
    assert len(ex.calls) == 1
    assert m.blocked == 1
    # Within the first backoff step (one 10s tick): no retry.
    clock.now += 5.0
    await m._tick_once()
    assert len(ex.calls) == 1
    assert m.blocked == 1
    # Delays double: 10, 20, 40, 80, 120, 120 ...
    attempts_at = []
    for _ in range(60):
        clock.now += 10.0
        before = len(ex.calls)
        await m._tick_once()
        if len(ex.calls) > before:
            attempts_at.append(clock.now)
    gaps = [b - a for a, b in zip(attempts_at, attempts_at[1:])]
    assert gaps[:3] == [20.0, 40.0, 80.0]
    assert max(gaps) <= CLOSE_BACKOFF_MAX_SECONDS + 10.0
    errors = [e for e in exit_log.entries() if e.verdict == "error"]
    assert len(errors) == 1
    assert errors[0].error == "sell not filled: ctf_cache_not_synced"


async def test_backoff_resets_when_the_close_succeeds() -> None:
    clock = _Clock()
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.30, 100.0)], asks=[(0.31, 100.0)])]
    )
    ex = _FakeExecutor(result=ExecResult.skip("ctf_cache_not_synced"))
    m = _v1_monitor(ex, clock=clock)
    await m._tick_once()
    ex._result = ExecResult.ok(price=0.30, qty=20.0, position_id=1)
    clock.now += 11.0
    await m._tick_once()
    assert len(ex.calls) == 2
    assert all(st.backoff is None for st in m._states.values())
    assert [e.verdict for e in exit_log.entries()] == ["error", "ok"]


async def test_backoff_drops_when_the_decision_changes() -> None:
    store = market_source_manager.store
    store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.30, 100.0)], asks=[(0.31, 100.0)])]
    )
    ex = _FakeExecutor(result=ExecResult.skip("ctf_cache_not_synced"))
    m = _v1_monitor(ex)
    await m._tick_once()
    assert m._states[1].backoff is not None
    # The book recovers: no close decision any more → the backoff is over.
    store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.50, 100.0)], asks=[(0.51, 100.0)])]
    )
    await m._tick_once()
    assert all(st.backoff is None for st in m._states.values())


async def test_hold_reasons_are_deduped_per_position_and_reason() -> None:
    """``market_closed_await_settlement`` and ``no_executable_bid`` used to
    share one log-once set: the first suppressed the second."""
    store = market_source_manager.store
    store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.45, 2.0)], asks=[(0.46, 1.0)])]
    )
    ex = _FakeExecutor()
    m = _v1_monitor(ex)
    await m._tick_once()
    await m._tick_once()
    assert _reasons() == ["no_executable_bid"]
    store.mark_book_gone("t1")
    await m._tick_once()
    await m._tick_once()
    assert _reasons() == ["no_executable_bid", "market_closed_await_settlement"]


def test_store_ordering_survives_a_backward_wall_clock_step(monkeypatch) -> None:
    """Regression: newest-wins and ages used ``time.time()``; a backward clock
    step froze the cache (every new book looked older) and made ages negative."""
    from openpoly.markets import store as store_mod

    wall = [2_000_000_000.0]
    monkeypatch.setattr(time, "time", lambda: wall[0])
    s = store_mod.MarketStore()
    s.put_order_book(OrderBook(token_id="t", ts=1.0, bids=[(0.1, 1)], asks=[]))
    wall[0] -= 3_600.0  # NTP steps the wall clock back an hour
    newer = OrderBook(token_id="t", ts=2.0, bids=[(0.2, 1)], asks=[])
    assert s.put_order_book(newer) is True
    assert s.get_order_book("t") is newer
    age = s.order_book_age("t")
    assert age is not None and 0.0 <= age < 1.0

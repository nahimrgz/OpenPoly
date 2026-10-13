"""Exit-side book freshness: confirm-before-close in ExitMonitor, and the
newest-wins guarantee for the peak tracker fed by the market source.

Reuses the ExitMonitor fakes from ``test_exit_monitor``; the book source is a
real ``MarketSourceManager`` over an injected fetcher, sharing the store the
monitor marks from.
"""

from __future__ import annotations

import logging

import pytest

from openpoly.execution import ExecResult
from openpoly.markets.manager import MAX_BOOK_AGE_SECONDS, MarketSourceManager
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import book_clock
from openpoly.portfolio import HeldPosition
from openpoly.runtime.exit_monitor import ExitMonitor
from openpoly.runtime.section_log import exit_log
from openpoly.sections.exit.threshold_v1 import ThresholdExitConfigV1, ThresholdExitV1
from tests.test_exit_monitor import (  # noqa: F401 — autouse fixture
    _FakeExecutor,
    _FakePortfolio,
    _held,
    _isolate,
)


def _book(token_id: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(token_id=token_id, ts=1.0, bids=[(bid, 100.0)], asks=[(ask, 100.0)])


class _Source(MarketSourceManager):
    """Real manager over the monitor's store; ``fetch`` returns ``next_book``
    (or raises when it is None)."""

    def __init__(self) -> None:
        self.next_book: OrderBook | None = None
        self.fetched: list[str] = []

        async def fetch(token_id: str) -> OrderBook:
            self.fetched.append(token_id)
            if self.next_book is None:
                raise RuntimeError("venue down")
            return self.next_book

        super().__init__(book_fetcher=fetch)
        self.store = market_source_manager.store


class _RecordingExecutor(_FakeExecutor):
    """Also records which book the sell would fill against (the paper
    executor reads the shared store at fill time)."""

    def __init__(self) -> None:
        super().__init__()
        self.books: list[OrderBook | None] = []

    def execute_sell(self, position: HeldPosition, **kw) -> ExecResult:  # type: ignore[override]
        self.books.append(market_source_manager.store.get_order_book(position.token_id))
        return super().execute_sell(position, **kw)


def _monitor(source: _Source | None, ex: _FakeExecutor) -> ExitMonitor:
    m = ExitMonitor(
        exit_section=ThresholdExitV1(ThresholdExitConfigV1()),
        executor=ex,
        tick_interval_seconds=3600,
        book_source=source,
    )
    m.configure(_FakePortfolio([_held(1, "t1", avg=0.50)]))  # type: ignore[arg-type]
    return m


def _seed(book: OrderBook, age: float) -> None:
    market_source_manager.store.put_order_book(book, fetched_at=book_clock() - age)


async def test_stale_stop_not_confirmed_by_a_fresh_book_does_not_close() -> None:
    """A 60s-old collapsed book says stop; the fresh book has recovered."""
    source = _Source()
    _seed(_book("t1", 0.30, 0.32), age=60.0)
    source.next_book = _book("t1", 0.49, 0.50)
    ex = _FakeExecutor()
    await _monitor(source, ex)._tick_once()
    assert source.fetched == ["t1"]
    assert ex.calls == []
    assert exit_log.entries() == []


async def test_stale_stop_confirmed_closes_on_the_fresh_book() -> None:
    source = _Source()
    _seed(_book("t1", 0.30, 0.32), age=60.0)
    fresh = _book("t1", 0.31, 0.33)
    source.next_book = fresh
    ex = _RecordingExecutor()
    await _monitor(source, ex)._tick_once()
    assert ex.calls[0]["trigger"] == "stop_loss"
    # The sell fills against the same refreshed book the decision used.
    assert ex.books == [fresh]


async def test_failed_refresh_still_closes(caplog: pytest.LogCaptureFixture) -> None:
    """A stop must not be blocked by a failed refresh — fail toward closing."""
    source = _Source()  # next_book None → the fetch raises
    _seed(_book("t1", 0.30, 0.32), age=60.0)
    ex = _FakeExecutor()
    with caplog.at_level(logging.WARNING):
        await _monitor(source, ex)._tick_once()
    assert source.fetched == ["t1"]
    assert ex.calls[0]["trigger"] == "stop_loss"
    assert "could not refresh" in caplog.text


async def test_fresh_book_closes_without_a_refresh() -> None:
    source = _Source()
    _seed(_book("t1", 0.30, 0.32), age=1.0)
    ex = _FakeExecutor()
    await _monitor(source, ex)._tick_once()
    assert source.fetched == []
    assert ex.calls[0]["trigger"] == "stop_loss"


async def test_hold_on_a_stale_book_does_not_refresh() -> None:
    """Only a close is confirmed; holding positions cost no REST call."""
    source = _Source()
    _seed(_book("t1", 0.49, 0.50), age=60.0)
    ex = _FakeExecutor()
    await _monitor(source, ex)._tick_once()
    assert source.fetched == []
    assert ex.calls == []


async def test_older_book_never_reaches_the_peak_tracker_after_a_newer_one() -> None:
    """Peak ratchet: a refresh lands a newer, lower book; a sample batch that
    started earlier carries an older, higher copy. The store keeps the newer
    one and the observer (the exit monitor's peak tracker) never sees the
    older — so the trailing stop cannot trail a price that is already gone."""
    source = _Source()
    m = _monitor(source, _FakeExecutor())
    await m._tick_once()  # registers position 1 as a watcher of t1 (no book → blocked)
    source.set_book_observer(m.observe_book)

    batch_started = book_clock()
    source.next_book = _book("t1", 0.52, 0.53)
    await source.refresh_order_book("t1")
    assert m._states[1].peak == 0.52  # noqa: SLF001

    accepted = source.store.set_order_books(
        [_book("t1", 0.70, 0.71)], fetched_at=batch_started - 1.0
    )
    source._deliver_books(accepted)  # noqa: SLF001 — what _sample_books_once does
    assert accepted == []
    assert m._states[1].peak == 0.52  # noqa: SLF001
    book = source.store.get_order_book("t1")
    assert book is not None and book.bids[0][0] == 0.52


async def test_overtaken_refresh_is_neither_stored_nor_observed() -> None:
    """A slow refresh whose fetch began before a newer cached book must not
    roll the store or the peak tracker back."""
    source = _Source()
    seen: list[float] = []
    source.set_book_observer(lambda b: seen.append(b.bids[0][0]))
    newer = _book("t1", 0.40, 0.41)
    source.store.put_order_book(newer, fetched_at=book_clock() + 60)
    source.next_book = _book("t1", 0.90, 0.91)
    await source.refresh_order_book("t1")
    assert source.store.get_order_book("t1") is newer
    assert seen == []


def test_max_book_age_is_the_shared_budget() -> None:
    from openpoly.runtime import orchestrator

    assert orchestrator.MAX_BOOK_AGE_SECONDS == MAX_BOOK_AGE_SECONDS


def test_default_exit_monitor_confirms_against_the_market_source() -> None:
    from openpoly.runtime.exit_monitor import exit_monitor

    assert exit_monitor._book_source is market_source_manager  # noqa: SLF001


def test_exit_monitor_is_built_from_the_saved_canvas(tmp_path, monkeypatch) -> None:
    """Regression: the singleton used default thresholds after a restart,
    ignoring the persisted canvas exit node."""
    import json

    from openpoly.runtime.exit_monitor import _build_exit_monitor

    canvas = tmp_path / "canvas.json"
    canvas.write_text(
        json.dumps({"nodes": [{"sectionType": "exit", "config": {"stop_loss_pct": 0.25}}]})
    )
    monkeypatch.setenv("OPENPOLY_CANVAS_STORE", str(canvas))
    m = _build_exit_monitor()
    assert isinstance(m._exit, ThresholdExitV1)  # noqa: SLF001
    assert m._exit.config.stop_loss_pct == 0.25  # noqa: SLF001

    monkeypatch.setenv("OPENPOLY_CANVAS_STORE", str(tmp_path / "absent.json"))
    assert _build_exit_monitor()._exit.config.stop_loss_pct == 0.15  # noqa: SLF001


# ---------- realistic multi-level books: entry / exit agree on prices ----------


def _thin_top_book(token_id: str) -> OrderBook:
    """3 + 2 probe shares above real size at 0.33; real asks at 0.50."""
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(0.48, 3.0), (0.40, 2.0), (0.33, 50.0)],
        asks=[(0.50, 100.0)],
    )


def test_entry_v1_gates_the_spread_on_the_bid_the_exit_marks_at() -> None:
    """Regression: v0 gated on the 0.48 probe (spread 4%) and entered; the
    exit then marked at 0.33 (-34%) and hard-stopped on the next tick. v1
    measures the spread to the same depth-guarded bid and refuses."""
    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import PollSummary
    from openpoly.sections._base import SectionInput
    from openpoly.sections.analyzer.llm_v0 import AnalysisResult
    from openpoly.sections.entry.edge_threshold_v0 import (
        EdgeThresholdConfig,
        EdgeThresholdEntryV0,
    )
    from openpoly.sections.entry.edge_threshold_v1 import (
        EdgeThresholdConfigV1,
        EdgeThresholdEntryV1,
    )

    market = normalize_gamma_market(
        {"id": "m1", "conditionId": "0xm1", "question": "Q?", "clobTokenIds": '["t1", "t2"]'},
        event={"id": "e", "title": "E", "tags": []},
    )
    assert market is not None
    store = market_source_manager.store
    store.replace([market], PollSummary(ts=1.0, fetched=1, kept=1))
    store.set_order_books([_thin_top_book("t1")])
    ar = AnalysisResult(market_id="m1", p_model=0.70, confidence="medium")

    v0 = EdgeThresholdEntryV0(EdgeThresholdConfig()).run(
        SectionInput(tick_type="event", payload=ar)
    )
    assert v0.verdict == "ok"  # v0 behavior unchanged
    v1 = EdgeThresholdEntryV1(EdgeThresholdConfigV1()).run(
        SectionInput(tick_type="event", payload=ar)
    )
    assert v1.verdict == "skip"
    assert v1.signals["spread"] == pytest.approx(0.17)


async def test_exit_marks_and_hard_stops_on_the_depth_guarded_bid() -> None:
    """The book the old entry let through: the exit mark is 0.33, not 0.48."""
    store = market_source_manager.store
    store.set_order_books([_thin_top_book("t1")])
    ex = _FakeExecutor()
    await _monitor(None, ex)._tick_once()
    assert ex.calls[0]["trigger"] == "stop_loss"
    # The sell may sweep down to the mark the decision used.
    assert ex.floors == [0.33]


async def test_real_size_below_a_dusty_top_three_is_found() -> None:
    """Regression: books were trimmed to 3 levels, so three dust bids made the
    mark None ('no_executable_bid') forever and no stop could fire."""
    from openpoly.markets.models import parse_clob_book

    raw = {
        "bids": [
            {"price": p, "size": s}
            for p, s in [
                ("0.49", "1"),
                ("0.48", "1"),
                ("0.47", "2"),
                ("0.46", "1"),
                ("0.30", "80"),
            ]
        ],
        "asks": [{"price": "0.50", "size": "100"}],
    }
    book = parse_clob_book(raw, "t1")
    assert len(book.bids) == 5
    market_source_manager.store.set_order_books([book])
    ex = _FakeExecutor()
    await _monitor(None, ex)._tick_once()
    assert ex.calls[0]["trigger"] == "stop_loss"
    assert ex.floors == [0.30]


async def test_no_executable_level_still_fires_the_hard_stop_on_the_raw_bid() -> None:
    """Every level is dust and the raw best bid shows a -40% collapse that the
    ask confirms (-34%): v1's hard stop closes into the thin book rather than
    riding it to zero, sweeping below the dust bid (floor 0.30 - 0.10 * 0.50)."""
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.30, 2.0), (0.20, 1.0)], asks=[(0.33, 1.0)])]
    )
    ex = _FakeExecutor()
    await _monitor(None, ex)._tick_once()
    assert ex.calls[0]["trigger"] == "stop_loss"
    assert ex.floors == [pytest.approx(0.25)]


async def test_no_executable_level_without_a_collapse_is_blocked_not_closed() -> None:
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.45, 2.0)], asks=[(0.50, 1.0)])]
    )
    ex = _FakeExecutor()
    m = _monitor(None, ex)
    await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert exit_log.entries()[0].reason == "no_executable_bid"


async def test_v0_section_never_sees_a_thin_mark() -> None:
    from openpoly.sections.exit.threshold_v0 import ThresholdExitConfig, ThresholdExitV0

    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.10, 2.0)], asks=[(0.60, 1.0)])]
    )
    ex = _FakeExecutor()
    m = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig()),
        executor=ex,
        tick_interval_seconds=3600,
    )
    m.configure(_FakePortfolio([_held(1, "t1", avg=0.50)]))  # type: ignore[arg-type]
    await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1


async def test_exit_sections_get_the_raw_level1_ask() -> None:
    """The ask handed to exit sections is the raw level-1 ask (as at HEAD):
    depth-guarding it only ever moved it up or to None, inflating v1's stop
    mid and, with an all-dust ask side, removing the trailing spread floor.
    threshold_v1's spread-floor cap bounds what a dust ask can do."""
    captured: list[object] = []

    class _Recorder:
        EVALUATES_THIN_MARKS = False

        def run(self, input):  # noqa: ANN001, ANN201
            captured.append(input.payload)
            return ThresholdExitV1(ThresholdExitConfigV1()).run(input)

    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.50, 50.0)], asks=[(0.51, 1.0), (0.53, 40.0)])]
    )
    m = ExitMonitor(exit_section=_Recorder(), executor=_FakeExecutor(), tick_interval_seconds=3600)
    m.configure(_FakePortfolio([_held(1, "t1", avg=0.50)]))  # type: ignore[arg-type]
    await m._tick_once()
    assert captured[0].ask == 0.51
    assert captured[0].spread == pytest.approx(0.01)

    captured.clear()
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[(0.50, 50.0)], asks=[(0.95, 1.0)])]
    )
    await m._tick_once()
    assert captured[0].ask == 0.95 and captured[0].spread == pytest.approx(0.45)


def test_v1_caps_the_spread_floor_of_the_trailing_distance() -> None:
    """Regression: an uncapped spread floor let one wide spread push the
    trailing distance past any retrace, disabling the lock."""
    from openpoly.sections._base import SectionInput
    from openpoly.sections.exit.threshold_v0 import (
        MarkedPosition,
        ThresholdExitConfig,
        ThresholdExitV0,
    )

    # Ran 0.50 → 0.80 (gain $6 on 20 shares ≥ the 30% floor), back to 0.72;
    # spread 0.40. pct_trail = 0.12 x 0.30 = 0.036.
    pos = MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=0.72,
        peak_price=0.80,
        spread=0.40,
        ask=0.97,  # a wide (real) ask
    )
    v0 = ThresholdExitV0(ThresholdExitConfig())
    v1 = ThresholdExitV1(ThresholdExitConfigV1())
    assert v0._trail_distance(pos) == pytest.approx(0.40)  # noqa: SLF001
    assert v1._trail_distance(pos) == pytest.approx(0.036 + 0.02)  # noqa: SLF001
    assert v0.run(SectionInput(tick_type="hard", payload=pos)).verdict == "skip"
    out = v1.run(SectionInput(tick_type="hard", payload=pos))
    assert out.payload.trigger == "peak_drawdown"
    # Never below the min_trail_ticks floor.
    calm = MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=0.505,
        peak_price=0.51,
        spread=0.0,
    )
    assert v1._trail_distance(calm) == pytest.approx(0.02)  # noqa: SLF001


def test_exit_tick_runs_at_the_held_book_cadence() -> None:
    from openpoly.markets.manager import HELD_BOOK_INTERVAL_SECONDS
    from openpoly.runtime.exit_monitor import DEFAULT_TICK_INTERVAL_SECONDS, exit_monitor

    assert DEFAULT_TICK_INTERVAL_SECONDS == HELD_BOOK_INTERVAL_SECONDS == 10
    assert exit_monitor._tick_interval == 10  # noqa: SLF001


async def test_failed_refresh_on_a_closed_market_holds_for_settlement() -> None:
    """Regression: a failed confirm refetch on a resolved market still booked
    a paper fill at the corpse book's bid."""
    import dataclasses

    from openpoly.markets.models import normalize_gamma_market
    from openpoly.markets.store import PollSummary

    market = normalize_gamma_market(
        {"id": "m1", "conditionId": "0xm1", "question": "Q?", "clobTokenIds": '["t1", "t2"]'},
        event={"id": "e", "title": "E", "tags": []},
    )
    assert market is not None
    closed = dataclasses.replace(market, closed=True, accepting_orders=False)
    market_source_manager.store.replace([closed], PollSummary(ts=1.0, fetched=1, kept=1))
    source = _Source()  # refetch fails
    _seed(_book("t1", 0.30, 0.32), age=60.0)
    ex = _FakeExecutor()
    m = _monitor(source, ex)
    await m._tick_once()
    assert ex.calls == []
    assert m.blocked == 1
    assert exit_log.entries()[0].reason == "market_closed_await_settlement"

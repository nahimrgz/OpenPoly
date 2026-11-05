"""Exit v1's ``max_hold_hours``: a news trade has a shelf life.

Off by default (0). When set, a position still open after that many hours is
sold at the depth-guarded bid, as ``max_hold``. Every price rule keeps
precedence, and a thin book holds: the rule is about time, not an emergency.
"""

from __future__ import annotations

import dataclasses
import time

from openpoly.execution import ExecResult
from openpoly.markets.manager import manager as market_source_manager
from openpoly.runtime.exit_monitor import ExitMonitor
from openpoly.runtime.section_log import exit_log
from openpoly.sections._base import SectionInput
from openpoly.sections.exit.threshold_v0 import CloseIntent, MarkedPosition
from openpoly.sections.exit.threshold_v1 import ThresholdExitConfigV1, ThresholdExitV1
from tests.test_exit_monitor import (  # noqa: F401 — autouse fixture
    _book,
    _FakeExecutor,
    _FakePortfolio,
    _held,
    _isolate,
)


def _pos(*, held_hours: float | None, bid: float = 0.50, thin: bool = False) -> MarkedPosition:
    return MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=0.50,
        qty=20.0,
        current_price=bid,
        peak_price=max(bid, 0.50),
        spread=0.02,
        ask=bid + 0.02,
        thin=thin,
        held_hours=held_hours,
    )


def _run(pos: MarkedPosition, max_hold_hours: float = 24.0):
    inst = ThresholdExitV1(ThresholdExitConfigV1(max_hold_hours=max_hold_hours))
    return inst.run(SectionInput(tick_type="hard", payload=pos))


def test_off_by_default_keeps_a_position_of_any_age() -> None:
    assert ThresholdExitConfigV1().max_hold_hours == 0.0
    assert _run(_pos(held_hours=10_000.0), max_hold_hours=0.0).verdict == "skip"


def test_closes_at_the_limit_at_the_bid() -> None:
    out = _run(_pos(held_hours=24.0, bid=0.49))
    assert out.verdict == "ok"
    assert out.reason == "max_hold"
    assert out.signals["trigger"] == "max_hold"
    assert out.signals["held_hours"] == 24.0
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "max_hold"
    assert out.payload.price == 0.49
    assert out.payload.qty == 20.0


def test_holds_under_the_limit() -> None:
    out = _run(_pos(held_hours=23.9))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"


def test_a_price_trigger_keeps_precedence() -> None:
    out = _run(_pos(held_hours=30.0, bid=0.30))
    assert out.reason == "stop_loss"


def test_a_thin_book_is_not_sold_for_age() -> None:
    out = _run(_pos(held_hours=30.0, thin=True))
    assert out.verdict == "skip"
    assert out.reason == "thin book: hard stop only"


def test_unknown_age_holds() -> None:
    assert _run(_pos(held_hours=None)).verdict == "skip"


def _monitor(held, executor) -> ExitMonitor:
    section = ThresholdExitV1(ThresholdExitConfigV1(max_hold_hours=24.0))
    m = ExitMonitor(exit_section=section, executor=executor, tick_interval_seconds=3600)
    m.configure(_FakePortfolio([held]))  # type: ignore[arg-type]
    return m


async def test_monitor_closes_an_old_position_as_max_hold() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor(result=ExecResult.ok(price=0.41, qty=20.0, position_id=1))
    old = dataclasses.replace(_held(1, "t1", avg=0.40), opened_at=time.time() - 25 * 3600)
    await _monitor(old, ex)._tick_once()
    assert ex.calls[0]["close_reason"] == "max_hold"
    assert exit_log.entries()[0].trigger == "max_hold"


async def test_monitor_leaves_a_recent_position_open() -> None:
    market_source_manager.store.set_order_books([_book("t1", bid=0.41)])
    ex = _FakeExecutor()
    recent = dataclasses.replace(_held(1, "t1", avg=0.40), opened_at=time.time() - 3600)
    await _monitor(recent, ex)._tick_once()
    assert ex.calls == []

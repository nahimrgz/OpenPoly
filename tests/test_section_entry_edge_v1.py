"""Tests for EdgeThresholdEntryV1 — side by edge, spread-relative and
opposite-side gates.

v0 behaviour v1 inherits unchanged (sizing, heat cap, kill switches, veto) is
covered in ``test_section_entry_edge``; these cover what v1 changes.
"""

from __future__ import annotations

import time as _time

import pytest

from openpoly.markets.models import OrderBook
from openpoly.sections._base import SectionInput
from openpoly.sections._registry import scan
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry.edge_threshold_v1 import (
    EdgeThresholdConfigV1,
    EdgeThresholdEntryV1,
    OrderIntent,
)
from tests.test_section_entry_edge import (  # noqa: F401 — autouse fixture
    _book,
    _FakePortfolio,
    _isolate_market_store,
    _market,
    _rec,
)
from tests.test_section_entry_edge import _populate as _populate_market


def _populate(*books: OrderBook) -> None:
    _populate_market(_market(), *books)


def _ar(p_model: float) -> AnalysisResult:
    return AnalysisResult(market_id="m1", p_model=p_model, confidence="medium")


def _run(inst: EdgeThresholdEntryV1, payload: object):
    return inst.run(SectionInput(tick_type="event", payload=payload))


def _cfg(**over) -> EdgeThresholdConfigV1:
    return EdgeThresholdConfigV1(**over)


# ---------- catalog / config ----------


def test_v1_in_default_catalog_next_to_v0() -> None:
    names = {e.name: e for e in scan() if e.type == "entry"}
    assert "EdgeThresholdEntryV0" in names
    assert "EdgeThresholdEntryV1" in names
    schema = names["EdgeThresholdEntryV1"].param_schema["properties"]
    assert schema["same_market_cooldown_minutes"]["default"] == 60
    assert schema["max_spread_pct"]["default"] == 0.10
    assert schema["block_opposite_side"]["default"] is True


def test_v1_keeps_every_v0_knob() -> None:
    from openpoly.sections.entry.edge_threshold_v0 import EdgeThresholdConfig

    assert set(EdgeThresholdConfig.model_fields) <= set(EdgeThresholdConfigV1.model_fields)


# ---------- side selection by edge ----------


def test_picks_no_when_no_has_the_larger_edge_despite_p_above_half() -> None:
    """p_model 0.55: YES ask 0.70 is -0.15 edge, NO ask 0.25 is +0.20.
    v0 would have taken YES (p >= 0.5); v1 takes NO."""
    _populate(_book("yes-m1", 0.69, 0.70), _book("no-m1", 0.24, 0.25))
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.55))
    assert out.verdict == "ok"
    assert isinstance(out.payload, OrderIntent)
    assert out.payload.side == "no"
    assert out.payload.price == 0.25
    assert out.signals["edge"] == pytest.approx(0.20)
    assert out.signals["edge_yes"] == pytest.approx(-0.15)
    assert out.signals["edge_no"] == pytest.approx(0.20)


def test_picks_yes_when_yes_has_the_larger_edge_despite_p_below_half() -> None:
    _populate(_book("yes-m1", 0.29, 0.30), _book("no-m1", 0.74, 0.75))
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.45))
    assert out.verdict == "ok"
    assert out.payload.side == "yes"


def test_tie_goes_to_yes() -> None:
    _populate(_book("yes-m1", 0.49, 0.50), _book("no-m1", 0.39, 0.40))
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.60))  # both edges 0.10
    assert out.payload.side == "yes"


def test_side_lock_only_considers_yes() -> None:
    _populate(_book("yes-m1", 0.69, 0.70), _book("no-m1", 0.24, 0.25))
    out = _run(EdgeThresholdEntryV1(_cfg(side_lock=True)), _ar(0.55))
    assert out.verdict == "skip"
    assert out.reason == "edge below min_edge"
    assert out.signals["side"] == "yes"
    assert "edge_no" not in out.signals


def test_one_side_without_a_book_prices_the_other() -> None:
    _populate(_book("no-m1", 0.24, 0.25))
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.55))
    assert out.payload.side == "no"


def test_no_books_skips() -> None:
    _populate()
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.55))
    assert out.verdict == "skip"
    assert out.reason == "no order book"


# ---------- max_spread_pct ----------


def test_spread_pct_gate_rejects_a_spread_large_relative_to_price() -> None:
    """0.03 spread passes the absolute 0.05 cap but is 15% of a 0.20 ask."""
    _populate(_book("yes-m1", 0.17, 0.20))
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.40))
    assert out.verdict == "skip"
    assert out.reason == "spread above max_spread_pct"
    assert out.signals["spread_pct"] == pytest.approx(0.15)


def test_spread_pct_gate_passes_the_same_spread_on_a_dear_token() -> None:
    _populate(_book("yes-m1", 0.57, 0.60))  # 0.03 / 0.60 = 5%
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.80))
    assert out.verdict == "ok"


# ---------- portfolio gates ----------


def test_block_opposite_side_skips_while_other_side_is_open() -> None:
    _populate(_book("yes-m1", 0.49, 0.50))
    portfolio = _FakePortfolio([_rec("m1", "no", opened_at=_time.time() - 7200)])
    inst = EdgeThresholdEntryV1(_cfg(), portfolio_provider=lambda: portfolio)
    out = _run(inst, _ar(0.70))
    assert out.verdict == "skip"
    assert out.reason == "opposite_side_open"


def test_block_opposite_side_ignores_a_closed_opposite_position() -> None:
    _populate(_book("yes-m1", 0.49, 0.50))
    old = _time.time() - 7200
    portfolio = _FakePortfolio([_rec("m1", "no", opened_at=old, closed_at=old + 60)])
    inst = EdgeThresholdEntryV1(_cfg(), portfolio_provider=lambda: portfolio)
    assert _run(inst, _ar(0.70)).verdict == "ok"


def test_block_opposite_side_can_be_turned_off() -> None:
    _populate(_book("yes-m1", 0.49, 0.50))
    portfolio = _FakePortfolio([_rec("m1", "no", opened_at=_time.time() - 7200)])
    inst = EdgeThresholdEntryV1(
        _cfg(block_opposite_side=False), portfolio_provider=lambda: portfolio
    )
    assert _run(inst, _ar(0.70)).verdict == "ok"


def test_default_cooldown_is_sixty_minutes() -> None:
    _populate(_book("yes-m1", 0.49, 0.50))
    now = _time.time()
    recent = _FakePortfolio([_rec("m1", "yes", opened_at=now - 3600, closed_at=now - 30 * 60)])
    inst = EdgeThresholdEntryV1(_cfg(), portfolio_provider=lambda: recent)
    out = _run(inst, _ar(0.70))
    assert out.verdict == "skip"
    assert out.reason == "same_market_cooldown"

    older = _FakePortfolio([_rec("m1", "yes", opened_at=now - 7200, closed_at=now - 90 * 60)])
    inst = EdgeThresholdEntryV1(_cfg(), portfolio_provider=lambda: older)
    assert _run(inst, _ar(0.70)).verdict == "ok"


def test_no_provider_makes_portfolio_gates_noops() -> None:
    _populate(_book("yes-m1", 0.49, 0.50))
    assert _run(EdgeThresholdEntryV1(_cfg()), _ar(0.70)).verdict == "ok"


def test_intent_carries_the_entry_signals() -> None:
    _populate(_book("yes-m1", 0.49, 0.50))
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.70))
    intent = out.payload
    assert intent.p_model == 0.70
    assert intent.confidence == "medium"
    assert intent.edge == pytest.approx(0.20)
    assert intent.qty == pytest.approx(10.0 / 0.50)


def test_one_decision_reads_each_portfolio_slice_at_most_once() -> None:
    """Kill switch, cooldown and the opposite-side gate share one read."""
    _populate(_book("yes-m1", 0.49, 0.50))
    calls = {"list": 0, "open": 0}

    class _Counting(_FakePortfolio):
        def list_positions(self, limit: int = 100):
            calls["list"] += 1
            return super().list_positions(limit)

        def get_open_positions(self):
            calls["open"] += 1
            return super().get_open_positions()

    portfolio = _Counting([])
    cfg = _cfg(heat_cap_usd=1000.0, kill_max_consecutive_losses=3, kill_daily_loss_usd=50.0)
    inst = EdgeThresholdEntryV1(cfg, portfolio_provider=lambda: portfolio)
    assert _run(inst, _ar(0.70)).verdict == "ok"
    assert calls == {"list": 1, "open": 1}


def test_intent_is_priced_from_the_quote_that_chose_the_side() -> None:
    """Regression: v1 chose the side on one book read and priced it on a
    second one; a book that moved in between priced the order off a quote the
    choice never saw. Each side's book is now read exactly once."""
    from openpoly.markets.manager import manager as market_source_manager

    _populate(_book("yes-m1", 0.69, 0.70), _book("no-m1", 0.24, 0.25))
    store = market_source_manager.store
    original = store.get_order_book
    reads: list[str] = []

    def moving(token_id: str):
        reads.append(token_id)
        book = original(token_id)
        if reads.count(token_id) > 1 and book is not None:
            # Any second read of a side sees a moved book.
            return OrderBook(token_id=token_id, ts=1.0, bids=[(0.59, 100.0)], asks=[(0.60, 100.0)])
        return book

    store.get_order_book = moving  # type: ignore[method-assign]
    out = _run(EdgeThresholdEntryV1(_cfg()), _ar(0.55))
    assert out.verdict == "ok"
    assert out.payload.side == "no"
    assert out.payload.price == 0.25
    assert sorted(reads) == ["no-m1", "yes-m1"]

"""Tests for ThresholdExitV1 — v0's thresholds with a mid-based stop-loss.

The v0 behaviour these build on is covered in ``test_section_exit_threshold``;
here only what v1 changes (the stop-loss basis) and what it deliberately keeps
(take-profit / peak-drawdown on the bid).
"""

from __future__ import annotations

from openpoly.runtime.exit_monitor import exit_monitor
from openpoly.sections._base import SectionInput
from openpoly.sections._registry import scan
from openpoly.sections.exit.threshold_v0 import ThresholdExitV0
from openpoly.sections.exit.threshold_v1 import (
    CloseIntent,
    MarkedPosition,
    ThresholdExitConfigV1,
    ThresholdExitV1,
)
from openpoly.sections.exit.threshold_v0 import ThresholdExitConfig


def _pos(
    *,
    entry: float = 0.50,
    bid: float,
    peak: float | None = None,
    spread: float | None = None,
    qty: float = 20.0,
) -> MarkedPosition:
    """``spread`` sets the book's ask to ``bid + spread`` (both fields, as the
    exit monitor fills them)."""
    return MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=entry,
        qty=qty,
        current_price=bid,
        peak_price=peak if peak is not None else bid,
        spread=spread,
        ask=bid + spread if spread is not None else None,
    )


def _run(inst, pos: MarkedPosition):
    return inst.run(SectionInput(tick_type="hard", payload=pos))


def test_v1_in_default_catalog_next_to_v0() -> None:
    names = {e.name: e for e in scan() if e.type == "exit"}
    assert "ThresholdExitV0" in names
    assert "ThresholdExitV1" in names
    assert names["ThresholdExitV1"].version == "1.0.0"


def test_exit_monitor_runs_v1_by_default() -> None:
    assert isinstance(exit_monitor._exit, ThresholdExitV1)  # noqa: SLF001


def test_spread_alone_does_not_stop_out() -> None:
    """Bought at the 0.50 ask, book unchanged at 0.42 / 0.50: v0 reads -16%
    and stops out; v1 marks the stop at the 0.46 mid (-8%) and holds."""
    pos = _pos(bid=0.42, spread=0.08)
    assert _run(ThresholdExitV0(ThresholdExitConfig()), pos).payload.trigger == "stop_loss"
    out = _run(ThresholdExitV1(ThresholdExitConfigV1()), pos)
    assert out.verdict == "skip"
    assert out.signals["stop_return_pct"] == -0.08
    # The realizable (bid) return is still reported as-is.
    assert out.signals["return_pct"] == -0.16


def test_real_move_still_stops_out() -> None:
    """The whole book fell (0.36 / 0.40, mid -24%) → stop-loss fires, and the
    close is priced at the bid the sell will actually hit."""
    out = _run(ThresholdExitV1(ThresholdExitConfigV1()), _pos(bid=0.36, spread=0.04))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "stop_loss"
    assert out.payload.price == 0.36


def test_bid_collapse_under_a_stale_ask_still_stops_out() -> None:
    """Regression: an ask-based stop read 0.48 vs 0.50 (-4%) here and rode the
    position to zero. The mid (0.29, -42%) falls with the bid."""
    out = _run(ThresholdExitV1(ThresholdExitConfigV1()), _pos(bid=0.10, spread=0.38, peak=0.50))
    assert out.verdict == "ok"
    assert out.payload.trigger == "stop_loss"
    assert out.signals["stop_return_pct"] == -0.42


def test_missing_ask_falls_back_to_bid() -> None:
    out = _run(ThresholdExitV1(ThresholdExitConfigV1()), _pos(bid=0.42, spread=None))
    assert out.verdict == "ok"
    assert out.payload.trigger == "stop_loss"


def test_crossed_book_falls_back_to_bid() -> None:
    """A crossed book is not a discount to credit against the loss."""
    out = _run(ThresholdExitV1(ThresholdExitConfigV1()), _pos(bid=0.42, spread=-0.02))
    assert out.payload.trigger == "stop_loss"


def test_take_profit_stays_on_the_bid() -> None:
    """Take-profit realizes a gain, so it is measured where the sell fills:
    a +20% *ask* with a +16% bid must not close."""
    inst = ThresholdExitV1(ThresholdExitConfigV1(take_profit_enabled=True))
    assert _run(inst, _pos(bid=0.58, spread=0.02)).verdict == "skip"
    out = _run(inst, _pos(bid=0.62, spread=0.02))
    assert out.payload.trigger == "take_profit"


def test_peak_drawdown_unchanged_from_v0() -> None:
    """Same retrace case as v0's contract: peak 0.70, bid back to 0.65."""
    pos = _pos(bid=0.65, peak=0.70, spread=0.01)
    v0 = _run(ThresholdExitV0(ThresholdExitConfig()), pos)
    v1 = _run(ThresholdExitV1(ThresholdExitConfigV1()), pos)
    assert v0.payload.trigger == v1.payload.trigger == "peak_drawdown"


def test_dust_ask_over_a_collapsed_bid_fires_the_hard_stop() -> None:
    """Regression: bid 0.02 under a 0.97 dust ask reads -1% on the mid, so the
    mid stop alone never fired; the bid-based backstop does."""
    out = _run(ThresholdExitV1(ThresholdExitConfigV1()), _pos(bid=0.02, spread=0.95, peak=0.50))
    assert out.signals["stop_return_pct"] == -0.01
    assert out.verdict == "ok"
    assert out.payload.trigger == "stop_loss"


def test_hard_stop_cannot_fire_on_an_entry_spread_within_max_spread_pct() -> None:
    """Entry v1 admits spread/ask <= 0.10 measured to the depth-guarded bid —
    the same bid the exit marks at (here: the MarkedPosition's bid) — i.e. an
    instant bid-return of at most -10%, a third of the -30% backstop. The
    multi-level version of this lives in test_exit_monitor_freshness."""
    out = _run(ThresholdExitV1(ThresholdExitConfigV1()), _pos(bid=0.45, spread=0.05))
    assert out.verdict == "skip"


def test_hard_stop_is_configurable() -> None:
    inst = ThresholdExitV1(ThresholdExitConfigV1(hard_stop_loss_pct=1.0))
    assert _run(inst, _pos(bid=0.02, spread=0.95, peak=0.50)).verdict == "skip"

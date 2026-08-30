from __future__ import annotations

from openpoly.sections._base import SectionInput
from openpoly.sections._registry import scan
from openpoly.sections.exit.threshold_v0 import (
    CloseIntent,
    MarkedPosition,
    ThresholdExitConfig,
    ThresholdExitV0,
)


def _pos(
    current_price: float,
    avg_entry_price: float = 0.50,
    *,
    peak_price: float | None = None,
    qty: float = 20.0,
    spread: float | None = None,
    tick_size: float | None = None,
) -> MarkedPosition:
    return MarkedPosition(
        market_id="m1",
        side="yes",
        avg_entry_price=avg_entry_price,
        qty=qty,
        current_price=current_price,
        peak_price=current_price if peak_price is None else peak_price,
        spread=spread,
        tick_size=tick_size,
    )


def test_exit_in_default_catalog() -> None:
    entries = scan()
    matches = [e for e in entries if e.name == "ThresholdExitV0"]
    assert len(matches) == 1
    assert matches[0].type == "exit"


def test_run_no_position_skips() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig())
    out = inst.run(SectionInput(tick_type="hard", payload=None))
    assert out.verdict == "skip"
    assert out.reason == "no position upstream"


def test_within_thresholds_holds() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig())
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.52)))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"
    assert out.signals["return_pct"] == 0.04


def test_take_profit_closes() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True))
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.65)))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "take_profit"
    assert out.payload.market_id == "m1"
    assert out.payload.side == "yes"
    assert out.payload.qty == 20.0
    assert out.payload.price == 0.65
    assert out.signals["trigger"] == "take_profit"


def test_stop_loss_closes() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig())
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.40)))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "stop_loss"
    assert out.payload.price == 0.40


def test_invalid_entry_price_skips() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig())
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.5, avg_entry_price=0.0)))
    assert out.verdict == "skip"
    assert out.reason == "invalid avg_entry_price"


def test_custom_thresholds() -> None:
    inst = ThresholdExitV0(
        ThresholdExitConfig(take_profit_enabled=True, take_profit_pct=0.05, stop_loss_pct=0.05)
    )
    # +6% return → take-profit at the lowered 5% threshold
    tp = inst.run(SectionInput(tick_type="hard", payload=_pos(0.53)))
    assert tp.verdict == "ok"
    assert isinstance(tp.payload, CloseIntent)
    assert tp.payload.trigger == "take_profit"
    # -6% return → stop-loss at the lowered 5% threshold
    sl = inst.run(SectionInput(tick_type="hard", payload=_pos(0.47)))
    assert sl.verdict == "ok"
    assert isinstance(sl.payload, CloseIntent)
    assert sl.payload.trigger == "stop_loss"


def test_no_side_position_return_is_side_agnostic() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True))
    pos = MarkedPosition(
        market_id="m2",
        side="no",
        avg_entry_price=0.30,
        qty=10.0,
        current_price=0.40,
        peak_price=0.40,
    )
    out = inst.run(SectionInput(tick_type="hard", payload=pos))
    # (0.40 - 0.30) / 0.30 = 0.333 ≥ 0.20 → take_profit; held side carried through
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "take_profit"
    assert out.payload.side == "no"


# ---------- peak drawdown ----------


def test_peak_drawdown_triggers_when_meaningful_retrace() -> None:
    # take_profit disabled so the trailing lock is the only trigger that can
    # fire — with it enabled, +30% would take profit before the retrace.
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False))
    # Peak 0.70 (+40%, peak_gain = $4.00 >= the 30%-of-cost-basis floor $3.00);
    # now 0.65. Retrace 0.05 >= trail distance max(2 ticks = 0.02,
    # 0.12 * (0.70 - 0.50) = 0.024) = 0.024 -> close.
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.65, peak_price=0.70)))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "peak_drawdown"
    assert out.signals["peak_meaningful"] is True
    assert out.signals["peak_dd"] == 0.25
    assert out.signals["trail_distance"] == 0.024


def test_peak_drawdown_skipped_when_peak_below_usd_floor() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig())
    # qty 2 so even a +30% peak only banks $0.30 < $1 floor.
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.55, peak_price=0.65, qty=2.0)))
    # +10% return < TP 20%, no SL → within thresholds despite the retrace.
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"
    assert out.signals["peak_meaningful"] is False


def test_peak_drawdown_skipped_when_peak_below_pct_floor() -> None:
    # qty 1000 → cost basis $500 → 30% floor = $150. A +0.4pt peak banks only
    # $4, far below the floor, so the trailing lock stays disarmed.
    inst = ThresholdExitV0(ThresholdExitConfig())
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.51, peak_price=0.504, qty=1000.0)))
    assert out.verdict == "skip"
    assert out.signals["peak_meaningful"] is False


def test_stop_loss_beats_peak_drawdown() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig())
    # Peak 0.65 then crashed to 0.40 (-20% return). Both SL (-20% ≤ -15%) and
    # peak_dd ((0.65-0.40)/(0.65-0.50) = 1.67) fire; SL wins per precedence.
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.40, peak_price=0.65)))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "stop_loss"


def test_take_profit_beats_peak_drawdown() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True))
    # Peak 0.80 (+60%), now 0.62 (+24%, past TP) with a 0.18 retrace that is
    # far outside the 0.036 trailing distance. Both the take-profit ceiling and
    # the trailing lock qualify; precedence now puts take_profit second (right
    # after stop_loss) so the position books its target gain instead of
    # reporting a drawdown close at the very same price.
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.62, peak_price=0.80)))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "take_profit"


def test_peak_below_entry_does_not_trigger_peak_dd() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig())
    # Peak never went above entry — peak_dd undefined; section must skip
    # (not divide by zero or trigger spuriously).
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.48, peak_price=0.49)))
    assert out.verdict == "skip"
    assert out.signals["peak_meaningful"] is False
    assert out.signals["peak_dd"] is None


# ---------- trailing distance floor ----------


def test_single_tick_dip_holds_at_default_min_trail_ticks() -> None:
    # Entry 0.50, peak 0.70 (+$4.00 peak gain, armed), one tick down to 0.69.
    # Percentage trail alone would be 0.12 * 0.20 = 0.024, but the retrace is
    # only 0.01 — under both that and the 2-tick floor, so the position holds.
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False))
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.69, peak_price=0.70)))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"


def test_min_trail_ticks_floor_dominates_small_percentage_trail() -> None:
    # Peak barely above the arming floor: peak 0.66, entry 0.50, qty 20 →
    # peak gain $3.20 ≥ $3.00 floor. Percentage trail = 0.12 * 0.16 = 0.0192,
    # i.e. under two ticks; the floor lifts it to 0.02 so a single 0.01 dip
    # (which the raw percentage rule would close on) is held.
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False))
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.65, peak_price=0.66)))
    assert out.verdict == "skip"
    assert out.signals["peak_meaningful"] is True
    assert out.signals["trail_distance"] == 0.02
    # Two ticks down does clear the floor.
    out2 = inst.run(SectionInput(tick_type="hard", payload=_pos(0.64, peak_price=0.66)))
    assert out2.verdict == "ok"
    assert isinstance(out2.payload, CloseIntent)
    assert out2.payload.trigger == "peak_drawdown"


def test_spread_widens_the_trail_distance() -> None:
    # Same peak/current as the holding case above, but a 0.06 spread: the mark
    # is a bid inside a wide book, so a 0.05 retrace is inside the noise the
    # spread itself implies → hold.
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False))
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.65, peak_price=0.70, spread=0.06)))
    assert out.verdict == "skip"
    assert out.signals["trail_distance"] == 0.06


def test_position_tick_size_overrides_config_tick_size() -> None:
    # A venue tick of 0.05 makes the 2-tick floor 0.10, so a 0.05 retrace holds.
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False))
    out = inst.run(
        SectionInput(tick_type="hard", payload=_pos(0.65, peak_price=0.70, tick_size=0.05))
    )
    assert out.verdict == "skip"
    assert out.signals["trail_distance"] == 0.10


def test_min_trail_ticks_zero_restores_pure_percentage_trail() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False, min_trail_ticks=0))
    # Trail = 0.12 * 0.16 = 0.0192; a single 0.01 dip is still inside it, but
    # 0.02 clears it — the floor is what the config removed, nothing else.
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.64, peak_price=0.66)))
    assert out.verdict == "ok"
    assert out.signals["trail_distance"] == 0.0192


# ---------- arming floor ----------


def test_arming_floor_defaults_to_30_pct_of_cost_basis() -> None:
    cfg = ThresholdExitConfig()
    assert cfg.peak_meaningful_floor_pct == 0.30
    assert cfg.peak_meaningful_floor_usd == 1.0
    # Entry 0.50 × qty 20 = $10 cost basis → floor $3.00 → arms at peak 0.65.
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False))
    below = inst.run(SectionInput(tick_type="hard", payload=_pos(0.55, peak_price=0.64)))
    assert below.signals["peak_meaningful"] is False
    at = inst.run(SectionInput(tick_type="hard", payload=_pos(0.60, peak_price=0.65)))
    assert at.signals["peak_meaningful"] is True


# ---------- take profit switch ----------


def test_take_profit_is_off_by_default() -> None:
    cfg = ThresholdExitConfig()
    assert cfg.take_profit_enabled is False
    # The pct is kept as an opt-in cap for callers that want a hard ceiling.
    assert cfg.take_profit_pct == 0.20
    # +30% and the peak is the current price: with the ceiling off the position
    # keeps running toward the trailing lock's +30% arming floor instead of
    # being closed before the lock can ever engage.
    out = ThresholdExitV0(cfg).run(SectionInput(tick_type="hard", payload=_pos(0.65)))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"


def test_take_profit_can_be_disabled() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False))
    # +30% with the peak at the current price: nothing to retrace, and the
    # take-profit ceiling is off → the position stays open to keep running.
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.65)))
    assert out.verdict == "skip"
    assert out.reason == "within thresholds"


def test_take_profit_disabled_still_stops_out() -> None:
    inst = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=False))
    out = inst.run(SectionInput(tick_type="hard", payload=_pos(0.40)))
    assert out.verdict == "ok"
    assert isinstance(out.payload, CloseIntent)
    assert out.payload.trigger == "stop_loss"


# ---------- config schema (canvas-facing) ----------


def test_all_config_fields_carry_descriptions() -> None:
    props = ThresholdExitConfig.model_json_schema()["properties"]
    for name in (
        "take_profit_pct",
        "take_profit_enabled",
        "stop_loss_pct",
        "peak_drawdown_pct",
        "min_trail_ticks",
        "tick_size",
        "peak_meaningful_floor_usd",
        "peak_meaningful_floor_pct",
    ):
        assert name in props, name
        assert props[name].get("description"), name


# ---------- synthetic price path regression ----------


def _rising_then_retracing_path() -> list[float]:
    """0.50 → 0.80 in +2/-1 tick steps (single-tick noise on every leg),
    then a clean retrace back down to 0.55."""
    prices: list[float] = []
    price = 0.50
    while price < 0.80 - 1e-9:
        price = round(price + 0.02, 2)
        prices.append(price)
        prices.append(round(price - 0.01, 2))
    prices.append(0.80)
    down = 0.80
    while down > 0.55 + 1e-9:
        down = round(down - 0.01, 2)
        prices.append(down)
    return prices


def _walk(config: ThresholdExitConfig, path: list[float]) -> tuple[float, float] | None:
    """Replay ``path`` through the section the way ExitMonitor does (monotone
    peak, 0.01 spread). Returns (exit_price, peak_at_exit) or None if the
    position was never closed."""
    inst = ThresholdExitV0(config)
    peak = path[0]
    for price in path:
        peak = max(peak, price)
        out = inst.run(
            SectionInput(
                tick_type="hard",
                payload=_pos(price, peak_price=peak, spread=0.01),
            )
        )
        if out.verdict == "ok":
            return price, peak
    return None


def test_old_defaults_close_on_the_first_downtick() -> None:
    # The pre-fix configuration: 1%-of-cost-basis arming floor, no trailing
    # floor, peak_drawdown ahead of take_profit. It gives the move back at
    # ~0.55 — barely above entry — because the trailing distance at that peak
    # is 0.12 × 0.06 = 0.007, i.e. under one Polymarket tick.
    old = ThresholdExitConfig(
        peak_meaningful_floor_pct=0.01,
        min_trail_ticks=0,
        take_profit_enabled=False,
    )
    result = _walk(old, _rising_then_retracing_path())
    assert result is not None
    exit_price, _ = result
    assert 0.54 <= exit_price <= 0.55


def test_new_defaults_hold_through_noise_and_capture_most_of_the_peak() -> None:
    # Bare defaults — the configuration the runtime actually ships with. The
    # trailing lock is the primary exit path; take-profit is off, so nothing
    # closes this move before the lock arms.
    new = ThresholdExitConfig()
    path = _rising_then_retracing_path()
    result = _walk(new, path)
    assert result is not None
    exit_price, peak = result
    assert peak == 0.80
    captured = (exit_price - 0.50) / (peak - 0.50)
    assert captured >= 0.50, f"captured only {captured:.2%} at {exit_price}"
    # It must have survived every single-tick dip on the way up.
    assert exit_price > 0.75

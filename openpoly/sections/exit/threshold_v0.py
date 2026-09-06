"""Threshold exit baseline — take-profit / stop-loss / peak-drawdown.

Atomized from a prior project v8 §10: Rule 6 (take_profit), Rule 2 (max_loss) and a
*lightweight* Rule 3 (peak_drawdown). openPoly tracks ``peak_price`` as a
scalar but deliberately drops ``peak_exit_cost`` (the cost-adjusted form) and
the heavier trailing-stop / LLM-consult variants. This keeps the public baseline
deterministic, auditable, and cheap to run.

The section is a pure function of its input ``MarkedPosition``: the runtime
injects the held side's current price *and* its tracked peak price into the
position before each call. Peak tracking itself lives in ``ExitMonitor`` —
the section never holds state across ticks.

Trigger precedence is ``stop_loss → take_profit → peak_drawdown``: the
absolute-loss circuit fires first, then the absolute take-profit ceiling, and
the trailing lock on banked gains last.

Two properties of the trailing lock are worth stating explicitly, because the
naive form of the rule gives the whole edge back (v0.3.0):

* The retrace is compared against an *absolute price distance*, not against a
  fraction of ``peak - entry``. ``peak_drawdown_pct × (peak - entry)`` shrinks
  to nothing right after the position arms — on a $10 position it can land
  below one Polymarket tick (0.01), which closes every winner on its first
  downtick. The effective distance is therefore
  ``max(min_trail_ticks × tick_size, spread, peak_drawdown_pct × (peak - entry))``:
  never tighter than a couple of ticks, never tighter than the book's own
  spread, and widening with the size of the move.
* It arms late. ``peak_meaningful_floor_pct`` defaults to 30% of cost basis so
  the lock only engages on a move large enough that giving part of it back is
  a real loss of banked profit, rather than noise around entry.

Because the lock arms at +30%, ``take_profit_enabled`` ships **off**: a +20%
ceiling would close every winner before the lock could ever engage, leaving the
trailing behaviour dead code. The shipped trade-off is explicit — between entry
and +30% a position is protected by the stop-loss alone; above +30% the
trailing lock takes over and take-profit is an opt-in cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from openpoly.sections._base import SectionInput, SectionOutput


Side = Literal["yes", "no"]
Trigger = Literal["take_profit", "stop_loss", "peak_drawdown"]


@dataclass(frozen=True)
class MarkedPosition:
    """An open position the exit section evaluates. ``avg_entry_price``,
    ``current_price`` and ``peak_price`` are all prices of the held ``side``
    (Polymarket token price in 0..1), so return math is side-agnostic. The
    monitor injects ``peak_price`` from its per-position max — see
    ``ExitMonitor`` — and the section uses it for peak-drawdown only.

    ``spread`` and ``tick_size`` are optional book context added in v0.3.0 for
    the trailing-distance floor. Both default to ``None`` so any caller written
    against the older five-field shape keeps working; the section falls back to
    ``ThresholdExitConfig.tick_size`` and a zero spread when they are absent.
    """

    market_id: str
    side: Side
    avg_entry_price: float
    qty: float
    current_price: float
    peak_price: float
    spread: float | None = None  # best_ask - best_bid at mark time
    tick_size: float | None = None  # venue tick, when the book exposes one


@dataclass(frozen=True)
class CloseIntent:
    """A decision to close (sell) an open position. ``trigger`` records which
    threshold fired."""

    market_id: str
    side: Side
    price: float
    qty: float
    trigger: Trigger


class ThresholdExitConfig(BaseModel):
    take_profit_pct: float = Field(
        default=0.20,
        ge=0.0,
        le=10.0,
        description=(
            "Take-profit ceiling, as a fraction of entry (0.20 = +20%). It caps every "
            "winner at this return, so it is OFF by default (see take_profit_enabled) "
            "and only applies once you switch it on: at +20% the trailing lock has not "
            "armed yet, so leaving it on means no position ever reaches the lock."
        ),
    )
    take_profit_enabled: bool = Field(
        default=False,
        description=(
            "Whether the take-profit ceiling is active. Off by default: the trailing "
            "peak-drawdown lock is the primary exit for winners, with the stop-loss "
            "underneath. Turn it on to cap every winner at take_profit_pct instead."
        ),
    )
    stop_loss_pct: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Close the position when its loss reaches this fraction (0.15 = -15%).",
    )
    peak_drawdown_pct: float = Field(
        default=0.12,
        ge=0.0,
        le=1.0,
        description=(
            "Trailing lock: close when the price has retraced this fraction of the "
            "banked gain (peak - entry) from the peak. Only ever widens the trailing "
            "distance — the min_trail_ticks and spread floors below set its minimum."
        ),
    )
    min_trail_ticks: int = Field(
        default=2,
        ge=0,
        le=100,
        description=(
            "Floor on the trailing distance, in price ticks. A percentage-only trail is "
            "tightest right after the position arms, where it can fall below a single "
            "tick and close on ordinary quote noise; two ticks is the smallest distance "
            "a real move can be distinguished from that noise."
        ),
    )
    tick_size: float = Field(
        default=0.01,
        gt=0.0,
        le=1.0,
        description=(
            "Price tick used for the min_trail_ticks floor (Polymarket CLOB is 0.01). "
            "A tick size carried on the marked position overrides this."
        ),
    )
    peak_meaningful_floor_usd: float = Field(
        default=1.0,
        ge=0.0,
        description="Skip peak_drawdown unless the peak gain in USD exceeds this floor.",
    )
    peak_meaningful_floor_pct: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description=(
            "Skip peak_drawdown unless the peak gain exceeds this fraction of cost basis. "
            "Defaults to 30%: at grain-scale stakes the USD floor alone arms the trailing "
            "lock after a ~+10% move, where a retrace is noise rather than given-back "
            "profit. Arming at +30% means the lock only ever protects a real gain."
        ),
    )


class ThresholdExitV0:
    SECTION_TYPE = "exit"
    SECTION_VERSION = "0.3.0"
    REQUIRES = ["market_data", "portfolio"]
    Config = ThresholdExitConfig

    def __init__(self, config: ThresholdExitConfig) -> None:
        self.config = config

    def _trail_distance(self, pos: MarkedPosition) -> float:
        """Effective trailing distance in price. The percentage trail is a
        floor-of-last-resort: the tick floor and the live spread both override
        it while the banked gain is still small."""
        tick = pos.tick_size if pos.tick_size and pos.tick_size > 0 else self.config.tick_size
        spread = pos.spread if pos.spread is not None and pos.spread > 0 else 0.0
        pct_trail = self.config.peak_drawdown_pct * (pos.peak_price - pos.avg_entry_price)
        return max(self.config.min_trail_ticks * tick, spread, pct_trail)

    def run(self, input: SectionInput) -> SectionOutput:
        pos = input.payload
        if not isinstance(pos, MarkedPosition):
            return SectionOutput(payload=None, verdict="skip", reason="no position upstream")
        if pos.avg_entry_price <= 0:
            return SectionOutput(payload=None, verdict="skip", reason="invalid avg_entry_price")

        return_pct = (pos.current_price - pos.avg_entry_price) / pos.avg_entry_price

        cost_basis = pos.avg_entry_price * pos.qty
        peak_gain_usd = (pos.peak_price - pos.avg_entry_price) * pos.qty
        floor = max(
            self.config.peak_meaningful_floor_usd,
            self.config.peak_meaningful_floor_pct * cost_basis,
        )
        peak_meaningful = pos.peak_price > pos.avg_entry_price and peak_gain_usd >= floor
        retrace = pos.peak_price - pos.current_price
        trail_distance = self._trail_distance(pos)
        if peak_meaningful:
            peak_dd = retrace / (pos.peak_price - pos.avg_entry_price)
        else:
            peak_dd = 0.0

        trigger: Trigger | None
        if return_pct <= -self.config.stop_loss_pct:
            trigger = "stop_loss"
        elif self.config.take_profit_enabled and return_pct >= self.config.take_profit_pct:
            trigger = "take_profit"
        elif peak_meaningful and retrace >= trail_distance:
            trigger = "peak_drawdown"
        else:
            trigger = None

        signals: dict[str, object] = {
            "return_pct": round(return_pct, 4),
            "peak_price": round(pos.peak_price, 4),
            "peak_dd": round(peak_dd, 4) if peak_meaningful else None,
            "peak_meaningful": peak_meaningful,
            "trail_distance": round(trail_distance, 4),
        }

        if trigger is None:
            return SectionOutput(
                payload=None,
                verdict="skip",
                reason="within thresholds",
                signals=signals,
            )

        intent = CloseIntent(
            market_id=pos.market_id,
            side=pos.side,
            price=pos.current_price,
            qty=pos.qty,
            trigger=trigger,
        )
        signals["trigger"] = trigger
        return SectionOutput(
            payload=intent,
            verdict="ok",
            reason=trigger,
            signals=signals,
        )

    @staticmethod
    def CONTRACT_TEST() -> None:
        inst = ThresholdExitV0(ThresholdExitConfig())

        out_skip = inst.run(SectionInput(tick_type="hard", payload=None))
        assert out_skip.verdict == "skip"

        hold = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.52,
            peak_price=0.52,
        )
        out_hold = inst.run(SectionInput(tick_type="hard", payload=hold))
        assert out_hold.verdict == "skip"

        # take_profit is opt-in (off by default), so the ceiling gets its own
        # instance here.
        capped = ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True))
        win = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.65,
            peak_price=0.65,
        )
        out_tp = capped.run(SectionInput(tick_type="hard", payload=win))
        assert out_tp.verdict == "ok"
        assert isinstance(out_tp.payload, CloseIntent)
        assert out_tp.payload.trigger == "take_profit"

        loss = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.40,
            peak_price=0.52,
        )
        out_sl = inst.run(SectionInput(tick_type="hard", payload=loss))
        assert out_sl.verdict == "ok"
        assert isinstance(out_sl.payload, CloseIntent)
        assert out_sl.payload.trigger == "stop_loss"

        # Peak drawdown: ran up to 0.70 (peak gain $4.00 ≥ the $3.00 arming
        # floor), now back to 0.65 → retrace 0.05 ≥ the trailing distance
        # max(2 × 0.01, 0.12 × 0.20) = 0.024. The default config already has
        # take_profit off, so the +30% return doesn't take precedence.
        trailing = inst
        retrace = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.65,
            peak_price=0.70,
        )
        out_pd = trailing.run(SectionInput(tick_type="hard", payload=retrace))
        assert out_pd.verdict == "ok"
        assert isinstance(out_pd.payload, CloseIntent)
        assert out_pd.payload.trigger == "peak_drawdown"

        # A single tick down from the same peak must NOT close — this is the
        # regression the trailing floor exists to prevent.
        one_tick = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.69,
            peak_price=0.70,
        )
        assert trailing.run(SectionInput(tick_type="hard", payload=one_tick)).verdict == "skip"

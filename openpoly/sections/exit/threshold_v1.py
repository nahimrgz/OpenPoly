"""Threshold exit v1 — take-profit / stop-loss / peak-drawdown, spread-aware stop.

Same rules, precedence and config as ``threshold_v0`` (see that module for the
trailing-lock design); v1 changes what the stop-loss compares, caps the
spread floor of the trailing distance, and adds one config field, the
``hard_stop_loss_pct`` backstop.

An entry fills at the level-1 *ask*; the exit monitor marks the position at a
depth-guarded *bid*. v0's stop-loss computed ``(bid - ask_entry) / ask_entry``,
so the spread alone counted as a loss: any book whose ``spread / ask`` was at
least ``stop_loss_pct`` (0.15 by default — a 0.06 spread on a 0.40 token)
stopped the position out on the very next tick with no market move at all.

v1's stop-loss marks at the current *mid* — ``(bid + ask) / 2``, with
``MarkedPosition.ask`` supplied by the exit monitor — against the entry ask. A
book that has not moved then costs only half its spread: under the entry's
``max_spread_pct`` (0.10) that is at most ~5%, well inside the 15% stop. It
falls back to the bid — v0's behaviour — when the ask is unknown or below the
bid (a crossed book is not a discount to credit against the loss).

Why not the ask itself, which would cancel the spread entirely: when the bids
collapse but the asks stay put (entry 0.50, bid 0.10, ask 0.48), an ask-based
stop reads -4% and rides the position to zero. The mid still falls with the
bid (0.29, -42%), so a real collapse still stops out.

Take-profit and peak-drawdown keep v0's bid-based semantics on purpose. Both
decide when to *realize* a gain, and the bid is the price a sell actually
gets, so measuring them off the ask would book profit the position cannot
collect. The spread also already widens the trailing distance (see
``_trail_distance``), and peak tracking is bid-vs-bid, so the spread does not
bias the retrace. The cost of the bid basis there is only that the lock arms a
spread later than an ask basis would — conservative, not a false exit.

The mid has its own blind spot: when the bids collapse under a far-away ask
(entry 0.50, bid 0.02, ask 0.97) the mid barely moves (-1%) and the normal
stop would never fire. A catastrophic backstop covers that:
``hard_stop_loss_pct`` (default 0.30) is checked on the *bid* — the
depth-guarded, executable price — and closes regardless of the mid. It cannot
fire on spread alone at entry: entry v1 gates ``spread / ask`` at
``max_spread_pct`` (0.10) using the same depth-guarded bid the exit marks at
(``openpoly.markets.pricing``), so the instant bid-return is at most -10%, a
third of the backstop.

The mark is depth-guarded by the exit monitor (the first bid level with real
size); ``MarkedPosition.ask`` is the raw level-1 ask — the spread-floor cap
below already bounds what a dust ask can do to the trailing distance, while
depth-guarding the ask only ever moved it up (or to None), inflating the stop
mid and suppressing the normal stop. When *no* bid level has real size, a v0
position is unevaluable; v1 declares ``EVALUATES_THIN_MARKS`` and is still run
on the raw best bid (``MarkedPosition.thin``), where only the hard stop is
evaluated — closing into a thin book beats riding a collapse to zero.

A thin bid is not evidence of a collapse on its own: a momentary bid pull
leaves only dust at level 1 while the asks stay healthy. The thin hard stop
therefore fires only when the *ask* confirms it — the level-1 ask's return
vs. entry is also at or below ``-hard_stop_loss_pct`` — or, with no ask at
all, when the collapse persists for ``THIN_NO_ASK_CONFIRM_TICKS`` consecutive
ticks (``MarkedPosition.thin_streak``, kept by the exit monitor). Its close
floor is ``THIN_STOP_SWEEP_MARGIN`` of the entry price below the raw bid (and
never under the venue's minimum price): pinned to the raw level-1 bid, a sell
into a sub-share dust level could never fill.

The trailing distance's spread floor is capped at ``pct_trail + 2 ticks``: an
uncapped floor let one wide (or dust-inflated) spread push the trailing
distance past any realistic retrace, disabling the lock.

v1 subclasses v0 and overrides the stop-loss test and the trailing distance;
``MarkedPosition`` / ``CloseIntent`` are v0's (the exit monitor type-checks
against them), so v1 is a drop-in swap.
"""

from __future__ import annotations

from pydantic import Field

from openpoly.execution.sizing import MIN_TOKEN_PRICE
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.sections.exit.threshold_v0 import (
    CloseIntent,
    MarkedPosition,
    ThresholdExitConfig,
    ThresholdExitV0,
)

__all__ = ["CloseIntent", "MarkedPosition", "ThresholdExitConfigV1", "ThresholdExitV1"]


class ThresholdExitConfigV1(ThresholdExitConfig):
    hard_stop_loss_pct: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description=(
            "Catastrophic backstop: close when the executable bid's loss reaches "
            "this fraction (0.30 = -30%), whatever the mid says. The normal "
            "stop_loss_pct is measured on the mid so the spread alone cannot "
            "trigger it; this one catches a bid collapse hidden behind a stale "
            "or dust ask."
        ),
    )


# The trailing distance's spread floor may exceed the percentage trail by at
# most this many ticks.
_SPREAD_CAP_TICKS = 2

# A thin-book hard stop with no ask to confirm it fires only after the bid
# collapse has been seen on this many consecutive ticks (~30s at the 10s tick).
THIN_NO_ASK_CONFIRM_TICKS = 3

# How far below the raw best bid a confirmed thin hard stop may sweep, as a
# fraction of the entry price. The raw bid is a dust level by definition here
# (no level has real size), so the sell must be allowed to reach the levels
# under it; this bounds the slippage it accepts to do so.
THIN_STOP_SWEEP_MARGIN = 0.10


def _stop_mark(pos: MarkedPosition) -> float:
    """The price the stop-loss compares against the entry ask: the mid when
    the ask is known and not crossed below the bid, else the bid."""
    if pos.ask is not None and pos.ask >= pos.current_price:
        return (pos.current_price + pos.ask) / 2.0
    return pos.current_price


class ThresholdExitV1(ThresholdExitV0):
    SECTION_TYPE = "exit"
    SECTION_VERSION = "1.0.0"
    REQUIRES = ["market_data", "portfolio"]
    Config = ThresholdExitConfigV1
    # Run me on the raw best bid when no bid level has real size (see the
    # module docstring); only the hard stop is evaluated there.
    EVALUATES_THIN_MARKS = True

    config: ThresholdExitConfigV1

    def _trail_distance(self, pos: MarkedPosition) -> float:
        """v0's trailing distance with the spread floor capped at
        ``pct_trail + 2 ticks`` (still never below ``min_trail_ticks``)."""
        tick = pos.tick_size if pos.tick_size and pos.tick_size > 0 else self.config.tick_size
        spread = pos.spread if pos.spread is not None and pos.spread > 0 else 0.0
        pct_trail = self.config.peak_drawdown_pct * (pos.peak_price - pos.avg_entry_price)
        capped_spread = min(spread, max(pct_trail, 0.0) + _SPREAD_CAP_TICKS * tick)
        return max(self.config.min_trail_ticks * tick, capped_spread, pct_trail)

    def _thin_mark_decision(self, pos: MarkedPosition) -> SectionOutput:
        """No executable bid level: only the hard stop, on the raw best bid,
        and only once the ask side (or, with no ask, persistence) confirms it
        (see the module docstring)."""
        entry = pos.avg_entry_price
        hard = self.config.hard_stop_loss_pct
        return_pct = (pos.current_price - entry) / entry
        signals: dict[str, object] = {"return_pct": round(return_pct, 4), "thin": True}
        if return_pct > -hard:
            return SectionOutput(
                payload=None, verdict="skip", reason="thin book: hard stop only", signals=signals
            )
        if pos.ask is not None:
            ask_return = (pos.ask - entry) / entry
            signals["ask_return_pct"] = round(ask_return, 4)
            if ask_return > -hard:
                return SectionOutput(
                    payload=None,
                    verdict="skip",
                    reason="thin book: bid collapse not confirmed by the ask",
                    signals=signals,
                )
        else:
            streak = pos.thin_streak + 1
            signals["thin_streak"] = streak
            if streak < THIN_NO_ASK_CONFIRM_TICKS:
                signals["thin_stop_pending"] = True
                return SectionOutput(
                    payload=None,
                    verdict="skip",
                    reason="thin book: no ask, awaiting a persistent collapse",
                    signals=signals,
                )
        signals["trigger"] = "stop_loss"
        floor = max(MIN_TOKEN_PRICE, pos.current_price - THIN_STOP_SWEEP_MARGIN * entry)
        signals["floor_price"] = round(floor, 4)
        intent = CloseIntent(
            market_id=pos.market_id,
            side=pos.side,
            price=floor,
            qty=pos.qty,
            trigger="stop_loss",
        )
        return SectionOutput(payload=intent, verdict="ok", reason="stop_loss", signals=signals)

    @staticmethod
    def _stop_return_pct(pos: MarkedPosition) -> float:
        """Return on the mid-based stop mark."""
        return (_stop_mark(pos) - pos.avg_entry_price) / pos.avg_entry_price

    def _stop_triggered(self, pos: MarkedPosition, return_pct: float) -> bool:
        # ``return_pct`` is the bid return — the backstop's basis.
        return (
            self._stop_return_pct(pos) <= -self.config.stop_loss_pct
            or return_pct <= -self.config.hard_stop_loss_pct
        )

    def run(self, input: SectionInput) -> SectionOutput:
        pos = input.payload
        if isinstance(pos, MarkedPosition) and pos.thin and pos.avg_entry_price > 0:
            return self._thin_mark_decision(pos)
        out = super().run(input)
        if isinstance(pos, MarkedPosition) and pos.avg_entry_price > 0:
            out.signals["stop_return_pct"] = round(self._stop_return_pct(pos), 4)
        return out

    @staticmethod
    def CONTRACT_TEST() -> None:
        inst = ThresholdExitV1(ThresholdExitConfigV1())
        assert inst.run(SectionInput(tick_type="hard", payload=None)).verdict == "skip"

        # A wide spread with no market move must NOT stop out. Bought at the
        # 0.50 ask; the book is unchanged (bid 0.42 / ask 0.50), so the
        # bid-based return is -16% but the mid (0.46) is only -8%.
        wide = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.42,
            peak_price=0.42,
            spread=0.08,
            ask=0.50,
        )
        assert inst.run(SectionInput(tick_type="hard", payload=wide)).verdict == "skip"

        # Bids collapsed under a stale ask (0.10 / 0.48): mid 0.29 → stop.
        moved = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.10,
            peak_price=0.50,
            spread=0.38,
            ask=0.48,
        )
        out = inst.run(SectionInput(tick_type="hard", payload=moved))
        assert isinstance(out.payload, CloseIntent)
        assert out.payload.trigger == "stop_loss"

        # Bids collapsed under a dust ask (0.02 / 0.97): mid -1%, but the
        # bid-based backstop fires.
        dust = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.02,
            peak_price=0.50,
            spread=0.95,
            ask=0.97,
        )
        out = inst.run(SectionInput(tick_type="hard", payload=dust))
        assert isinstance(out.payload, CloseIntent)
        assert out.payload.trigger == "stop_loss"

        # Thin book, dust bid at -60% but healthy asks: a bid pull, not a
        # collapse — the thin hard stop must not fire.
        pulled = MarkedPosition(
            market_id="m1",
            side="yes",
            avg_entry_price=0.50,
            qty=20.0,
            current_price=0.20,
            peak_price=0.50,
            ask=0.49,
            thin=True,
        )
        assert inst.run(SectionInput(tick_type="hard", payload=pulled)).verdict == "skip"

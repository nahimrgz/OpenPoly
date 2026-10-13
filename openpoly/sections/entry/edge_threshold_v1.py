"""Edge-threshold entry v1 — side chosen by edge, spread-relative and
opposite-side gates.

Same decision shape as ``edge_threshold_v0`` (see that module for the sizing,
heat-cap, cooldown / lockout and kill-switch design); every v0 knob keeps its
meaning. v1 changes:

* **Side by edge, not by probability.** v0 bought YES whenever
  ``p_model >= 0.5``. That ignores price: ``p_model = 0.55`` against a YES ask
  of 0.70 and a NO ask of 0.25 is a 0.20 edge on NO and a negative one on YES,
  and v0 skipped it. v1 reads both books and computes::

      edge_yes = p_model       - yes_ask
      edge_no  = (1 - p_model) - no_ask

  and takes the larger (YES on a tie). ``side_lock`` still restricts the
  choice to YES. The gates below then run on the chosen side only — a side
  that wins on edge but fails, say, the spread gate is skipped, not swapped
  for the weaker side.
* **Spread measured to the depth-guarded bid** — the first bid level with real
  size (``openpoly.markets.pricing``), the same price the exit monitor marks
  the position at, not the raw level-1 bid; a side with no such level is not
  priceable.
* **``max_spread_pct``** — the absolute ``max_spread`` treats a 0.04 spread
  the same on a 0.80 token and a 0.10 one. On the cheap token that spread is
  40% of the price, and the round-trip gives it all back; v1 also rejects an
  entry whose ``spread / ask`` exceeds this fraction.
* **``block_opposite_side``** (on by default) — skip the entry while a position
  on the *other* side of the same market is open. Holding YES and NO on one
  market is a guaranteed loss of both spreads, not a hedge.
* **``same_market_cooldown_minutes`` defaults to 60** (v0: 0, off).

The section itself does no IO for book freshness: the orchestrator refreshes
stale books *before* calling ``run()`` (``PipelineOrchestrator.
_ensure_fresh_books``) and, because v1 declares ``ACCEPTS_FRESH_BOOKS``, hands
it the analysis wrapped in ``FreshBooks`` — the tokens whose books are within
budget. A side whose book is stale is then left out of the side choice (as
``stale_book``) instead of winning on a cached price and vetoing the whole
decision afterwards; the other, fresh side is still considered. When no side
can be priced, the skip names each side's real reason.

v1 subclasses v0 and overrides only the decision steps it changes
(``_choose_side``, ``_price_gates``, ``_extra_portfolio_gates``); the step
sequence, sizing and the late-buy veto are v0's. ``OrderIntent`` is shared
with v0 (the orchestrator and both executors type-check against it), so v1 is
a drop-in swap.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from openpoly.execution.sizing import is_dust_qty
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.pricing import first_level_with_size
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry.edge_threshold_v0 import (
    EdgeThresholdConfig,
    EdgeThresholdEntryV0,
    OrderIntent,
    PortfolioProvider,
    PortfolioReader,
    Quote,
    Side,
    SideChoice,
)

__all__ = ["EdgeThresholdConfigV1", "EdgeThresholdEntryV1", "FreshBooks", "OrderIntent"]


@dataclass(frozen=True)
class FreshBooks:
    """Entry payload for sections declaring ``ACCEPTS_FRESH_BOOKS``: the
    analysis plus the tokens whose books the orchestrator found within the
    freshness budget for this decision."""

    analysis: AnalysisResult
    fresh_tokens: frozenset[str]


class EdgeThresholdConfigV1(EdgeThresholdConfig):
    same_market_cooldown_minutes: int = Field(
        default=60,
        ge=0,
        le=1440,
        description=(
            "Skip the entry if a position on the same (market, side) was "
            "opened or closed within this many minutes. 0 disables the "
            "check. Targets the repeated-loss-on-same-market pattern. "
            "Superseded by ``same_market_lifetime_lockout`` when that is "
            "True."
        ),
    )
    max_spread_pct: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description=(
            "Skip the entry if the held side's spread is more than this "
            "fraction of its ask (0.10 = the spread may be at most 10% of the "
            "price). Complements the absolute max_spread, which cannot tell a "
            "spread that is noise on an expensive token from one that eats a "
            "cheap token's whole edge."
        ),
    )
    block_opposite_side: bool = Field(
        default=True,
        description=(
            "Skip the entry if a position on the OTHER side of the same "
            "market is currently open. Holding YES and NO together locks in "
            "both spreads as a loss."
        ),
    )


class EdgeThresholdEntryV1(EdgeThresholdEntryV0):
    SECTION_TYPE = "entry"
    SECTION_VERSION = "1.0.0"
    REQUIRES = ["order_book", "market_data"]
    Config = EdgeThresholdConfigV1
    # The orchestrator passes ``FreshBooks`` (see the module docstring).
    ACCEPTS_FRESH_BOOKS = True

    config: EdgeThresholdConfigV1

    def __init__(
        self,
        config: EdgeThresholdConfigV1,
        portfolio_provider: PortfolioProvider | None = None,
    ) -> None:
        super().__init__(config, portfolio_provider)
        # Fresh tokens of the decision in progress (None: not checked). Set
        # only for the duration of one ``run``; the entry runs serially.
        self._fresh_tokens: frozenset[str] | None = None

    def run(self, input: SectionInput) -> SectionOutput:
        payload = input.payload
        if not isinstance(payload, FreshBooks):
            return super().run(input)
        self._fresh_tokens = payload.fresh_tokens
        try:
            return super().run(SectionInput(tick_type=input.tick_type, payload=payload.analysis))
        finally:
            self._fresh_tokens = None

    def _choose_side(self, res: AnalysisResult) -> SideChoice | SectionOutput:
        """Price both sides and take the larger edge (YES on a tie: ``max``
        keeps the first of equal keys). ``side_lock`` removes NO from the
        choice entirely, as in v0. Each side's quote is read once; the chosen
        one is returned so the order is priced from the very quote that won."""
        market = market_source_manager.store.get(res.market_id)
        if market is None:
            return SectionOutput(payload=None, verdict="skip", reason="market not found")
        sides: tuple[Side, ...] = ("yes",) if self.config.side_lock else ("yes", "no")
        quotes: dict[Side, Quote] = {}
        edges: dict[Side, float] = {}
        skipped: dict[Side, str] = {}
        fresh = self._fresh_tokens
        for side in sides:
            token = market.token_for(side)
            if fresh is not None and token is not None and token not in fresh:
                # Ranking on a stale cached price would pick a side the
                # orchestrator then refuses to trade, losing the fresh one.
                skipped[side] = "stale_book"
                continue
            # A side is priceable when v0's own quote read succeeds for it.
            quote = self._read_quote(res, side)
            if isinstance(quote, SectionOutput):
                skipped[side] = quote.reason or "unpriceable"
                continue
            fair = res.p_model if side == "yes" else 1.0 - res.p_model
            quotes[side] = quote
            edges[side] = fair - quote[1]
        if not edges:
            return SectionOutput(
                payload=None,
                verdict="skip",
                reason=_skip_reason(skipped),
                signals={f"skip_{side}": reason for side, reason in skipped.items()},
            )
        # max() keeps the first of equal keys; YES is inserted first.
        chosen = max(edges, key=lambda s: edges[s])
        signals: dict[str, object] = {"side": chosen}
        for side, edge in edges.items():
            signals[f"edge_{side}"] = round(edge, 4)
        return chosen, signals, quotes[chosen]

    def _quote_bid(self, book: OrderBook) -> float | None:
        """The depth-guarded bid — the same price the exit monitor marks a
        position at (``first_level_with_size``). Gating the spread on a raw
        level-1 bid let a 3-share probe at 0.48 over real size at 0.33 pass
        a 4% spread check, then hard-stop the position on the next exit tick
        at -34%. None (no level with real size) skips the side."""
        return first_level_with_size(book.bids)

    def _needs_portfolio(self) -> bool:
        return super()._needs_portfolio() or self.config.block_opposite_side

    def _extra_portfolio_gates(
        self,
        portfolio: PortfolioReader,
        res: AnalysisResult,
        side: Side,
        signals: dict[str, object],
    ) -> SectionOutput | None:
        if not self.config.block_opposite_side:
            return None
        opposite: Side = "no" if side == "yes" else "yes"
        # A dust remainder (under one share — not a placeable order, it just
        # waits for settlement) is not a position anyone holds against: it
        # used to block every opposite-side entry on the market until it
        # resolved.
        if any(
            h.market_id == res.market_id and h.side == opposite and not is_dust_qty(h.qty)
            for h in portfolio.get_open_positions()
        ):
            return SectionOutput(
                payload=None,
                verdict="skip",
                reason="opposite_side_open",
                signals=dict(signals),
            )
        return None

    def _price_gates(
        self, edge: float, spread: float, held_price: float, signals: dict[str, object]
    ) -> SectionOutput | None:
        spread_pct = spread / held_price
        signals["spread_pct"] = round(spread_pct, 4)
        skip = super()._price_gates(edge, spread, held_price, signals)
        if skip is not None:
            return skip
        if spread_pct > self.config.max_spread_pct:
            return SectionOutput(
                payload=None,
                verdict="skip",
                reason="spread above max_spread_pct",
                signals=signals,
            )
        return None

    @staticmethod
    def CONTRACT_TEST() -> None:
        inst = EdgeThresholdEntryV1(EdgeThresholdConfigV1())
        res0 = AnalysisResult(market_id="__contract_test__", p_model=0.6, confidence="medium")
        wrapped = inst.run(SectionInput(tick_type="event", payload=FreshBooks(res0, frozenset())))
        assert wrapped.verdict == "skip"

        out_none = inst.run(SectionInput(tick_type="event", payload=None))
        assert out_none.verdict == "skip"

        # A synthetic market_id is never in the live catalog, so this skips at
        # the market lookup — registry scan stays light, touches no DB.
        res = AnalysisResult(market_id="__contract_test__", p_model=0.6, confidence="medium")
        out_no_market = inst.run(SectionInput(tick_type="event", payload=res))
        assert out_no_market.verdict == "skip"


def _skip_reason(skipped: dict[Side, str]) -> str:
    """One skip reason for "no side could be priced": the shared reason when
    every side failed the same way, else each side's own (``yes: …; no: …``)."""
    reasons = set(skipped.values())
    if len(reasons) == 1:
        return reasons.pop()
    return "; ".join(f"{side}: {reason}" for side, reason in skipped.items())

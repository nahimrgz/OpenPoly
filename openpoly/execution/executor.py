"""Execution — the level-1 paper fill service.

A fixed system service (not a pluggable section): it turns an entry
``OrderIntent`` or an exit close decision into an actual fill, recorded through
``PortfolioStore``. The fill model is deliberately crude. A BUY takes the
level-1 ask, capped by that level's depth. A SELL from the exit monitor sweeps
bid levels down to the price its decision was made on (``floor_price``) and
books their volume-weighted price — a stop marked at a depth-guarded bid below
a thin level 1 would otherwise sell a sliver at level 1 and leave the rest
open for another tick. A SELL without a floor (manual close) takes level 1
only. No slippage model beyond that, no fees (zero-fee rule).

Paper is the simulation of live, so the two must not disagree about *whether*
an order is fillable or *how much* of it fills: the price band, order size,
and the minimum notional all come from ``openpoly.execution.sizing``, the same
module the live executor sizes through, and a partial sell leaves the
remainder open exactly as the live path does. What stays paper-only is the
price model (level-1, no venue round-trip).

Entry and exit share this one executor so their accounting is symmetric. It
reads the live ``MarketStore`` singleton directly (same pattern as the
embedding section — no capability injection). Construction touches no DB; the
``PortfolioStore`` is injected by the FastAPI lifespan once the database is up.
"""

from __future__ import annotations

import logging

from openpoly.execution.sizing import (
    MIN_NOTIONAL_USD,
    dust_remainder_skip,
    in_price_band,
    quantize_size,
)
from openpoly.execution.types import ExecResult
from openpoly.markets.manager import manager as market_source_manager
from openpoly.portfolio import CloseReason, HeldPosition, PortfolioStore
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

logger = logging.getLogger(__name__)


class PaperExecutor:
    """Level-1 paper fill service. Routed to by ExecutorDispatcher when
    ``runtime_state.exec_mode == "paper"`` (default)."""

    def __init__(self, portfolio: PortfolioStore | None = None) -> None:
        self._portfolio = portfolio

    def configure(self, portfolio: PortfolioStore) -> None:
        """Inject the PortfolioStore. The FastAPI lifespan calls this once the
        database is up; tests pass a store to ``__init__`` directly."""
        self._portfolio = portfolio

    @property
    def _store(self) -> PortfolioStore:
        if self._portfolio is None:
            raise RuntimeError("Executor has no PortfolioStore — call configure() first")
        return self._portfolio

    def execute_buy(self, intent: OrderIntent, *, news_id: str | None, ts: float) -> ExecResult:
        """Open a position from an entry ``OrderIntent`` at the level-1 ask.

        Skips (nothing opened) when the market / order book / ask liquidity is
        missing, when a position for (market, side) is already open, when the
        ask is outside the venue's price band, or when the fill notional
        rounds to dust.
        """
        catalog = market_source_manager.store
        market = catalog.get(intent.market_id)
        if market is None:
            return ExecResult.skip("market_not_found")

        token_id = market.token_for(intent.side)
        if token_id is None:
            return ExecResult.skip("no_token")

        book = catalog.get_order_book(token_id)
        if book is None:
            return ExecResult.skip("no_order_book")
        if not book.asks:
            return ExecResult.skip("no_ask_liquidity")

        if self._store.get_open_position(intent.market_id, intent.side) is not None:
            return ExecResult.skip("position_exists")

        ask_price, ask_size = book.asks[0]
        if not in_price_band(ask_price):
            return ExecResult.skip("price_out_of_band")

        # Depth cap, then the venue's whole-share + min-notional rules — the
        # same gates the live executor applies before it signs an order.
        qty = quantize_size(min(intent.qty, ask_size), ask_price)
        if qty * ask_price < MIN_NOTIONAL_USD:
            return ExecResult.skip("dust")

        held = self._store.open_position(
            market_id=intent.market_id,
            side=intent.side,
            token_id=token_id,
            condition_id=market.condition_id,
            price=ask_price,
            qty=qty,
            ts=ts,
            news_id=news_id,
            entry_p_model=intent.p_model,
            entry_confidence=intent.confidence,
            entry_edge=intent.edge,
        )
        logger.info(
            "buy filled: %s %s qty=%.4f @ %.4f (position %d)",
            intent.market_id,
            intent.side,
            qty,
            ask_price,
            held.position_id,
        )
        return ExecResult.ok(price=ask_price, qty=qty, position_id=held.position_id)

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason: CloseReason,
        ts: float,
        trigger: str | None = None,
        floor_price: float | None = None,
    ) -> ExecResult:
        """Close a held position into the bid side of its own token's book.

        Reads ``position.token_id`` directly, so a close never depends on the
        market still being in the live catalog.

        The sell sweeps bid levels best-first for the position's qty, down to
        ``floor_price`` — the mark the exit decision was made on — and books
        the volume-weighted price of what it took. Whatever the levels at or
        above the floor cannot absorb stays OPEN for the next tick, the same
        shape ``LiveExecutor`` records through ``record_sell``. Without a
        floor (manual closes) only the level-1 bid is taken, as before.
        Selling more than the book could hold, or below the price the
        decision saw, is a price paper could never have realized.

        A floor above the best bid (the book fell since the decision) sells
        level 1 — fail toward closing, as the live limit does.

        Skips when the token's book is gone (404 — the market resolved:
        ``market_closed_await_settlement``) or has stopped refreshing
        (``stale_order_book``, see ``KEPT_BOOK_MAX_AGE_SECONDS``), when the
        order book / bid liquidity is missing, when a level
        taken is outside the venue's price band, and when what is left is
        below one share — that remainder is not a placeable order but is still
        worth its resolution price, so it stays open (see
        ``dust_remainder_skip``).
        """
        catalog = market_source_manager.store
        if catalog.is_book_gone(position.token_id):
            # CLOB answered 404 for this token: the market resolved (or was
            # removed). There is nothing to sell into — the settlement
            # monitor closes the position at the resolution price.
            return ExecResult.skip("market_closed_await_settlement")
        book = catalog.get_order_book(position.token_id)
        if book is None:
            return ExecResult.skip("no_order_book")
        if not catalog.is_order_book_usable(position.token_id):
            # A held token's book is refreshed every ~10s; one this old has
            # stopped updating (a dead market's last quote). Filling at it
            # would book a phantom close at a price that no longer exists.
            return ExecResult.skip("stale_order_book")
        if not book.bids:
            return ExecResult.skip("no_bid_liquidity")

        best_bid = book.bids[0][0]
        # Never above the best bid: a book that moved below the decision's mark
        # since the decision still sells level 1 (fail toward closing), the
        # same price the live limit uses.
        floor = best_bid if floor_price is None else min(floor_price, best_bid)
        levels = [(p, s) for p, s in book.bids if p >= floor]
        if not all(in_price_band(p) for p, _ in levels):
            return ExecResult.skip("price_out_of_band")

        depth = sum(s for _, s in levels)
        qty = quantize_size(min(position.qty, depth), levels[-1][0])
        if qty <= 0:
            # Either the position itself is a sub-share remainder (not
            # placeable — leave it open for settlement) or the book's bid is:
            # hold and retry on the next tick.
            if quantize_size(position.qty, best_bid) <= 0:
                return dust_remainder_skip(position)
            return ExecResult.skip("bid_depth_below_min_size")
        price = _sweep_vwap(levels, qty)

        self._store.record_sell(
            position.position_id,
            sold_qty=qty,
            sell_price=price,
            ts=ts,
            close_reason=close_reason,
            trigger=trigger,
        )
        logger.info(
            "sell filled: %s %s qty=%.4f @ %.4f vwap over %d level(s) (position %d, %s)",
            position.market_id,
            position.side,
            qty,
            price,
            len(levels),
            position.position_id,
            close_reason,
        )
        return ExecResult.ok(
            price=price,
            qty=qty,
            position_id=position.position_id,
        )


def _sweep_vwap(levels: list[tuple[float, float]], qty: float) -> float:
    """Volume-weighted price of taking ``qty`` from ``levels`` best-first
    (``qty`` must not exceed their total size)."""
    remaining = qty
    notional = 0.0
    for price, size in levels:
        take = min(remaining, size)
        notional += take * price
        remaining -= take
        if remaining <= 0:
            break
    return notional / qty

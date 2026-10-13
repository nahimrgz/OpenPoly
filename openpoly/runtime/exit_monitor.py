"""Exit monitor — the position-driven, timer-driven close loop.

The news pipeline (orchestrator) is event-driven; closing a position is
position-driven + periodic. ``ExitMonitor`` runs a tick loop: every
``tick_interval_seconds`` it walks every open position, marks it with the held
side's current price (a depth-guarded bid from the held token's order book),
runs the ``exit`` section, and — when the section returns a ``CloseIntent`` —
routes it to ``executor.execute_sell``. Each evaluation is recorded in
``exit_log``.

It shares the one module-level ``executor`` with the orchestrator — entry buys
and exit sells go through the same fill path. The ``PortfolioStore`` is
injected by the FastAPI lifespan once the DB is up.

A position whose market has resolved drops out of the catalog → no order book →
the monitor logs a ``skip`` and leaves the position open. Settlement-close is a
separate concern, out of scope.

Marking is depth-guarded (v0.3.0). A resting level-1 bid can be a single
minimum-size probe order sitting far from fair value; marking there produced
false stop-outs. The mark is the first bid level carrying at least
``min_mark_bid_size`` shares (``openpoly.markets.pricing`` — the same rule
entry v1 gates its spread on); the ask passed along is the raw level-1 ask
(see ``_decide``). There is deliberately no mid mark: take-profit and peak-drawdown must be
evaluated against a price the position can realize. The sell sweeps bid
levels down to the mark the decision used (``floor_price``), so the fill
matches the decision rather than a level-1 sliver.

When no bid level qualifies, a v0 exit section never sees the position: it is
reported *blocked*, held, and logged once as ``no_executable_bid`` so the gap
is visible rather than silent. A section declaring ``EVALUATES_THIN_MARKS``
(threshold_v1) is still run on the raw best bid, where it evaluates only its
hard stop — closing into a thin book beats riding a collapse to zero. It fires
only once the ask confirms the collapse, or — with no ask — once it has
persisted for several ticks; the monitor keeps that streak per position
(``MarkedPosition.thin_streak``).

Held, not traded: a position whose token's CLOB book is gone (404 — the
market resolved) or has stopped refreshing is never closed off its last
quote. It is reported blocked and logged once as
``market_closed_await_settlement`` / ``stale_order_book``; the settlement
monitor closes it at 0/1.

A close that is decided but does not fill backs off per position —
exponentially from one tick to ``CLOSE_BACKOFF_MAX_SECONDS`` — and counts as
blocked meanwhile; the backoff resets on a fill or when the decision changes.
Every skip / error row is written once per (position, reason) while that
reason persists, so a failing close cannot flood the exit-log ring.

The tick's own work is sub-millisecond (DB read/write, in-memory book lookup,
the pure exit section) so it runs inline, but ``execute_sell`` is a live
network call that sleeps for seconds (CTF cache polling, close-persist
retries), so it is offloaded with ``asyncio.to_thread`` — same pattern the
orchestrator uses for its blocking section calls (docs/architecture/05).

Freshness: the market source re-fetches held tokens' books every ~10s
(``HELD_BOOK_INTERVAL_SECONDS``), and a close decided on a book older than
``MAX_BOOK_AGE_SECONDS`` is confirmed on a re-fetched book before it is acted
on (``_confirm_close``) — the one place a tick awaits network I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import OrderBookSnapshot
from openpoly.execution import ExecResult
from openpoly.execution import executor as _executor_singleton
from openpoly.execution.sizing import is_dust_qty
from openpoly.markets.manager import (
    HELD_BOOK_INTERVAL_SECONDS,
    MAX_BOOK_AGE_SECONDS,
    FreshBookSource,
)
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.pricing import MIN_LEVEL_SIZE, first_level_with_size
from openpoly.markets.store import KEPT_BOOK_MAX_AGE_SECONDS, MarketStore
from openpoly.portfolio import HeldPosition, PortfolioStore
from openpoly.runtime.closing_registry import clear_closing, is_closing, mark_closing
from openpoly.runtime.section_log import ExitDecision, exit_log
from openpoly.runtime.tick_loop import State, TickLoopMonitor
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.runtime.active_sections import build_section
from openpoly.sections.exit.threshold_v0 import CloseIntent, MarkedPosition

logger = logging.getLogger(__name__)

# The exit tick runs at the cadence the held tokens' books are refreshed
# (``HELD_BOOK_INTERVAL_SECONDS``): evaluating less often than the data changes
# only delays every stop. A tick is cheap — one DB read, in-memory book
# lookups and the pure exit section; the only network I/O is the rare
# confirm-before-close refetch.
DEFAULT_TICK_INTERVAL_SECONDS = int(HELD_BOOK_INTERVAL_SECONDS)

# Per-position backoff after a close that was decided but did not fill: the
# first retry waits one tick, each further failure doubles the wait, capped
# here. Without it a failing live sell (CTF cache polling sleeps ~4s per
# attempt) was retried — and logged — every 10s tick.
CLOSE_BACKOFF_MAX_SECONDS = 120.0

# How long ``stop()`` waits for an in-flight sell to finish recording itself.
# The live sell runs in a worker thread and cannot be cancelled, so the choice
# is between waiting for its bookkeeping and losing the record of a close that
# already happened on-chain. 30s covers the live executor's own retry budget.
INFLIGHT_DRAIN_TIMEOUT_SECONDS = 30.0

__all__ = ["ExitMonitor", "State", "exit_monitor"]


class _ExitSection(Protocol):
    """Minimal exit-section shape used by the monitor."""

    def run(self, input: SectionInput) -> SectionOutput: ...


class _Executor(Protocol):
    """Minimal executor shape used by the monitor."""

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason: str,
        ts: float,
        trigger: str | None,
        floor_price: float | None = None,
    ) -> ExecResult: ...


class ExitMonitor(TickLoopMonitor):
    """Timer-driven loop that closes open positions via the exit section.

    Lifecycle (start / stop / the loop itself) comes from ``TickLoopMonitor``.
    This monitor adds one thing to shutdown that the others do not need: a sell
    already handed to a worker thread cannot be cancelled, so ``_after_stop``
    waits for its bookkeeping (see ``_drain_inflight``).
    """

    LOG_NAME = "exit monitor"

    def __init__(
        self,
        *,
        exit_section: _ExitSection,
        executor: _Executor,
        tick_interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS,
        # Minimum resting size (in shares) for a bid level to be accepted as
        # the mark — the shared rule (see ``openpoly.markets.pricing``).
        min_mark_bid_size: float = MIN_LEVEL_SIZE,
        book_source: FreshBookSource | None = None,
    ) -> None:
        super().__init__(tick_interval_seconds=tick_interval_seconds)
        self._exit = exit_section
        self._executor = executor
        self._min_mark_bid_size = min_mark_bid_size
        # Confirm-before-close (see ``_confirm_close``); None disables it.
        self._book_source = book_source
        self._portfolio: PortfolioStore | None = None
        # The sell currently in flight, if any. ``execute_sell`` runs in a
        # worker thread and cannot be cancelled, so the sell + its bookkeeping
        # live in their own task: cancelling the tick loop must not strand a
        # close that already happened on-chain (see ``stop``).
        self._inflight: asyncio.Task[bool] | None = None
        # canvas-sync v2: atomic swap lock — same model as orchestrator's
        # _sections_lock. _tick_once reads self._exit; replace happens between
        # ticks (or between in-flight section.run calls within a tick — Python
        # GC keeps the old instance alive for any caller already holding it).
        self._exit_lock = asyncio.Lock()
        # Per-position runtime state (peak, hold / error dedupe, close backoff,
        # thin streak, dust marker — see ``_PositionState``). Pruned to the open
        # positions on every sweep, so it cannot grow across the process
        # lifetime.
        self._states: dict[int, _PositionState] = {}
        # token_id → position_ids, rebuilt each sweep. Lets ``observe_price``
        # update peaks from the book sampler without touching the DB on what is
        # a per-book hot path.
        self._watch: dict[str, list[int]] = {}
        # Tick telemetry (v18) — the "is the monitor working" heartbeat,
        # surfaced via /api/exit/log so the canvas badge / Closes tab can show
        # liveness without flooding exit_log with a skip entry per position per
        # tick. Within-threshold + no-order-book evaluations no longer write a
        # log entry at all (the ring keeps only the rare ok / error closes, so
        # they never get evicted); these counts carry that signal instead.
        self._last_tick_at: float | None = None
        self._last_tick_open: int = 0
        self._last_tick_blocked: int = 0

    @property
    def last_tick_at(self) -> float | None:
        """Wall-clock of the last completed sweep (None before the first)."""
        return self._last_tick_at

    @property
    def open_positions(self) -> int:
        """Open positions seen on the last sweep."""
        return self._last_tick_open

    @property
    def blocked(self) -> int:
        """Positions on the last sweep that could not be evaluated (no order
        book, or no book level deep enough to mark against — their stop-loss
        can't fire)."""
        return self._last_tick_blocked

    def configure(self, portfolio: PortfolioStore) -> None:
        """Inject the PortfolioStore — the FastAPI lifespan calls this once the
        DB is up. Construction itself touches no DB."""
        self._portfolio = portfolio

    def bootstrap_peaks(self, session_factory: sessionmaker[Session]) -> None:
        """Rebuild per-position peaks from persisted order-book snapshots.

        For each open position, scan ``order_book_snapshot`` rows where
        ``token_id == position.token_id AND recorded_at >= opened_at`` and take
        the max of the same depth-guarded mark the live tick uses — a dust bid
        recorded in a snapshot must not seed a peak the position can never live
        up to. Falls back to ``avg_entry_price`` when no snapshot exists yet.
        Called once at startup, before ``start()``.
        """
        if self._portfolio is None:
            return
        opens = self._portfolio.get_open_positions()
        if not opens:
            return
        with session_factory() as session:
            for held in opens:
                stmt = select(OrderBookSnapshot.bids_json).where(
                    OrderBookSnapshot.token_id == held.token_id,
                    OrderBookSnapshot.recorded_at >= held.opened_at,
                )
                peak = held.avg_entry_price
                for (bids_json,) in session.execute(stmt):
                    bids = _parse_levels(bids_json)
                    mark = first_level_with_size(bids, self._min_mark_bid_size)
                    if mark is not None and mark > peak:
                        peak = mark
                self._position_state(held.position_id).peak = peak
        loaded = sum(1 for state in self._states.values() if state.peak is not None)
        logger.info("exit monitor: bootstrap_peaks loaded %d positions", loaded)

    # ---------- lifecycle ----------

    async def _after_stop(self) -> None:
        """Cancelling the loop does not cancel a sell already handed to a worker
        thread: it completes on-chain and in the DB regardless. Wait for its
        task so the exit_log entry and the peak cleanup that follow the await
        actually run."""
        await self._drain_inflight()

    async def _drain_inflight(self) -> None:
        """Wait (bounded) for the in-flight sell task to finish."""
        task = self._inflight
        self._inflight = None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=INFLIGHT_DRAIN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error(
                "exit monitor: in-flight sell still running after %.0fs at shutdown — "
                "its close may not be recorded",
                INFLIGHT_DRAIN_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — shutdown must not raise on a failed sell
            logger.exception("exit monitor: in-flight sell failed during shutdown")

    # ---------- price observation ----------

    def _mark(self, book: OrderBook) -> float | None:
        """Depth-guarded mark for ``book`` — an *executable* price or None
        (``first_level_with_size``). The first bid level carrying
        ``min_mark_bid_size`` or more is where the position could actually be
        sold."""
        return first_level_with_size(book.bids, self._min_mark_bid_size)

    def _position_state(self, position_id: int) -> _PositionState:
        """The runtime state of ``position_id``, created on first use."""
        state = self._states.get(position_id)
        if state is None:
            state = self._states[position_id] = _PositionState()
        return state

    def observe_price(self, token_id: str, bid: float) -> None:
        """Push hook — record a fresh mark for ``token_id`` into the peaks.

        The exit tick runs every ``DEFAULT_TICK_INTERVAL_SECONDS``; books
        arriving between two ticks (the samplers, entry-time refreshes) still
        carry prices, and without this hook a run-up that happens and
        reverses between two ticks is invisible and the trailing lock trails
        a peak that never existed. Only tokens held by a
        position seen on the last sweep are tracked, so this stays a dict
        lookup — a position opened between sweeps starts being observed after
        the next tick, which is harmless (its peak seeds at that tick's mark).
        """
        for position_id in self._watch.get(token_id, ()):
            state = self._position_state(position_id)
            if state.peak is None or bid > state.peak:
                state.peak = bid

    def _unwatch(self, token_id: str, position_id: int) -> None:
        """Stop observing one position's token (called when it closes)."""
        watchers = self._watch.get(token_id)
        if watchers is None:
            return
        if position_id in watchers:
            watchers.remove(position_id)
        if not watchers:
            self._watch.pop(token_id, None)

    def observe_book(self, book: OrderBook) -> None:
        """``observe_price`` adapter for order-book deliveries.

        Wired to the market-source book sampler by the FastAPI lifespan. It
        applies the same depth guard as the tick so peaks and marks come from
        one price series. Note the limitation: the runtime has no push/WS book
        feed, so "fresh" here means the sampler's poll interval (60s by
        default) rather than every quote update.
        """
        if book.token_id not in self._watch:
            return
        mark = self._mark(book)
        if mark is not None:
            self.observe_price(book.token_id, mark)

    # ---------- tick ----------

    async def _tick_once(self) -> None:
        """One sweep — evaluate every open position. Records tick telemetry
        (open / blocked counts + timestamp); within-threshold + unmarkable
        holds no longer write a log entry — only ok / error closes land in
        exit_log."""
        if self._portfolio is None:
            return
        ts = time.time()
        catalog = market_source_manager.store
        opens = await asyncio.to_thread(self._portfolio.get_open_positions)
        watch: dict[str, list[int]] = {}
        for held in opens:
            watch.setdefault(held.token_id, []).append(held.position_id)
        self._watch = watch
        # Drop the state of anything no longer open, so it cannot grow across
        # the process lifetime. The peak in particular: ``_close`` drops a peak
        # only for the positions *this* monitor closed, and the settlement and
        # reconciliation monitors close positions behind its back — every one
        # of those left an entry here forever.
        open_ids = {held.position_id for held in opens}
        self._states = {pid: st for pid, st in self._states.items() if pid in open_ids}
        blocked = 0
        for held in opens:
            try:
                if await self._evaluate(held, catalog, ts):
                    blocked += 1
            except Exception as exc:  # noqa: BLE001 — one bad position must not abort the sweep
                logger.exception("exit monitor: position %d failed", held.position_id)
                self._log(held, ts, verdict="error", error=repr(exc)[:200])
        self._last_tick_at = ts
        self._last_tick_open = len(opens)
        self._last_tick_blocked = blocked

    async def _evaluate(self, held: HeldPosition, catalog: MarketStore, ts: float) -> bool:
        """Evaluate one position. Returns True when it could not be evaluated
        or closed (no order book, no level deep enough to mark against, a dead
        / stale book held for settlement, or a decided close that did not
        fill or is backing off — counted as ``blocked``); False when held
        within thresholds, dust, or closed. ok / error closes and holds are
        logged once per (position, reason); within-threshold evaluations are
        not (see tick telemetry)."""
        if is_dust_qty(held.qty):
            # A remainder below one share cannot be sold at all (see
            # execution.sizing): the row stays open until settlement closes it
            # at the resolution price. Evaluating it anyway means a CloseIntent
            # every tick and an ``error`` row for a sell that was never
            # placeable — noise that evicts the real closes from the log ring.
            # Not blocked either: nothing is wrong with the book, there is
            # simply nothing to do. Logged once per position
            # (``_PositionState.dust``).
            state = self._position_state(held.position_id)
            if not state.dust:
                state.dust = True
                self._log(held, ts, verdict="skip", reason="dust_remainder")
            return False
        book = catalog.get_order_book(held.token_id)
        if book is None:
            if catalog.is_book_gone(held.token_id):
                # CLOB has no book for the token any more: the market
                # resolved. Hold for the settlement monitor.
                self._hold_once(held, ts, "market_closed_await_settlement")
            return True
        if not book.bids:
            return True
        decision = self._decide(held, book)
        intent = _close_intent(decision.out) if decision is not None else None
        if decision is not None and intent is not None:
            if self._backing_off(held.position_id, intent.trigger):
                # A close for this very decision failed recently: wait out the
                # backoff instead of retrying (and re-confirming) every tick.
                return True
            confirmed = await self._confirm_close(held, decision, catalog)
            if isinstance(confirmed, str):
                # Refetch failed on a market that is gone / looks closed, or
                # whose book stopped refreshing: a fill at the stale bid would
                # be a phantom close. Hold (logged once per reason).
                self._hold_once(held, ts, confirmed)
                return True
            decision = confirmed
            intent = _close_intent(decision.out)
        self._track_thin_streak(held.position_id, decision)
        if decision is None or (decision.thin and intent is None):
            # The book has bids but none deep enough to sell into: the position
            # is unevaluable, not cheap (a thin-mark section still got to fire
            # its hard stop above). Log the first tick of each occurrence (see
            # ``_hold_once``) so it doesn't fail silently.
            self._hold_once(held, ts, "no_executable_bid")
            return True
        self._clear_holds(held.position_id)
        out, peak_price = decision.out, decision.peak_price
        return_pct = out.signals.get("return_pct")
        if intent is None:
            # Held within thresholds — no close, no log entry (peak already
            # tracked above; tick telemetry records that this position was
            # evaluated). The decision changed, so any close backoff is over.
            self._clear_backoff(held.position_id)
            return False

        # The sweep's open-position list was read once, at the top of the tick,
        # and every sell since then handed the loop back for seconds. In that
        # window the settlement or reconciliation monitor can have closed *this*
        # position. Selling it from the stale snapshot means an execute_sell
        # against an already-closed row: a spurious ``error`` entry on paper, a
        # real on-chain sell that can never be persisted on live. Re-read the
        # row and skip it instead. There is deliberately no ``await`` between
        # this check and the claim below, so nothing can close it in between.
        if not self._still_open(held.position_id):
            self._log(held, ts, verdict="skip", reason="position_no_longer_open")
            return False
        if is_closing(held.position_id):
            # A manual close route already holds the in-flight claim for this
            # id (the routes claim before their first await, exactly as we do
            # below) — selling it too is the double-sell the registry exists
            # to prevent. Reconsider on the next tick.
            self._log(held, ts, verdict="skip", reason="exit_in_flight")
            return False
        # Claim the position before yielding the loop: from here until the sell
        # has been persisted, the settlement and reconciliation monitors must
        # not close this id underneath us (see closing_registry).
        mark_closing(held.position_id)
        task = asyncio.create_task(
            self._close(held, intent.trigger, ts, return_pct, peak_price, floor_price=intent.price)
        )
        self._inflight = task
        try:
            # Shielded: if the tick loop is cancelled mid-sell, this await is
            # cancelled but the task keeps running and stop() drains it. The
            # sell itself is already uncancellable — only its bookkeeping was
            # at risk.
            filled = await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight = None
        # A close that was decided but did not fill leaves a position that
        # should be closed still open: it is blocked, not "evaluated".
        return not filled

    # ---------- hold logging / backoff ----------

    def _log_once(
        self,
        held: HeldPosition,
        ts: float,
        slot: Literal["hold_reason", "error_key"],
        key: str,
        **fields: Any,
    ) -> None:
        """Write the exit-log row ``fields`` unless ``key`` is already the
        logged value of this position's dedupe ``slot``. A slot holds one key:
        logging a different one replaces it, so returning to a key logs again.

        A position that cannot be evaluated or closed has to be visible in
        exit_log, but a row per position per tick would evict the ok / error
        closes from the ring: one row per occurrence and key."""
        state = self._position_state(held.position_id)
        if getattr(state, slot) == key:
            return
        setattr(state, slot, key)
        self._log(held, ts, **fields)

    def _hold_once(self, held: HeldPosition, ts: float, reason: str) -> None:
        """Log a ``skip`` row for ``reason`` once while it persists for this
        position (``no_executable_bid``, ``market_closed_await_settlement``,
        ``stale_order_book``); any other held reason for it is forgotten, so
        returning to it logs again."""
        self._log_once(held, ts, "hold_reason", reason, verdict="skip", reason=reason)

    def _clear_holds(self, position_id: int) -> None:
        """Forget the logged hold reason of ``position_id`` (it became
        evaluable), so the next hold logs again."""
        state = self._states.get(position_id)
        if state is not None:
            state.hold_reason = None

    def _clear_backoff(self, position_id: int) -> None:
        """Drop the close backoff of ``position_id`` and forget its logged
        close failure — the close filled, or the decision changed."""
        state = self._states.get(position_id)
        if state is not None:
            state.backoff = None
            state.error_key = None

    def _backing_off(self, position_id: int, trigger: str) -> bool:
        """Whether a close of ``position_id`` for ``trigger`` is still inside
        its failure backoff. A different trigger is a changed decision: the
        backoff is dropped and the close is attempted."""
        state = self._states.get(position_id)
        backoff = state.backoff if state is not None else None
        if backoff is None:
            return False
        if backoff.trigger != trigger:
            self._clear_backoff(position_id)
            return False
        return time.monotonic() < backoff.until

    def _record_close_failure(self, position_id: int, trigger: str) -> None:
        """Start (or double) the backoff of a close that did not fill."""
        state = self._position_state(position_id)
        prev = state.backoff
        if prev is not None and prev.trigger == trigger:
            delay = min(prev.delay * 2.0, CLOSE_BACKOFF_MAX_SECONDS)
        else:
            delay = min(float(self._tick_interval), CLOSE_BACKOFF_MAX_SECONDS)
        state.backoff = _Backoff(trigger=trigger, delay=delay, until=time.monotonic() + delay)

    def _track_thin_streak(self, position_id: int, decision: _Decision | None) -> None:
        """Count consecutive ticks on which a thin hard stop was pending
        confirmation; any other outcome resets the streak."""
        if (
            decision is not None
            and decision.thin
            and decision.out.signals.get("thin_stop_pending") is True
        ):
            self._position_state(position_id).thin_streak += 1
        else:
            state = self._states.get(position_id)
            if state is not None:
                state.thin_streak = 0

    def _decide(self, held: HeldPosition, book: OrderBook) -> _Decision | None:
        """Mark ``held`` against ``book`` and run the exit section on it.

        None when the book has no bid level deep enough to mark against —
        unless the section declares ``EVALUATES_THIN_MARKS``: then it is run
        on the raw best bid with ``thin=True`` (threshold_v1 evaluates only
        its hard stop there — closing into a thin book beats riding a
        collapse to zero) and the peak is left alone, a dust bid not being a
        price the position ever had."""
        current_price = self._mark(book)
        thin = False
        if current_price is None:
            if not book.bids or not getattr(self._exit, "EVALUATES_THIN_MARKS", False):
                return None
            current_price = book.bids[0][0]
            thin = True
        # The bid is depth-guarded (a dust probe far below the real bids would
        # inflate the spread floor until the trailing lock can never fire);
        # the ask is the raw level 1. Depth-guarding the ask only ever moved
        # it up or to None: that inflated threshold_v1's stop mid (suppressing
        # the normal stop) and an all-dust ask side removed the trailing spread
        # floor altogether. threshold_v1 caps the spread floor, which already
        # bounds what a dust ask can do to the trailing distance.
        ask = book.asks[0][0] if book.asks else None
        spread = ask - current_price if ask is not None else None
        # Monotone-increasing per-position peak. New open positions seed at
        # current_price; bootstrap_peaks / observe_price may have seeded a
        # higher one already.
        state = self._position_state(held.position_id)
        prev_peak = state.peak if state.peak is not None else current_price
        peak_price = prev_peak if thin else max(prev_peak, current_price)
        if not thin:
            state.peak = peak_price

        marked = MarkedPosition(
            market_id=held.market_id,
            side=held.side,
            avg_entry_price=held.avg_entry_price,
            qty=held.qty,
            current_price=current_price,
            peak_price=peak_price,
            spread=spread,
            ask=ask,
            # Polymarket exposes no per-market tick size through Gamma or the
            # book endpoint, so the section falls back to its configured tick.
            tick_size=None,
            thin=thin,
            thin_streak=state.thin_streak if thin else 0,
        )
        out = self._exit.run(SectionInput(tick_type="hard", payload=marked))
        return _Decision(out=out, peak_price=peak_price, thin=thin)

    async def _confirm_close(
        self, held: HeldPosition, decision: _Decision, catalog: MarketStore
    ) -> _Decision | str:
        """Confirm a close decided on a stale book before acting on it.

        The cached book can be up to a sample interval old; a stop fired off
        it may be firing on a price long gone. When the book is older than
        ``MAX_BOOK_AGE_SECONDS`` it is re-fetched (``ensure_fresh``), the
        position re-marked and the exit section re-run on the fresh mark —
        the returned decision is the one to act on, which may no longer close.
        A refresh that fails keeps the original decision (with a warning): a
        stop must not be blocked by the venue being slow — fail toward
        closing, not toward holding. The refreshed book stays in the shared
        store, so the sell fill that follows reads it (or a newer one).

        The exceptions are a failed refresh on a market whose book is gone
        (CLOB 404) or that the catalog shows as closed / not accepting orders
        — its cached book is a corpse, and a fill at its bid would book a
        phantom close (``"market_closed_await_settlement"``) — and one whose
        cached book has stopped refreshing altogether (older than
        ``KEPT_BOOK_MAX_AGE_SECONDS``: ``"stale_order_book"``). The hold
        reason is returned then: hold, and let the settlement monitor close
        the position at the resolution price.
        """
        source = self._book_source
        if source is None:
            return decision
        token = held.token_id
        age = source.store.order_book_age(token)
        if age is not None and age <= MAX_BOOK_AGE_SECONDS:
            return decision
        fresh_age = (await source.ensure_fresh([token], MAX_BOOK_AGE_SECONDS)).get(token)
        book = source.store.get_order_book(token)
        if fresh_age is None or fresh_age > MAX_BOOK_AGE_SECONDS or book is None:
            market = catalog.get(held.market_id)
            # Only explicit evidence counts: a 404 from CLOB, or "closed" from
            # the venue. A market merely missing from the catalog (startup, a
            # failed holding sync) is not evidence of resolution, and holding
            # there would block a real stop.
            if source.store.is_book_gone(token) or (
                market is not None and (market.closed or not market.accepting_orders)
            ):
                logger.warning(
                    "exit monitor: position %d — book refresh failed and market %s looks "
                    "closed; holding for settlement",
                    held.position_id,
                    held.market_id,
                )
                return "market_closed_await_settlement"
            if fresh_age is not None and fresh_age > KEPT_BOOK_MAX_AGE_SECONDS:
                logger.warning(
                    "exit monitor: position %d — the %.0fs-old book could not be "
                    "refreshed; not closing at a quote that stopped updating",
                    held.position_id,
                    fresh_age,
                )
                return "stale_order_book"
            logger.warning(
                "exit monitor: position %d — could not refresh a %s book before "
                "closing; acting on it",
                held.position_id,
                "missing" if age is None else f"{age:.0f}s-old",
            )
            return decision
        confirmed = self._decide(held, book) if book.bids else None
        if confirmed is None:
            # The fresh book cannot be marked; the stale decision stands (the
            # sell itself decides whether there is anything to sell into).
            return decision
        if _close_intent(confirmed.out) is None:
            logger.info(
                "exit monitor: position %d — close on a %s book not confirmed by a fresh one",
                held.position_id,
                "missing" if age is None else f"{age:.0f}s-old",
            )
        return confirmed

    def _still_open(self, position_id: int) -> bool:
        """Fresh read of one position's status — synchronous by design, so the
        caller can claim the position without yielding the loop in between."""
        portfolio = self._portfolio
        if portfolio is None:
            return False
        record = portfolio.get_position(position_id)
        return record is not None and record.status == "open"

    async def _close(
        self,
        held: HeldPosition,
        trigger: str,
        ts: float,
        return_pct: float | None,
        peak_price: float,
        *,
        floor_price: float | None = None,
    ) -> bool:
        """Sell one position and record the outcome; returns whether it filled.

        Runs as its own task so a cancelled tick loop cannot strand the
        bookkeeping; clears the in-flight claim on every exit path. A fill
        resets the position's close backoff; a failure starts / doubles it and
        logs its ``error`` row once per (position, reason).
        """
        try:
            # execute_sell blocks for seconds on the live path — keep the event
            # loop free (same offload the orchestrator does for its sections).
            # ``floor_price`` is the mark the decision was made on: the sell
            # may sweep bid levels down to it, but never below the price the
            # trigger was evaluated at.
            result = await asyncio.to_thread(
                self._executor.execute_sell,
                held,
                close_reason=trigger,
                ts=ts,
                trigger=trigger,
                floor_price=floor_price,
            )
        finally:
            clear_closing(held.position_id)
        if result.filled and result.price is not None:
            self._clear_backoff(held.position_id)
            # A sell is not necessarily the whole position: a crossing order
            # takes whatever depth was resting and the executor cancels the
            # rest rather than leaving it on the book; ``record_sell`` reduces
            # ``qty`` and leaves the row open when the residual is still
            # sellable. Realizing against ``held.qty`` therefore booked the
            # gain on shares that were never sold.
            sold_qty = result.qty if result.qty is not None else held.qty
            realized = (result.price - held.avg_entry_price) * sold_qty
            # "Still open" is the authoritative test for a partial, not
            # ``sold_qty < held.qty``: a sell leaving a sub-0.01 residue is a
            # *full* close (see ``PortfolioStore.record_sell``), and treating
            # it as partial would strand a peak for a closed position. The
            # cheap comparison guards the DB read for the common full fill.
            partial = sold_qty < held.qty and self._still_open(held.position_id)
            if not partial:
                # Position is closed; drop its peak so a future re-entry on the
                # same position_id (shouldn't happen, but be safe) starts fresh,
                # and stop observing its token so a book delivered before the
                # next sweep cannot resurrect the entry.
                state = self._states.get(held.position_id)
                if state is not None:
                    state.peak = None
                self._unwatch(held.token_id, held.position_id)
            # Partial: the remainder is still an open position with a trailing
            # stop, so its peak and its book subscription are kept — resetting
            # them would re-seed the stop at the next tick's mark and throw
            # away the run-up the position has already had.
            self._log(
                held,
                ts,
                verdict="ok",
                trigger=trigger,
                return_pct=return_pct,
                peak_price=peak_price,
                fill_price=result.price,
                realized_pnl=realized,
                reason=trigger,
            )
            return True
        # The section decided to close but the fill did not land — a position
        # that should be closed is still open: surface as error, once per
        # (position, reason) while it persists, and back off the retries.
        self._record_close_failure(held.position_id, trigger)
        error = f"sell not filled: {result.skip_reason}"
        # Kept apart from the holds (``error_key``): a retried close of the
        # same decision must not log again just because the position was
        # evaluable in between.
        self._log_once(
            held,
            ts,
            "error_key",
            error,
            verdict="error",
            trigger=trigger,
            return_pct=return_pct,
            peak_price=peak_price,
            error=error,
        )
        return False

    # ---------- canvas-sync v2 ----------

    async def replace_exit_section(self, new_section: _ExitSection) -> None:
        """Hot-swap the exit section without restarting the monitor.

        Caller (``api/canvas_routes._apply_canvas_reload``) builds the new
        instance from the latest canvas, then awaits this. Same atomicity story
        as the orchestrator: an in-flight ``self._exit.run(...)`` keeps a
        reference to the old instance via Python GC, and the next tick reads
        ``self._exit`` and gets the new one.
        """
        async with self._exit_lock:
            self._exit = new_section

    def _log(
        self,
        held: HeldPosition,
        ts: float,
        *,
        verdict: str,
        trigger: str | None = None,
        return_pct: float | None = None,
        peak_price: float | None = None,
        fill_price: float | None = None,
        realized_pnl: float | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        exit_log.append(
            ExitDecision(
                ts=ts,
                position_id=held.position_id,
                market_id=held.market_id,
                side=held.side,
                verdict=verdict,  # type: ignore[arg-type]
                trigger=trigger,
                return_pct=return_pct,
                peak_price=peak_price,
                fill_price=fill_price,
                realized_pnl=realized_pnl,
                reason=reason,
                error=error,
            )
        )


@dataclass
class _PositionState:
    """Runtime state the monitor keeps for one open position."""

    # Peak of the held side's mark across this process's lifetime. Rebuilt at
    # startup by ``bootstrap_peaks`` from the order_book_snapshot table;
    # updated every tick and on every observed book (see ``observe_price``);
    # dropped on close. Process-restart loses anything not in that table —
    # accepted trade-off for keeping runtime state out of the database schema.
    peak: float | None = None
    # The hold reason already logged (``_hold_once``); dropped as soon as that
    # reason stops holding (the position becomes markable, or is held for
    # another reason), so a book that goes thin twice is logged twice.
    hold_reason: str | None = None
    # The close failure already logged — kept apart from the hold: a retried
    # close of the same decision must not log again just because the position
    # was evaluable in between. Cleared on a fill, or when the decision stops
    # being a close.
    error_key: str | None = None
    # Close backoff (see ``CLOSE_BACKOFF_MAX_SECONDS``).
    backoff: _Backoff | None = None
    # Consecutive ticks on which a thin-book hard stop was pending
    # confirmation (``MarkedPosition.thin_streak``).
    thin_streak: int = 0
    # Already logged as ``dust_remainder``. A sub-one-share remainder is not a
    # placeable order, so evaluating it produced a CloseIntent → a sell the
    # executor can only skip → an ``error`` row, every tick, for as long as the
    # market stayed unresolved (~720 rows/day into a 200-entry ring). One row
    # per position.
    dust: bool = False


class _Backoff(NamedTuple):
    """A failed close's retry backoff for one position."""

    trigger: str  # the decision that failed — a different one resets the backoff
    delay: float  # seconds of the current backoff step
    until: float  # time.monotonic() before which the close is not retried


class _Decision(NamedTuple):
    """One exit-section evaluation and the peak it was marked with."""

    out: SectionOutput
    peak_price: float
    thin: bool = False  # marked on the raw best bid (no executable level)


def _close_intent(out: SectionOutput) -> CloseIntent | None:
    """The CloseIntent of an ok exit decision, else None."""
    if out.verdict == "ok" and isinstance(out.payload, CloseIntent):
        return out.payload
    return None


def _parse_levels(raw: str | None) -> list[tuple[float, float]]:
    """Parse a persisted ``bids_json`` / ``asks_json`` ladder. Malformed rows
    yield an empty ladder rather than aborting the bootstrap scan."""
    try:
        levels = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []
    if not isinstance(levels, list):
        return []
    out: list[tuple[float, float]] = []
    for level in levels:
        try:
            out.append((float(level[0]), float(level[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return out


# Module-level singleton — the FastAPI lifespan injects its PortfolioStore via
# configure() and start()s it. Shares the one executor with the orchestrator.
def _build_exit_monitor() -> ExitMonitor:
    """The process exit monitor: the active exit impl with the persisted
    canvas exit node's config (defaults when absent / invalid), so a restart
    keeps the operator's thresholds; a later canvas edit swaps in via
    ``replace_exit_section``."""
    return ExitMonitor(
        exit_section=build_section("exit"),
        executor=_executor_singleton,
        book_source=market_source_manager,
    )


exit_monitor = _build_exit_monitor()

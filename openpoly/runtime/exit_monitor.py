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
``min_mark_bid_size`` shares — and nothing else. There is deliberately no mid
fallback: the executors sell into the book's raw level-1 bid, so a mid mark
would evaluate take-profit and peak-drawdown against a price the position can
never realize, closing a "winner" into a dust bid at a loss. When no bid level
qualifies the position is reported *blocked*, held, and logged once as
``no_executable_bid`` so the gap is visible rather than silent.

The tick's own work is sub-millisecond (DB read/write, in-memory book lookup,
the pure exit section) so it runs inline, but ``execute_sell`` is a live
network call that sleeps for seconds (CTF cache polling, close-persist
retries), so it is offloaded with ``asyncio.to_thread`` — same pattern the
orchestrator uses for its blocking section calls (docs/architecture/05).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import OrderBookSnapshot
from openpoly.execution import ExecResult
from openpoly.execution import executor as _executor_singleton
from openpoly.execution.sizing import is_dust_qty
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import MarketStore
from openpoly.portfolio import HeldPosition, PortfolioStore
from openpoly.runtime.closing_registry import clear_closing, mark_closing
from openpoly.runtime.section_log import ExitDecision, exit_log
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.sections.exit.threshold_v0 import (
    CloseIntent,
    MarkedPosition,
    ThresholdExitConfig,
    ThresholdExitV0,
)

logger = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_SECONDS = 120  # v8 §10.1 "hard" tick

# Minimum resting size (in shares) for a bid level to be accepted as the mark.
# Polymarket's minimum order notional is $1, which at the 0.20–0.80 prices this
# system trades is 1.25–5 shares — so any level below ~5 shares can be a single
# minimum-size probe or dust order rather than a price anyone is committed to.
# Requiring 5 shares means the mark is always backed by at least one order
# larger than the venue minimum, which is what makes a stop-loss fired off it
# an executable price rather than an artifact.
DEFAULT_MIN_MARK_BID_SIZE = 5.0

# How long ``stop()`` waits for an in-flight sell to finish recording itself.
# The live sell runs in a worker thread and cannot be cancelled, so the choice
# is between waiting for its bookkeeping and losing the record of a close that
# already happened on-chain. 30s covers the live executor's own retry budget.
INFLIGHT_DRAIN_TIMEOUT_SECONDS = 30.0

State = Literal["stopped", "running"]


def _mark_from_levels(
    bids: list[tuple[float, float]],
    min_bid_size: float,
) -> float | None:
    """Depth-guarded mark for one book — an *executable* price or nothing.

    The first bid level carrying ``min_bid_size`` or more is where the position
    could actually be sold, so that is the mark. Returns ``None`` when no bid
    level qualifies: the book has no real bid side, and every alternative (the
    mid, the dust bid) is a price the position cannot be sold at, which is
    exactly what makes a trigger fired off it a fabricated exit.
    """
    for price, size in bids:
        if size >= min_bid_size:
            return price
    return None


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
    ) -> ExecResult: ...


class ExitMonitor:
    """Timer-driven loop that closes open positions via the exit section."""

    def __init__(
        self,
        *,
        exit_section: _ExitSection,
        executor: _Executor,
        tick_interval_seconds: int = DEFAULT_TICK_INTERVAL_SECONDS,
        min_mark_bid_size: float = DEFAULT_MIN_MARK_BID_SIZE,
    ) -> None:
        self._exit = exit_section
        self._executor = executor
        self._tick_interval = tick_interval_seconds
        self._min_mark_bid_size = min_mark_bid_size
        self._portfolio: PortfolioStore | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # The sell currently in flight, if any. ``execute_sell`` runs in a
        # worker thread and cannot be cancelled, so the sell + its bookkeeping
        # live in their own task: cancelling the tick loop must not strand a
        # close that already happened on-chain (see ``stop``).
        self._inflight: asyncio.Task[None] | None = None
        self._state: State = "stopped"
        # canvas-sync v2: atomic swap lock — same model as orchestrator's
        # _sections_lock. _tick_once reads self._exit; replace happens between
        # ticks (or between in-flight section.run calls within a tick — Python
        # GC keeps the old instance alive for any caller already holding it).
        self._exit_lock = asyncio.Lock()
        # Per-position peak of the held side's mark across this process's
        # lifetime. Rebuilt at startup by ``bootstrap_peaks`` from the
        # order_book_snapshot table; updated every tick and on every observed
        # book (see ``observe_price``); dropped on close. Process-restart loses
        # anything not in that table — accepted trade-off for keeping runtime
        # state out of the database schema.
        self._peak: dict[int, float] = {}
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
        # Positions already logged as ``no_executable_bid``. A position whose
        # book has no depth-qualified bid cannot be evaluated at all — its
        # stop-loss can't fire — so that has to be visible in exit_log, but a
        # row per position per tick would evict the ok / error closes from the
        # ring. One row per occurrence: the id is dropped again as soon as the
        # position becomes markable (or closes), so a book that goes thin twice
        # is logged twice.
        self._unmarkable: set[int] = set()
        # Positions already logged as ``dust_remainder`` — same dedup contract
        # as ``_unmarkable``. A sub-one-share remainder is not a placeable
        # order, so evaluating it produced a CloseIntent → a sell the executor
        # can only skip → an ``error`` row, every tick, for as long as the
        # market stayed unresolved (~720 rows/day into a 200-entry ring). One
        # row per position; the id is dropped again when it stops being open.
        self._dust: set[int] = set()

    @property
    def state(self) -> State:
        return self._state

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
                    mark = _mark_from_levels(bids, self._min_mark_bid_size)
                    if mark is not None and mark > peak:
                        peak = mark
                self._peak[held.position_id] = peak
        logger.info("exit monitor: bootstrap_peaks loaded %d positions", len(self._peak))

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._state = "running"
        # Recreate the Event each start so it binds to the *current* loop —
        # this module singleton may be start()ed across distinct loops (tests).
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._stop.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
        # Cancelling the loop does not cancel a sell already handed to a worker
        # thread: it completes on-chain and in the DB regardless. Wait for its
        # task so the exit_log entry and the peak cleanup that follow the await
        # actually run.
        await self._drain_inflight()
        self._state = "stopped"

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

    # ---------- loop ----------

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop must survive any tick error
                logger.exception("exit monitor: tick failed")
            # Cooperative yield, then sleep the interval — waking early on stop.
            await asyncio.sleep(0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_interval)

    # ---------- price observation ----------

    def _mark(self, book: OrderBook) -> float | None:
        """Depth-guarded mark for ``book`` (None when it cannot be marked)."""
        return _mark_from_levels(book.bids, self._min_mark_bid_size)

    def observe_price(self, token_id: str, bid: float) -> None:
        """Push hook — record a fresh mark for ``token_id`` into the peaks.

        The exit tick runs every ``DEFAULT_TICK_INTERVAL_SECONDS`` (120s) but
        the order-book sampler refreshes far more often; without this hook a
        run-up that happens and reverses between two ticks is invisible and the
        trailing lock trails a peak that never existed. Only tokens held by a
        position seen on the last sweep are tracked, so this stays a dict
        lookup — a position opened between sweeps starts being observed after
        the next tick, which is harmless (its peak seeds at that tick's mark).
        """
        for position_id in self._watch.get(token_id, ()):
            prev = self._peak.get(position_id)
            if prev is None or bid > prev:
                self._peak[position_id] = bid

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
        opens = self._portfolio.get_open_positions()
        watch: dict[str, list[int]] = {}
        for held in opens:
            watch.setdefault(held.token_id, []).append(held.position_id)
        self._watch = watch
        # Drop the unmarkable / dust markers for anything no longer open, so
        # neither set can grow across the process lifetime. The peak dict is
        # pruned on the same line and for a stronger reason: ``_close`` drops a
        # peak only for the positions *this* monitor closed, and the settlement
        # and reconciliation monitors close positions behind its back — every
        # one of those left an entry here forever.
        open_ids = {held.position_id for held in opens}
        self._unmarkable &= open_ids
        self._dust &= open_ids
        self._peak = {pid: peak for pid, peak in self._peak.items() if pid in open_ids}
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
        (no order book, or no level deep enough to mark against — counted as
        ``blocked``); False when held within thresholds, dust, or closed. ok /
        error closes are logged; within-threshold and unmarkable holds are not
        (see tick telemetry)."""
        if is_dust_qty(held.qty):
            # A remainder below one share cannot be sold at all (see
            # execution.sizing): the row stays open until settlement closes it
            # at the resolution price. Evaluating it anyway means a CloseIntent
            # every tick and an ``error`` row for a sell that was never
            # placeable — noise that evicts the real closes from the log ring.
            # Not blocked either: nothing is wrong with the book, there is
            # simply nothing to do. Logged once per position (see ``_dust``).
            if held.position_id not in self._dust:
                self._dust.add(held.position_id)
                self._log(held, ts, verdict="skip", reason="dust_remainder")
            return False
        book = catalog.get_order_book(held.token_id)
        if book is None or not book.bids:
            return True
        current_price = self._mark(book)
        if current_price is None:
            # The book has bids but none deep enough to sell into: the position
            # is unevaluable, not cheap. Log the first tick of each occurrence
            # (see ``_unmarkable``) so it doesn't fail silently.
            if held.position_id not in self._unmarkable:
                self._unmarkable.add(held.position_id)
                self._log(held, ts, verdict="skip", reason="no_executable_bid")
            return True
        self._unmarkable.discard(held.position_id)
        spread = book.asks[0][0] - book.bids[0][0] if book.asks else None
        # Monotone-increasing per-position peak. New open positions seed at
        # current_price; bootstrap_peaks / observe_price may have seeded a
        # higher one already.
        prev_peak = self._peak.get(held.position_id, current_price)
        peak_price = max(prev_peak, current_price)
        self._peak[held.position_id] = peak_price

        marked = MarkedPosition(
            market_id=held.market_id,
            side=held.side,
            avg_entry_price=held.avg_entry_price,
            qty=held.qty,
            current_price=current_price,
            peak_price=peak_price,
            spread=spread,
            # Polymarket exposes no per-market tick size through Gamma or the
            # book endpoint, so the section falls back to its configured tick.
            tick_size=None,
        )
        out = self._exit.run(SectionInput(tick_type="hard", payload=marked))
        return_pct = out.signals.get("return_pct")
        if out.verdict != "ok" or not isinstance(out.payload, CloseIntent):
            # Held within thresholds — no close, no log entry (peak already
            # tracked above; tick telemetry records that this position was
            # evaluated).
            return False

        intent = out.payload
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
        # Claim the position before yielding the loop: from here until the sell
        # has been persisted, the settlement and reconciliation monitors must
        # not close this id underneath us (see closing_registry).
        mark_closing(held.position_id)
        task = asyncio.create_task(self._close(held, intent.trigger, ts, return_pct, peak_price))
        self._inflight = task
        try:
            # Shielded: if the tick loop is cancelled mid-sell, this await is
            # cancelled but the task keeps running and stop() drains it. The
            # sell itself is already uncancellable — only its bookkeeping was
            # at risk.
            await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight = None
        return False

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
    ) -> None:
        """Sell one position and record the outcome.

        Runs as its own task so a cancelled tick loop cannot strand the
        bookkeeping; clears the in-flight claim on every exit path.
        """
        try:
            # execute_sell blocks for seconds on the live path — keep the event
            # loop free (same offload the orchestrator does for its sections).
            result = await asyncio.to_thread(
                self._executor.execute_sell,
                held,
                close_reason=trigger,
                ts=ts,
                trigger=trigger,
            )
        finally:
            clear_closing(held.position_id)
        if result.filled and result.price is not None:
            # A sell is not necessarily the whole position: an IOC order fills
            # against whatever depth was resting, and ``record_sell`` reduces
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
                self._peak.pop(held.position_id, None)
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
        else:
            # The section decided to close but the fill did not land — a
            # position that should be closed is still open: surface as error.
            self._log(
                held,
                ts,
                verdict="error",
                trigger=trigger,
                return_pct=return_pct,
                peak_price=peak_price,
                error=f"sell not filled: {result.skip_reason}",
            )

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
exit_monitor = ExitMonitor(
    exit_section=ThresholdExitV0(ThresholdExitConfig()),
    executor=_executor_singleton,
)


# canvas-sync v2: hot-swap the exit section without restarting the monitor.
# Caller (api/canvas_routes._apply_canvas_reload) builds the new instance from
# the latest canvas, then awaits this. Same atomicity story as orchestrator:
# in-flight ``self._exit.run(...)`` keeps a reference to the old instance via
# Python GC; the next tick reads ``self._exit`` and gets the new one.
async def _replace_exit_section_impl(self: ExitMonitor, new_section: _ExitSection) -> None:
    async with self._exit_lock:
        self._exit = new_section


ExitMonitor.replace_exit_section = _replace_exit_section_impl  # type: ignore[attr-defined]

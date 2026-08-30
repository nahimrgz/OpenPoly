"""Write-behind DB writer.

Hot paths (the news WS callback, the market poll / book-sample loops) must
never block on a DB round-trip. They ``enqueue`` rows synchronously into a
bounded queue; a single background task drains it in batches and persists each
batch off the event loop. The actual persist call is an injected ``sink``, so
the buffering logic is fully testable without a database.

Overflow drops the newest row rather than blocking a producer — the same
discipline as the pipeline orchestrator's queue. Both failure modes (an
overflow drop, a sink error) are *counted* and reported at WARNING rather than
swallowed: a dropped row is a lost order-book or news sample, and a failing
sink is a persistence outage, and neither used to leave any trace outside a
counter nobody read. The reporting is rate-limited to one message per failure
kind per ``WARN_INTERVAL_SECONDS``, carrying the cumulative count — a saturated
queue must not turn its own diagnosis into the flood.

``stop`` waits for the write already in flight instead of cancelling out from
under it: the worker thread behind ``asyncio.to_thread`` runs to completion
regardless, so cancelling only threw away the bookkeeping for a batch that did
get written.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_MAXSIZE = 5000
DEFAULT_BATCH_SIZE = 200

# One WARNING per failure kind per window, carrying the cumulative count.
WARN_INTERVAL_SECONDS = 60.0
# How long ``stop`` waits for the in-flight batch. The sink runs in a worker
# thread and cannot be cancelled, so the choice is between waiting for its
# bookkeeping and shutting down while it writes.
STOP_DRAIN_TIMEOUT_SECONDS = 10.0

# Persists one batch of rows. Sync — runs in a worker thread, off the loop.
Sink = Callable[[list[Any]], None]


class WriteBehindWriter:
    """Bounded queue + a single drain task.

    ``enqueue`` is sync and non-blocking. ``start`` launches the drain loop;
    ``stop`` drains the in-flight write, cancels the loop, and flushes whatever
    is still queued.
    """

    def __init__(
        self,
        sink: Sink,
        *,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._sink = sink
        self._batch_size = batch_size
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_maxsize)
        self._task: asyncio.Task[None] | None = None
        # The batch currently being written, if any. It lives in its own task
        # so ``stop`` can wait for it after cancelling the loop (see _write).
        self._inflight: asyncio.Task[None] | None = None
        self._dropped = 0
        self._written = 0
        self._errors = 0
        # kind -> monotonic timestamp of the last WARNING emitted for it.
        self._last_warn_at: dict[str, float] = {}

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def written(self) -> int:
        return self._written

    @property
    def errors(self) -> int:
        """Sink failures since start — each one is a batch that was lost."""
        return self._errors

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def enqueue(self, row: Any) -> bool:
        """Queue one row. Returns False (and counts a drop) when the queue is
        full — never blocks the caller."""
        try:
            self._queue.put_nowait(row)
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            self._warn(
                "drop",
                "write-behind queue full (maxsize %d): dropped %d rows since start",
                self._queue.maxsize,
                self._dropped,
            )
            return False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        """Cancel the drain loop, wait for the write already in flight, then
        flush whatever is still queued.

        Cancel comes first on purpose: the in-flight write is shielded, so
        cancelling stops the loop from starting *another* batch without
        touching the one already running. Draining before cancelling would
        leave exactly that window open — the loop is free to pick up a new
        batch while we wait — and the cancel would then land mid-write after
        all. The wait itself is what matters: the sink runs in a worker thread
        that completes regardless, so cancelling around it only threw away the
        record of a write that did happen."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._drain_inflight()
        await self._flush()

    # ---------- internals ----------

    def _warn(self, kind: str, message: str, *args: Any) -> None:
        """Emit at most one WARNING per ``kind`` per ``WARN_INTERVAL_SECONDS``.

        The counts in ``message`` are cumulative, so a suppressed burst is
        still fully accounted for by the next message that does get through.
        """
        now = time.monotonic()
        last = self._last_warn_at.get(kind)
        if last is not None and now - last < WARN_INTERVAL_SECONDS:
            return
        self._last_warn_at[kind] = now
        logger.warning(message, *args)

    async def _drain_inflight(self) -> None:
        """Wait (bounded) for the batch currently being persisted."""
        task = self._inflight
        if task is None or task.done():
            self._inflight = None
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=STOP_DRAIN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error(
                "write-behind: sink still running after %.0fs at shutdown — "
                "that batch may not be recorded",
                STOP_DRAIN_TIMEOUT_SECONDS,
            )
            return  # keep the reference so the task is not garbage-collected
        except Exception:  # noqa: BLE001 — _write already handles sink errors
            logger.exception("write-behind: in-flight batch failed during shutdown")
        self._inflight = None

    async def _drain_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._write_tracked(batch)

    async def _flush(self) -> None:
        """Drain everything still queued in one final pass."""
        batch: list[Any] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._write(batch)

    async def _write_tracked(self, batch: list[Any]) -> None:
        """Run one ``_write`` as a task ``stop`` can wait on.

        Shielded: cancelling the drain loop mid-write cancels this await, but
        the task (and the worker thread under it) keeps going, and ``stop``
        drains it.
        """
        task = asyncio.create_task(self._write(batch))
        self._inflight = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight = None

    async def _write(self, batch: list[Any]) -> None:
        """Persist a batch via the sink, off the event loop. Sink errors are
        counted and reported, never raised — a bad write must not kill the
        drain loop, but it must not be invisible either."""
        try:
            await asyncio.to_thread(self._sink, batch)
            self._written += len(batch)
        except Exception as exc:  # noqa: BLE001 — drain loop must survive sink errors
            self._errors += 1
            self._warn(
                "sink",
                "write-behind sink failed for %d rows (%d sink errors since start): %r",
                len(batch),
                self._errors,
                exc,
            )

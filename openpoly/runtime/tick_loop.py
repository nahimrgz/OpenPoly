"""Shared lifecycle for the three timer-driven runtime monitors.

``ExitMonitor``, ``SettlementMonitor`` and ``ReconciliationMonitor`` do very
different work, but they are the same *machine*: a background task that calls
``_tick_once`` every ``tick_interval_seconds``, survives any error that one tick
raises, and wakes early when asked to stop. That machine was written out three
times, and the copies had already drifted — only one of them re-raised
``CancelledError`` explicitly, only one of them handled being stopped before it
was ever started. Drift in a shutdown path is how a monitor ends up still
running after ``stop()`` returned.

The base class owns the parts that must not differ: the state flag, the stop
event, task creation and cancellation, and the loop's error containment.
Subclasses provide ``_tick_once`` and, where they need more shutdown than
cancelling the loop (the exit monitor has to drain a sell already handed to a
worker thread), override ``_after_stop``.

Two details are load-bearing and deliberately live here rather than in any
subclass:

* The stop ``Event`` is **recreated on every start**, not once in ``__init__``.
  These monitors are module-level singletons and an ``asyncio.Event`` binds to
  the loop it was first awaited on; a singleton started on a second loop (which
  is every test after the first) would otherwise wait on an event nothing can
  ever set.
* The loop yields with ``asyncio.sleep(0)`` before sleeping the interval. A tick
  that finds nothing to do can otherwise complete without a single await point,
  and a loop with no await points starves every other task on the event loop —
  the reconnect/poll starvation documented in
  ``docs/architecture/05-runtime-network-risk.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Literal

State = Literal["stopped", "running"]


class TickLoopMonitor:
    """Start / stop / tick-loop machinery for a periodic runtime monitor."""

    #: Prefix on the "tick failed" log line, e.g. ``"exit monitor"``.
    LOG_NAME = "monitor"

    def __init__(self, *, tick_interval_seconds: int) -> None:
        self._tick_interval = tick_interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state: State = "stopped"
        # The subclass's own module logger, so a tick failure is still
        # attributed to ``openpoly.runtime.exit_monitor`` rather than to this
        # shared base — log filtering and caplog assertions both depend on it.
        self._loop_logger = logging.getLogger(type(self).__module__)

    @property
    def state(self) -> State:
        return self._state

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """Start the tick loop. Idempotent while it is already running."""
        if self._task is not None and not self._task.done():
            return
        self._state = "running"
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        """Stop the tick loop and run any subclass shutdown. Safe when never
        started."""
        await self._cancel_loop()
        await self._after_stop()
        self._state = "stopped"

    async def _cancel_loop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _after_stop(self) -> None:
        """Hook for shutdown work the loop's cancellation does not cover.

        Runs after the loop is down and before the state flips to ``stopped``,
        on every ``stop()`` — including one where the monitor was never started,
        because work handed off to a thread outlives the task that started it.
        Default: nothing.
        """
        return None

    # ---------- loop ----------

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                # Shutdown, not a tick failure: it must reach the task, not the
                # containment below. (``CancelledError`` is a BaseException on
                # 3.8+, so this is documentation of intent rather than a fix —
                # and it stays correct if the containment is ever widened.)
                raise
            except Exception:  # noqa: BLE001 — the loop must survive any tick error
                self._loop_logger.exception("%s: tick failed", self.LOG_NAME)
            # Cooperative yield, then sleep the interval — waking early on stop.
            await asyncio.sleep(0)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_interval)

    async def _tick_once(self) -> None:
        """One sweep. Subclasses implement; errors are contained by the loop."""
        raise NotImplementedError

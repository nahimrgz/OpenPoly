"""Tests for openpoly.db.writer — the write-behind writer (no DB; fake sink)."""

from __future__ import annotations

import asyncio
import logging
import time

from openpoly.db.writer import WriteBehindWriter


async def _wait_until(predicate, *, iterations: int = 300) -> None:
    for _ in range(iterations):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_enqueue_and_drain():
    received: list = []
    writer = WriteBehindWriter(received.extend)
    await writer.start()
    for i in range(5):
        assert writer.enqueue(i) is True
    await _wait_until(lambda: writer.written == 5)
    assert sorted(received) == [0, 1, 2, 3, 4]
    await writer.stop()


async def test_overflow_drops_newest():
    writer = WriteBehindWriter(lambda batch: None, queue_maxsize=3)
    assert writer.enqueue("a") is True
    assert writer.enqueue("b") is True
    assert writer.enqueue("c") is True
    assert writer.enqueue("d") is False  # queue full
    assert writer.dropped == 1
    assert writer.pending == 3


async def test_stop_flushes_remaining():
    received: list = []
    writer = WriteBehindWriter(received.extend, batch_size=2)
    for i in range(6):
        writer.enqueue(i)
    await writer.start()
    await writer.stop()  # stop must flush whatever is still queued
    assert sorted(received) == [0, 1, 2, 3, 4, 5]


async def test_sink_error_does_not_kill_loop():
    received: list = []
    calls = {"n": 0}

    def flaky(batch: list) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        received.extend(batch)

    writer = WriteBehindWriter(flaky, batch_size=1)
    await writer.start()
    writer.enqueue("x")  # batch 1 -> sink raises, loop must survive
    await _wait_until(lambda: calls["n"] >= 1)
    writer.enqueue("y")  # batch 2 -> sink ok
    await _wait_until(lambda: received == ["y"])
    await writer.stop()


async def test_batching():
    batches: list[list] = []
    writer = WriteBehindWriter(lambda batch: batches.append(list(batch)), batch_size=10)
    for i in range(25):
        writer.enqueue(i)
    await writer.start()
    await _wait_until(lambda: writer.written == 25)
    await writer.stop()
    assert sum(len(b) for b in batches) == 25
    assert max(len(b) for b in batches) <= 10


async def test_stop_when_not_started_is_safe():
    writer = WriteBehindWriter(lambda batch: None)
    await writer.stop()  # never started -> no-op, must not raise


# ---------- observability: drops and sink errors must be countable + visible ----------


async def test_overflow_counts_and_warns_once(caplog) -> None:
    """A silently dropped row is a silently lost book / news sample. Drops are
    counted and surfaced at WARNING — rate-limited so a saturated queue cannot
    itself become the flood."""
    writer = WriteBehindWriter(lambda batch: None, queue_maxsize=2)
    with caplog.at_level(logging.WARNING, logger="openpoly.db.writer"):
        assert writer.enqueue("a") is True
        assert writer.enqueue("b") is True
        for _ in range(5):
            assert writer.enqueue("overflow") is False

    assert writer.dropped == 5
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "dropped" in warnings[0].getMessage()


async def test_sink_error_counts_and_warns_without_killing_the_loop(caplog) -> None:
    received: list = []
    calls = {"n": 0}

    def flaky(batch: list) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        received.extend(batch)

    writer = WriteBehindWriter(flaky, batch_size=1)
    with caplog.at_level(logging.WARNING, logger="openpoly.db.writer"):
        await writer.start()
        writer.enqueue("x")
        await _wait_until(lambda: writer.errors == 1)
        writer.enqueue("y")
        await _wait_until(lambda: received == ["y"])
        await writer.stop()

    assert writer.errors == 1
    assert writer.written == 1  # only the successful batch counted
    assert any("sink failed" in r.getMessage() for r in caplog.records)


async def test_warnings_are_rate_limited_then_resume(monkeypatch) -> None:
    """The throttle is a time window, not a one-shot mute: once the window has
    passed the next drop is reported again, with the cumulative count."""
    import openpoly.db.writer as writer_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(writer_mod.time, "monotonic", lambda: clock["t"])
    writer = WriteBehindWriter(lambda batch: None, queue_maxsize=1)
    writer.enqueue("a")

    logged: list[str] = []
    monkeypatch.setattr(
        writer_mod.logger,
        "warning",
        lambda msg, *args: logged.append(msg % args),
    )

    writer.enqueue("drop-1")
    writer.enqueue("drop-2")
    assert len(logged) == 1

    clock["t"] += writer_mod.WARN_INTERVAL_SECONDS + 1
    writer.enqueue("drop-3")
    assert len(logged) == 2
    assert "3" in logged[1]  # cumulative count, not a per-event message


async def test_stop_completes_the_in_flight_batch() -> None:
    """``stop`` used to cancel the drain task mid-``to_thread``: the batch the
    sink was already writing was never counted (and, for a slow sink, the
    process could exit under it). The in-flight write is drained first."""
    received: list = []

    def slow(batch: list) -> None:
        time.sleep(0.2)
        received.extend(batch)

    writer = WriteBehindWriter(slow, batch_size=10)
    await writer.start()
    writer.enqueue("row")
    await _wait_until(lambda: writer.pending == 0)  # the drain has taken it
    await writer.stop()

    assert received == ["row"]
    assert writer.written == 1


async def test_stop_drain_times_out_without_raising(monkeypatch) -> None:
    """A sink wedged forever must not wedge shutdown — the drain is bounded."""
    import openpoly.db.writer as writer_mod

    monkeypatch.setattr(writer_mod, "STOP_DRAIN_TIMEOUT_SECONDS", 0.05)
    started = asyncio.Event()

    def wedged(batch: list) -> None:
        started.set()
        time.sleep(0.6)

    writer = WriteBehindWriter(wedged, batch_size=10)
    await writer.start()
    writer.enqueue("row")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await writer.stop()  # must return promptly, must not raise
    assert writer.written == 0
    # Let the wedged worker thread finish so it does not outlive the test.
    await asyncio.sleep(0.7)


async def test_errors_counter_starts_at_zero() -> None:
    writer = WriteBehindWriter(lambda batch: None)
    assert writer.errors == 0

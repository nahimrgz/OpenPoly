"""Async WebSocket client with exponential-backoff reconnect.

Connects to a news provider's WS endpoint with the `X-API-Key` header. Each
incoming message is parsed and pushed into the supplied ring buffer. The loop
yields cooperatively (``await asyncio.sleep(0)``) so co-scheduled tasks are
never starved (lesson from a prior project's async-reconnect-starvation incident).

Lifecycle: external code calls ``run_forever()`` as an asyncio task; ``stop()``
sets an event that breaks the loop, then the task should be cancelled / awaited.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import re
from typing import Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from openpoly.markets.models import parse_iso

from .ring_buffer import NewsItem, NewsRingBuffer

# (kind, detail) — see manager.NewsSourceManager.record_event for the contract.
OnEventHook = Callable[[str, str | None], None]

# Single fresh news item — wired by manager to orchestrator.enqueue in v7
# lifespan. Sync because orchestrator.enqueue is sync; called for its side
# effect, the return value is ignored.
OnItemHook = Callable[[NewsItem], object]

logger = logging.getLogger(__name__)

# How far in the future a ``published_at`` may sit before it is distrusted.
# Allows ordinary clock skew between the provider and this host; anything
# beyond it is a bad timestamp whose negative age would otherwise sail past
# every freshness check, so the item is timed by its arrival instead.
MAX_FUTURE_SKEW_SECONDS = 60.0

# A numeric epoch above this is in milliseconds, not seconds: 1e12 seconds is
# the year ~33,700, while 1e12 ms is September 2001.
_EPOCH_MS_THRESHOLD = 1e12


def _epoch_seconds(value: float) -> float:
    """A numeric epoch in seconds, converting a millisecond epoch."""
    return value / 1000.0 if value > _EPOCH_MS_THRESHOLD else value


# An optionally signed decimal number: digits with an optional fraction.
_NUMERIC_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


def _to_epoch(value: object, fallback: float) -> float:
    """Accept ISO 8601 string or numeric epoch; fall back if neither parses.

    A naive ISO timestamp (no offset) is read as UTC — never as the host's
    local time, which would shift every such item's age by the host's UTC
    offset (clocks are UTC throughout this codebase).
    """
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        return _epoch_seconds(float(value))
    if isinstance(value, str):
        # A purely numeric string is an epoch, never a date: Python 3.13's
        # ``fromisoformat`` accepts ISO *basic* format, so it would read
        # "1792013060023.0" (epoch ms) as the year 1792. Parse numbers first.
        if _NUMERIC_RE.fullmatch(value.strip()):
            return _epoch_seconds(float(value))
        # tradingnews emits ISO 8601 with offset (e.g. 2026-05-19T10:49:35+00:00).
        parsed = parse_iso(value)
        if parsed is not None:
            return parsed.timestamp()
        try:
            return _epoch_seconds(float(value))
        except ValueError:
            return fallback
    return fallback


def default_parse(raw: str) -> NewsItem | None:
    """Parse a news provider WS message. Returns None on malformed input.

    Handles tradingnews shape (ISO 8601 timestamps, free-form urgency string).
    Unknown providers can override by passing a custom parser to NewsWSClient.
    """
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        # JSONDecodeError is a ValueError; so is an integer past the
        # int-digit limit. A deeply nested frame overflows the recursion limit.
        return None
    if not isinstance(data, dict) or "id" not in data:
        return None
    now = time.time()
    try:
        published_at = _to_epoch(data.get("published_at"), now)
        if published_at - now > MAX_FUTURE_SKEW_SECONDS:
            # Not trustworthy as a publish time, but the item itself may be
            # real news: time it by its arrival rather than dropping it.
            logger.warning(
                "news %s: published_at %r is %.0fs in the future; using receive time",
                data.get("id"),
                data.get("published_at"),
                published_at - now,
            )
            published_at = now
        return NewsItem(
            id=str(data["id"]),
            content=str(data.get("content", "")),
            urgency=str(data.get("urgency", "regular")),
            sentiment=data.get("sentiment"),
            published_at=published_at,
            received_at=now,
            raw=data,
        )
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def _redact(url: str) -> str:
    """Strip query string from URL for logging (api_key may live there)."""
    return url.split("?", 1)[0]


class NewsWSClient:
    def __init__(
        self,
        endpoint: str,
        buffer: NewsRingBuffer,
        *,
        headers: dict[str, str] | None = None,
        parse: Callable[[str], NewsItem | None] = default_parse,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        ping_interval: float | None = 20.0,
        on_event: OnEventHook | None = None,
        on_item: OnItemHook | None = None,
        freshness_seconds: float | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.headers = headers or {}
        self.buffer = buffer
        self.parse = parse
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.ping_interval = ping_interval
        self._stop = asyncio.Event()
        self._on_event = on_event
        self._on_item = on_item
        # When set, items older than this (by ``received_at - published_at``)
        # are silently dropped: not appended to buffer, no ``message`` emit,
        # not enqueued for the pipeline. Mainly guards against startup
        # backlog (server replaying recent history on reconnect). ``None``
        # disables the filter entirely.
        self._freshness_seconds = freshness_seconds
        # Exception types already logged for a dropped frame — a malformed
        # stream logs once per kind instead of once per frame.
        self._logged_frame_errors: set[str] = set()

    def stop(self) -> None:
        self._stop.set()

    def set_freshness_seconds(self, freshness_seconds: float | None) -> None:
        """Change the arrival freshness filter of a running client; applies
        from the next frame (``None`` disables it)."""
        self._freshness_seconds = freshness_seconds

    def _emit(self, kind: str, detail: str | None = None) -> None:
        """Forward a lifecycle event to the hook; exceptions are swallowed so
        a buggy consumer cannot kill the WS loop."""
        if self._on_event is None:
            return
        try:
            self._on_event(kind, detail)
        except Exception:  # noqa: BLE001 — hook is untrusted code
            logger.exception("on_event hook raised; suppressing")

    def _emit_item(self, item: NewsItem) -> None:
        """Forward a fresh news item to the on_item hook. Same exception
        discipline as ``_emit`` — never kills the WS loop."""
        if self._on_item is None:
            return
        try:
            self._on_item(item)
        except Exception:  # noqa: BLE001
            logger.exception("on_item hook raised; suppressing")

    async def run_forever(self) -> None:
        backoff = self.initial_backoff
        is_first_attempt = True
        while not self._stop.is_set():
            if is_first_attempt:
                self._emit("connecting", _redact(self.endpoint))
                is_first_attempt = False
            else:
                self._emit("reconnect_attempt")
            try:
                connect_kwargs: dict = {"ping_interval": self.ping_interval}
                if self.headers:
                    connect_kwargs["additional_headers"] = self.headers
                async with websockets.connect(self.endpoint, **connect_kwargs) as ws:
                    backoff = self.initial_backoff
                    self._emit("connected", _redact(self.endpoint))
                    logger.info("WS connected to %s", _redact(self.endpoint))
                    await self._consume(ws)
                self._emit("disconnected", "server closed cleanly")
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                # HTTP-status reject during upgrade — almost always auth/config,
                # not transient. Stop retrying; user must fix and restart.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                detail = f"HTTP {status}" if status else str(exc)
                self._emit("auth_fail", detail)
                logger.error("WS rejected by server (%s); stopping", detail)
                self._stop.set()
                break
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                self._emit("disconnected", str(exc))
                logger.warning("WS dropped: %s; reconnect in %.2fs", exc, backoff)
                await self._wait_backoff(backoff)
                backoff = min(backoff * 2.0, self.max_backoff)
            except Exception as exc:  # noqa: BLE001 — the task must outlive any bug
                # Anything else (a bug in a consumer, an unexpected library
                # error) would otherwise end the task silently while the
                # manager still reports 'connected'. Report a disconnect and
                # reconnect like any drop.
                self._emit("disconnected", repr(exc)[:200])
                logger.exception("WS loop error; reconnect in %.2fs", backoff)
                await self._wait_backoff(backoff)
                backoff = min(backoff * 2.0, self.max_backoff)

    async def _wait_backoff(self, backoff: float) -> None:
        """Sleep ``backoff`` seconds, or until ``stop()``."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=backoff)

    def _decode_and_parse(self, raw: str | bytes) -> tuple[NewsItem | None, str | None]:
        """``(item, text)`` for one frame; ``(None, text)`` when it does not
        parse. Never raises: a frame that fails to decode or blows up the
        parser (custom parsers included) is dropped and logged once per
        exception type."""
        text: str | None = None
        try:
            text = raw if isinstance(raw, str) else raw.decode()
            return self.parse(text), text
        except Exception as exc:  # noqa: BLE001 — one bad frame must not kill the stream
            kind = type(exc).__name__
            if kind not in self._logged_frame_errors:
                self._logged_frame_errors.add(kind)
                logger.warning(
                    "dropping malformed news frame (%s: %s); further %s frames are "
                    "dropped silently",
                    kind,
                    str(exc)[:120],
                    kind,
                )
            return None, text

    async def _consume(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                break
            item, text = self._decode_and_parse(raw)
            if item is None:
                snippet = text[:80] if isinstance(text, str) else None
                self._emit("parse_error", snippet)
                await asyncio.sleep(0)
                continue
            if self._freshness_seconds is not None:
                # Small negative ages (clock skew within
                # MAX_FUTURE_SKEW_SECONDS) pass; anything further in the
                # future was already re-timed to its arrival by the parser.
                # Only positive ages past the threshold drop.
                age = item.received_at - item.published_at
                if age > self._freshness_seconds:
                    # Silent drop per plan §P2 — no buffer, no emit.
                    await asyncio.sleep(0)
                    continue
            self.buffer.append(item)
            self._emit("message", item.id)
            self._emit_item(item)
            await asyncio.sleep(0)

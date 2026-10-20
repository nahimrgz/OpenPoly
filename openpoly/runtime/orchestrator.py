"""Pipeline orchestrator (v7 / P3, EM4 four-stage).

Event-driven serial worker. ``enqueue(item)`` is sync (called from ws_client's
on_item hook); a single asyncio worker drains the queue, runs ``embedding →
analyzer → entry`` per item, and appends per-step results into the section log
stores.

Concurrency model: **one worker, one queue**. LLM rate limits + LLM cost
favor serial over fan-out (micro-stakes paper). Queue is bounded; overflow drops
the newest item and logs a sentinel error entry so the user can see it on
the Calls tab.

Lifecycle is owned by ``PipelineOrchestrator.start() / stop()``. FastAPI
lifespan wires this in P4 (shutdown order: orchestrator first, manager
second).

Two freshness guards run between stages, because a serial queue can hold an
item for minutes and the book sampler polls only every 60s:

* **News age** — the news source drops stale items on *arrival*, but an item
  can age out while it waits in the queue. ``news_max_age_seconds`` is
  re-checked when the item is dequeued, before the embedding encode and the
  LLM call; an item past it is skipped as ``stale_news``.
* **Book age** — the entry decision (and the paper fill after it) read the
  cached order book. Before the entry section runs, any book it will read
  that is older than ``max_book_age_seconds`` is re-fetched through
  ``book_source``; if that fails the decision is skipped as ``stale_book``.
  The refreshed book lands in the shared ``MarketStore``, so the entry
  section, the paper fill and the live order price all read it.

Both guards are opt-in at construction (``None`` disables them) and wired by
``get_orchestrator``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import Any, Literal, NamedTuple, Protocol, TypeVar

from pydantic import BaseModel

from openpoly.embedding.models import MarketCandidates
from openpoly.llm.client import cost_or_none, int_or
from openpoly.llm.usage_store import usage_store
from openpoly.markets.manager import MAX_BOOK_AGE_SECONDS, FreshBookSource
from openpoly.markets.models import Market
from openpoly.news.ring_buffer import NewsItem
from openpoly.runtime.section_log import (
    AnalyzerCall,
    EmbeddingCall,
    EntryDecision,
    SectionLogStore,
)
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.execution import ExecResult, executor
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent
from openpoly.sections.entry._types import FreshBooks, candidate_sides

logger = logging.getLogger(__name__)


DEFAULT_QUEUE_MAXSIZE = 100

# Cap on the analyzer reasoning kept per analyzer_log entry (the ring is
# served whole to a 3s poll).
MAX_LOGGED_REASONING_CHARS = 2000


class _SyncSection(Protocol):
    """Minimal section shape used by the orchestrator."""

    def run(self, input: SectionInput) -> SectionOutput: ...


class _Executor(Protocol):
    """Minimal executor shape used by the orchestrator."""

    def execute_buy(self, intent: OrderIntent, *, news_id: str | None, ts: float) -> ExecResult: ...


State = Literal["stopped", "running"]


class _BookCheck(NamedTuple):
    """Outcome of the entry-time freshness check: the market and the ages of
    the books (by token id) within budget after the refresh."""

    market: Market
    fresh: dict[str, float]
    oldest: float | None  # oldest cached book read, fresh or not (None: no book)


class PipelineOrchestrator:
    def __init__(
        self,
        *,
        embedding_section: _SyncSection,
        analyzer_section: _SyncSection,
        entry_section: _SyncSection,
        executor: _Executor,
        embedding_log_store: SectionLogStore[EmbeddingCall],
        analyzer_log_store: SectionLogStore[AnalyzerCall],
        entry_log_store: SectionLogStore[EntryDecision],
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        book_source: FreshBookSource | None = None,
        news_max_age_seconds: float | None = None,
        max_book_age_seconds: float = MAX_BOOK_AGE_SECONDS,
    ) -> None:
        self._embedding = embedding_section
        self._analyzer = analyzer_section
        self._entry = entry_section
        self._executor = executor
        self._embedding_log = embedding_log_store
        self._analyzer_log = analyzer_log_store
        self._entry_log = entry_log_store
        self._book_source = book_source
        self._news_max_age_seconds = news_max_age_seconds
        self._max_book_age_seconds = max_book_age_seconds
        self._queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker_task: asyncio.Task[None] | None = None
        self._state: State = "stopped"
        # canvas-sync v2: lock for atomic section swap (called from
        # /api/canvas/template PUT handler when a section's config changes).
        # In-flight section.run(...) keeps its own reference; Python GC holds
        # the old instance alive until that call returns. Next call uses the
        # new instance.
        self._sections_lock = asyncio.Lock()
        # Fire-and-forget bookkeeping (the ``llm_usage`` insert): referenced
        # here so a task is not garbage-collected mid-flight, flushed on stop.
        self._background: set[asyncio.Task[None]] = set()

    def __repr__(self) -> str:
        return f"PipelineOrchestrator(state={self._state}, queue_depth={self.queue_depth})"

    # ---------- read-only properties ----------

    @property
    def state(self) -> State:
        return self._state

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._state = "running"
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        try:
            if self._worker_task is None:
                return
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        finally:
            self._worker_task = None
            self._state = "stopped"
            await self.flush_background()

    async def flush_background(self) -> None:
        """Wait for in-flight bookkeeping tasks (each logs its own failure)."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def _spawn_background(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------- canvas-sync v2: hot-reload section swap ----------

    async def replace_section(self, section_type: str, new_inst: _SyncSection) -> None:
        """Atomically swap one section instance while the pipeline runs.

        Held under ``_sections_lock`` so a swap cannot interleave with the
        read in ``_process``. An item already inside a section's ``run`` keeps
        the old instance alive through its own reference; the next item reads
        the attribute again and gets the new one.
        """
        async with self._sections_lock:
            if section_type == "embedding":
                self._embedding = new_inst
            elif section_type == "analyzer":
                self._analyzer = new_inst
            elif section_type == "entry":
                self._entry = new_inst
            else:
                raise ValueError(f"unknown orchestrator section_type: {section_type!r}")

    def set_news_max_age(self, seconds: float | None) -> None:
        """Replace the decision-time news age limit (canvas reload)."""
        self._news_max_age_seconds = seconds

    # ---------- enqueue (sync, called from ws_client hook) ----------

    def enqueue(self, item: NewsItem) -> bool:
        """Returns True if accepted, False if dropped due to queue full.

        Overflow drops the **newest** (this item) — older items in the queue
        keep their slot. This matches plan §OD1 and is simpler than peeking
        + evicting oldest. The dropped item is recorded as an error entry in
        ``embedding_log`` (the pipeline's first stage) so it's not silently
        lost.
        """
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self._embedding_log.append(
                EmbeddingCall(
                    ts=time.time(),
                    news_id=item.id,
                    news_content_preview=item.content[:80],
                    urgency=item.urgency,
                    verdict="error",
                    candidate_count=0,
                    top_market_id=None,
                    top_score=None,
                    catalog_size=0,
                    latency_ms=0,
                    error=f"queue_overflow (depth={self._queue.qsize()})",
                )
            )
            logger.warning("orchestrator queue full; dropped news_id=%s", item.id)
            return False

    # ---------- internals ----------

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._process(item)
            except Exception:  # noqa: BLE001 — defense-in-depth; _process catches
                logger.exception(
                    "orchestrator: unexpected error after _process for %s",
                    item.id,
                )
            finally:
                self._queue.task_done()

    async def _process(self, item: NewsItem) -> None:
        if self._news_is_stale(item):
            return
        candidates = await self._run_embedding(item)
        if candidates is None:
            return
        ar_payload = await self._run_analyzer(item, candidates)
        if ar_payload is None:
            return
        await self._run_entry(item, ar_payload)

    def _news_is_stale(self, item: NewsItem) -> bool:
        """Re-check the item's age when it is dequeued.

        The news source only checks freshness on arrival; the queue holds up
        to ``DEFAULT_QUEUE_MAXSIZE`` items drained one LLM call at a time, so
        an item can be well past the window by its turn. A stale item is
        logged as an analyzer ``skip`` with reason ``stale_news`` and goes no
        further (no encode, no LLM call). Returns True when it was skipped.
        """
        max_age = self._news_max_age_seconds
        if max_age is None:
            return False
        now = time.time()
        age = now - item.published_at
        if age <= max_age:
            return False
        logger.info(
            "orchestrator: skipping news_id=%s — %.0fs old at decision time (max %.0fs)",
            item.id,
            age,
            max_age,
        )
        self._analyzer_log.append(
            AnalyzerCall(
                ts=now,
                news_id=item.id,
                news_content_preview=item.content[:80],
                urgency=item.urgency,
                verdict="skip",
                p_model=None,
                confidence=None,
                market_id=None,
                latency_ms=0,
                reason=f"stale_news (age {age:.0f}s > {max_age:.0f}s)",
            )
        )
        return True

    async def _run_embedding(self, item: NewsItem) -> MarketCandidates | None:
        """Stage 1 — narrow the market catalog for this news item.

        Returns the ``MarketCandidates`` on ok, or None on skip / error (the
        pipeline then stops without calling the analyzer).

        The section encodes the news text with the local sentence-transformer
        — CPU-bound torch inference, and on the first call after a cache-warm
        start the model load itself (seconds) — so ``run()`` is offloaded to a
        worker thread like the other stages.
        """
        ts = time.time()
        start = time.monotonic()
        verdict: str
        error: str | None
        out: SectionOutput | None
        try:
            out = await asyncio.to_thread(
                self._embedding.run, SectionInput(tick_type="event", payload=item)
            )
            verdict = str(out.verdict)
            error = out.reason if verdict == "error" else None
        except Exception as exc:  # noqa: BLE001 — section impl is user code
            out = None
            verdict = "error"
            error = repr(exc)[:200]
        latency_ms = int((time.monotonic() - start) * 1000)

        candidates = (
            out.payload if out is not None and isinstance(out.payload, MarketCandidates) else None
        )
        signals = out.signals if out is not None else {}
        top = candidates.candidates[0] if candidates is not None and candidates.candidates else None

        self._embedding_log.append(
            EmbeddingCall(
                ts=ts,
                news_id=item.id,
                news_content_preview=item.content[:80],
                urgency=item.urgency,
                verdict=verdict,  # type: ignore[arg-type]
                candidate_count=(len(candidates.candidates) if candidates is not None else 0),
                top_market_id=top.market.market_id if top is not None else None,
                top_score=top.score if top is not None else None,
                catalog_size=int(signals.get("catalog_size", 0) or 0),
                latency_ms=latency_ms,
                error=error,
            )
        )

        if verdict != "ok" or candidates is None:
            return None
        return candidates

    async def _run_analyzer(
        self, item: NewsItem, candidates: MarketCandidates
    ) -> AnalysisResult | None:
        """Stage 2 — analyze the narrowed candidate set.

        ``item`` supplies the news fields for the log entry; ``candidates`` is
        the section payload. Returns the AnalysisResult on ok, else None.

        The analyzer makes a blocking LLM API call, so its ``run()`` is
        offloaded to a worker thread — the event loop stays free for the WS
        reconnect / market-poll tasks (docs/architecture/05).
        """
        ts = time.time()
        start = time.monotonic()
        verdict: str
        error: str | None
        out: SectionOutput | None
        try:
            out = await asyncio.to_thread(
                self._analyzer.run,
                SectionInput(tick_type="event", payload=candidates),
            )
            verdict = str(out.verdict)
            error = out.reason if verdict == "error" else None
        except Exception as exc:  # noqa: BLE001 — section impl is user code
            out = None
            verdict = "error"
            error = repr(exc)[:200]
        latency_ms = int((time.monotonic() - start) * 1000)

        ar = out.payload if out is not None and isinstance(out.payload, AnalysisResult) else None
        pick = _skipped_pick(out) if ar is None else {}
        usage = _usage_signal(out)
        reasoning, reasoning_truncated = _reasoning(out)

        self._analyzer_log.append(
            AnalyzerCall(
                ts=ts,
                news_id=item.id,
                news_content_preview=item.content[:80],
                urgency=item.urgency,
                verdict=verdict,  # type: ignore[arg-type]
                p_model=ar.p_model if ar is not None else pick.get("p_model"),
                confidence=ar.confidence if ar is not None else pick.get("confidence"),
                market_id=ar.market_id if ar is not None else pick.get("market_id"),
                latency_ms=latency_ms,
                error=error,
                # Why a non-error call produced no signal (abstain vs. a pick
                # cut by min_confidence); errors already carry it in ``error``.
                reason=out.reason if out is not None and verdict != "error" else None,
                # PD1: surface the LLM's stated reason for the decision so
                # PositionDetail UI can show it next to the position. Empty
                # string from the model is also informative ("intentionally
                # said nothing") — keep it; only None when ar itself is None.
                rationale=ar.rationale if ar is not None else None,
                reasoning=reasoning,
                reasoning_truncated=reasoning_truncated,
                **_usage_fields(usage),
            )
        )
        if usage is not None:
            # Fire-and-forget: a slow insert (a busy SQLite file) must not
            # delay the entry decision this result feeds.
            self._spawn_background(
                self._record_usage(usage, ts=ts, news_id=item.id, verdict=verdict)
            )

        # Only forward to entry on ok + valid AR payload.
        if verdict != "ok" or ar is None:
            return None
        return ar

    async def _record_usage(
        self, usage: dict[str, Any], *, ts: float, news_id: str, verdict: str
    ) -> None:
        """Persist one analyzer call's usage (``llm_usage`` table) off the
        event loop. Bookkeeping only: a failure is logged, never raised."""
        try:
            await asyncio.to_thread(
                usage_store.record, usage, ts=ts, news_id=news_id, verdict=verdict
            )
        except Exception:  # noqa: BLE001 — usage logging must not break the pipeline
            logger.exception("orchestrator: recording LLM usage for news_id=%s failed", news_id)

    def _entry_tokens(self, market: Market) -> list[str]:
        """The tokens whose books the entry decision reads: YES only under the
        entry's ``side_lock``, else both sides (v1 picks the side by comparing
        both books)."""
        side_lock = getattr(getattr(self._entry, "config", None), "side_lock", False)
        sides = candidate_sides(side_lock)
        return [t for t in (market.token_for(s) for s in sides) if t is not None]

    async def _ensure_fresh_books(self, market_id: str) -> _BookCheck | None:
        """Refresh any book the entry will read that is older than the budget.

        Returns the market plus the ages of the books that are within budget
        after the refresh — a side whose refresh failed is simply absent.
        None when freshness is not checked: no book source, or a market
        missing from the catalog (left to the entry section, which reports it
        in its own words).
        """
        source = self._book_source
        if source is None:
            return None
        market = source.store.get(market_id)
        if market is None:
            return None
        tokens = self._entry_tokens(market)
        max_age = self._max_book_age_seconds
        ages = await source.ensure_fresh(tokens, max_age)
        fresh: dict[str, float] = {}
        oldest: float | None = None
        for token, age in ages.items():
            if age is not None and (oldest is None or age > oldest):
                oldest = age
            if age is not None and age <= max_age:
                fresh[token] = age
            else:
                logger.warning(
                    "orchestrator: order book %s for %s is stale (%s, max %.1fs)",
                    token,
                    market_id,
                    "missing" if age is None else f"{age:.1f}s",
                    max_age,
                )
        return _BookCheck(market=market, fresh=fresh, oldest=oldest)

    async def _run_entry(self, item: NewsItem, ar: AnalysisResult) -> None:
        """Stage 3 — entry decision + execution.

        Before the section runs, the market's order books are brought within
        the freshness budget (``_ensure_fresh_books``; each book's age is
        judged from its own fetch against the decision time, so one slow
        refresh cannot age a sibling). The decision is skipped as
        ``stale_book`` when no book it would read is fresh, or when the
        section picks a side whose book could not be refreshed. One failed
        refresh therefore does not block a trade on the other, fresh side —
        but a stale side is never traded. A section declaring
        ``ACCEPTS_FRESH_BOOKS`` (entry v1) is handed the fresh token set
        (``FreshBooks``) and leaves a stale side out of its side choice, so a
        stale side cannot win on a cached price and veto the decision; the
        chosen-side check afterwards keeps the guarantee for any section.

        The entry section may do a blocking HTTP fetch (the late-buy veto) and
        the live executor blocks on network + sleeps, so both are offloaded to
        worker threads; only the log append stays inline.
        """
        ts = time.time()
        start = time.monotonic()
        verdict: str
        reason: str | None
        error: str | None
        intent: OrderIntent | None = None
        books = await self._ensure_fresh_books(ar.market_id)
        book_age_s = books.oldest if books is not None else None
        if books is not None and not books.fresh:
            verdict, reason, error = "skip", "stale_book", None
        else:
            payload: AnalysisResult | FreshBooks = ar
            if books is not None and getattr(self._entry, "ACCEPTS_FRESH_BOOKS", False):
                payload = FreshBooks(analysis=ar, fresh_tokens=frozenset(books.fresh))
            try:
                out = await asyncio.to_thread(
                    self._entry.run,
                    SectionInput(tick_type="event", payload=payload),
                )
                verdict = str(out.verdict)
                reason = out.reason
                error = out.reason if verdict == "error" else None
                if verdict == "ok" and isinstance(out.payload, OrderIntent):
                    intent = out.payload
            except Exception as exc:  # noqa: BLE001 — section impl is user code
                verdict = "error"
                reason = None
                error = repr(exc)[:200]
            if intent is not None and books is not None:
                chosen = books.market.token_for(intent.side)
                if chosen is None or chosen not in books.fresh:
                    logger.warning(
                        "orchestrator: entry chose %s on %s but its book is stale — skipping",
                        intent.side,
                        ar.market_id,
                    )
                    intent = None
                    verdict, reason, error = "skip", "stale_book", None
                else:
                    book_age_s = books.fresh[chosen]
                    logger.info(
                        "orchestrator: entry decision for %s on a %.1fs-old book",
                        ar.market_id,
                        book_age_s,
                    )

        # Execution stage — only when the section produced an OrderIntent.
        fill_status: str | None = None
        fill_price: float | None = None
        fill_qty: float | None = None
        position_id: int | None = None
        if intent is not None:
            try:
                # The live executor sleeps for seconds inside execute_buy
                # (balance-allowance refresh, CTF polling after a lost
                # response), so it is offloaded like the section calls above —
                # the event loop must stay free for the WS reconnect / market
                # poll tasks.
                result = await asyncio.to_thread(
                    self._executor.execute_buy, intent, news_id=item.id, ts=ts
                )
                if result.filled:
                    fill_status = "filled"
                    fill_price = result.price
                    fill_qty = result.qty
                    position_id = result.position_id
                else:
                    fill_status = result.skip_reason
                    # Most skip reasons are benign, routine strategy outcomes
                    # (dust, price out of band, position already open) and
                    # keep verdict="ok". The one exception: the on-chain buy
                    # already filled but the ledger write permanently failed
                    # (``_persist_open``'s never-raise contract turns that
                    # into a skip, not an exception) — the wallet now holds
                    # tokens no ledger row manages, which is not benign.
                    if fill_status is not None and fill_status.startswith("open_persist_failed:"):
                        verdict = "error"
                        error = f"on-chain buy filled but ledger write failed: {fill_status}"
            except Exception as exc:  # noqa: BLE001 — DB write may raise
                verdict = "error"
                error = repr(exc)[:200]
                fill_status = "error"

        latency_ms = int((time.monotonic() - start) * 1000)

        self._entry_log.append(
            EntryDecision(
                ts=ts,
                news_id=item.id,
                ar_p_model=ar.p_model,
                ar_market_id=ar.market_id,
                verdict=verdict,  # type: ignore[arg-type]
                side=intent.side if intent is not None else None,
                qty=intent.qty if intent is not None else None,
                price=intent.price if intent is not None else None,
                reason=reason,
                latency_ms=latency_ms,
                error=error,
                fill_status=fill_status,
                fill_price=fill_price,
                fill_qty=fill_qty,
                position_id=position_id,
                book_age_s=book_age_s,
            )
        )


def _reasoning(out: SectionOutput | None) -> tuple[str | None, bool]:
    """The analyzer's written self-check, when its impl produces one — capped
    so the polled /api/analyzer/log payload stays small — and whether the cap
    cut it (surfaced so the UI can mark it rather than read as the model
    stopping mid-thought)."""
    reasoning = out.signals.get("reasoning") if out is not None else None
    if not isinstance(reasoning, str) or not reasoning:
        return None, False
    return reasoning[:MAX_LOGGED_REASONING_CHARS], len(reasoning) > MAX_LOGGED_REASONING_CHARS


def _skipped_pick(out: SectionOutput | None) -> dict[str, Any]:
    """The market / p_yes / confidence an analyzer chose but did not act on
    (e.g. a ``below min_confidence`` skip), from its signals. Logged only, so
    a skip can be told from an abstain; it never reaches entry."""
    if out is None:
        return {}
    sig = out.signals
    market_id = sig.get("market_id")
    p_yes = sig.get("p_yes")
    confidence = sig.get("confidence")
    return {
        "market_id": market_id if isinstance(market_id, str) else None,
        "p_model": float(p_yes)
        if isinstance(p_yes, (int, float)) and not isinstance(p_yes, bool)
        else None,
        "confidence": confidence if isinstance(confidence, str) else None,
    }


def _usage_signal(out: SectionOutput | None) -> dict[str, Any] | None:
    """The analyzer's ``llm_usage`` signal (analyzer v1), when present."""
    if out is None:
        return None
    usage = out.signals.get("llm_usage")
    return usage if isinstance(usage, dict) else None


def _usage_fields(usage: dict[str, Any] | None) -> dict[str, Any]:
    """``AnalyzerCall`` keyword fields for one call's usage."""
    if usage is None:
        return {}

    model = usage.get("model")
    return {
        "llm_model": model if isinstance(model, str) else None,
        "input_tokens": int_or(usage.get("input_tokens"), None),
        "output_tokens": int_or(usage.get("output_tokens"), None),
        "cache_read_input_tokens": int_or(usage.get("cache_read_input_tokens"), None),
        "cache_creation_input_tokens": int_or(usage.get("cache_creation_input_tokens"), None),
        "llm_attempts": int_or(usage.get("attempts"), None),
        "cost_usd": cost_or_none(usage.get("cost_usd")),
    }


# Module-level singleton built lazily so tests can substitute. Real
# wire-up (with manager hook) lives in main.py's lifespan (P4).
_singleton: PipelineOrchestrator | None = None

_C = TypeVar("_C", bound=BaseModel)


def _canvas_config(config_cls: type[_C], section_type: str) -> _C:
    """Build a section Config from the persisted canvas node config, falling
    back to the Config's own defaults on a missing node or invalid values — so
    a stale or hand-broken canvas can never block pipeline startup."""
    from openpoly.runtime.canvas_store import section_config

    raw = section_config(section_type)
    if not raw:
        return config_cls()
    try:
        return config_cls(**raw)
    except Exception as exc:  # noqa: BLE001 — bad canvas config must not break startup
        logger.warning(
            "invalid canvas config for section %r (%s); using defaults",
            section_type,
            exc,
        )
        return config_cls()


def _news_max_age_seconds() -> float:
    """The news source's freshness window, re-used as the max age at decision
    time: the canvas news_source node's ``freshness_seconds``. Resolved from
    the canvas when the orchestrator is built (and again on a canvas edit,
    ``reload_news_max_age``). The arrival filter matches it because both
    autostart (``api.main._news_autostart_config``) and this read the same
    canvas node; a source started by hand from the Live tab with a different
    window is the operator's explicit choice."""
    from openpoly.sections.news_source.tradingnews_ws import TradingNewsWSConfig

    return float(_canvas_config(TradingNewsWSConfig, "news_source").freshness_seconds)


def get_orchestrator() -> PipelineOrchestrator:
    """Lazily build the pipeline orchestrator. Each section is the active impl
    (``runtime.active_sections``) with params from the persisted canvas
    (``canvas_store``); restart the backend to apply a canvas edit, same as
    picking up a new section impl."""
    global _singleton
    if _singleton is None:
        from openpoly.markets.manager import manager as market_source_manager
        from openpoly.runtime.active_sections import build_section
        from openpoly.runtime.section_log import (
            analyzer_log,
            embedding_log,
            entry_log,
        )

        _singleton = PipelineOrchestrator(
            embedding_section=build_section("embedding"),
            analyzer_section=build_section("analyzer"),
            entry_section=build_section("entry"),
            executor=executor,
            embedding_log_store=embedding_log,
            analyzer_log_store=analyzer_log,
            entry_log_store=entry_log,
            book_source=market_source_manager,
            news_max_age_seconds=_news_max_age_seconds(),
        )
    return _singleton


def _reset_singleton_for_tests() -> None:
    global _singleton
    _singleton = None


# ---------- canvas-sync v2: hot-reload section swap ----------


async def replace_section(section_type: str, new_inst: _SyncSection) -> None:
    """Module-level entry point — atomically replace a section in the running
    orchestrator (if any). No-op when no singleton has been built yet (e.g.
    PUT arrived before first news; the orchestrator's next lazy build will
    read the updated canvas anyway).

    Callers: ``api/canvas_routes._apply_canvas_reload`` after a PUT diff
    detects a section's config changed. Build the new instance in the
    caller so failures there don't pollute the orchestrator with a
    half-constructed object.
    """
    if _singleton is None:
        return
    await _singleton.replace_section(section_type, new_inst)


def reload_news_max_age() -> None:
    """Re-read the news source's freshness window into the running
    orchestrator (if any) — called by the canvas reload when the news_source
    node changes, since the limit is otherwise resolved once at build time."""
    if _singleton is None:
        return
    _singleton.set_news_max_age(_news_max_age_seconds())

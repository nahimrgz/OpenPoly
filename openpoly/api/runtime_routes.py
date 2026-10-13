"""Per-section log endpoints (v7 / P4, EM4 embedding stage).

``GET /api/{embedding,analyzer,entry,exit}/log`` expose the bounded ring +
counters + last_at for the inspector Calls / Decisions tabs. The routes share
a uniform response shape but carry section-specific entry payloads (loose
``dict[str, Any]`` to avoid duplicating dataclass field schemas — the
EmbeddingCall / AnalyzerCall / EntryDecision / ExitDecision dataclasses *are*
the contract, validated by their own unit tests). The exit log has no news
queue (``queue_depth`` is always ``0``); ``state`` mirrors the exit monitor's
loop state, and ``last_tick_at`` / ``open_positions`` / ``blocked`` carry its
tick heartbeat (within-threshold + no-order-book holds write no entry, so the
ring keeps only ok / error closes — these counts surface the rest).

``GET /api/analyzer/log/{news_id}/reasoning`` returns one entry's analyzer
self-check, which the polled list leaves out (``has_reasoning`` flags it).

``POST /api/analyzer/test`` is a connectivity probe — it builds an LLM client
from supplied analyzer-config fields and makes one minimal call, so the user
can verify the key / base URL / model before running the pipeline.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from openpoly.api.security import require_api_token
from openpoly.db.engine import get_session_factory
from openpoly.llm import LLMClient, LLMError
from openpoly.llm.usage_store import usage_totals
from openpoly.runtime.orchestrator import get_orchestrator
from openpoly.runtime.section_log import (
    AnalyzerCall,
    analyzer_log,
    embedding_log,
    embedding_warm_log,
    entry_log,
    exit_log,
    settlement_log,
)
from openpoly.runtime.exit_monitor import exit_monitor
from openpoly.runtime.settlement_monitor import settlement_monitor
from openpoly.sections.analyzer.llm_v0 import LLMAnalyzerConfig

router = APIRouter(prefix="/api", tags=["runtime"])


class SectionLogResponse(BaseModel):
    entries: list[dict[str, Any]]
    counters: dict[str, int]
    last_at: float | None
    queue_depth: int
    state: str
    # Embedding-only: background warm-cache events (model load / cache reload /
    # warm cycles). None on the analyzer / entry routes — they have no warm loop.
    warm: list[dict[str, Any]] | None = None
    # Exit-only: tick heartbeat. None on the other routes. ``last_tick_at`` is
    # the last sweep's wall-clock; ``open_positions`` / ``blocked`` are that
    # sweep's counts (blocked == positions with no order book, can't evaluate).
    last_tick_at: float | None = None
    open_positions: int | None = None
    blocked: int | None = None


@router.get("/embedding/log", response_model=SectionLogResponse)
def get_embedding_log(limit: int = 200) -> SectionLogResponse:
    orch = get_orchestrator()
    return SectionLogResponse(
        entries=[e.to_dict() for e in embedding_log.entries(limit=limit)],
        counters=embedding_log.counters(),
        last_at=embedding_log.last_at,
        queue_depth=orch.queue_depth,
        state=orch.state,
        warm=[e.to_dict() for e in embedding_warm_log.entries(limit=limit)],
    )


def _analyzer_list_entry(entry: AnalyzerCall) -> dict[str, Any]:
    """An analyzer log entry for the polled list: the (up to 2000-char)
    ``reasoning`` is replaced by a ``has_reasoning`` flag — the Calls tab polls
    the whole ring every 3s and fetches one entry's reasoning on demand
    (``GET /api/analyzer/log/{news_id}/reasoning?ts=<ts>``)."""
    d = entry.to_dict()
    d["has_reasoning"] = bool(d.pop("reasoning", None))
    return d


@router.get("/analyzer/log", response_model=SectionLogResponse)
def get_analyzer_log(limit: int = 200) -> SectionLogResponse:
    orch = get_orchestrator()
    return SectionLogResponse(
        entries=[_analyzer_list_entry(e) for e in analyzer_log.entries(limit=limit)],
        counters=analyzer_log.counters(),
        last_at=analyzer_log.last_at,
        queue_depth=orch.queue_depth,
        state=orch.state,
    )


class AnalyzerReasoningResponse(BaseModel):
    news_id: str
    reasoning: str | None
    # The reasoning was cut at the logging cap (2000 chars).
    truncated: bool = False


@router.get(
    "/analyzer/log/{news_id}/reasoning",
    response_model=AnalyzerReasoningResponse,
    # ``truncated`` is only sent when true (older clients compare the body).
    response_model_exclude_defaults=True,
)
def get_analyzer_reasoning(news_id: str, ts: float | None = None) -> AnalyzerReasoningResponse:
    """The analyzer's written self-check for one call, identified by its
    ``(ts, news_id)`` — unique per log entry. One news item can be analyzed
    more than once (a re-delivery, a stale-news skip and a later call), so
    ``news_id`` alone could return another call's reasoning. Without ``ts``
    (older clients) the newest entry for ``news_id`` is returned, as before.
    404 once the entry has left the ring."""
    for entry in reversed(analyzer_log.entries()):
        if entry.news_id == news_id and (ts is None or entry.ts == ts):
            return AnalyzerReasoningResponse(
                news_id=news_id,
                reasoning=entry.reasoning,
                truncated=entry.reasoning_truncated,
            )
    raise HTTPException(status_code=404, detail="no analyzer log entry for this call")


# ---------- GET /api/analyzer/usage — LLM token usage totals ----------


@router.get("/analyzer/usage")
def get_analyzer_usage(
    factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict[str, Any]:
    """Analyzer LLM usage totals — calls, input / output tokens (output
    includes thinking), cache tokens and estimated USD cost — for today
    (UTC calendar day), the last 7 days and all time, from the ``llm_usage``
    table (one row per analyzer call, retries summed in). ``unpriced_calls``
    counts calls on a model without a known price, which ``cost_usd`` leaves
    out. Read-only."""
    return usage_totals(factory)


# ---------- POST /api/analyzer/test — LLM connectivity probe ----------


class AnalyzerTestRequest(BaseModel):
    """The analyzer-config fields that affect connectivity. Mirrors the canvas
    node config; ``extra_guidance`` / ``min_confidence`` are irrelevant here."""

    llm_model: str
    api_key_ref: str
    base_url: str = ""
    temperature: float = 0.2
    # Analyzer v1's effort (sent only to models that accept it).
    llm_effort: Literal["low", "medium", "high"] = "low"


class AnalyzerTestResponse(BaseModel):
    ok: bool
    error: str | None = None
    latency_ms: int | None = None


@router.post(
    "/analyzer/test", response_model=AnalyzerTestResponse, dependencies=[Depends(require_api_token)]
)
def test_analyzer(req: AnalyzerTestRequest) -> AnalyzerTestResponse:
    """Verify the analyzer's LLM config with one minimal forced tool call.

    The request is run through ``LLMAnalyzerConfig`` first, so the probe uses
    the exact (validated + normalized) config the runtime would build — e.g.
    a ``base_url``'s trailing ``/v1`` is stripped the same way.
    """
    try:
        cfg = LLMAnalyzerConfig(
            llm_model=req.llm_model,
            api_key_ref=req.api_key_ref,
            base_url=req.base_url,
            temperature=req.temperature,
        )
    except ValidationError as exc:
        return AnalyzerTestResponse(ok=False, error=f"invalid config: {exc}")

    client = LLMClient(
        api_key_ref=cfg.api_key_ref,
        model=cfg.llm_model,
        temperature=cfg.temperature,
        base_url=cfg.base_url,
        effort=req.llm_effort,
    )
    start = time.monotonic()
    try:
        client.ping()
    except LLMError as exc:
        return AnalyzerTestResponse(ok=False, error=str(exc))
    return AnalyzerTestResponse(ok=True, latency_ms=int((time.monotonic() - start) * 1000))


@router.get("/entry/log", response_model=SectionLogResponse)
def get_entry_log(limit: int = 200) -> SectionLogResponse:
    orch = get_orchestrator()
    return SectionLogResponse(
        entries=[e.to_dict() for e in entry_log.entries(limit=limit)],
        counters=entry_log.counters(),
        last_at=entry_log.last_at,
        queue_depth=orch.queue_depth,
        state=orch.state,
    )


@router.get("/exit/log", response_model=SectionLogResponse)
def get_exit_log(limit: int = 200) -> SectionLogResponse:
    # The exit monitor is position-driven, not a news queue — queue_depth has
    # no meaning (always 0). state + the tick-heartbeat fields come straight
    # off the monitor singleton.
    return SectionLogResponse(
        entries=[e.to_dict() for e in exit_log.entries(limit=limit)],
        counters=exit_log.counters(),
        last_at=exit_log.last_at,
        queue_depth=0,
        state=exit_monitor.state,
        last_tick_at=exit_monitor.last_tick_at,
        open_positions=exit_monitor.open_positions,
        blocked=exit_monitor.blocked,
    )


@router.get("/settlement/log", response_model=SectionLogResponse)
def get_settlement_log(limit: int = 200) -> SectionLogResponse:
    # Settlement monitor mirrors the exit-monitor shape — position-driven, no
    # news queue. state surfaces whether the loop is running.
    return SectionLogResponse(
        entries=[e.to_dict() for e in settlement_log.entries(limit=limit)],
        counters=settlement_log.counters(),
        last_at=settlement_log.last_at,
        queue_depth=0,
        state=settlement_monitor.state,
    )

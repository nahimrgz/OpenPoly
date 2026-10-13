"""Persisted analyzer LLM usage — one ``llm_usage`` row per ``analyze`` call.

``UsageStore.record`` is called by the orchestrator (off the event loop) after
each analyzer call that spent tokens; ``totals`` backs
``GET /api/analyzer/usage``. Clocks are UTC: "today" is the UTC calendar day.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import LLMUsageRow

logger = logging.getLogger(__name__)

_WEEK_SECONDS = 7 * 86_400


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class UsageStore:
    """Writes and aggregates ``llm_usage`` rows. Unconfigured (no session
    factory yet) it records nothing — the orchestrator can run before the DB
    is up, and usage logging must never break the pipeline."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._factory = session_factory

    def configure(self, session_factory: sessionmaker[Session] | None) -> None:
        self._factory = session_factory

    def record(
        self,
        usage: dict[str, Any],
        *,
        ts: float,
        news_id: str | None,
        verdict: str | None,
    ) -> None:
        """Persist one call's usage (the ``llm_usage`` signal dict)."""
        if self._factory is None:
            return
        cost = usage.get("cost_usd")
        row = LLMUsageRow(
            ts=ts,
            news_id=news_id,
            model=str(usage.get("model") or ""),
            verdict=verdict,
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
            cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
            attempts=_int(usage.get("attempts")) or 1,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        )
        with self._factory() as session:
            session.add(row)
            session.commit()


def usage_totals(
    session_factory: sessionmaker[Session], now: float | None = None
) -> dict[str, dict[str, Any]]:
    """Call count, token and cost totals for today (UTC), the last 7 days
    and all time. ``cost_usd`` sums the rows with a known price;
    ``unpriced_calls`` counts the ones without (a model missing from the
    pricing table), so a partial cost is never mistaken for the whole."""
    now = time.time() if now is None else now
    day_start = (
        datetime.fromtimestamp(now, tz=UTC)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    windows = {"today": day_start, "last_7_days": now - _WEEK_SECONDS, "all_time": None}
    out: dict[str, dict[str, Any]] = {}
    with session_factory() as session:
        for name, since in windows.items():
            stmt = select(
                func.count(LLMUsageRow.id),
                func.coalesce(func.sum(LLMUsageRow.input_tokens), 0),
                func.coalesce(func.sum(LLMUsageRow.output_tokens), 0),
                func.coalesce(func.sum(LLMUsageRow.cache_read_input_tokens), 0),
                func.coalesce(func.sum(LLMUsageRow.cache_creation_input_tokens), 0),
                func.coalesce(func.sum(LLMUsageRow.cost_usd), 0.0),
                func.count(LLMUsageRow.id).filter(LLMUsageRow.cost_usd.is_(None)),
            )
            if since is not None:
                stmt = stmt.where(LLMUsageRow.ts >= since)
            calls, tin, tout, cread, ccreate, cost, unpriced = session.execute(stmt).one()
            out[name] = {
                "calls": int(calls),
                "input_tokens": int(tin),
                "output_tokens": int(tout),
                "cache_read_input_tokens": int(cread),
                "cache_creation_input_tokens": int(ccreate),
                "cost_usd": float(cost),
                "unpriced_calls": int(unpriced),
            }
    return out


# Process-wide store; the FastAPI lifespan configures it once the DB is up.
usage_store = UsageStore()

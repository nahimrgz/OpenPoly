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

from sqlalchemy import and_, func, select, true
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import LLMUsageRow
from openpoly.llm.client import cost_or_none, int_or

logger = logging.getLogger(__name__)

_WEEK_SECONDS = 7 * 86_400


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
        row = LLMUsageRow(
            ts=ts,
            news_id=news_id,
            model=str(usage.get("model") or ""),
            verdict=verdict,
            input_tokens=int_or(usage.get("input_tokens"), 0),
            output_tokens=int_or(usage.get("output_tokens"), 0),
            cache_read_input_tokens=int_or(usage.get("cache_read_input_tokens"), 0),
            cache_creation_input_tokens=int_or(usage.get("cache_creation_input_tokens"), 0),
            attempts=int_or(usage.get("attempts"), 0) or 1,
            cost_usd=cost_or_none(usage.get("cost_usd")),
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
    token_columns = (
        LLMUsageRow.input_tokens,
        LLMUsageRow.output_tokens,
        LLMUsageRow.cache_read_input_tokens,
        LLMUsageRow.cache_creation_input_tokens,
    )
    # One scan for every window: each aggregate is FILTERed to its window
    # (all-time: an always-true filter), seven columns per window in
    # ``windows`` order.
    columns: list[Any] = []
    for since in windows.values():
        cond = LLMUsageRow.ts >= since if since is not None else true()
        columns += [
            func.count(LLMUsageRow.id).filter(cond),
            *(func.coalesce(func.sum(col).filter(cond), 0) for col in token_columns),
            func.coalesce(func.sum(LLMUsageRow.cost_usd).filter(cond), 0.0),
            func.count(LLMUsageRow.id).filter(and_(cond, LLMUsageRow.cost_usd.is_(None))),
        ]
    with session_factory() as session:
        row = session.execute(select(*columns)).one()
    out: dict[str, dict[str, Any]] = {}
    for n, name in enumerate(windows):
        calls, tin, tout, cread, ccreate, cost, unpriced_calls = row[n * 7 : (n + 1) * 7]
        out[name] = {
            "calls": int(calls),
            "input_tokens": int(tin),
            "output_tokens": int(tout),
            "cache_read_input_tokens": int(cread),
            "cache_creation_input_tokens": int(ccreate),
            "cost_usd": float(cost),
            "unpriced_calls": int(unpriced_calls),
        }
    return out


# Process-wide store; the FastAPI lifespan configures it once the DB is up.
usage_store = UsageStore()

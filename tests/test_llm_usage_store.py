"""Persisted analyzer LLM usage: the ``llm_usage`` table, its migration, the
orchestrator recording it, and ``GET /api/analyzer/usage``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from openpoly.db.engine import get_session_factory, init_db, make_engine, make_session_factory
from openpoly.db.migrations import run_migrations
from openpoly.llm.usage_store import UsageStore, usage_totals

# Fixed "now": 2026-10-13 12:00:00Z.
_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/u.db")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _usage(inp: int, out: int, cost: float | None, model: str = "claude-haiku-4-5") -> dict:
    return {
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "attempts": 1,
        "cost_usd": cost,
    }


def test_totals_split_today_week_and_all_time(factory) -> None:
    store = UsageStore(factory)
    store.record(_usage(100, 10, 0.001), ts=_NOW - 3600, news_id="a", verdict="ok")  # today
    store.record(_usage(200, 20, 0.002), ts=_NOW - 2 * 86400, news_id="b", verdict="skip")  # week
    store.record(_usage(400, 40, None, "x"), ts=_NOW - 30 * 86400, news_id="c", verdict="ok")
    totals = usage_totals(factory, now=_NOW)
    assert totals["today"]["calls"] == 1
    assert totals["today"]["input_tokens"] == 100
    assert totals["last_7_days"]["calls"] == 2
    assert totals["last_7_days"]["output_tokens"] == 30
    assert totals["last_7_days"]["cost_usd"] == pytest.approx(0.003)
    assert totals["all_time"]["calls"] == 3
    assert totals["all_time"]["input_tokens"] == 700
    assert totals["all_time"]["unpriced_calls"] == 1


def test_today_is_the_utc_calendar_day(factory) -> None:
    store = UsageStore(factory)
    midnight = datetime(2026, 10, 13, tzinfo=UTC).timestamp()
    store.record(_usage(1, 1, 0.0), ts=midnight - 1, news_id="y", verdict="ok")
    store.record(_usage(1, 1, 0.0), ts=midnight + 1, news_id="t", verdict="ok")
    assert usage_totals(factory, now=_NOW)["today"]["calls"] == 1


def test_unconfigured_store_records_nothing() -> None:
    UsageStore().record(_usage(1, 1, 0.0), ts=_NOW, news_id="a", verdict="ok")  # no raise


def test_usage_endpoint(factory) -> None:
    from openpoly.api.main import app

    UsageStore(factory).record(_usage(100, 10, 0.5), ts=_NOW, news_id="a", verdict="ok")
    app.dependency_overrides[get_session_factory] = lambda: factory
    try:
        r = TestClient(app).get("/api/analyzer/usage")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"today", "last_7_days", "all_time"}
    assert body["all_time"]["calls"] == 1
    assert body["all_time"]["cost_usd"] == pytest.approx(0.5)


def test_migration_creates_the_table_and_is_idempotent(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path}/legacy.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE something_old (id INTEGER PRIMARY KEY)"))
    run_migrations(engine)
    assert "llm_usage" in inspect(engine).get_table_names()
    run_migrations(engine)  # re-run: no-op
    with engine.begin() as conn:
        from openpoly.db.migrations import _m005_llm_usage_table

        _m005_llm_usage_table(conn)  # table already there: still a no-op
    engine.dispose()


# ---------- orchestrator records usage ----------


async def test_orchestrator_records_usage_on_the_log_entry_and_the_table(
    factory, monkeypatch
) -> None:
    from openpoly.llm import usage_store as usage_mod
    from openpoly.runtime import orchestrator as orch_mod
    from openpoly.runtime.section_log import AnalyzerCall, SectionLogStore
    from openpoly.sections._base import SectionOutput
    from tests.test_orchestrator import _item

    monkeypatch.setattr(usage_mod.usage_store, "_factory", factory)
    usage = _usage(1234, 56, 0.0015, model="claude-haiku-5-5")

    class _Analyzer:
        def run(self, _input):  # noqa: ANN001, ANN201
            return SectionOutput(payload=None, verdict="skip", signals={"llm_usage": usage})

    log: SectionLogStore[AnalyzerCall] = SectionLogStore("analyzer")
    orch = orch_mod.PipelineOrchestrator.__new__(orch_mod.PipelineOrchestrator)
    orch._analyzer = _Analyzer()  # type: ignore[attr-defined]
    orch._analyzer_log = log  # type: ignore[attr-defined]
    orch._background = set()  # type: ignore[attr-defined]
    await orch._run_analyzer(_item(), None)  # type: ignore[arg-type]
    await orch.flush_background()  # the insert is fire-and-forget

    entry = log.entries()[0]
    assert entry.input_tokens == 1234
    assert entry.output_tokens == 56
    assert entry.cost_usd == pytest.approx(0.0015)
    assert entry.llm_model == "claude-haiku-5-5"
    totals = usage_totals(factory)
    assert totals["all_time"]["calls"] == 1
    assert totals["all_time"]["input_tokens"] == 1234


def test_windowed_totals_use_the_ts_index(factory) -> None:
    """The today / last-7-days aggregates must range-scan the ``ts`` index
    rather than read the whole table (EXPLAIN QUERY PLAN on every statement
    ``usage_totals`` runs that filters on ``ts``)."""
    from sqlalchemy import event

    store = UsageStore(factory)
    store.record(_usage(1, 1, 0.0), ts=_NOW - 60, news_id="a", verdict="ok")
    engine = factory.kw["bind"]
    statements: list[tuple[str, object]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        usage_totals(factory, now=_NOW)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    windowed = [(s, p) for s, p in statements if "ts >=" in s]
    assert windowed, statements
    with engine.connect() as conn:
        for statement, params in windowed:
            plan = " | ".join(
                str(r[-1]) for r in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", params)
            )
            assert "SEARCH llm_usage USING" in plan and "INDEX" in plan, plan


async def test_usage_write_does_not_delay_the_entry_decision(monkeypatch) -> None:
    """A slow ``llm_usage`` insert (a locked SQLite file) must not hold the
    analyzer result back from the entry stage: the write runs in the
    background and is flushed on stop."""
    import asyncio
    import threading

    from openpoly.llm import usage_store as usage_mod
    from openpoly.runtime import orchestrator as orch_mod
    from openpoly.runtime.section_log import AnalyzerCall, SectionLogStore
    from openpoly.sections._base import SectionOutput
    from openpoly.sections.analyzer.llm_v0 import AnalysisResult
    from tests.test_orchestrator import _item

    release = threading.Event()
    recorded: list[str] = []

    def _slow_record(usage, *, ts, news_id, verdict):  # noqa: ANN001, ANN202
        release.wait(timeout=5)
        recorded.append(news_id)

    monkeypatch.setattr(usage_mod.usage_store, "record", _slow_record)
    ar = AnalysisResult(market_id="m1", p_model=0.7, confidence="high")

    class _Analyzer:
        def run(self, _input):  # noqa: ANN001, ANN201
            return SectionOutput(payload=ar, verdict="ok", signals={"llm_usage": _usage(1, 1, 0.0)})

    orch = orch_mod.PipelineOrchestrator(
        embedding_section=None,  # type: ignore[arg-type]
        analyzer_section=_Analyzer(),  # type: ignore[arg-type]
        entry_section=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
        embedding_log_store=SectionLogStore("embedding"),
        analyzer_log_store=SectionLogStore[AnalyzerCall]("analyzer"),
        entry_log_store=SectionLogStore("entry"),
    )
    try:
        got = await asyncio.wait_for(orch._run_analyzer(_item(), None), timeout=1.0)  # type: ignore[arg-type]
        assert got is ar
        assert recorded == []
    finally:
        release.set()
        await orch.stop()
    assert recorded == ["n1"]

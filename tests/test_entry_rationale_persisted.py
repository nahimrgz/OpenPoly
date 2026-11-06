"""The analyzer's rationale survives a restart.

``analyzer_log`` is an in-memory ring, so a backend restart wiped the reason
behind every open position. The entry decision now freezes the rationale and
the news direction onto the position row, and the position routes fall back to
them when the ring has nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from openpoly.api.main import app
from openpoly.api.portfolio_routes import get_portfolio_store
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.migrations import LATEST_VERSION, run_migrations
from openpoly.portfolio import PortfolioStore
from openpoly.runtime.section_log import analyzer_log
from openpoly.sections._base import SectionInput
from openpoly.sections.analyzer.llm_v0 import AnalysisResult
from openpoly.sections.entry.edge_threshold_v1 import (
    EdgeThresholdConfigV1,
    EdgeThresholdEntryV1,
    OrderIntent,
)
from tests.test_db_migrations import _columns, _legacy_engine
from tests.test_section_entry_edge import (  # noqa: F401 — autouse fixture
    _book,
    _isolate_market_store,
    _market,
)
from tests.test_section_entry_edge import _populate as _populate_market


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    store = PortfolioStore(make_session_factory(engine))
    app.dependency_overrides[get_portfolio_store] = lambda: store
    saved = list(analyzer_log.entries())
    analyzer_log.reset()  # the state right after a restart
    yield store, TestClient(app)
    analyzer_log.reset()
    for entry in saved:
        analyzer_log.append(entry)
    app.dependency_overrides.clear()
    engine.dispose()


def _open(store: PortfolioStore, **over):
    return store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xm1",
        price=0.42,
        qty=20.0,
        ts=100.0,
        news_id="n1",
        entry_p_model=0.68,
        entry_confidence="medium",
        **over,
    )


def test_entry_intent_carries_the_rationale_and_direction() -> None:
    _populate_market(_market(), _book("yes-m1", 0.49, 0.50), _book("no-m1", 0.48, 0.49))
    res = AnalysisResult(
        market_id="m1",
        p_model=0.70,
        confidence="medium",
        rationale="Filing confirms it.",
        news_direction="raises_yes",
    )
    inst = EdgeThresholdEntryV1(EdgeThresholdConfigV1())
    out = inst.run(SectionInput(tick_type="event", payload=res))
    assert isinstance(out.payload, OrderIntent)
    assert out.payload.rationale == "Filing confirms it."
    assert out.payload.news_direction == "raises_yes"


def test_store_persists_and_returns_them(env) -> None:
    store, _client = env
    h = _open(store, entry_rationale="Filing confirms it.", entry_news_direction="raises_yes")
    rec = store.get_position(h.position_id)
    assert rec is not None
    assert rec.entry_rationale == "Filing confirms it."
    assert rec.entry_news_direction == "raises_yes"


def test_a_position_without_a_decision_has_neither(env) -> None:
    store, _client = env
    rec = store.get_position(_open(store).position_id)
    assert rec is not None
    assert rec.entry_rationale is None
    assert rec.entry_news_direction is None


@pytest.mark.parametrize("route", ["detail", "list"])
def test_routes_fall_back_to_the_stored_rationale_after_a_restart(env, route: str) -> None:
    store, client = env
    h = _open(store, entry_rationale="Filing confirms it.", entry_news_direction="raises_yes")
    if route == "detail":
        body = client.get(f"/api/positions/{h.position_id}").json()
    else:
        body = client.get("/api/positions").json()["positions"][0]
    assert body["analyzer_decisions"] == [
        {"rationale": "Filing confirms it.", "p_model": 0.68, "confidence": "medium", "ts": 100.0}
    ]
    assert body["entry_news_direction"] == "raises_yes"


def test_migrated_and_fresh_databases_have_the_same_columns(tmp_path) -> None:
    migrated = _legacy_engine(tmp_path, "migrated.db")
    run_migrations(migrated)
    fresh = make_engine(f"sqlite:///{tmp_path}/fresh.db")
    init_db(fresh)
    for table in ("position", "shadow_pick"):
        assert _columns(migrated, table) == _columns(fresh, table)
    migrated.dispose()
    fresh.dispose()


def test_migration_adds_the_columns_and_keeps_existing_rows(tmp_path) -> None:
    engine = _legacy_engine(tmp_path, "rationale.db")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO position (market_id, side, token_id, condition_id, qty, "
                "avg_entry_price, status, opened_at) "
                "VALUES ('m1', 'yes', 't1', '0xm1', 20.0, 0.42, 'open', 100.0)"
            )
        )
    assert run_migrations(engine) == LATEST_VERSION
    assert run_migrations(engine) == LATEST_VERSION  # re-run is a no-op
    assert {"entry_rationale", "entry_news_direction"} <= _columns(engine, "position")
    assert "taker_fee_rate" in _columns(engine, "shadow_pick")
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT market_id, qty, entry_rationale, entry_news_direction FROM position")
        ).one()
    assert tuple(row) == ("m1", 20.0, None, None)
    engine.dispose()

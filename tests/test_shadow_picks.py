"""Shadow picks: analyzer picks discarded by ``min_confidence`` are persisted
with the side / ask / edge the entry would have used (``shadow_pick`` table,
migration 6), and ``GET /api/analytics/shadow`` reports what they would have
returned from later order-book snapshots."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect, text

from openpoly.analytics.shadow import ShadowStore, choose_shadow_side, shadow_report
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.migrations import LATEST_VERSION, run_migrations
from openpoly.db.tables import OrderBookSnapshot, ShadowPickRow
from openpoly.markets.models import OrderBook

_T0 = 1_793_000_000.0
_H = 3600.0


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _book(token: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]):
    return OrderBook(token_id=token, ts=_T0, bids=bids, asks=asks)


# ---------- side choice (S2) ----------


def test_side_is_the_larger_edge_on_depth_guarded_asks() -> None:
    # p 0.40: YES fair 0.40 vs ask 0.30 -> edge 0.10; NO fair 0.60 vs ask 0.55 -> 0.05.
    q = choose_shadow_side(
        0.40,
        yes=("y", _book("y", [(0.29, 50)], [(0.30, 50)])),
        no=("n", _book("n", [(0.54, 50)], [(0.55, 50)])),
    )
    assert q.side == "yes" and q.token_id == "y"
    assert q.ask == pytest.approx(0.30)
    assert q.bid == pytest.approx(0.29)
    assert q.edge == pytest.approx(0.10)


def test_dust_ask_is_skipped_for_the_first_level_with_size() -> None:
    # A 1-share YES ask at 0.10 would fake a 0.30 edge; the real YES ask is 0.45.
    q = choose_shadow_side(
        0.40,
        yes=("y", _book("y", [(0.40, 50)], [(0.10, 1), (0.45, 50)])),
        no=("n", _book("n", [(0.54, 50)], [(0.55, 50)])),
    )
    assert q.side == "no"
    assert q.ask == pytest.approx(0.55)
    assert q.edge == pytest.approx(0.05)


def test_no_usable_ask_keeps_the_pick_without_a_side() -> None:
    q = choose_shadow_side(0.40, yes=("y", None), no=("n", _book("n", [], [(0.5, 1)])))
    assert q.side is None and q.ask is None and q.edge is None


# ---------- persistence + migration (S1) ----------


def test_migration_6_creates_the_table_on_an_existing_db(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path}/old.db")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE shadow_pick"))
        conn.execute(text("UPDATE schema_version SET version = 5"))
    assert run_migrations(engine) == LATEST_VERSION == 6
    assert "shadow_pick" in inspect(engine).get_table_names()
    assert run_migrations(engine) == 6  # re-run is a no-op
    engine.dispose()


def test_store_records_a_pick(factory) -> None:
    store = ShadowStore(factory)
    q = choose_shadow_side(0.40, yes=("y", _book("y", [(0.29, 50)], [(0.30, 50)])), no=("n", None))
    store.record(ts=_T0, news_id="n1", market_id="m1", p_model=0.40, confidence="low", quote=q)
    with factory() as s:
        rows = s.query(ShadowPickRow).all()
    assert len(rows) == 1
    r = rows[0]
    assert (r.market_id, r.side, r.token_id, r.confidence) == ("m1", "yes", "y", "low")
    assert r.ask == pytest.approx(0.30) and r.edge == pytest.approx(0.10)


def test_unconfigured_store_records_nothing() -> None:
    ShadowStore(None).record(
        ts=_T0, news_id="n", market_id="m", p_model=0.5, confidence="low", quote=None
    )


async def test_orchestrator_persists_a_below_min_confidence_skip(factory, monkeypatch) -> None:
    from openpoly.analytics import shadow as shadow_mod
    from openpoly.runtime import orchestrator as orch_mod
    from openpoly.runtime.section_log import AnalyzerCall, SectionLogStore
    from openpoly.sections._base import SectionOutput
    from tests.test_orchestrator import _item

    monkeypatch.setattr(shadow_mod.shadow_store, "_factory", factory)

    class _Analyzer:
        def run(self, _input):  # noqa: ANN001, ANN201
            return SectionOutput(
                payload=None,
                verdict="skip",
                reason="below min_confidence",
                signals={"market_id": "m1", "p_yes": 0.4, "confidence": "low"},
            )

    orch = orch_mod.PipelineOrchestrator.__new__(orch_mod.PipelineOrchestrator)
    orch._analyzer = _Analyzer()  # type: ignore[attr-defined]
    orch._analyzer_log = SectionLogStore[AnalyzerCall]("analyzer")  # type: ignore[attr-defined]
    orch._background = set()  # type: ignore[attr-defined]
    orch._book_source = None  # type: ignore[attr-defined]
    await orch._run_analyzer(_item(), None)  # type: ignore[arg-type]
    await orch.flush_background()

    with factory() as s:
        rows = s.query(ShadowPickRow).all()
    assert [(r.market_id, r.confidence, r.p_model) for r in rows] == [("m1", "low", 0.4)]
    assert rows[0].side is None  # no book source -> no quote, still recorded


async def test_orchestrator_ignores_other_skips(factory, monkeypatch) -> None:
    from openpoly.analytics import shadow as shadow_mod
    from openpoly.runtime import orchestrator as orch_mod
    from openpoly.runtime.section_log import AnalyzerCall, SectionLogStore
    from openpoly.sections._base import SectionOutput
    from tests.test_orchestrator import _item

    monkeypatch.setattr(shadow_mod.shadow_store, "_factory", factory)

    class _Analyzer:
        def run(self, _input):  # noqa: ANN001, ANN201
            return SectionOutput(payload=None, verdict="skip", reason="no actionable market")

    orch = orch_mod.PipelineOrchestrator.__new__(orch_mod.PipelineOrchestrator)
    orch._analyzer = _Analyzer()  # type: ignore[attr-defined]
    orch._analyzer_log = SectionLogStore[AnalyzerCall]("analyzer")  # type: ignore[attr-defined]
    orch._background = set()  # type: ignore[attr-defined]
    orch._book_source = None  # type: ignore[attr-defined]
    await orch._run_analyzer(_item(), None)  # type: ignore[arg-type]
    await orch.flush_background()
    with factory() as s:
        assert s.query(ShadowPickRow).count() == 0


# ---------- report (S3, S5) ----------


def _snap(s, token: str, at: float, bid: float, size: float = 50) -> None:
    s.add(
        OrderBookSnapshot(
            token_id=token,
            recorded_at=at,
            bids_json=json.dumps([[bid, size]]),
            asks_json=json.dumps([[bid + 0.01, size]]),
        )
    )


def test_report_marks_horizons_and_groups_by_confidence(factory) -> None:
    store = ShadowStore(factory)
    q = choose_shadow_side(0.6, yes=("y", _book("y", [(0.39, 50)], [(0.40, 50)])), no=("n", None))
    store.record(ts=_T0, news_id="a", market_id="m", p_model=0.6, confidence="low", quote=q)
    with factory() as s:
        _snap(s, "y", _T0 + 6 * _H + 30, 0.50)  # +6h: +25%
        _snap(s, "y", _T0 + 24 * _H + 30, 0.30)  # +24h: -25%
        s.commit()
    now = _T0 + 30 * _H  # +48h not reached yet
    rep = shadow_report(factory, now=now, positions=[], cost_basis={})
    low = rep["shadow"]["low"]
    assert low["picks"] == 1
    assert low["horizons"]["6h"]["count"] == 1
    assert low["horizons"]["6h"]["mean_return"] == pytest.approx(0.25)
    assert low["horizons"]["6h"]["win_rate"] == pytest.approx(1.0)
    assert low["horizons"]["24h"]["mean_return"] == pytest.approx(-0.25)
    assert low["horizons"]["48h"]["count"] == 0
    assert low["horizons"]["48h"]["pending"] == 1
    assert low["horizons"]["latest"]["mean_return"] == pytest.approx(-0.25)


def test_report_counts_a_horizon_without_snapshot_as_no_data(factory) -> None:
    store = ShadowStore(factory)
    q = choose_shadow_side(0.6, yes=("y", _book("y", [], [(0.40, 50)])), no=("n", None))
    store.record(ts=_T0, news_id="a", market_id="m", p_model=0.6, confidence="low", quote=q)
    rep = shadow_report(factory, now=_T0 + 100 * _H, positions=[], cost_basis={})
    h6 = rep["shadow"]["low"]["horizons"]["6h"]
    assert h6["count"] == 0 and h6["no_data"] == 1


def test_report_includes_real_positions_by_entry_confidence(factory) -> None:
    from types import SimpleNamespace as NS

    positions = [
        NS(id=1, status="closed", entry_confidence="low", realized_pnl=-2.0),
        NS(id=2, status="closed", entry_confidence="low", realized_pnl=1.0),
        NS(id=3, status="closed", entry_confidence="medium", realized_pnl=1.5),
        NS(id=4, status="open", entry_confidence="medium", realized_pnl=None),
    ]
    rep = shadow_report(
        factory, now=_T0, positions=positions, cost_basis={1: 10.0, 2: 10.0, 3: 10.0}
    )
    real = rep["real"]
    assert real["low"]["count"] == 2
    assert real["low"]["mean_return"] == pytest.approx(-0.05)
    assert real["low"]["win_rate"] == pytest.approx(0.5)
    assert real["medium"]["count"] == 1
    assert real["medium"]["mean_return"] == pytest.approx(0.15)


def test_endpoint_serves_the_report(factory) -> None:
    from fastapi.testclient import TestClient

    from openpoly.api.main import app
    from openpoly.api.portfolio_routes import get_portfolio_store
    from openpoly.db.engine import get_session_factory
    from openpoly.portfolio import PortfolioStore

    q = choose_shadow_side(0.6, yes=("y", _book("y", [], [(0.40, 50)])), no=("n", None))
    ShadowStore(factory).record(
        ts=_T0, news_id="a", market_id="m", p_model=0.6, confidence="low", quote=q
    )
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_portfolio_store] = lambda: PortfolioStore(factory)
    try:
        r = TestClient(app).get("/api/analytics/shadow")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"shadow", "real", "horizons_hours", "generated_at"}
    assert body["shadow"]["low"]["picks"] == 1


def test_script_reads_a_db_file_and_renders(tmp_path, capsys) -> None:
    import importlib.util
    from pathlib import Path

    db = tmp_path / "cli.db"
    engine = make_engine(f"sqlite:///{db}")
    init_db(engine)
    f = make_session_factory(engine)
    q = choose_shadow_side(0.6, yes=("y", _book("y", [], [(0.40, 50)])), no=("n", None))
    ShadowStore(f).record(
        ts=_T0, news_id="a", market_id="m", p_model=0.6, confidence="low", quote=q
    )
    engine.dispose()

    path = Path(__file__).resolve().parent.parent / "scripts" / "shadow_report.py"
    spec = importlib.util.spec_from_file_location("shadow_report_cli", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "low: 1 picks" in out
    assert "Real closed positions" in out

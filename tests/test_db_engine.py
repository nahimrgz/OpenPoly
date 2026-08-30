"""Tests for openpoly.db.engine — engine / session / bootstrap (SQLite)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from openpoly.db.engine import (
    DEFAULT_DB_URL,
    database_url,
    init_db,
    make_engine,
    make_session_factory,
)


def test_make_engine_and_session_smoke():
    engine = make_engine("sqlite:///:memory:")
    factory = make_session_factory(engine)
    with factory() as session:
        result = session.execute(text("SELECT 1")).scalar_one()
    assert result == 1
    engine.dispose()


def test_init_db_runs():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)  # creates registered tables; must not raise
    engine.dispose()


def test_database_url_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENPOLY_DB_URL", raising=False)
    assert database_url() == DEFAULT_DB_URL


def test_database_url_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENPOLY_DB_URL", "sqlite:///custom.db")
    assert database_url() == "sqlite:///custom.db"
    assert str(make_engine().url) == "sqlite:///custom.db"


def test_init_db_creates_fill_with_order_id_tx_hash(tmp_path) -> None:
    """Fresh DB: order_id + tx_hash columns exist via create_all."""
    from sqlalchemy import text

    engine = make_engine(f"sqlite:///{tmp_path}/x.db")
    init_db(engine)
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(fill)")).fetchall()}
    assert "order_id" in cols
    assert "tx_hash" in cols


def test_position_table_has_entry_calibration_columns(tmp_path) -> None:
    """A new DB gets the entry-signal columns straight from create_all."""
    from sqlalchemy import text

    engine = make_engine(f"sqlite:///{tmp_path}/cal.db")
    init_db(engine)
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(position)")).fetchall()}
    assert {"entry_p_model", "entry_confidence", "entry_edge"} <= cols

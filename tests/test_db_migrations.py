"""Tests for openpoly.db.migrations — the versioned schema migration runner."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from openpoly.db.engine import init_db, make_engine
from openpoly.db.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION_TABLE,
    current_version,
    run_migrations,
)

# --- legacy (pre-schema_version) DDL, verbatim from the old create_all shape ---

_LEGACY_FILL = """
CREATE TABLE fill (
    id INTEGER PRIMARY KEY,
    ts FLOAT NOT NULL,
    market_id VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    price FLOAT NOT NULL,
    qty FLOAT NOT NULL,
    fee FLOAT NOT NULL,
    position_id INTEGER NOT NULL,
    news_id VARCHAR,
    "trigger" VARCHAR
)
"""

_LEGACY_POSITION = """
CREATE TABLE position (
    id INTEGER PRIMARY KEY,
    market_id VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    token_id VARCHAR NOT NULL,
    condition_id VARCHAR NOT NULL,
    qty FLOAT NOT NULL,
    avg_entry_price FLOAT NOT NULL,
    status VARCHAR NOT NULL,
    opened_at FLOAT NOT NULL,
    closed_at FLOAT,
    close_reason VARCHAR,
    realized_pnl FLOAT
)
"""

_LEGACY_ORDER_BOOK = """
CREATE TABLE order_book_snapshot (
    id INTEGER PRIMARY KEY,
    token_id VARCHAR NOT NULL,
    recorded_at FLOAT NOT NULL,
    bids_json VARCHAR NOT NULL,
    asks_json VARCHAR NOT NULL
)
"""


def _columns(engine, table: str) -> set[str]:
    with engine.begin() as conn:
        return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _indexes(engine, table: str) -> set[str]:
    with engine.begin() as conn:
        return {r[1] for r in conn.execute(text(f"PRAGMA index_list({table})")).fetchall()}


def _legacy_engine(tmp_path, name: str = "legacy.db"):
    """A DB shaped like an older openPoly: application tables, no schema_version."""
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_FILL))
        conn.execute(text(_LEGACY_POSITION))
        conn.execute(text(_LEGACY_ORDER_BOOK))
    return engine


# ---------- registry invariants ----------


def test_migrations_are_ordered_and_unique() -> None:
    versions = [v for v, _ in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[0] == 1
    assert LATEST_VERSION == versions[-1]


# ---------- fresh DB ----------


def test_fresh_db_is_stamped_to_latest(tmp_path) -> None:
    """create_all on an empty file stamps the latest version — no migration
    needs to run, the schema is already current."""
    engine = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    init_db(engine)
    assert current_version(engine) == LATEST_VERSION
    engine.dispose()


def test_fresh_db_run_migrations_is_a_noop(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    init_db(engine)
    assert run_migrations(engine) == LATEST_VERSION
    engine.dispose()


# ---------- legacy DB ----------


def test_legacy_db_has_no_version_table_before_migrating(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_FILL))
    with engine.begin() as conn:
        names = {
            r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert SCHEMA_VERSION_TABLE not in names
    engine.dispose()


def test_legacy_db_migrates_to_latest_and_is_idempotent(tmp_path) -> None:
    """A DB predating schema_version migrates cleanly, and running twice is a
    no-op (each migration PRAGMA-checks before it alters)."""
    engine = _legacy_engine(tmp_path)
    for _ in range(2):
        assert run_migrations(engine) == LATEST_VERSION
        assert current_version(engine) == LATEST_VERSION
        assert {"order_id", "tx_hash"} <= _columns(engine, "fill")
        assert {"entry_p_model", "entry_confidence", "entry_edge"} <= _columns(engine, "position")
        assert "ix_order_book_snapshot_token_recorded" in _indexes(engine, "order_book_snapshot")
    engine.dispose()


def test_legacy_db_that_already_has_new_columns_migrates(tmp_path) -> None:
    """The old hand-rolled ``_ensure_*`` path already added the columns on some
    deployed DBs; the migration must recognise that and only stamp."""
    engine = _legacy_engine(tmp_path, "half.db")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE fill ADD COLUMN order_id VARCHAR"))
        conn.execute(text("ALTER TABLE fill ADD COLUMN tx_hash VARCHAR"))
        conn.execute(text("ALTER TABLE position ADD COLUMN entry_p_model FLOAT"))
    assert run_migrations(engine) == LATEST_VERSION
    assert {"order_id", "tx_hash"} <= _columns(engine, "fill")
    assert {"entry_p_model", "entry_confidence", "entry_edge"} <= _columns(engine, "position")
    engine.dispose()


def test_init_db_then_run_migrations_on_legacy_db(tmp_path) -> None:
    """The real bootstrap order: create_all fills in missing tables, then the
    runner migrates the pre-existing ones."""
    engine = _legacy_engine(tmp_path, "boot.db")
    init_db(engine)
    assert run_migrations(engine) == LATEST_VERSION
    assert {"order_id", "tx_hash"} <= _columns(engine, "fill")
    engine.dispose()


# ---------- failure ----------


def test_failing_migration_leaves_version_unchanged(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration that raises must not advance (or partially advance) the
    recorded version — the next start retries it from the same point."""
    engine = _legacy_engine(tmp_path, "boom.db")

    def _boom(_conn) -> None:
        raise RuntimeError("migration exploded")

    first_version, first_fn = MIGRATIONS[0]
    monkeypatch.setattr(
        "openpoly.db.migrations.MIGRATIONS",
        [(first_version, first_fn), (first_version + 1, _boom)],
    )
    with pytest.raises(RuntimeError, match="migration exploded"):
        run_migrations(engine)
    assert current_version(engine) == first_version
    engine.dispose()


def test_failing_first_migration_leaves_version_at_zero(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _legacy_engine(tmp_path, "boom0.db")

    def _boom(_conn) -> None:
        raise RuntimeError("nope")

    monkeypatch.setattr("openpoly.db.migrations.MIGRATIONS", [(1, _boom)])
    with pytest.raises(RuntimeError):
        run_migrations(engine)
    assert current_version(engine) == 0
    engine.dispose()

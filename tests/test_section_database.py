"""Tests for the database section (SqliteDatabase) + registry discovery."""

from __future__ import annotations

from openpoly.db.manager import DatabaseConfig
from openpoly.sections._base import SectionInput
from openpoly.sections._registry import scan
from openpoly.sections.database.sqlite import SqliteDatabase


def test_section_attrs():
    assert SqliteDatabase.SECTION_TYPE == "database"
    assert SqliteDatabase.SECTION_VERSION == "0.1.0"
    assert SqliteDatabase.REQUIRES == []


def test_run_returns_status_dict():
    out = SqliteDatabase(DatabaseConfig()).run(SectionInput(tick_type="warm"))
    assert out.verdict == "ok"
    assert isinstance(out.payload, dict)
    assert "tables" in out.payload
    assert "writers" in out.payload


def test_contract_test_passes():
    SqliteDatabase.CONTRACT_TEST()  # must not raise


def test_registry_discovers_database_section():
    db_entries = [e for e in scan() if e.type == "database"]
    assert len(db_entries) == 1
    assert db_entries[0].name == "SqliteDatabase"
    assert db_entries[0].version == "0.1.0"


def test_section_construction_does_not_mutate_the_manager_config():
    """Constructing a section is not a lifecycle event. The manager singleton is
    process-wide, so a constructor that pushed its config into it would let any
    incidental construction — the registry's contract test, an inspector preview —
    overwrite the window the operator actually deployed."""
    from openpoly.db.manager import manager

    try:
        manager.apply_config(DatabaseConfig(order_book_retention_days=30.0))
        SqliteDatabase(DatabaseConfig(order_book_retention_days=1.0))
        assert manager.status()["retention"]["retention_days"] == 30.0
    finally:
        manager.apply_config(DatabaseConfig())


def test_catalog_scan_leaves_an_applied_retention_intact():
    """The lazy catalog scan runs every section's CONTRACT_TEST, which builds a
    SqliteDatabase with *default* config. That must not roll the live retention
    window back to the default the first time somebody opens the catalog."""
    from openpoly.db.manager import manager

    try:
        manager.apply_config(DatabaseConfig(order_book_retention_days=30.0))
        scan()
        assert manager.status()["retention"]["retention_days"] == 30.0
    finally:
        manager.apply_config(DatabaseConfig())

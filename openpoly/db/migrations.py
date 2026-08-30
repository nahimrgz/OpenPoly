"""Versioned schema migrations.

``create_all`` alone only ever *adds* tables — it never notices that an
existing table is missing a column added later, so every schema change used to
arrive as another hand-rolled ``_ensure_*`` PRAGMA-then-ALTER helper in
``db.manager``, run unconditionally on every start with no record of what had
already been applied. That is a migration system with the bookkeeping left out:
nothing says which changes a given database has seen, a half-applied change
looks identical to a fully-applied one, and a failure is silent.

This module is that bookkeeping, kept deliberately small (Alembic is not a
dependency and a single-file SQLite database does not warrant one):

* ``schema_version`` — one table, one row, one integer: the highest migration
  applied to this database. Not part of ``Base.metadata`` on purpose, so
  ``create_all`` / ``drop_all`` never own it.
* ``MIGRATIONS`` — an ordered ``(version, fn)`` list. Each ``fn`` takes a
  ``Connection`` and is applied inside its own transaction, with the version
  row written in the same transaction: a migration that raises leaves the
  recorded version exactly where it was, and the next start retries it.
* Every migration is **idempotent** — it PRAGMA-checks before it alters. Some
  deployed databases already had these columns from the old ``_ensure_*`` path
  but have no ``schema_version`` row, so migration 1 must be safe to re-run
  against a database that is effectively already at version N.

A fresh database gets ``create_all`` (which builds the current schema
directly) and is then stamped to ``LATEST_VERSION`` — see ``engine.init_db``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import Connection, Engine, text

logger = logging.getLogger(__name__)

SCHEMA_VERSION_TABLE = "schema_version"

# Composite index backing ``ExitMonitor.bootstrap_peaks`` and the per-token
# history route, both of which filter (token_id, recorded_at) — and the
# retention prune, which deletes by ``recorded_at``. Kept in sync with the
# ``Index(...)`` declared on ``OrderBookSnapshot`` so fresh databases get the
# same index from ``create_all``.
ORDER_BOOK_TOKEN_TIME_INDEX = "ix_order_book_snapshot_token_recorded"

Migration = Callable[[Connection], None]


# ---------- helpers ----------


def _table_columns(conn: Connection, table: str) -> set[str]:
    """Column names of ``table`` — empty set when the table does not exist."""
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _table_exists(conn: Connection, table: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name = :name"),
        {"name": table},
    ).first()
    return row is not None


def _add_missing_columns(
    conn: Connection,
    table: str,
    columns: tuple[tuple[str, str], ...],
) -> None:
    """``ALTER TABLE ADD COLUMN`` for each missing column.

    SQLite's ``ADD COLUMN`` fails when the column already exists, so the
    PRAGMA check is what makes this re-runnable — which it must be, because a
    database migrated by the old ``_ensure_*`` helpers arrives here with the
    columns present and no version row.
    """
    if not _table_exists(conn, table):
        # create_all builds the table at its current shape; nothing to alter.
        return
    existing = _table_columns(conn, table)
    for column, sql_type in columns:
        if column in existing:
            continue
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        logger.info("migration: added %s.%s", table, column)


# ---------- the migrations ----------


def _m001_fill_live_columns(conn: Connection) -> None:
    """Live-execution provenance on the fill ledger (was ``_ensure_fill_live_columns``)."""
    _add_missing_columns(
        conn,
        "fill",
        (("order_id", "VARCHAR"), ("tx_hash", "VARCHAR")),
    )


def _m002_position_entry_columns(conn: Connection) -> None:
    """Entry-signal columns for calibration (was ``_ensure_position_entry_columns``)."""
    _add_missing_columns(
        conn,
        "position",
        (
            ("entry_p_model", "FLOAT"),
            ("entry_confidence", "VARCHAR"),
            ("entry_edge", "FLOAT"),
        ),
    )


def _m003_order_book_token_time_index(conn: Connection) -> None:
    """Composite ``(token_id, recorded_at)`` index on ``order_book_snapshot``.

    The table only had a ``token_id`` index, so both readers that matter —
    peak bootstrap and the per-token history route — scanned every row ever
    recorded for a token to find the ones inside their time window, and the
    retention prune had no index at all to delete by.
    """
    if not _table_exists(conn, "order_book_snapshot"):
        return
    conn.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {ORDER_BOOK_TOKEN_TIME_INDEX} "
            "ON order_book_snapshot (token_id, recorded_at)"
        )
    )


# Ordered, append-only. Never renumber or reuse a version: the number recorded
# in a deployed database is the only thing that says what has run there.
MIGRATIONS: list[tuple[int, Migration]] = [
    (1, _m001_fill_live_columns),
    (2, _m002_position_entry_columns),
    (3, _m003_order_book_token_time_index),
]

LATEST_VERSION: int = MIGRATIONS[-1][0]


# ---------- the version row ----------


def _ensure_version_table(conn: Connection) -> None:
    conn.execute(
        text(f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} (version INTEGER NOT NULL)")
    )


def _read_version(conn: Connection) -> int:
    row = conn.execute(text(f"SELECT version FROM {SCHEMA_VERSION_TABLE}")).first()
    return int(row[0]) if row is not None else 0


def _write_version(conn: Connection, version: int) -> None:
    """Single-row upsert, written inside the caller's transaction."""
    updated = conn.execute(
        text(f"UPDATE {SCHEMA_VERSION_TABLE} SET version = :v"),
        {"v": version},
    ).rowcount
    if not updated:
        conn.execute(
            text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (:v)"),
            {"v": version},
        )


def current_version(engine: Engine) -> int:
    """The highest migration applied to this database (0 = none recorded)."""
    with engine.begin() as conn:
        _ensure_version_table(conn)
        return _read_version(conn)


def stamp_version(engine: Engine, version: int = LATEST_VERSION) -> None:
    """Record ``version`` without running anything.

    Used for a database ``create_all`` just built at the current schema: every
    migration would be a no-op against it, so recording the endpoint directly
    is both correct and the only way a fresh database starts clean.
    """
    with engine.begin() as conn:
        _ensure_version_table(conn)
        _write_version(conn, version)


def run_migrations(engine: Engine) -> int:
    """Apply every migration newer than the recorded version. Returns the new
    version.

    Each migration runs in its own transaction together with the version write,
    so an exception aborts that migration whole and leaves the recorded version
    at the last one that fully succeeded. The exception propagates: a database
    that could not be migrated must not be handed to the writers as if it had
    been.
    """
    version = current_version(engine)
    if version >= LATEST_VERSION:
        return version
    for target, migrate in MIGRATIONS:
        if target <= version:
            continue
        with engine.begin() as conn:
            migrate(conn)
            _write_version(conn, target)
        logger.info("migration %d applied", target)
        version = target
    return version

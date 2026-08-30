"""order_book_snapshot retention — the prune that keeps the one unbounded
table bounded (DatabaseManager.prune_order_books + its hourly loop)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import func, select

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.manager import (
    PRUNE_BATCH_ROWS,
    DatabaseConfig,
    DatabaseManager,
)
from openpoly.db.tables import OrderBookSnapshot

DAY = 86400.0


def _engine(tmp_path, name: str = "retention.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    init_db(engine)
    return engine


def _seed(engine, ages_days: list[float], *, now: float) -> None:
    """One snapshot row per entry, ``age_days`` old."""
    with make_session_factory(engine)() as session:
        for i, age in enumerate(ages_days):
            session.add(
                OrderBookSnapshot(
                    token_id=f"tok-{i % 3}",
                    recorded_at=now - age * DAY,
                    bids_json="[[0.4, 10.0]]",
                    asks_json="[[0.42, 8.0]]",
                )
            )
        session.commit()


def _count(engine) -> int:
    with make_session_factory(engine)() as session:
        return session.execute(select(func.count()).select_from(OrderBookSnapshot)).scalar_one()


def _remaining_ages(engine, now: float) -> list[float]:
    with make_session_factory(engine)() as session:
        rows = session.execute(select(OrderBookSnapshot.recorded_at)).scalars().all()
    return sorted((now - r) / DAY for r in rows)


# ---------- the prune itself ----------


def test_prune_deletes_only_rows_older_than_retention(tmp_path) -> None:
    now = time.time()
    engine = _engine(tmp_path)
    _seed(engine, [0.1, 3.0, 6.9, 7.1, 30.0], now=now)

    mgr = DatabaseManager()
    mgr.configure(engine, DatabaseConfig(order_book_retention_days=7.0))
    deleted = mgr.prune_order_books(now=now)

    assert deleted == 2
    assert _count(engine) == 3
    assert all(age < 7.0 for age in _remaining_ages(engine, now))
    engine.dispose()


def test_prune_is_idempotent(tmp_path) -> None:
    now = time.time()
    engine = _engine(tmp_path)
    _seed(engine, [1.0, 20.0], now=now)
    mgr = DatabaseManager()
    mgr.configure(engine, DatabaseConfig(order_book_retention_days=7.0))

    assert mgr.prune_order_books(now=now) == 1
    assert mgr.prune_order_books(now=now) == 0
    assert _count(engine) == 1
    engine.dispose()


def test_prune_accumulates_the_pruned_rows_counter(tmp_path) -> None:
    now = time.time()
    engine = _engine(tmp_path)
    _seed(engine, [10.0, 11.0], now=now)
    mgr = DatabaseManager()
    mgr.configure(engine, DatabaseConfig(order_book_retention_days=7.0))

    assert mgr.pruned_rows == 0
    mgr.prune_order_books(now=now)
    assert mgr.pruned_rows == 2
    _seed(engine, [12.0], now=now)
    mgr.prune_order_books(now=now)
    assert mgr.pruned_rows == 3
    engine.dispose()


def test_prune_disabled_when_retention_is_zero(tmp_path) -> None:
    now = time.time()
    engine = _engine(tmp_path)
    _seed(engine, [1000.0], now=now)
    mgr = DatabaseManager()
    mgr.configure(engine, DatabaseConfig(order_book_retention_days=0.0))

    assert mgr.prune_order_books(now=now) == 0
    assert _count(engine) == 1
    engine.dispose()


def test_prune_before_start_is_a_noop() -> None:
    assert DatabaseManager().prune_order_books() == 0


def test_prune_batches_the_delete(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """More expired rows than one batch: the delete runs in chunks so the
    single SQLite writer is released between them, and still clears them all."""
    now = time.time()
    engine = _engine(tmp_path)
    monkeypatch.setattr("openpoly.db.manager.PRUNE_BATCH_ROWS", 3)
    _seed(engine, [10.0] * 7 + [1.0], now=now)

    mgr = DatabaseManager()
    mgr.configure(engine, DatabaseConfig(order_book_retention_days=7.0))
    assert mgr.prune_order_books(now=now) == 7
    assert _count(engine) == 1
    engine.dispose()


def test_prune_batch_default_is_five_thousand() -> None:
    assert PRUNE_BATCH_ROWS == 5000


# ---------- status ----------


def test_status_reports_retention_block(tmp_path) -> None:
    now = time.time()
    engine = _engine(tmp_path)
    _seed(engine, [40.0], now=now)
    mgr = DatabaseManager()
    mgr.configure(engine, DatabaseConfig(order_book_retention_days=7.0))
    mgr.prune_order_books(now=now)

    retention = mgr.status()["retention"]
    assert retention["pruned_rows"] == 1
    assert retention["retention_days"] == pytest.approx(7.0)
    assert retention["last_prune_at"] == pytest.approx(now)
    engine.dispose()


def test_status_retention_before_any_prune(tmp_path) -> None:
    mgr = DatabaseManager()
    retention = mgr.status()["retention"]
    assert retention["pruned_rows"] == 0
    assert retention["last_prune_at"] is None


# ---------- lifecycle ----------


async def test_start_prunes_expired_rows(tmp_path) -> None:
    """The sweep runs on start, so a process that was down for a month does
    not wait an hour to reclaim the space."""
    now = time.time()
    engine = make_engine(f"sqlite:///{tmp_path / 'life.db'}")
    init_db(engine)
    _seed(engine, [1.0, 90.0], now=now)

    mgr = DatabaseManager()
    await mgr.start(engine=engine, config=DatabaseConfig(order_book_retention_days=7.0))
    try:
        await mgr.wait_for_prune()
        assert _count(engine) == 1
        assert mgr.pruned_rows == 1
    finally:
        await mgr.stop()
    engine.dispose()


async def test_stop_cancels_the_prune_loop(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'loop.db'}")
    init_db(engine)
    mgr = DatabaseManager()
    await mgr.start(engine=engine)
    await mgr.stop()
    assert mgr.prune_task_running is False
    engine.dispose()


# ---------- index ----------


def test_order_book_snapshot_has_composite_index(tmp_path) -> None:
    from sqlalchemy import text

    engine = _engine(tmp_path, "idx.db")
    with engine.begin() as conn:
        names = {
            r[1] for r in conn.execute(text("PRAGMA index_list(order_book_snapshot)")).fetchall()
        }
    assert "ix_order_book_snapshot_token_recorded" in names
    engine.dispose()


# ---------- retention reconfigured after start ----------


def test_apply_config_changes_the_window_for_the_next_sweep(tmp_path) -> None:
    """The prune reads its window per sweep, so a config applied after start
    takes effect without a restart."""
    now = time.time()
    engine = _engine(tmp_path, "apply.db")
    _seed(engine, [10.0], now=now)
    mgr = DatabaseManager()
    mgr.configure(engine, DatabaseConfig(order_book_retention_days=30.0))
    assert mgr.prune_order_books(now=now) == 0
    mgr.apply_config(DatabaseConfig(order_book_retention_days=7.0))
    assert mgr.prune_order_books(now=now) == 1
    assert _count(engine) == 0


def test_apply_config_zero_disables_the_prune(tmp_path) -> None:
    now = time.time()
    engine = _engine(tmp_path, "apply_zero.db")
    _seed(engine, [400.0], now=now)
    mgr = DatabaseManager()
    mgr.configure(engine, DatabaseConfig(order_book_retention_days=7.0))
    mgr.apply_config(DatabaseConfig(order_book_retention_days=0.0))
    assert mgr.prune_order_books(now=now) == 0
    assert _count(engine) == 1
    assert mgr.status()["retention"]["retention_days"] == 0.0

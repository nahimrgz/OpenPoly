"""Database runtime manager.

Owns the persistence layer's runtime objects — the SQLAlchemy engine, the two
write-behind writers (order book + news), and the retention prune loop. Lifted
out of the FastAPI lifespan so the ``database`` section has a manager to back
it, mirroring ``MarketSourceManager`` / ``NewsSourceManager``.

Schema evolution lives in ``openpoly.db.migrations``: ``start`` runs
``init_db`` (create_all + stamp a fresh database) then ``run_migrations``,
which is the only thing that can alter a table an older process created.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Engine, func, select, text

from openpoly.db.book_store import make_order_book_sink
from openpoly.db.engine import get_engine, init_db, make_session_factory
from openpoly.db.migrations import run_migrations
from openpoly.db.news_store import make_news_sink
from openpoly.db.tables import (
    FillRow,
    NewsItemRow,
    OrderBookSnapshot,
    PositionRow,
)
from openpoly.db.writer import WriteBehindWriter
from openpoly.markets.models import OrderBook
from openpoly.news.ring_buffer import NewsItem

logger = logging.getLogger(__name__)


# How often the retention sweep runs. Order-book rows accumulate at the book
# sampler's pace (one row per tracked token per sample), so nothing about this
# is latency-sensitive — hourly keeps each sweep's delete set small enough that
# the single SQLite writer is never held for long.
PRUNE_INTERVAL_SECONDS = 3600.0

# Rows deleted per statement. SQLite has one writer: a single unbounded DELETE
# over months of snapshots holds that writer (and the WAL) for as long as it
# takes, stalling the executor's synchronous fill writes behind it. Batching
# means the lock is released between chunks.
PRUNE_BATCH_ROWS = 5000

SECONDS_PER_DAY = 86400.0


class DatabaseConfig(BaseModel):
    """Config for the ``database`` section.

    The persistence wiring itself (one SQLite file, two write-behind writers)
    is fixed system infrastructure. The one tunable is retention: order-book
    snapshots are the only table that grows without bound, and they are
    sampling telemetry rather than a ledger — the fill / position tables are
    the record that must never be dropped.
    """

    order_book_retention_days: float = Field(
        default=7.0,
        ge=0.0,
        description=(
            "Delete order_book_snapshot rows older than this many days. 0 "
            "disables the prune entirely (rows are kept forever). The window "
            "has to outlive the longest-held position, because peak bootstrap "
            "rebuilds a trailing stop from snapshots taken since the position "
            "opened."
        ),
    )


class DatabaseManager:
    """Owns the persistence runtime: the engine + the two write-behind writers.

    Lifecycle (start / stop) is driven by the FastAPI lifespan. Backs the
    ``database`` section; ``status`` powers its inspector.
    """

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._config = DatabaseConfig()
        self._book_writer: WriteBehindWriter | None = None
        self._news_writer: WriteBehindWriter | None = None
        # Retention prune loop.
        self._prune_task: asyncio.Task[None] | None = None
        self._prune_stop = asyncio.Event()
        self._pruned_once = asyncio.Event()
        self._pruned_rows = 0
        self._last_prune_at: float | None = None

    # ---------- lifecycle ----------

    def configure(self, engine: Engine, config: DatabaseConfig | None = None) -> None:
        """Bind the engine + section config without starting anything.

        ``start`` calls this first; it is also the seam for anything that needs
        the read/prune side against a specific engine without the write-behind
        writers running (the same shape as ``ExitMonitor.configure``).
        """
        self._engine = engine
        self._config = config or DatabaseConfig()

    def apply_config(self, config: DatabaseConfig) -> None:
        """Swap the section config without touching the engine or the writers.

        Retention is the only tunable, and the prune reads its window at the
        start of every sweep — so a config applied while the loop is running
        takes effect on the next sweep instead of needing a restart. A plain
        attribute assignment is all the synchronization this needs: the prune
        runs in a worker thread but only ever *reads* the reference, and CPython
        rebinds it atomically, so a sweep sees either the old config or the new
        one and never a half-applied mix.
        """
        self._config = config

    async def start(
        self,
        engine: Engine | None = None,
        config: DatabaseConfig | None = None,
    ) -> None:
        """Create the engine + schema + write-behind writers and start them.

        ``engine`` overrides the process engine — tests pass a throwaway one.
        Schema bootstrap is two steps: ``init_db`` creates any table that does
        not exist yet (and stamps a brand-new database at the latest version),
        then ``run_migrations`` alters the tables an older process created. A
        migration that fails raises out of here — the writers must not be
        pointed at a database whose schema is unknown.
        """
        self.configure(engine or get_engine(), config)
        assert self._engine is not None  # narrowed by configure
        init_db(self._engine)
        run_migrations(self._engine)
        factory = make_session_factory(self._engine)
        self._book_writer = WriteBehindWriter(make_order_book_sink(factory))
        self._news_writer = WriteBehindWriter(make_news_sink(factory))
        await self._book_writer.start()
        await self._news_writer.start()
        self._prune_stop = asyncio.Event()
        self._pruned_once = asyncio.Event()
        self._prune_task = asyncio.create_task(self._prune_loop())

    async def stop(self) -> None:
        """Stop the prune loop and both writers, flushing whatever is queued."""
        if self._prune_task is not None:
            self._prune_stop.set()
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass
            finally:
                self._prune_task = None
        if self._book_writer is not None:
            await self._book_writer.stop()
        if self._news_writer is not None:
            await self._news_writer.stop()

    async def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self.stop()

    # ---------- retention ----------

    @property
    def pruned_rows(self) -> int:
        """Order-book snapshot rows deleted by retention this process."""
        return self._pruned_rows

    @property
    def prune_task_running(self) -> bool:
        return self._prune_task is not None and not self._prune_task.done()

    async def wait_for_prune(self, timeout: float = 5.0) -> None:
        """Block until the prune loop has completed its first sweep."""
        await asyncio.wait_for(self._pruned_once.wait(), timeout=timeout)

    def prune_order_books(self, now: float | None = None) -> int:
        """Delete ``order_book_snapshot`` rows past the retention window.

        Synchronous and batched: ``PRUNE_BATCH_ROWS`` ids per statement, each
        its own transaction, so the single SQLite writer is handed back between
        chunks instead of being held for the whole delete. Returns the number
        of rows removed (0 when retention is disabled or the manager has no
        engine yet).
        """
        if self._engine is None:
            return 0
        retention_days = self._config.order_book_retention_days
        if retention_days <= 0:
            return 0
        stamp = time.time() if now is None else now
        cutoff = stamp - retention_days * SECONDS_PER_DAY
        deleted = 0
        # Bail between batches once shutdown starts: stop() cancels only the
        # awaiting task — this worker thread runs on regardless — and a large
        # backlog sweep would otherwise keep contending with the writers' final
        # flush (and hold process exit) until it finished on its own.
        while not self._prune_stop.is_set():
            with self._engine.begin() as conn:
                removed = conn.execute(
                    text(
                        "DELETE FROM order_book_snapshot WHERE id IN ("
                        "  SELECT id FROM order_book_snapshot"
                        "  WHERE recorded_at < :cutoff LIMIT :batch"
                        ")"
                    ),
                    {"cutoff": cutoff, "batch": PRUNE_BATCH_ROWS},
                ).rowcount
            deleted += removed
            if removed < PRUNE_BATCH_ROWS:
                break
        self._pruned_rows += deleted
        self._last_prune_at = stamp
        if deleted:
            logger.info(
                "retention: pruned %d order_book_snapshot rows older than %.1f days",
                deleted,
                retention_days,
            )
        return deleted

    async def _prune_loop(self) -> None:
        """Prune on start, then once an hour until stopped.

        Pruning on start matters: a process that was down for a week comes back
        to a table holding a week of rows nobody will read, and waiting an hour
        to reclaim that is pure downside. The delete is offloaded to a thread —
        it is blocking DB work and the event loop is also serving requests.
        """
        while not self._prune_stop.is_set():
            try:
                await asyncio.to_thread(self.prune_order_books)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — retention must never kill the loop
                logger.exception("retention prune failed")
            finally:
                self._pruned_once.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._prune_stop.wait(), timeout=PRUNE_INTERVAL_SECONDS)

    # ---------- persist hooks (wired into the source managers) ----------

    def enqueue_order_book(self, book: OrderBook) -> bool:
        """Queue one order book for write-behind persistence. Returns False if
        the manager has not started."""
        if self._book_writer is None:
            return False
        return self._book_writer.enqueue(book)

    def enqueue_news(self, item: NewsItem) -> bool:
        """Queue one news item for write-behind persistence."""
        if self._news_writer is None:
            return False
        return self._news_writer.enqueue(item)

    # ---------- status (powers the database section inspector) ----------

    def status(self) -> dict[str, Any]:
        """Snapshot of the persistence layer — table row counts, writer stats,
        and what retention has reclaimed.

        ``retention.pruned_rows`` is the only outward sign the prune is running
        at all: a stuck sweep otherwise shows up as nothing but a table that
        keeps growing.
        """
        return {
            "tables": self._table_counts(),
            "writers": {
                "order_book": self._writer_stats(self._book_writer),
                "news": self._writer_stats(self._news_writer),
            },
            "retention": {
                "retention_days": self._config.order_book_retention_days,
                "pruned_rows": self._pruned_rows,
                "last_prune_at": self._last_prune_at,
            },
        }

    def _table_counts(self) -> dict[str, int]:
        if self._engine is None:
            return {}
        with make_session_factory(self._engine)() as session:
            return {
                "order_book_snapshot": session.execute(
                    select(func.count()).select_from(OrderBookSnapshot)
                ).scalar_one(),
                "news_item": session.execute(
                    select(func.count()).select_from(NewsItemRow)
                ).scalar_one(),
                "fill": session.execute(select(func.count()).select_from(FillRow)).scalar_one(),
                "position": session.execute(
                    select(func.count()).select_from(PositionRow)
                ).scalar_one(),
            }

    @staticmethod
    def _writer_stats(writer: WriteBehindWriter | None) -> dict[str, int] | None:
        if writer is None:
            return None
        return {
            "written": writer.written,
            "dropped": writer.dropped,
            # Sink failures: a non-zero count means batches were lost to a
            # persistence outage, which is otherwise invisible from outside.
            "errors": writer.errors,
            "pending": writer.pending,
        }


# Module-level singleton; the FastAPI lifespan + the database section wire to this.
manager = DatabaseManager()

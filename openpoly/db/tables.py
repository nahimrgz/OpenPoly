"""SQLAlchemy ORM table definitions — the single SQLite database's schema."""

from __future__ import annotations

from sqlalchemy import Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from openpoly.db.engine import Base


class OrderBookSnapshot(Base):
    """One sampled order book — top-N depth levels per side, stored as JSON.

    A time series: one row per market per book-sampling cycle. ``bids_json`` /
    ``asks_json`` hold ``[[price, size], ...]`` best-first — the depth ladder,
    not a quote snapshot (size is what makes walk-book / slippage answerable).

    This is the one table that grows without bound (every token, every sampling
    cycle, forever), so it carries the retention prune (see
    ``DatabaseManager.prune_order_books``) and a composite
    ``(token_id, recorded_at)`` index: every reader that matters filters on
    both — peak bootstrap and the per-token history route — and the prune
    deletes by ``recorded_at``. The plain ``token_id`` index is kept because
    dropping it would rewrite the table on existing databases for no gain; the
    composite one supersedes it for these queries.
    """

    __tablename__ = "order_book_snapshot"
    __table_args__ = (
        Index("ix_order_book_snapshot_token_recorded", "token_id", "recorded_at"),
        # Bare recorded_at index for the retention prune: its DELETE filters on
        # recorded_at alone, which the token_id-led composite can only
        # full-scan, never seek (no skip-scan without ANALYZE). Mirrors
        # migration 4 so fresh databases match migrated ones.
        Index("ix_order_book_snapshot_recorded_at", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[str] = mapped_column(index=True)
    recorded_at: Mapped[float]  # epoch seconds, UTC (the OrderBook.ts)
    bids_json: Mapped[str]
    asks_json: Mapped[str]


class NewsItemRow(Base):
    """One persisted news item — the durable mirror of the in-memory news ring.

    Surrogate ``id`` PK (not ``news_id``): the write-behind sink must never
    crash on a rare upstream-dedup miss, so duplicates are tolerated rather
    than raising an integrity error.
    """

    __tablename__ = "news_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[str] = mapped_column(index=True)
    content: Mapped[str]
    urgency: Mapped[str]
    # Free-form categorical label from upstream ('neutral' / 'positive' / ...),
    # treated as text like ``urgency`` — never coerced to a number. SQLite's
    # dynamic typing stores text into this column even if an older DB created it
    # with NUMERIC affinity, so no migration is needed.
    sentiment: Mapped[str | None]
    published_at: Mapped[float]
    received_at: Mapped[float] = mapped_column(index=True)


class MarketEmbeddingRow(Base):
    """One cached sentence embedding for a market ``question``.

    The durable backing for ``EmbeddingManager``'s in-memory vector dict — it
    lets a process restart reload the catalog's embeddings instead of paying
    the cold-start recompute. ``vector`` is a float32 ndarray serialized to
    bytes; ``text_hash`` is a digest of the encoded ``question``, so a
    re-titled market invalidates its stale vector. Rows are unique per
    (market, model): switching the embedding model naturally misses and
    recomputes rather than reading a dimension-mismatched vector.
    """

    __tablename__ = "market_embedding"
    __table_args__ = (UniqueConstraint("market_id", "model_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(index=True)
    model_name: Mapped[str]
    text_hash: Mapped[str]
    vector: Mapped[bytes]
    created_at: Mapped[float]  # epoch seconds, UTC (our wall clock at cache write)


class FillRow(Base):
    """One executed fill — an append-only ledger row, the portfolio's source of
    truth.

    Every buy and sell is one immutable row; the ``position`` table is a
    materialized projection that can always be rebuilt by folding fills.
    ``news_id`` traces a buy back to its triggering news; ``trigger`` records
    why a sell fired. ``fee`` is 0 under the zero-fee rule.
    """

    __tablename__ = "fill"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float]  # epoch seconds, UTC
    market_id: Mapped[str] = mapped_column(index=True)
    side: Mapped[str]  # yes | no
    action: Mapped[str]  # buy | sell
    price: Mapped[float]
    qty: Mapped[float]
    fee: Mapped[float]
    position_id: Mapped[int] = mapped_column(index=True)
    news_id: Mapped[str | None]
    trigger: Mapped[str | None]
    order_id: Mapped[str | None] = mapped_column(default=None)  # NEW (slice C)
    tx_hash: Mapped[str | None] = mapped_column(default=None)  # NEW (slice C)


class PositionRow(Base):
    """One position — a materialized projection of the ``fill`` ledger.

    openPoly is one-shot per (market, side): a position is exactly one buy fill
    and later one sell fill, so ``qty`` / ``avg_entry_price`` equal that buy
    fill with no weighted-average recompute. ``realized_pnl`` is set once at
    close and is itself derivable from the two fills. ``token_id`` is stored so
    a close can read the order book without the market still being in the live
    catalog. The partial unique index allows at most one ``open`` position per
    (market_id, side) — re-entry after a close is fine (closed rows fall
    outside the index).
    """

    __tablename__ = "position"
    __table_args__ = (
        Index(
            "ix_position_open_unique",
            "market_id",
            "side",
            unique=True,
            sqlite_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[str] = mapped_column(index=True)
    side: Mapped[str]  # yes | no
    token_id: Mapped[str]
    condition_id: Mapped[str]
    qty: Mapped[float]
    avg_entry_price: Mapped[float]
    status: Mapped[str]  # open | closed
    opened_at: Mapped[float]  # epoch seconds, UTC
    closed_at: Mapped[float | None]
    close_reason: Mapped[str | None]
    realized_pnl: Mapped[float | None]
    # Entry-time analyzer/section signals, denormalized onto the position so an
    # outcome can be joined back to the belief that opened it. Without them a
    # closed position says what happened but not what we predicted, and the
    # calibration question ("is p_model 0.7 actually right 70% of the time?")
    # is unanswerable after the fact — the analyzer_log ring has long evicted
    # the call. Nullable: hand-opened / reconciled positions have no signal.
    entry_p_model: Mapped[float | None] = mapped_column(default=None)
    entry_confidence: Mapped[str | None] = mapped_column(default=None)
    entry_edge: Mapped[float | None] = mapped_column(default=None)

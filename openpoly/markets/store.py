"""In-memory market catalog.

``MarketStore`` holds the current discovered + filtered market universe plus a
summary of the most recent discovery poll. Single-writer (the market-source
polling task) / multi-reader (HTTP status route, sections). ``replace`` rebinds
the whole catalog dict in one assignment, so a reader always sees a complete
snapshot — no lock needed under FastAPI's single event loop.

The catalog and latest order books are in-memory reference data, rebuilt by the
next poll / sample after a restart. Persisting the order book depth ladder as a
time series is a separate concern (memory ``openpoly_market_data_persistence``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from openpoly.markets.models import Market, OrderBook

# A held token's book survives a sample batch that no longer carries it (see
# ``set_order_books``' ``keep``), but only while it is still being refreshed:
# the held-book loop re-fetches it every ~10s, so a kept book whose last
# successful fetch is older than this has stopped updating — typically a
# resolved market whose CLOB /book now fails. Trading on it would book a
# phantom fill at a price that no longer exists, so it is dropped, and readers
# that act on a held token's book refuse one older than this
# (``is_order_book_usable``).
KEPT_BOOK_MAX_AGE_SECONDS = 90.0


def book_clock() -> float:
    """The clock every order-book stamp in this store is on: monotonic, so a
    wall-clock step (NTP, a manual change) can neither freeze newest-wins
    ordering nor make an age negative. Callers that pass ``fetched_at`` /
    ``received_at`` / ``now`` must read it from here. Nothing in the store is
    persisted or displayed as a wall-clock time."""
    return time.monotonic()


@dataclass(frozen=True)
class PollSummary:
    """Outcome of one discovery poll — surfaced on the Live tab."""

    ts: float  # epoch seconds, UTC
    fetched: int  # raw markets returned by Gamma
    kept: int  # markets that passed the discovery filter
    reason_counts: dict[str, int] = field(default_factory=dict)  # reject histogram
    holding_synced: int = 0  # markets pulled in by holding sync (open positions not in discovery)
    holding_sync_failed: int = 0  # holding sync failures this poll

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "fetched": self.fetched,
            "kept": self.kept,
            "reason_counts": dict(self.reason_counts),
            "holding_synced": self.holding_synced,
            "holding_sync_failed": self.holding_sync_failed,
        }


class _CachedBook(NamedTuple):
    """A cached order book plus the ``book_clock()`` reading at which its
    fetch *completed* — the response's receipt, so ordering two books by it is
    ordering them by what the venue actually answered last. Tracked here
    rather than read off ``OrderBook.ts``: that is the venue's own stamp (or
    the parse time when absent), so it says nothing about how long the book
    has sat in this cache."""

    book: OrderBook
    fetched_at: float


class MarketStore:
    """Current market catalog + last-poll summary. In-memory, not persisted."""

    def __init__(self) -> None:
        self._catalog: dict[str, Market] = {}
        self._last_poll: PollSummary | None = None
        self._order_books: dict[str, _CachedBook] = {}
        # Tokens whose CLOB /book answered 404 (the market is gone, normally
        # resolved) since their last successful fetch, mapped to the
        # ``book_clock()`` reading of the latest 404 — see ``mark_book_gone``.
        self._gone: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._catalog)

    def replace(self, markets: list[Market], summary: PollSummary) -> None:
        """Atomically swap the catalog and record the poll summary."""
        self._catalog = {m.market_id: m for m in markets}
        self._last_poll = summary

    def get(self, market_id: str) -> Market | None:
        return self._catalog.get(market_id)

    def get_by_condition(self, condition_id: str) -> Market | None:
        """O(n) lookup by Polymarket on-chain ``conditionId`` (PositionRecord
        stores condition_id, not the Gamma market_id). Catalog is ~50
        markets in steady state — sub-millisecond walk. Returns ``None``
        when the market is no longer in the catalog (filtered out or
        resolved), which the caller must handle as "evicted"."""
        for m in self._catalog.values():
            if m.condition_id == condition_id:
                return m
        return None

    def snapshot(self) -> list[Market]:
        """All catalogued markets, in poll order (Gamma volume-desc)."""
        return list(self._catalog.values())

    def union(self, markets: list[Market]) -> int:
        """Add markets not already present in catalog. Returns count added.

        Existing market_ids keep their current entry (discovery wins).
        """
        added = 0
        for m in markets:
            if m.market_id not in self._catalog:
                self._catalog[m.market_id] = m
                added += 1
        return added

    def snapshot_ids(self) -> set[str]:
        """Set of all market_ids currently in catalog."""
        return set(self._catalog.keys())

    def update_last_poll(self, summary: PollSummary) -> None:
        """Replace the last-poll summary without touching the catalog.

        Used after holding sync to fold the holding_synced / holding_sync_failed
        counters into the already-stored summary.
        """
        self._last_poll = summary

    @property
    def last_poll(self) -> PollSummary | None:
        return self._last_poll

    # ---------- order book depth (sampled by the book loop, MS8) ----------

    def set_order_books(
        self,
        books: list[OrderBook],
        *,
        fetched_at: float | None = None,
        received_at: dict[str, float] | None = None,
        keep: set[str] | frozenset[str] = frozenset(),
    ) -> list[OrderBook]:
        """Atomically replace the order-book set with a fresh sample batch.

        ``fetched_at`` is when the batch's cycle started (default: now);
        ``received_at`` maps each batch token to when its own fetch completed
        (default: ``fetched_at``). Newest wins per token:

        * a cached book received after a batch book's receipt (an on-demand
          refresh that answered later) is kept instead of it;
        * a cached book the batch does not carry is kept when it was received
          after the cycle started — a sample cycle that was already in flight
          must not drop an entry-time refresh between decision and fill — or
          when its token is in ``keep`` (tokens with an open position: the
          exit monitor marks them, and a market missing from one discovery
          batch must not leave a position without a book) *and* it was
          fetched within ``KEPT_BOOK_MAX_AGE_SECONDS`` of the cycle — a held
          book that stopped refreshing is a dead market's last quote, not a
          price; dropped otherwise, as before.

        All stamps are ``book_clock()`` readings.

        Returns the batch books actually stored, so the caller forwards only
        those to its persist / observer hooks.
        """
        batch_at = fetched_at if fetched_at is not None else book_clock()
        stamps = received_at or {}
        carried = {b.token_id for b in books}
        cached: dict[str, _CachedBook] = {
            token: prev
            for token, prev in self._order_books.items()
            if token not in carried
            and (
                prev.fetched_at > batch_at
                or (token in keep and batch_at - prev.fetched_at <= KEPT_BOOK_MAX_AGE_SECONDS)
            )
        }
        accepted: list[OrderBook] = []
        for b in books:
            at = stamps.get(b.token_id, batch_at)
            prev = self._order_books.get(b.token_id)
            if self._predates_gone_mark(b.token_id, at):
                # Received before the venue said the book is gone: a quote of
                # a market that no longer exists. Neither stored nor allowed
                # to clear the mark.
                continue
            if prev is not None and prev.fetched_at > at:
                cached[b.token_id] = prev
            else:
                cached[b.token_id] = _CachedBook(b, at)
                accepted.append(b)
                self._gone.pop(b.token_id, None)
        self._order_books = cached
        return accepted

    def put_order_book(self, book: OrderBook, *, fetched_at: float | None = None) -> bool:
        """Insert / replace one token's book (an on-demand refresh).
        ``fetched_at`` is when its fetch completed (default: now). Newest
        wins: a book received before the one already cached (a slow refresh
        overtaken by a sample batch or another refresh) is not stored.
        Returns whether it was stored."""
        at = fetched_at if fetched_at is not None else book_clock()
        prev = self._order_books.get(book.token_id)
        if prev is not None and prev.fetched_at > at:
            return False
        if self._predates_gone_mark(book.token_id, at):
            return False
        self._order_books[book.token_id] = _CachedBook(book, at)
        self._gone.pop(book.token_id, None)
        return True

    def mark_book_gone(self, token_id: str, *, at: float | None = None) -> None:
        """Record that ``token_id``'s CLOB /book answered 404 — the market no
        longer has a book (it resolved or was removed) — and evict its cached
        book, so nothing fills or marks against that market's last quote.

        The mark is stamped (``at``, a ``book_clock()`` reading; default now).
        Only a book *received after* it clears it: a sample batch whose fetch
        of this token completed before the 404 carries the dead market's last
        quote and must not resurrect it."""
        self._order_books.pop(token_id, None)
        self._gone[token_id] = at if at is not None else book_clock()

    def is_book_gone(self, token_id: str) -> bool:
        """Whether the venue reported ``token_id``'s book as gone (404) since
        its last successful fetch."""
        return token_id in self._gone

    def gone_since(self, token_id: str) -> float | None:
        """``book_clock()`` reading of the latest 404 for ``token_id``, or None
        when it is not marked gone."""
        return self._gone.get(token_id)

    def _predates_gone_mark(self, token_id: str, received_at: float) -> bool:
        """Whether a book received at ``received_at`` is older than the gone
        mark of its token (so it must not be stored nor clear the mark)."""
        gone_at = self._gone.get(token_id)
        return gone_at is not None and received_at <= gone_at

    def is_order_book_usable(
        self,
        token_id: str,
        max_age: float = KEPT_BOOK_MAX_AGE_SECONDS,
        now: float | None = None,
    ) -> bool:
        """Whether ``token_id`` has a cached book young enough to fill
        against: present, not reported gone, and fetched within ``max_age``
        seconds. A held token's book is refreshed every ~10s, so one older
        than the default has stopped updating."""
        age = self.order_book_age(token_id, now)
        # A gone token has no cached book (``mark_book_gone`` evicts it and
        # every store clears the mark), so ``age is not None`` excludes it.
        return age is not None and age <= max_age

    def get_order_book(self, token_id: str) -> OrderBook | None:
        cached = self._order_books.get(token_id)
        return cached.book if cached is not None else None

    def order_book_fetched_at(self, token_id: str) -> float | None:
        """``book_clock()`` reading at which ``token_id``'s cached book was
        received, or None when there is none. Two reads returning the same
        stamp saw the same fetch."""
        cached = self._order_books.get(token_id)
        return cached.fetched_at if cached is not None else None

    def order_book_age(self, token_id: str, now: float | None = None) -> float | None:
        """Seconds since ``token_id``'s cached book was fetched, or None when
        there is no cached book. ``now`` is a ``book_clock()`` reading."""
        cached = self._order_books.get(token_id)
        if cached is None:
            return None
        return (now if now is not None else book_clock()) - cached.fetched_at

    @property
    def order_book_count(self) -> int:
        return len(self._order_books)

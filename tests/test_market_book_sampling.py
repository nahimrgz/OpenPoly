"""Tests for MS8 — order book sampling: MarketStore order-book storage +
MarketSourceManager's second loop. Both fetcher seams are injected (no network).
"""

from __future__ import annotations

import asyncio

from openpoly.markets.manager import MarketSourceConfig, MarketSourceManager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import MarketStore, book_clock


def _book(token_id: str) -> OrderBook:
    return OrderBook(token_id=token_id, ts=1.0, bids=[(0.4, 1.0)], asks=[(0.5, 1.0)])


def _raw_pair(market_id: str):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": "Q?",
        "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
        "endDate": "2027-01-01T00:00:00Z",
        "bestBid": 0.40,
        "bestAsk": 0.42,
        "spread": 0.02,
        "volume24hr": 50_000.0,
        "liquidityNum": 20_000.0,
        "feesEnabled": False,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }
    return (raw, {"id": "e1", "title": "E", "tags": []})


def _fetcher(pairs):
    async def fetch(*, limit):
        return list(pairs)

    return fetch


async def _book_fetcher(token_id: str) -> OrderBook:
    return _book(token_id)


async def _wait_until(predicate, *, iterations: int = 600) -> None:
    for _ in range(iterations):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# ---------- MarketStore order-book storage ----------


def test_store_set_get_order_books():
    store = MarketStore()
    assert store.order_book_count == 0
    store.set_order_books([_book("a"), _book("b")])
    assert store.order_book_count == 2
    got = store.get_order_book("a")
    assert got is not None
    assert got.token_id == "a"


def test_store_set_order_books_replaces():
    store = MarketStore()
    store.set_order_books([_book("old1"), _book("old2")])
    store.set_order_books([_book("new1")])
    assert store.order_book_count == 1
    assert store.get_order_book("old1") is None
    assert store.get_order_book("new1") is not None


def test_store_get_order_book_missing():
    store = MarketStore()
    store.set_order_books([_book("a")])
    assert store.get_order_book("zzz") is None


# ---------- MarketSourceConfig ----------


def test_config_book_interval_default():
    assert MarketSourceConfig().book_sample_interval_seconds == 60


# ---------- _sample_books_once ----------


async def test_sample_books_once():
    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a"), _raw_pair("b")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()  # populate the catalog

    # Both sides of both markets are sampled.
    count = await mgr._sample_books_once()
    assert count == 4
    assert mgr.store.order_book_count == 4
    for token_id in ("yes-a", "no-a", "yes-b", "no-b"):
        assert mgr.store.get_order_book(token_id) is not None


async def test_sample_books_empty_catalog():
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    mgr._config = MarketSourceConfig()
    count = await mgr._sample_books_once()
    assert count == 0
    assert mgr.store.order_book_count == 0


async def test_sample_books_tolerates_fetch_failure():
    async def flaky(token_id: str) -> OrderBook:
        if token_id == "yes-b":
            raise RuntimeError("boom")
        return _book(token_id)

    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a"), _raw_pair("b"), _raw_pair("c")]),
        book_fetcher=flaky,
    )
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()

    # 3 markets x 2 sides = 6 fetches; only yes-b fails.
    count = await mgr._sample_books_once()
    assert count == 5
    assert mgr.store.get_order_book("yes-b") is None
    assert mgr.store.get_order_book("no-b") is not None  # NO side still kept
    assert mgr.store.get_order_book("yes-a") is not None
    assert mgr.store.get_order_book("yes-c") is not None


# ---------- book loop lifecycle ----------


async def test_book_loop_runs_on_start():
    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a"), _raw_pair("b")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()  # pre-populate so the first book sample has markets

    await mgr.start(MarketSourceConfig(poll_interval_seconds=3600, book_sample_interval_seconds=5))
    await _wait_until(lambda: mgr.store.order_book_count >= 1)
    assert mgr.store.order_book_count == 4  # 2 markets x YES + NO
    assert any(e.kind == "book_sample_ok" for e in mgr.events())

    await mgr.stop()
    assert mgr.status().state == "stopped"


async def test_stop_cancels_both_loops():
    mgr = MarketSourceManager(fetcher=_fetcher([_raw_pair("a")]), book_fetcher=_book_fetcher)
    await mgr.start(
        MarketSourceConfig(poll_interval_seconds=3600, book_sample_interval_seconds=3600)
    )
    await _wait_until(lambda: mgr.status().poll_count >= 1)
    snap = await mgr.stop()
    assert snap.state == "stopped"
    assert mgr._tasks == []
    # second stop is a safe no-op
    assert (await mgr.stop()).state == "stopped"


# ---------- book observer hook ----------


async def test_sample_books_notifies_observer():
    seen: list[str] = []
    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    mgr.set_book_observer(lambda book: seen.append(book.token_id))
    await mgr._poll_once()
    await mgr._sample_books_once()
    assert sorted(seen) == ["no-a", "yes-a"]


async def test_book_observer_exception_does_not_break_the_cycle():
    def _boom(book: OrderBook) -> None:
        raise RuntimeError("observer blew up")

    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    mgr.set_book_observer(_boom)
    await mgr._poll_once()
    count = await mgr._sample_books_once()
    assert count == 2
    assert mgr.store.order_book_count == 2


async def test_book_observer_can_be_cleared():
    seen: list[str] = []
    mgr = MarketSourceManager(
        fetcher=_fetcher([_raw_pair("a")]),
        book_fetcher=_book_fetcher,
    )
    mgr._config = MarketSourceConfig()
    mgr.set_book_observer(lambda book: seen.append(book.token_id))
    mgr.set_book_observer(None)
    await mgr._poll_once()
    await mgr._sample_books_once()
    assert seen == []


# ---------- entry-time refresh / book age ----------


def test_store_order_book_age_tracks_fetch_time_not_book_ts():
    store = MarketStore()
    assert store.order_book_age("t") is None
    store.set_order_books([_book("t")], fetched_at=1_000.0)  # book.ts is 1.0
    assert store.order_book_age("t", now=1_004.0) == 4.0
    store.put_order_book(_book("t"), fetched_at=1_010.0)
    assert store.order_book_age("t", now=1_012.0) == 2.0


def test_store_sample_batch_does_not_roll_back_a_newer_refresh():
    """A sample cycle that started before an on-demand refresh must not
    replace the refreshed book with its older copy between decision and fill."""
    store = MarketStore()
    refreshed = OrderBook(token_id="t", ts=1.0, bids=[(0.45, 1.0)], asks=[(0.47, 1.0)])
    store.put_order_book(refreshed, fetched_at=2_000.0)
    store.set_order_books([_book("t"), _book("u")], fetched_at=1_990.0)
    assert store.get_order_book("t") is refreshed
    assert store.order_book_age("t", now=2_001.0) == 1.0
    assert store.get_order_book("u") is not None
    # A later batch is newer and does replace it.
    store.set_order_books([_book("t")], fetched_at=2_010.0)
    assert store.get_order_book("t") is not refreshed


async def test_refresh_order_book_stores_and_observes():
    observed: list[str] = []
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    mgr.set_book_observer(lambda b: observed.append(b.token_id))
    book = await mgr.refresh_order_book("t")
    assert book is not None
    assert mgr.store.get_order_book("t") is book
    age = mgr.store.order_book_age("t")
    assert age is not None and age < 5.0
    assert observed == ["t"]


async def test_refresh_order_book_failure_returns_none_and_keeps_cache():
    async def boom(token_id: str) -> OrderBook:
        raise RuntimeError("venue down")

    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=boom)
    cached = _book("t")
    mgr.store.put_order_book(cached, fetched_at=1.0)
    assert await mgr.refresh_order_book("t") is None
    assert mgr.store.get_order_book("t") is cached


def test_store_sample_batch_keeps_a_newer_refresh_it_does_not_carry():
    """Regression: a token refreshed after the cycle started but absent from
    the batch (e.g. its fetch failed this cycle) used to be dropped."""
    store = MarketStore()
    refreshed = _book("t")
    store.put_order_book(refreshed, fetched_at=2_000.0)
    store.set_order_books([_book("u")], fetched_at=1_990.0)
    assert store.get_order_book("t") is refreshed
    # An older entry the batch does not carry is dropped, as before.
    store.put_order_book(_book("old"), fetched_at=1_000.0)
    store.set_order_books([_book("u")], fetched_at=1_995.0)
    assert store.get_order_book("old") is None


def test_store_set_order_books_returns_only_accepted_books():
    store = MarketStore()
    store.put_order_book(_book("t"), fetched_at=2_000.0)
    batch_t, batch_u = _book("t"), _book("u")
    assert store.set_order_books([batch_t, batch_u], fetched_at=1_990.0) == [batch_u]


async def test_sample_cycle_delivers_only_books_the_store_kept():
    """Regression: the batch's older copy of a refreshed token reached the
    observer / persist hooks, so the exit monitor saw prices go backwards."""
    mgr = MarketSourceManager(fetcher=_fetcher([_raw_pair("a")]), book_fetcher=_book_fetcher)
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()
    delivered: list[str] = []
    persisted: list[str] = []
    mgr.set_book_observer(lambda b: delivered.append(b.token_id))
    mgr.set_book_persist(lambda b: persisted.append(b.token_id))
    # A refresh that lands "after" the cycle starts.
    mgr.store.put_order_book(_book("yes-a"), fetched_at=10**12)
    await mgr._sample_books_once()
    assert delivered == ["no-a"]
    assert persisted == ["no-a"]


async def test_refresh_order_book_survives_a_failing_persist_hook():
    """Regression: a persist failure escaped refresh_order_book (and with it
    the orchestrator's entry stage), losing the EntryDecision."""
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)

    def boom(_book_):
        raise RuntimeError("writer full")

    mgr.set_book_persist(boom)
    book = await mgr.refresh_order_book("t")
    assert book is not None
    assert mgr.store.get_order_book("t") is book


# ---------- ensure_fresh / held-book loop ----------


async def test_ensure_fresh_refreshes_only_stale_tokens_and_reports_ages():
    fetched: list[str] = []

    async def fetch(token_id: str) -> OrderBook:
        fetched.append(token_id)
        if token_id == "down":
            raise RuntimeError("venue down")
        return _book(token_id)

    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=fetch)
    now = book_clock()
    mgr.store.put_order_book(_book("fresh"), fetched_at=now - 1)
    mgr.store.put_order_book(_book("stale"), fetched_at=now - 60)
    mgr.store.put_order_book(_book("down"), fetched_at=now - 60)
    ages = await mgr.ensure_fresh(["fresh", "stale", "missing", "down"], 5.0)
    assert sorted(fetched) == ["down", "missing", "stale"]
    assert ages["fresh"] is not None and ages["fresh"] < 5.0
    assert ages["stale"] is not None and ages["stale"] < 5.0
    assert ages["missing"] is not None and ages["missing"] < 5.0
    # A failed refresh shows as a still-stale age, never as an exception.
    assert ages["down"] is not None and ages["down"] >= 60.0


async def test_held_book_refresh_fetches_only_open_position_tokens():
    from types import SimpleNamespace

    fetched: list[str] = []

    async def fetch(token_id: str) -> OrderBook:
        fetched.append(token_id)
        return _book(token_id)

    class _Portfolio:
        def get_open_positions(self):
            return [SimpleNamespace(token_id="held-a"), SimpleNamespace(token_id="held-b")]

    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=fetch)
    mgr.store.put_order_book(_book("not-held"), fetched_at=1.0)
    mgr.set_portfolio_store(_Portfolio())
    assert await mgr._refresh_held_books_once() == 2
    assert sorted(fetched) == ["held-a", "held-b"]
    # Just refreshed → the next cycle right away costs nothing.
    await mgr._refresh_held_books_once()
    assert len(fetched) == 2


async def test_held_book_refresh_without_portfolio_is_a_noop():
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    assert await mgr._refresh_held_books_once() == 0


async def test_held_book_loop_runs_on_start():
    from types import SimpleNamespace

    class _Portfolio:
        def get_open_positions(self):
            return [SimpleNamespace(token_id="held-a", market_id="m-held")]

    mgr = MarketSourceManager(
        fetcher=_fetcher([]),
        book_fetcher=_book_fetcher,
        market_fetcher=lambda _mid: _none(),
    )
    mgr.set_portfolio_store(_Portfolio())
    await mgr.start(MarketSourceConfig())
    try:
        await _wait_until(lambda: mgr.store.get_order_book("held-a") is not None)
    finally:
        await mgr.stop()
    assert len(mgr._tasks) == 0


async def _none():
    return None


def test_put_order_book_newest_wins():
    store = MarketStore()
    newer = _book("t")
    assert store.put_order_book(newer, fetched_at=2_000.0) is True
    assert store.put_order_book(_book("t"), fetched_at=1_000.0) is False
    assert store.get_order_book("t") is newer


def test_batch_book_received_after_a_refresh_wins():
    """Freshness is the response's receipt: a batch fetch that answered after
    an on-demand refresh is the newer book, even though the cycle started
    first."""
    store = MarketStore()
    refreshed = _book("t")
    store.put_order_book(refreshed, fetched_at=2_000.0)
    later = _book("t")
    accepted = store.set_order_books([later], fetched_at=1_990.0, received_at={"t": 2_005.0})
    assert accepted == [later]
    assert store.get_order_book("t") is later
    # ...and an older-completed refresh then cannot overwrite it.
    assert store.put_order_book(_book("t"), fetched_at=2_003.0) is False
    assert store.get_order_book("t") is later


def test_refresh_received_after_the_batch_book_wins():
    store = MarketStore()
    refreshed = _book("t")
    store.put_order_book(refreshed, fetched_at=2_010.0)
    accepted = store.set_order_books([_book("t")], fetched_at=1_990.0, received_at={"t": 2_005.0})
    assert accepted == []
    assert store.get_order_book("t") is refreshed


async def test_refresh_stamps_the_book_on_receipt():
    """A slow fetch is aged from when it answered, not from when it began."""

    async def slow(token_id: str) -> OrderBook:
        await asyncio.sleep(0.2)
        return _book(token_id)

    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=slow)
    await mgr.refresh_order_book("t")
    age = mgr.store.order_book_age("t", now=book_clock())
    assert age is not None and age < 0.1


async def test_refresh_persists_at_most_once_per_sample_interval_but_observes_every_book():
    """Regression: held-book refreshes every ~10s persisted every book (~6x
    snapshot growth). Persistence is throttled per token; observers are not."""
    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    persisted: list[str] = []
    observed: list[str] = []
    mgr.set_book_persist(lambda b: persisted.append(b.token_id))
    mgr.set_book_observer(lambda b: observed.append(b.token_id))
    for _ in range(3):
        await mgr.refresh_order_book("t")
    await mgr.refresh_order_book("u")
    assert persisted == ["t", "u"]
    assert observed == ["t", "t", "t", "u"]
    # Once the interval has passed, the next refresh persists again.
    mgr._last_persisted["t"] -= mgr._persist_interval()
    await mgr.refresh_order_book("t")
    assert persisted == ["t", "u", "t"]


async def test_sample_cycle_persistence_is_not_throttled():
    mgr = MarketSourceManager(fetcher=_fetcher([_raw_pair("a")]), book_fetcher=_book_fetcher)
    mgr._config = MarketSourceConfig()
    await mgr._poll_once()
    persisted: list[str] = []
    mgr.set_book_persist(lambda b: persisted.append(b.token_id))
    await mgr.refresh_order_book("yes-a")  # persisted, starts the throttle window
    await mgr._sample_books_once()
    assert sorted(persisted) == ["no-a", "yes-a", "yes-a"]


async def test_held_book_refresh_reads_positions_off_the_event_loop():
    import threading
    from types import SimpleNamespace

    loop_thread = threading.get_ident()
    seen: list[int] = []

    class _Portfolio:
        def get_open_positions(self):
            seen.append(threading.get_ident())
            return [SimpleNamespace(token_id="held-a")]

    mgr = MarketSourceManager(fetcher=_fetcher([]), book_fetcher=_book_fetcher)
    mgr.set_portfolio_store(_Portfolio())
    await mgr._refresh_held_books_once()
    assert seen and seen[0] != loop_thread


def test_set_order_books_keeps_held_tokens_missing_from_the_batch():
    """Regression: a held token whose market dropped out of one discovery
    batch lost its book, leaving the exit monitor nothing to mark."""
    store = MarketStore()
    held = _book("held")
    store.put_order_book(held, fetched_at=1_950.0)
    store.put_order_book(_book("gone"), fetched_at=1_950.0)
    store.set_order_books([_book("x")], fetched_at=2_000.0, keep=frozenset({"held"}))
    assert store.get_order_book("held") is held
    assert store.get_order_book("gone") is None


async def test_sample_cycle_keeps_books_of_held_tokens():
    from types import SimpleNamespace

    class _Portfolio:
        def get_open_positions(self):
            return [SimpleNamespace(token_id="held-a", market_id="a")]

    mgr = MarketSourceManager(fetcher=_fetcher([_raw_pair("a")]), book_fetcher=_book_fetcher)
    mgr._config = MarketSourceConfig()
    mgr.set_portfolio_store(_Portfolio())
    await mgr._refresh_held_books_once()
    # Rewind its fetch stamp (still within the kept-book max age) so only the
    # held-token rule can keep it.
    mgr.store._order_books["held-a"] = mgr.store._order_books["held-a"]._replace(
        fetched_at=book_clock() - 30.0
    )
    await mgr._poll_once()
    await mgr._sample_books_once()  # the batch carries yes-a / no-a only
    assert mgr.store.get_order_book("held-a") is not None


def test_parse_clob_book_keeps_twenty_levels_by_default():
    from openpoly.markets.models import BOOK_DEPTH, parse_clob_book

    levels = [{"price": f"0.{10 + i}", "size": "1"} for i in range(30)]
    book = parse_clob_book({"bids": levels, "asks": levels}, "t")
    assert BOOK_DEPTH == 20
    assert len(book.bids) == len(book.asks) == 20

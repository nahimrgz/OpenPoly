"""Endpoint tests for POST /api/positions/{id}/close — manual close (EX3).

The route's module-level ``executor`` is monkeypatched to one bound to the
test DB, so the close writes through the same store the route reads from.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import openpoly.api.portfolio_routes as portfolio_routes
from openpoly.api.main import app
from openpoly.api.portfolio_routes import get_portfolio_store
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.execution import PaperExecutor as Executor
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import MarketStore
from openpoly.portfolio import PortfolioStore
from openpoly.runtime.closing_registry import closing_ids, is_closing, mark_closing


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp-DB PortfolioStore wired into the route (read) + executor (write),
    a fresh market catalog, and a TestClient."""
    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    store = PortfolioStore(make_session_factory(engine))
    app.dependency_overrides[get_portfolio_store] = lambda: store
    monkeypatch.setattr(portfolio_routes, "executor", Executor(store))
    saved_market = market_source_manager.store
    market_source_manager.store = MarketStore()
    yield store, TestClient(app)
    app.dependency_overrides.clear()
    market_source_manager.store = saved_market
    engine.dispose()


def _open(store: PortfolioStore, *, token_id: str = "t1", market_id: str = "m1"):
    return store.open_position(
        market_id=market_id,
        side="yes",
        token_id=token_id,
        condition_id=f"0x{market_id}",
        price=0.40,
        qty=25.0,
        ts=100.0,
        news_id="n1",
    )


def _book(token_id: str, bid: float) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(bid, 100.0)],
        asks=[(bid + 0.02, 100.0)],
    )


def test_close_open_position(env) -> None:
    store, client = env
    held = _open(store)
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])

    r = client.post(f"/api/positions/{held.position_id}/close")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filled"] is True
    assert body["price"] == 0.55
    assert body["position_id"] == held.position_id

    rec = store.get_position(held.position_id)
    assert rec is not None
    assert rec.status == "closed"
    assert rec.close_reason == "manual"


def test_close_nonexistent_returns_404(env) -> None:
    _store, client = env
    r = client.post("/api/positions/9999/close")
    assert r.status_code == 404


def test_close_already_closed_returns_409(env) -> None:
    store, client = env
    held = _open(store)
    store.close_position(
        held.position_id,
        sell_price=0.50,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    r = client.post(f"/api/positions/{held.position_id}/close")
    assert r.status_code == 409
    assert "closed" in r.json()["detail"]


def test_close_no_bid_liquidity_returns_200_not_filled(env) -> None:
    store, client = env
    held = _open(store)
    market_source_manager.store.set_order_books(
        [OrderBook(token_id="t1", ts=1.0, bids=[], asks=[(0.5, 100.0)])]
    )
    r = client.post(f"/api/positions/{held.position_id}/close")
    assert r.status_code == 200
    body = r.json()
    assert body["filled"] is False
    assert body["skip_reason"] == "no_bid_liquidity"
    # Nothing filled — the position stays open.
    rec = store.get_position(held.position_id)
    assert rec is not None
    assert rec.status == "open"


# ---------- close-all ----------


def test_close_all_with_no_open_returns_noop(env) -> None:
    _store, client = env
    r = client.post("/api/positions/close-all")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "attempted": 0,
        "filled": 0,
        "partial": 0,
        "skipped": 0,
        "errored": 0,
        "details": [],
    }


def test_close_all_three_positions_all_succeed(env) -> None:
    store, client = env
    # Open 3 positions on 3 different (market, side) pairs.
    p1 = _open(store, token_id="t1", market_id="m1")
    p2 = _open(store, token_id="t2", market_id="m2")
    p3 = _open(store, token_id="t3", market_id="m3")
    market_source_manager.store.set_order_books(
        [
            _book("t1", bid=0.55),
            _book("t2", bid=0.50),
            _book("t3", bid=0.45),
        ]
    )

    r = client.post("/api/positions/close-all")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attempted"] == 3
    assert body["filled"] == 3
    assert body["skipped"] == 0
    assert body["errored"] == 0
    by_id = {d["position_id"]: d for d in body["details"]}
    assert by_id[p1.position_id]["ok"] is True
    assert by_id[p1.position_id]["price"] == 0.55
    assert by_id[p2.position_id]["price"] == 0.50
    assert by_id[p3.position_id]["price"] == 0.45

    # All actually closed in the store.
    for held in (p1, p2, p3):
        rec = store.get_position(held.position_id)
        assert rec is not None and rec.status == "closed"
        assert rec.close_reason == "manual"


def test_close_all_partial_failure_does_not_block_others(env) -> None:
    store, client = env
    p1 = _open(store, token_id="t1", market_id="m1")
    p2 = _open(store, token_id="t2", market_id="m2")  # no bid → skipped
    p3 = _open(store, token_id="t3", market_id="m3")
    market_source_manager.store.set_order_books(
        [
            _book("t1", bid=0.55),
            OrderBook(token_id="t2", ts=1.0, bids=[], asks=[(0.5, 100.0)]),
            _book("t3", bid=0.45),
        ]
    )

    r = client.post("/api/positions/close-all")
    assert r.status_code == 200
    body = r.json()
    assert body["attempted"] == 3
    assert body["filled"] == 2
    assert body["skipped"] == 1
    assert body["errored"] == 0
    by_id = {d["position_id"]: d for d in body["details"]}
    assert by_id[p2.position_id]["ok"] is False
    assert by_id[p2.position_id]["skip_reason"] == "no_bid_liquidity"
    # 1 and 3 actually closed.
    assert store.get_position(p1.position_id).status == "closed"
    assert store.get_position(p3.position_id).status == "closed"
    # 2 still open.
    assert store.get_position(p2.position_id).status == "open"


# ---------- in-flight close claims (the exit monitor holds the position) ----------


def test_close_conflicts_with_an_in_flight_exit(env) -> None:
    """The exit monitor's sell runs in a worker thread, so its position stays
    ``open`` in the DB for seconds while the tokens are already being sold.
    A manual close in that window is a second on-chain sell of the same
    position — refuse it."""
    store, client = env
    held = _open(store)
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])
    mark_closing(held.position_id)

    r = client.post(f"/api/positions/{held.position_id}/close")

    assert r.status_code == 409
    assert r.json()["detail"] == "exit_in_flight"
    # Untouched: the exit monitor still owns this position.
    assert store.get_position(held.position_id).status == "open"


def test_close_registers_and_releases_the_claim(env) -> None:
    """The manual close must itself claim the position, so the settlement and
    reconciliation monitors skip it while the sell is in flight — and must
    release it on the way out."""
    store, client = env
    held = _open(store)
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])

    seen: list[bool] = []

    class _Watching:
        def execute_sell(self, position, *, close_reason, ts, trigger=None):
            seen.append(is_closing(position.position_id))
            return Executor(store).execute_sell(
                position, close_reason=close_reason, ts=ts, trigger=trigger
            )

    portfolio_routes.executor = _Watching()
    r = client.post(f"/api/positions/{held.position_id}/close")

    assert r.status_code == 200, r.text
    assert seen == [True]  # claimed for the duration of the sell
    assert closing_ids() == frozenset()  # released afterwards


def test_close_releases_the_claim_when_the_sell_raises(env) -> None:
    store, client = env
    held = _open(store)

    class _Boom:
        def execute_sell(self, position, *, close_reason, ts, trigger=None):
            raise RuntimeError("clob down")

    portfolio_routes.executor = _Boom()
    with pytest.raises(RuntimeError):
        client.post(f"/api/positions/{held.position_id}/close")

    assert closing_ids() == frozenset()


def test_close_all_skips_positions_with_an_in_flight_exit(env) -> None:
    """Bulk close must not fight the exit monitor either — the claimed ids are
    skipped and reported, the rest still close."""
    store, client = env
    p1 = _open(store, token_id="t1", market_id="m1")
    p2 = _open(store, token_id="t2", market_id="m2")
    market_source_manager.store.set_order_books([_book("t1", bid=0.55), _book("t2", bid=0.50)])
    mark_closing(p2.position_id)

    r = client.post("/api/positions/close-all")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attempted"] == 2
    assert body["filled"] == 1
    assert body["skipped"] == 1
    by_id = {d["position_id"]: d for d in body["details"]}
    assert by_id[p2.position_id]["ok"] is False
    assert by_id[p2.position_id]["skip_reason"] == "exit_in_flight"
    assert store.get_position(p1.position_id).status == "closed"
    assert store.get_position(p2.position_id).status == "open"


# ---------- partial fills (the bid could not absorb the whole position) ----------


def _thin_book(token_id: str, bid: float, size: float) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(bid, size)],
        asks=[(bid + 0.02, 100.0)],
    )


def test_close_reports_a_partial_fill(env) -> None:
    """A 25-share position into a 10-share bid sells 10 and stays open. The
    response said ``filled: true`` and nothing else — indistinguishable from a
    completed exit, so the operator had no way to know 15 shares were still on
    the book."""
    store, client = env
    held = _open(store)  # qty 25
    market_source_manager.store.set_order_books([_thin_book("t1", bid=0.55, size=10.0)])

    body = client.post(f"/api/positions/{held.position_id}/close").json()

    assert body["filled"] is True
    assert body["partial"] is True
    assert body["qty"] == pytest.approx(10.0)
    assert body["remaining_qty"] == pytest.approx(15.0)
    assert store.get_position(held.position_id).status == "open"


def test_close_reports_a_full_fill_as_not_partial(env) -> None:
    store, client = env
    held = _open(store)
    market_source_manager.store.set_order_books([_book("t1", bid=0.55)])

    body = client.post(f"/api/positions/{held.position_id}/close").json()

    assert body["filled"] is True
    assert body["partial"] is False
    assert "remaining_qty" not in body


def test_close_all_counts_partials_separately(env) -> None:
    store, client = env
    p1 = _open(store, token_id="t1", market_id="m1")  # fully absorbed
    p2 = _open(store, token_id="t2", market_id="m2")  # thin bid → partial
    market_source_manager.store.set_order_books(
        [
            _book("t1", bid=0.55),
            _thin_book("t2", bid=0.50, size=10.0),
        ]
    )

    body = client.post("/api/positions/close-all").json()

    assert body["attempted"] == 2
    assert body["filled"] == 1
    assert body["partial"] == 1
    assert body["skipped"] == 0
    by_id = {d["position_id"]: d for d in body["details"]}
    # ``ok`` means flat: a position with 15 shares still on the book is not.
    assert by_id[p1.position_id]["ok"] is True
    assert by_id[p2.position_id]["ok"] is False
    assert by_id[p2.position_id]["partial"] is True
    assert by_id[p2.position_id]["remaining_qty"] == pytest.approx(15.0)
    assert store.get_position(p2.position_id).status == "open"


def test_close_all_noop_body_carries_the_partial_counter(env) -> None:
    _store, client = env
    body = client.post("/api/positions/close-all").json()
    assert body["partial"] == 0


# ---------- the sell must not run on the event loop ----------


@pytest.mark.parametrize("route", ["single", "close-all"])
async def test_close_keeps_the_event_loop_running(env, monkeypatch, route: str) -> None:  # noqa: ANN001
    """``execute_sell`` blocks for seconds on live (CTF cache polling, order
    placement, close-persist retries). Called inline from these ``async def``
    routes it froze the whole event loop for that long — every WS reconnect,
    market poll and monitor tick stalled behind one operator click. It has to
    run in a worker thread.

    The request is driven through ``ASGITransport`` on purpose: ``TestClient``
    runs the app on its own event loop, so the heartbeat below would keep
    ticking on this one no matter what the route did.
    """
    import asyncio
    import contextlib
    import time as _time

    from httpx import ASGITransport, AsyncClient

    from openpoly.execution import ExecResult

    store, _client = env
    held = _open(store)

    class _Blocking:
        def execute_sell(self, position, *, close_reason, ts, trigger=None):  # noqa: ANN001, ANN201
            _time.sleep(0.3)
            return ExecResult.ok(price=0.55, qty=position.qty, position_id=position.position_id)

    monkeypatch.setattr(portfolio_routes, "executor", _Blocking())
    url = (
        f"/api/positions/{held.position_id}/close"
        if route == "single"
        else "/api/positions/close-all"
    )

    beats = 0

    async def _heartbeat() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.01)
            beats += 1

    hb = asyncio.create_task(_heartbeat())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(url)
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb

    assert r.status_code == 200, r.text
    # ~30 beats fit in 0.3s; anything past a handful proves the loop kept
    # running while the sell slept.
    assert beats >= 10, f"event loop stalled: only {beats} heartbeats"

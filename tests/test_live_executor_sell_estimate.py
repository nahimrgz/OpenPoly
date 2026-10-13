"""Live SELL fills inferred without the venue's amounts are booked at a
realistic price, not at the limit.

The sell limit is the *floor* of a crossing sweep (``min(best bid, floor)``),
so booking an inferred fill at it understated the proceeds by the whole sweep
and fed a fabricated loss into the entry kill switch. Those paths now book the
VWAP of the pre-sell book's levels at or above the limit for the filled qty,
falling back to the best bid at sell time.
"""

from __future__ import annotations

from typing import Any

import pytest

from openpoly.execution.live_executor import LiveExecutor, _sell_price_estimate
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.portfolio import PortfolioStore
from tests.test_live_executor import (  # noqa: F401 — autouse fixtures
    _FakeClob,
    _held,
    _isolate_market_store,
    _market,
    _populate,
    no_sleep,
)


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


# Three bid levels: a stop decided at 0.40 sweeps all of them.
_DEEP_BIDS = [(0.55, 4.0), (0.50, 4.0), (0.40, 50.0)]


def _deep_book(token_id: str) -> OrderBook:
    return OrderBook(token_id=token_id, ts=1.0, bids=list(_DEEP_BIDS), asks=[(0.56, 100.0)])


def _expected_vwap(qty: float) -> float:
    remaining, notional = qty, 0.0
    for price, size in _DEEP_BIDS:
        take = min(remaining, size)
        notional += take * price
        remaining -= take
    return notional / qty


def test_lost_response_sell_books_the_pre_sell_vwap_not_the_floor(store) -> None:
    m = _market("m1")
    _populate(m, _deep_book(m.yes_token_id))
    held = _held(store, m, qty=10.0)
    clob = _FakeClob(
        exception=RuntimeError("request exception"),
        ctf_balance_sequence=[10_000_000, 0],  # gate syncs, then all 10 shares gone
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0, floor_price=0.40)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    # 4 @ 0.55 + 4 @ 0.50 + 2 @ 0.40 = 0.50, not the 0.40 limit.
    assert r.price == pytest.approx(_expected_vwap(10.0))
    assert r.price > 0.40
    # The order itself is unchanged: still a crossing limit at the floor.
    assert clob.posted[0]["order_args"].price == pytest.approx(0.40)
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.realized_pnl == pytest.approx((r.price - 0.40) * 10.0)


@pytest.mark.parametrize("taking", ["", "n/a"])
def test_no_usable_amounts_sell_books_the_pre_sell_vwap(store, taking: Any) -> None:
    m = _market("m1")
    _populate(m, _deep_book(m.yes_token_id))
    held = _held(store, m, qty=6.0)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xS",
            "status": "matched",
            "makingAmount": "6",
            "takingAmount": taking,
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0, floor_price=0.40)
    assert r.filled is True
    assert r.price == pytest.approx(_expected_vwap(6.0))


def test_usable_amounts_are_still_believed(store) -> None:
    """Only the inferred paths change: a response with amounts books its own
    average, exactly as before."""
    m = _market("m1")
    _populate(m, _deep_book(m.yes_token_id))
    held = _held(store, m, qty=10.0)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xS",
            "status": "matched",
            "makingAmount": "10",
            "takingAmount": "4.7",
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0, floor_price=0.40)
    assert r.price == pytest.approx(0.47)


def test_estimate_prices_uncovered_qty_at_the_limit() -> None:
    bids = [(0.60, 2.0), (0.30, 100.0)]
    # Only 2 shares sit at or above the 0.50 limit; the other 8 at the limit.
    assert _sell_price_estimate(bids, limit=0.50, qty=10.0, token_id="t") == pytest.approx(
        (2 * 0.60 + 8 * 0.50) / 10
    )


def test_estimate_falls_back_to_the_best_bid_at_sell_time() -> None:
    market_source_manager.store.put_order_book(
        OrderBook(token_id="t", ts=1.0, bids=[(0.52, 10.0)], asks=[(0.53, 10.0)])
    )
    assert _sell_price_estimate([], limit=0.45, qty=5.0, token_id="t") == pytest.approx(0.52)
    # Never below the limit: a sell limit cannot fill under it.
    assert _sell_price_estimate([], limit=0.60, qty=5.0, token_id="t") == pytest.approx(0.60)
    assert _sell_price_estimate([], limit=0.45, qty=5.0, token_id="missing") == pytest.approx(0.45)

"""Paper fills pay the taker fee the venue would charge.

Polymarket charges takers ``shares × rate × p × (1 − p)`` USDC on every fill
(docs.polymarket.com/trading/fees). The paper executor records that fee on
each buy and sell fill, and realized PnL is net of both: the buy fee is
charged pro rata to the shares each sell (or the settlement close) disposes of.
"""

from __future__ import annotations

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.execution import PaperExecutor
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import normalize_gamma_market
from openpoly.portfolio import PortfolioStore
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent
from tests.test_executor import (  # noqa: F401 — autouse fixture
    _book,
    _isolate_market_store,
    _populate,
)

RATE = 0.04


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


def _fee(price: float, qty: float, rate: float = RATE) -> float:
    return qty * rate * price * (1.0 - price)


def _market(rate: float):
    raw = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Q?",
        "clobTokenIds": '["yes-m1", "no-m1"]',
        "feesEnabled": rate > 0,
        "feeSchedule": {"rate": rate, "exponent": 1, "takerOnly": True},
    }
    m = normalize_gamma_market(raw, event={"id": "e", "title": "E", "tags": []})
    assert m is not None
    return m


def _buy(store, *, rate: float = RATE, ask: float = 0.40, qty: float = 20.0):
    _populate(_market(rate), _book("yes-m1", bid=ask - 0.01, ask=ask))
    r = PaperExecutor(store).execute_buy(
        OrderIntent(market_id="m1", side="yes", price=ask, qty=qty), news_id="n1", ts=100.0
    )
    assert r.filled
    return store.get_open_position("m1", "yes")


def _set_bid(bid: float, size: float = 100.0) -> None:
    market_source_manager.store.set_order_books(
        [_book("yes-m1", bid=bid, ask=bid + 0.01, bid_size=size)]
    )


def test_buy_fill_records_the_taker_fee(store) -> None:
    held = _buy(store)
    (fill,) = store.list_fills()
    assert fill.action == "buy"
    assert fill.fee == pytest.approx(_fee(0.40, 20.0))
    # The position keeps the price actually paid per share; the fee is a fill cost.
    assert held.avg_entry_price == 0.40


def test_sell_realized_pnl_is_net_of_both_fees(store) -> None:
    held = _buy(store)
    _set_bid(0.50)
    r = PaperExecutor(store).execute_sell(held, close_reason="take_profit", ts=200.0)
    assert r.filled
    sell = store.list_fills()[0]
    assert sell.action == "sell"
    assert sell.fee == pytest.approx(_fee(0.50, 20.0))
    pos = store.get_position(held.position_id)
    assert pos.status == "closed"
    expected = (0.50 - 0.40) * 20.0 - _fee(0.40, 20.0) - _fee(0.50, 20.0)
    assert pos.realized_pnl == pytest.approx(expected)


def test_partial_sell_charges_the_buy_fee_pro_rata(store) -> None:
    held = _buy(store)
    _set_bid(0.50, size=5.0)
    PaperExecutor(store).execute_sell(held, close_reason="take_profit", ts=200.0)
    pos = store.get_position(held.position_id)
    assert pos.status == "open"
    assert pos.qty == pytest.approx(15.0)
    expected = (0.50 - 0.40) * 5.0 - _fee(0.40, 20.0) * 5.0 / 20.0 - _fee(0.50, 5.0)
    assert pos.realized_pnl == pytest.approx(expected)


def test_settlement_close_still_pays_the_buy_fee(store) -> None:
    # Redeeming at resolution is not a trade (no sell fee), but the shares
    # were bought as a taker.
    held = _buy(store)
    pos = store.close_position(held.position_id, sell_price=1.0, ts=300.0, close_reason="settled")
    assert store.list_fills()[0].fee == 0.0
    assert pos.realized_pnl == pytest.approx((1.0 - 0.40) * 20.0 - _fee(0.40, 20.0))


def test_cost_basis_includes_the_buy_fee(store) -> None:
    held = _buy(store)
    basis = store.buy_cost_basis([held.position_id])
    assert basis[held.position_id] == pytest.approx(0.40 * 20.0 + _fee(0.40, 20.0))


def test_zero_fee_market_books_no_fee(store) -> None:
    held = _buy(store, rate=0.0)
    _set_bid(0.50)
    PaperExecutor(store).execute_sell(held, close_reason="take_profit", ts=200.0)
    assert all(f.fee == 0.0 for f in store.list_fills())
    assert store.get_position(held.position_id).realized_pnl == pytest.approx(2.0)

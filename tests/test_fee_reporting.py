"""Every report of a trade agrees with the ledger once fees exist.

The store books realized PnL net of the taker fee. The exit and settlement
logs, the position exit price, the equity curve and the shadow returns must
show the same economics instead of recomputing them gross.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from openpoly.analytics.shadow import ShadowQuote, ShadowStore, shadow_report
from openpoly.api.main import app
from openpoly.api.portfolio_routes import get_portfolio_store
from openpoly.db.tables import OrderBookSnapshot
from openpoly.execution import PaperExecutor
from openpoly.portfolio.equity import build_equity_curve
from openpoly.runtime.exit_monitor import ExitMonitor
from openpoly.runtime.section_log import exit_log, settlement_log
from openpoly.runtime.settlement_monitor import SettlementMonitor
from openpoly.sections.exit.threshold_v0 import ThresholdExitConfig, ThresholdExitV0
from tests.test_executor import _isolate_market_store  # noqa: F401 — autouse fixture
from tests.test_paper_fees import RATE, _buy, _fee, _set_bid, store  # noqa: F401
from tests.test_settlement_monitor import _fetcher_returning, _raw_market


async def test_exit_log_shows_the_pnl_net_of_both_fees(store) -> None:  # noqa: F811
    exit_log.reset()
    _buy(store)
    _set_bid(0.50)
    monitor = ExitMonitor(
        exit_section=ThresholdExitV0(ThresholdExitConfig(take_profit_enabled=True)),
        executor=PaperExecutor(store),
        tick_interval_seconds=3600,
    )
    monitor.configure(store)
    await monitor._tick_once()
    entry = [e for e in exit_log.entries() if e.verdict == "ok"][-1]
    expected = (0.50 - 0.40) * 20.0 - _fee(0.40, 20.0) - _fee(0.50, 20.0)
    assert entry.realized_pnl == pytest.approx(expected)
    closed = store.list_positions()[0]
    assert closed.realized_pnl == pytest.approx(expected)


async def test_settlement_log_shows_the_pnl_net_of_the_buy_fee(store) -> None:  # noqa: F811
    settlement_log.reset()
    held = _buy(store)
    raw = [_raw_market(condition_id=held_condition(store), closed=True, outcome_prices=["1", "0"])]
    monitor = SettlementMonitor(fetcher=_fetcher_returning(raw))
    monitor.configure(store)
    await monitor._tick_once()
    entry = settlement_log.entries()[-1]
    assert entry.verdict == "ok"
    rec = store.get_position(held.position_id)
    assert rec.realized_pnl == pytest.approx((1.0 - 0.40) * 20.0 - _fee(0.40, 20.0))
    assert entry.realized_pnl == pytest.approx(rec.realized_pnl)


def held_condition(store) -> str:  # noqa: F811
    return store.list_positions()[0].condition_id


@pytest.mark.parametrize("route", ["detail", "list"])
def test_routes_report_the_exit_price_actually_sold_at(store, route: str) -> None:  # noqa: F811
    app.dependency_overrides[get_portfolio_store] = lambda: store
    try:
        held = _buy(store)
        _set_bid(0.50)
        PaperExecutor(store).execute_sell(held, close_reason="take_profit", ts=200.0)
        client = TestClient(app)
        if route == "detail":
            body = client.get(f"/api/positions/{held.position_id}").json()
        else:
            body = client.get("/api/positions").json()["positions"][0]
    finally:
        app.dependency_overrides.clear()
    assert body["exit_price"] == pytest.approx(0.50)


def test_exit_price_is_the_vwap_of_partial_sells(store) -> None:  # noqa: F811
    held = _buy(store)
    store.record_sell(
        held.position_id, sold_qty=10.0, sell_price=0.50, ts=200.0, close_reason="take_profit"
    )
    store.record_sell(
        held.position_id, sold_qty=10.0, sell_price=0.60, ts=201.0, close_reason="take_profit"
    )
    assert store.exit_prices([held.position_id]) == {held.position_id: pytest.approx(0.55)}


def test_open_position_has_no_exit_price(store) -> None:  # noqa: F811
    held = _buy(store)
    assert store.exit_prices([held.position_id]) == {}


def test_equity_charges_the_paid_buy_fee_while_open(store) -> None:  # noqa: F811
    held = _buy(store)
    factory = store._session_factory
    with factory() as session:
        session.add(
            OrderBookSnapshot(
                token_id=held.token_id,
                recorded_at=150.0,
                bids_json=json.dumps([[0.40, 100.0]]),
                asks_json=json.dumps([[0.41, 100.0]]),
            )
        )
        session.commit()
    curve = build_equity_curve(factory)
    assert curve.unrealized == pytest.approx(-_fee(0.40, 20.0))
    assert curve.total == pytest.approx(-_fee(0.40, 20.0))


def _snapshot(factory, token: str, at: float, bid: float) -> None:
    with factory() as session:
        session.add(
            OrderBookSnapshot(
                token_id=token,
                recorded_at=at,
                bids_json=json.dumps([[bid, 50.0]]),
                asks_json=json.dumps([[0.99, 50.0]]),
            )
        )
        session.commit()


def test_shadow_return_is_net_of_the_round_trip_fee(store) -> None:  # noqa: F811
    factory = store._session_factory
    quote = ShadowQuote(side="yes", token_id="t", ask=0.40, bid=0.39, edge=0.1, fee_rate=RATE)
    ShadowStore(factory).record(
        ts=1000.0, news_id="n", market_id="m", p_model=0.6, confidence="low", quote=quote
    )
    _snapshot(factory, "t", 1000.0 + 6 * 3600.0, 0.50)
    report = shadow_report(factory, positions=[], cost_basis={}, now=1000.0 + 7 * 3600.0)
    cost = 0.40 + RATE * 0.40 * 0.60
    proceeds = 0.50 - RATE * 0.50 * 0.50
    measured = report["shadow"]["low"]["horizons"]["6h"]["mean_return"]
    assert measured == pytest.approx((proceeds - cost) / cost)


def test_shadow_pick_without_a_rate_stays_gross(store) -> None:  # noqa: F811
    factory = store._session_factory
    quote = ShadowQuote(side="yes", token_id="t", ask=0.40, bid=0.39, edge=0.1)
    ShadowStore(factory).record(
        ts=1000.0, news_id="n", market_id="m", p_model=0.6, confidence="low", quote=quote
    )
    _snapshot(factory, "t", 1000.0 + 6 * 3600.0, 0.50)
    report = shadow_report(factory, positions=[], cost_basis={}, now=1000.0 + 7 * 3600.0)
    assert report["shadow"]["low"]["horizons"]["6h"]["mean_return"] == pytest.approx(0.25)

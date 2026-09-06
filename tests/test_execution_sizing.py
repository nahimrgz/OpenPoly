"""Tests for openpoly.execution.sizing — the one order-sizing rule both
executors share, plus the paper/live parity it is meant to guarantee."""

from __future__ import annotations

import time
import types

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.execution import PaperExecutor
from openpoly.execution import live_executor as le_mod
from openpoly.execution.live_executor import LiveExecutor
from openpoly.execution.sizing import (
    MAX_TOKEN_PRICE,
    MIN_NOTIONAL_USD,
    MIN_SELL_SHARES,
    MIN_TOKEN_PRICE,
    SIZE_DECIMALS,
    in_price_band,
    is_dust_qty,
    quantize_size,
)
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook, normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary
from openpoly.portfolio import PortfolioStore
from openpoly.portfolio.store import MIN_SELLABLE_QTY
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

# ---------- quantize_size ----------


def test_quantize_floors_to_two_decimal_size() -> None:
    """The SDK's ROUNDING_CONFIG allows 2 size decimals at every tick size."""
    assert quantize_size(5.567, 0.50) == pytest.approx(5.56)
    assert quantize_size(20.0, 0.42) == 20.0


def test_quantize_below_one_share_is_zero() -> None:
    """0.6 shares is a remainder, not a placeable order."""
    assert quantize_size(0.6, 0.50) == 0.0
    assert quantize_size(0.0, 0.50) == 0.0


def test_quantize_does_not_require_a_cent_aligned_notional() -> None:
    """3-decimal prices (the 0.001-tick regime winners exit through) must not
    zero a whole-share qty: the venue rounds the amount itself."""
    assert quantize_size(6.0, 0.993) >= 6.0 - 0.01
    assert quantize_size(9.0, 0.999) == 9.0
    assert quantize_size(3.0, 0.005) == 3.0


def test_min_notional_is_the_venue_floor_plus_buffer() -> None:
    """$1.00 server minimum + a $0.10 rounding buffer."""
    assert MIN_NOTIONAL_USD == pytest.approx(1.10)


def test_is_dust_qty_is_the_shared_one_share_sell_minimum() -> None:
    """The exit monitor and both executors have to agree on what dust is, or
    the monitor keeps producing sells the executors will only ever skip."""
    assert MIN_SELL_SHARES == pytest.approx(1.0)
    assert is_dust_qty(0.6) is True
    assert is_dust_qty(0.999999) is True
    assert is_dust_qty(1.0) is False
    assert is_dust_qty(6.0) is False


def test_portfolio_sellable_minimum_tracks_the_venue_size_precision() -> None:
    """``MIN_SELLABLE_QTY`` is duplicated in the portfolio layer (which must
    not import execution); this pins the two definitions together."""
    assert MIN_SELLABLE_QTY == pytest.approx(10**-SIZE_DECIMALS)


# ---------- in_price_band ----------


def test_in_price_band_true_at_the_exact_edges() -> None:
    """0.0001 and 0.9999 are the finest tick's own edges — venue-legal."""
    assert MIN_TOKEN_PRICE == pytest.approx(0.0001)
    assert MAX_TOKEN_PRICE == pytest.approx(0.9999)
    assert in_price_band(MIN_TOKEN_PRICE) is True
    assert in_price_band(MAX_TOKEN_PRICE) is True


def test_in_price_band_false_just_outside_the_edges() -> None:
    """0.00005 and 0.99995 are legal book prices in (0, 1) but the SDK's own
    order builder would refuse them — the tighter venue band must reject
    both."""
    assert in_price_band(0.00005) is False
    assert in_price_band(0.99995) is False


def test_in_price_band_false_for_none() -> None:
    assert in_price_band(None) is False


# ---------- paper / live parity ----------


@pytest.fixture(autouse=True)
def _isolate_market_store():
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    yield
    market_source_manager.store = saved


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch) -> None:
    """Rebind ``live_executor``'s own ``time`` name to a fake namespace so any
    retry loop a test drives through ``LiveExecutor`` (settle retries, CTF
    polls, persist retries) never sleeps for real.

    Rebinding the module-level name, rather than patching attributes on the
    real ``time`` module object, keeps this scoped to ``live_executor.py``:
    ``sys.modules["time"]`` — the one real module every other piece of code in
    the process sees — is left untouched. See ``tests/test_live_executor.py``'s
    identically-shaped ``no_sleep`` fixture for the full rationale.
    """
    fake_time = types.SimpleNamespace(sleep=lambda *_a, **_k: None, monotonic=time.monotonic)
    monkeypatch.setattr(le_mod, "time", fake_time)


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


class _NoopClob:
    """Records posts; every read succeeds so nothing but sizing can skip.

    ``ctf_balance_raw`` is what the SELL path's CTF-cache poll reads — high by
    default so only sizing decides the outcome.
    """

    def __init__(
        self,
        *,
        order_response: dict | None = None,
        ctf_balance_raw: int = 10**18,
    ) -> None:
        self.posted: list = []
        self._response = order_response or {
            "success": True,
            "orderID": "0x1",
            "makingAmount": "1",
            "takingAmount": "2",
        }
        self._ctf_balance_raw = ctf_balance_raw

    def create_and_post_order(self, order_args, options, order_type):
        self.posted.append(order_args)
        return self._response

    def update_balance_allowance(self, params):
        return None

    def get_balance_allowance(self, params):
        return {"balance": str(self._ctf_balance_raw), "allowances": {}}

    def cancel_order(self, payload):
        return {"canceled": [payload.orderID], "not_canceled": {}}

    def get_order(self, order_id):
        return {"size_matched": "0"}


def _market(market_id: str = "m1"):
    m = normalize_gamma_market(
        {
            "id": market_id,
            "conditionId": f"0x{market_id}",
            "question": "Q?",
            "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
        },
        event={"id": "e", "title": "E", "tags": []},
    )
    assert m is not None
    return m


def _populate(market, *books: OrderBook) -> None:
    s = market_source_manager.store
    s.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
    s.set_order_books(list(books))


def _book(token_id: str, *, bid: float, ask: float, bid_size: float = 100.0) -> OrderBook:
    return OrderBook(token_id=token_id, ts=1.0, bids=[(bid, bid_size)], asks=[(ask, 100.0)])


def test_both_executors_reject_the_same_sub_floor_buy(store) -> None:
    """2 shares @ $0.50 = $1.00 — under the shared $1.10 floor. Neither
    executor may place it, and neither may open a position."""
    m = _market()
    _populate(m, _book(m.yes_token_id, bid=0.48, ask=0.50))
    intent = OrderIntent(market_id="m1", side="yes", price=0.50, qty=2.0)

    clob = _NoopClob()
    live = LiveExecutor(portfolio=store, clob_client=clob).execute_buy(intent, news_id="n", ts=1.0)
    paper = PaperExecutor(store).execute_buy(intent, news_id="n", ts=1.0)

    assert live.filled is False
    assert paper.filled is False
    assert clob.posted == []
    assert store.get_open_position("m1", "yes") is None


def test_paper_sell_is_capped_by_bid_depth_and_leaves_remainder_open(store) -> None:
    """The paper BUY caps at level-1 ask depth; the SELL must cap at level-1
    bid depth the same way, and leave the unsold remainder open (live does)."""
    m = _market()
    _populate(m, _book(m.yes_token_id, bid=0.55, ask=0.56, bid_size=4.0))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )

    r = PaperExecutor(store).execute_sell(held, close_reason="take_profit", ts=200.0)

    assert r.filled is True
    assert r.qty == pytest.approx(4.0)  # only the bid depth filled
    rec = store.get_position(held.position_id)
    assert rec is not None
    assert rec.status == "open"
    assert rec.qty == pytest.approx(6.0)
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 4.0)


def test_both_executors_sell_a_whole_position_into_a_three_decimal_bid(store) -> None:
    """0.999 is where winners exit (the 0.001-tick regime above 0.96). A
    9-share position is a placeable order there: both executors must fill it
    at 0.999 — neither may treat it as unsellable."""
    m = _market()
    _populate(m, _book(m.yes_token_id, bid=0.999, ask=1.0))

    paper_held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=9.0,
        ts=100.0,
        news_id="n",
    )
    paper = PaperExecutor(store).execute_sell(paper_held, close_reason="take_profit", ts=200.0)
    assert paper.filled is True
    assert paper.price == pytest.approx(0.999)
    assert paper.qty == pytest.approx(9.0)
    paper_rec = store.get_position(paper_held.position_id)
    assert paper_rec is not None
    assert paper_rec.status == "closed"
    assert paper_rec.close_reason == "take_profit"

    live_held = store.open_position(
        market_id="m1",
        side="no",
        token_id=m.no_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=9.0,
        ts=100.0,
        news_id="n",
    )
    _populate(
        m, _book(m.yes_token_id, bid=0.999, ask=1.0), _book(m.no_token_id, bid=0.999, ask=1.0)
    )
    clob = _NoopClob(
        order_response={
            "success": True,
            "orderID": "0xS",
            "makingAmount": "9.0",  # tokens sent
            "takingAmount": "8.991",  # pUSD received → 0.999
            "transactionsHashes": ["0xT"],
        }
    )
    live = LiveExecutor(portfolio=store, clob_client=clob).execute_sell(
        live_held, close_reason="take_profit", ts=200.0
    )
    assert live.filled is True
    assert live.price == pytest.approx(0.999)
    assert clob.posted[0].size == pytest.approx(9.0)
    live_rec = store.get_position(live_held.position_id)
    assert live_rec is not None
    assert live_rec.status == "closed"
    assert live_rec.close_reason == "take_profit"


def test_both_executors_skip_a_sub_one_share_remainder_and_leave_it_open(store) -> None:
    """A genuine <1-share remainder is still worth its resolution price at
    settlement. Neither executor may write it off: both skip and leave the row
    open for the settlement monitor to close."""
    m = _market()
    _populate(m, _book(m.yes_token_id, bid=0.55, ask=0.56))

    paper_held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=0.6,
        ts=100.0,
        news_id="n",
    )
    paper = PaperExecutor(store).execute_sell(paper_held, close_reason="take_profit", ts=200.0)
    assert paper.filled is False
    assert paper.skip_reason == "dust_remainder"
    paper_rec = store.get_position(paper_held.position_id)
    assert paper_rec is not None
    assert paper_rec.status == "open"
    assert paper_rec.qty == pytest.approx(0.6)
    assert paper_rec.realized_pnl is None

    live_held = store.open_position(
        market_id="m1",
        side="no",
        token_id=m.no_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=0.6,
        ts=100.0,
        news_id="n",
    )
    _populate(
        m, _book(m.yes_token_id, bid=0.55, ask=0.56), _book(m.no_token_id, bid=0.55, ask=0.56)
    )
    clob = _NoopClob()
    live = LiveExecutor(portfolio=store, clob_client=clob).execute_sell(
        live_held, close_reason="take_profit", ts=200.0
    )
    assert live.filled is False
    assert live.skip_reason == "dust_remainder"
    assert clob.posted == []
    live_rec = store.get_position(live_held.position_id)
    assert live_rec is not None
    assert live_rec.status == "open"
    assert live_rec.qty == pytest.approx(0.6)

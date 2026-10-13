"""Tests for Executor — the level-1 paper fill service (PF3).

The executor reads the live MarketStore singleton, so each test gets a fresh
catalog via the autouse fixture and a fresh PortfolioStore on a throwaway DB.
"""

from __future__ import annotations

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.execution import PaperExecutor as Executor
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook, normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary
from openpoly.portfolio import PortfolioStore
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent


@pytest.fixture(autouse=True)
def _isolate_market_store():
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    yield
    market_source_manager.store = saved


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


def _market(market_id: str = "m1", *, clob: str | None = None):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": "Q?",
        "clobTokenIds": clob or f'["yes-{market_id}", "no-{market_id}"]',
    }
    m = normalize_gamma_market(raw, event={"id": "e", "title": "E", "tags": []})
    assert m is not None
    return m


def _book(
    token_id: str,
    *,
    bid: float = 0.40,
    ask: float = 0.42,
    bid_size: float = 100.0,
    ask_size: float = 100.0,
) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(bid, bid_size)] if bid_size else [],
        asks=[(ask, ask_size)] if ask_size else [],
    )


def _populate(market, *books: OrderBook) -> None:
    s = market_source_manager.store
    s.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
    s.set_order_books(list(books))


def _intent(market_id: str = "m1", side: str = "yes", qty: float = 20.0) -> OrderIntent:
    return OrderIntent(market_id=market_id, side=side, price=0.42, qty=qty)


# ---------- execute_buy ----------


def test_buy_fills_at_level1_ask(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    r = Executor(store).execute_buy(_intent(qty=20.0), news_id="n1", ts=100.0)
    assert r.filled
    assert r.price == 0.42
    assert r.qty == 20.0
    assert r.position_id is not None
    assert store.get_open_position("m1", "yes") is not None


def test_buy_qty_capped_by_level1_depth(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42, ask_size=5.0))
    r = Executor(store).execute_buy(_intent(qty=100.0), news_id="n1", ts=1.0)
    assert r.filled
    assert r.qty == 5.0  # capped to the level-1 ask depth


def test_buy_dust_skip(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    # qty 1 @ 0.42 = $0.42 notional, below the $1 floor.
    r = Executor(store).execute_buy(_intent(qty=1.0), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "dust"


def test_buy_position_exists_skip(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    ex = Executor(store)
    assert ex.execute_buy(_intent(), news_id="n1", ts=1.0).filled
    r = ex.execute_buy(_intent(), news_id="n2", ts=2.0)
    assert not r.filled
    assert r.skip_reason == "position_exists"


def test_buy_market_not_found_skip(store) -> None:
    r = Executor(store).execute_buy(_intent(market_id="zzz"), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "market_not_found"


def test_buy_no_order_book_skip(store) -> None:
    _populate(_market())  # market in catalog, no books sampled
    r = Executor(store).execute_buy(_intent(), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "no_order_book"


def test_buy_no_ask_liquidity_skip(store) -> None:
    _populate(_market(), _book("yes-m1", ask_size=0.0))  # bids only
    r = Executor(store).execute_buy(_intent(), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "no_ask_liquidity"


def test_buy_out_of_band_ask_skip(store) -> None:
    """A book level of 0.00005 is legal in (0, 1) but outside the venue's
    tighter tick band — paper must skip it exactly as live would refuse it,
    never open a position at a price the venue could not have produced."""
    _populate(_market(), _book("yes-m1", ask=0.00005))
    r = Executor(store).execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "price_out_of_band"
    assert store.get_open_position("m1", "yes") is None


def test_buy_no_token_skip(store) -> None:
    # Market carries only the YES token; side=no has no token.
    _populate(_market(clob='["yes-m1"]'), _book("yes-m1", ask=0.42))
    r = Executor(store).execute_buy(_intent(side="no"), news_id="n1", ts=1.0)
    assert not r.filled
    assert r.skip_reason == "no_token"


def test_buy_no_side_reads_no_token_book(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42), _book("no-m1", ask=0.55))
    r = Executor(store).execute_buy(_intent(side="no", qty=20.0), news_id="n1", ts=1.0)
    assert r.filled
    assert r.price == 0.55  # the NO token's own book, not a flipped YES book
    held = store.get_open_position("m1", "no")
    assert held is not None and held.token_id == "no-m1"


# ---------- execute_sell ----------


def test_sell_fills_at_level1_bid(store) -> None:
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None
    r = ex.execute_sell(held, close_reason="take_profit", ts=200.0, trigger="take_profit")
    assert r.filled
    assert r.price == 0.40  # level-1 bid
    assert store.get_open_position("m1", "yes") is None  # now closed


def test_sell_out_of_band_bid_skip(store) -> None:
    """Twin of the buy-side check: a bid of 0.99995 is legal in (0, 1) but
    outside the venue's tighter tick band — paper must skip and leave the
    position open, never book a sell at a price the venue could not have
    produced."""
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None
    market_source_manager.store.set_order_books([_book("yes-m1", bid=0.99995, ask=1.0)])
    r = ex.execute_sell(held, close_reason="take_profit", ts=200.0)
    assert not r.filled
    assert r.skip_reason == "price_out_of_band"
    assert store.get_open_position("m1", "yes") is not None


def test_sell_no_order_book_skip(store) -> None:
    _populate(_market(), _book("yes-m1", bid=0.40, ask=0.42))
    ex = Executor(store)
    ex.execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None
    market_source_manager.store.set_order_books([])  # book gone
    r = ex.execute_sell(held, close_reason="manual", ts=2.0)
    assert not r.filled
    assert r.skip_reason == "no_order_book"


# ---------- configuration ----------


def test_unconfigured_executor_raises() -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    with pytest.raises(RuntimeError, match="PortfolioStore"):
        Executor().execute_buy(_intent(), news_id="n1", ts=1.0)


# ---------- entry calibration signals ----------


def test_buy_persists_the_entry_signals_from_the_intent(store) -> None:
    """The executor is the only thing that touches the position row, so it is
    what has to carry the entry decision's signals into it."""
    _populate(_market(), _book("yes-m1", ask=0.42))
    intent = OrderIntent(
        market_id="m1",
        side="yes",
        price=0.42,
        qty=20.0,
        p_model=0.68,
        confidence="high",
        edge=0.26,
    )
    r = Executor(store).execute_buy(intent, news_id="n1", ts=1.0)
    assert r.filled
    rec = store.get_position(r.position_id)
    assert rec is not None
    assert rec.entry_p_model == 0.68
    assert rec.entry_confidence == "high"
    assert rec.entry_edge == 0.26


def test_buy_without_signals_leaves_them_null(store) -> None:
    _populate(_market(), _book("yes-m1", ask=0.42))
    r = Executor(store).execute_buy(_intent(qty=20.0), news_id="n1", ts=1.0)
    rec = store.get_position(r.position_id)
    assert rec is not None and rec.entry_p_model is None


# ---------- sell sweep down to the decision's mark (floor_price) ----------


def _thin_top_book(token_id: str) -> OrderBook:
    """A realistic thin top: 3 + 2 probe shares above real size at 0.33."""
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(0.48, 3.0), (0.40, 2.0), (0.33, 50.0)],
        asks=[(0.50, 100.0)],
    )


def _open(store, qty: float = 20.0):
    return store.open_position(
        market_id="m1",
        side="yes",
        token_id="yes-m1",
        condition_id="0xm1",
        price=0.50,
        qty=qty,
        ts=1.0,
        news_id="n",
    )


def test_sell_sweeps_levels_down_to_the_floor_at_vwap(store) -> None:
    """Regression: the sell took min(qty, level-1 size) at the level-1 bid —
    3 of 20 shares at 0.48 — and left the rest for another tick, though the
    decision was made at the 0.33 mark."""
    _populate(_market(), _thin_top_book("yes-m1"))
    held = _open(store)
    r = Executor(store).execute_sell(held, close_reason="stop_loss", ts=2.0, floor_price=0.33)
    assert r.filled
    assert r.qty == 20.0
    assert r.price == pytest.approx((3 * 0.48 + 2 * 0.40 + 15 * 0.33) / 20)
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "closed"


def test_sell_never_goes_below_the_floor(store) -> None:
    _populate(_market(), _thin_top_book("yes-m1"))
    held = _open(store)
    r = Executor(store).execute_sell(held, close_reason="stop_loss", ts=2.0, floor_price=0.40)
    assert r.filled
    assert r.qty == 5.0  # 0.48 x3 + 0.40 x2; nothing at 0.33
    assert r.price == pytest.approx((3 * 0.48 + 2 * 0.40) / 5)
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open" and rec.qty == pytest.approx(15.0)


def test_sell_floor_above_the_best_bid_still_sells_level_one(store) -> None:
    """The book fell since the decision: fail toward closing at level 1."""
    _populate(_market(), _thin_top_book("yes-m1"))
    held = _open(store)
    r = Executor(store).execute_sell(held, close_reason="stop_loss", ts=2.0, floor_price=0.60)
    assert r.filled
    assert (r.qty, r.price) == (3.0, 0.48)


def test_sell_without_floor_keeps_level_one_behavior(store) -> None:
    _populate(_market(), _thin_top_book("yes-m1"))
    held = _open(store)
    r = Executor(store).execute_sell(held, close_reason="manual", ts=2.0)
    assert (r.qty, r.price) == (3.0, 0.48)

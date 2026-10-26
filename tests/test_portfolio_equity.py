"""Tests for build_equity_curve — equity-curve reconstruction (Portfolio Overview)."""

from __future__ import annotations

import json

import pytest

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.db.tables import OrderBookSnapshot
from openpoly.portfolio import PortfolioStore
from openpoly.portfolio.equity import MAX_POINTS, build_equity_curve


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/equity.db")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _book_row(token_id: str, recorded_at: float, bid: float | None) -> OrderBookSnapshot:
    """One order-book snapshot row. ``bid is None`` builds an empty book."""
    return OrderBookSnapshot(
        token_id=token_id,
        recorded_at=recorded_at,
        bids_json=json.dumps([] if bid is None else [[bid, 100.0]]),
        asks_json=json.dumps([[0.99, 100.0]]),
    )


def _snapshot(factory, token_id: str, recorded_at: float, bid: float | None) -> None:
    """Persist one order-book snapshot."""
    with factory() as s:
        s.add(_book_row(token_id, recorded_at, bid))
        s.commit()


def test_empty_ledger_returns_empty_curve(factory) -> None:
    curve = build_equity_curve(factory)
    assert curve.points == ()
    assert curve.realized == 0.0
    assert curve.unrealized == 0.0
    assert curve.total == 0.0
    assert curve.open_positions == 0


def test_open_position_no_snapshots_marks_at_entry(factory) -> None:
    PortfolioStore(factory).open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    curve = build_equity_curve(factory)
    # No snapshot → mark == entry → unrealized 0 at every point.
    assert curve.unrealized == 0.0
    assert curve.realized == 0.0
    assert curve.open_positions == 1
    assert all(p.unrealized == 0.0 for p in curve.points)


def test_open_position_unrealized_tracks_best_bid(factory) -> None:
    PortfolioStore(factory).open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    _snapshot(factory, "ty1", 150.0, 0.50)  # (0.50 - 0.40) * 10 = +1.00
    curve = build_equity_curve(factory)
    assert curve.unrealized == pytest.approx(1.00)
    assert curve.total == pytest.approx(1.00)
    pt = next(p for p in curve.points if p.ts == 150.0)
    assert pt.unrealized == pytest.approx(1.00)


def test_closed_position_realized_step(factory) -> None:
    store = PortfolioStore(factory)
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    store.close_position(
        held.position_id,
        sell_price=0.55,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    curve = build_equity_curve(factory)
    assert curve.realized == pytest.approx(1.50)  # (0.55 - 0.40) * 10
    assert curve.unrealized == 0.0
    assert curve.open_positions == 0
    before = [p for p in curve.points if p.ts < 200.0]
    after = [p for p in curve.points if p.ts >= 200.0]
    assert all(p.realized == 0.0 for p in before)
    assert all(p.realized == pytest.approx(1.50) for p in after)


def test_mix_open_and_closed(factory) -> None:
    store = PortfolioStore(factory)
    h1 = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    store.close_position(
        h1.position_id,
        sell_price=0.50,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    store.open_position(
        market_id="m2",
        side="no",
        token_id="tn2",
        condition_id="0xc2",
        price=0.30,
        qty=20.0,
        ts=300.0,
        news_id="n2",
    )
    _snapshot(factory, "tn2", 350.0, 0.35)  # (0.35 - 0.30) * 20 = +1.00
    curve = build_equity_curve(factory)
    assert curve.realized == pytest.approx(1.00)
    assert curve.unrealized == pytest.approx(1.00)
    assert curve.total == pytest.approx(2.00)
    assert curve.open_positions == 1


def test_snapshot_with_empty_bids_is_skipped(factory) -> None:
    PortfolioStore(factory).open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    _snapshot(factory, "ty1", 150.0, 0.50)
    _snapshot(factory, "ty1", 160.0, None)  # empty bids → ignored, mark held
    curve = build_equity_curve(factory)
    assert curve.unrealized == pytest.approx(1.00)


def test_same_token_closed_then_reopened(factory) -> None:
    store = PortfolioStore(factory)
    h1 = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    store.close_position(
        h1.position_id,
        sell_price=0.50,
        ts=200.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.45,
        qty=10.0,
        ts=300.0,
        news_id="n2",
    )
    _snapshot(factory, "ty1", 350.0, 0.48)  # (0.48 - 0.45) * 10 = +0.30
    curve = build_equity_curve(factory)
    assert curve.realized == pytest.approx(1.00)
    assert curve.unrealized == pytest.approx(0.30)
    assert curve.open_positions == 1


def test_dense_snapshots_are_thinned_but_events_and_summary_stay_exact(factory) -> None:
    store = PortfolioStore(factory)
    closed = store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )
    store.close_position(
        closed.position_id,
        sell_price=0.50,
        ts=5_000.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    store.open_position(
        market_id="m2",
        side="yes",
        token_id="ty2",
        condition_id="0xc2",
        price=0.20,
        qty=10.0,
        ts=200.0,
        news_id="n2",
    )
    # Bulk insert in one session; _snapshot commits per row.
    with factory() as s:
        s.add_all(
            _book_row(token_id, float(i * 2), bid)
            for i in range(5_000)
            for token_id, bid in (("ty1", 0.45), ("ty2", 0.10 + (i % 10) / 100))
        )
        s.commit()
    _snapshot(factory, "ty2", 10_000.0, 0.30)

    curve = build_equity_curve(factory)

    assert len(curve.points) <= MAX_POINTS + 4  # thinned axis + 2 opens, 1 close, now
    ts = [p.ts for p in curve.points]
    assert ts == sorted(ts)
    assert {100.0, 200.0, 5_000.0} <= set(ts)
    assert curve.realized == pytest.approx(1.00)
    assert curve.unrealized == pytest.approx(1.00)  # (0.30 - 0.20) * 10, latest mark
    assert curve.total == pytest.approx(2.00)


def test_short_position_keeps_its_marks_next_to_a_dense_long_one(factory) -> None:
    store = PortfolioStore(factory)
    store.open_position(
        market_id="m1",
        side="yes",
        token_id="ty1",
        condition_id="0xc1",
        price=0.40,
        qty=10.0,
        ts=0.0,
        news_id="n1",
    )
    short = store.open_position(
        market_id="m2",
        side="yes",
        token_id="ty2",
        condition_id="0xc2",
        price=0.20,
        qty=10.0,
        ts=5_000.0,
        news_id="n2",
    )
    store.close_position(
        short.position_id,
        sell_price=0.25,
        ts=5_050.0,
        close_reason="take_profit",
        trigger="take_profit",
    )
    short_marks = [5_000.5 + i for i in range(40)]
    with factory() as s:
        s.add_all(_book_row("ty1", float(i), 0.45) for i in range(20_000))
        s.add_all(_book_row("ty2", ts, 0.30) for ts in short_marks)
        s.commit()

    curve = build_equity_curve(factory)

    assert set(short_marks) <= {p.ts for p in curve.points}

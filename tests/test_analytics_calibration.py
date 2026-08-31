"""Tests for openpoly.analytics.calibration — the pure bucketing function.

Mostly synthetic ``PositionRecord``s: the bucketing itself needs no DB and no
live catalog. The cost basis a return is measured against is the exception —
it comes from the BUY fill ledger, because the position row's ``qty`` is the
*residual* after partial sells.
"""

from __future__ import annotations

import pytest

from openpoly.analytics.calibration import BUCKET_EDGES, calibration_report
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.portfolio import PortfolioStore, PositionRecord


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


def _closed(
    *,
    p_model: float,
    realized_pnl: float,
    side: str = "yes",
    qty: float = 10.0,
    entry: float = 0.50,
    position_id: int = 1,
    close_reason: str = "take_profit",
) -> PositionRecord:
    return PositionRecord(
        id=position_id,
        market_id=f"m{position_id}",
        side=side,  # type: ignore[arg-type]
        token_id=f"t{position_id}",
        condition_id=f"0xm{position_id}",
        qty=qty,
        avg_entry_price=entry,
        status="closed",
        opened_at=100.0,
        closed_at=200.0,
        close_reason=close_reason,
        realized_pnl=realized_pnl,
        entry_p_model=p_model,
        entry_confidence="medium",
        entry_edge=0.10,
    )


def _open(*, p_model: float, position_id: int = 99) -> PositionRecord:
    return PositionRecord(
        id=position_id,
        market_id="mo",
        side="yes",
        token_id="to",
        condition_id="0xmo",
        qty=10.0,
        avg_entry_price=0.50,
        status="open",
        opened_at=100.0,
        closed_at=None,
        close_reason=None,
        realized_pnl=None,
        entry_p_model=p_model,
    )


def test_report_shape_is_one_bucket_per_edge_pair() -> None:
    buckets = calibration_report([])
    assert [(b.lower, b.upper) for b in buckets] == list(zip(BUCKET_EDGES, BUCKET_EDGES[1:]))
    assert all(b.count == 0 for b in buckets)
    assert all(b.win_rate is None and b.mean_return is None for b in buckets)


def test_buckets_by_probability_and_counts_wins() -> None:
    positions = [
        _closed(p_model=0.62, realized_pnl=1.0, position_id=1),
        _closed(p_model=0.65, realized_pnl=-2.0, position_id=2),
        _closed(p_model=0.68, realized_pnl=3.0, position_id=3),
        _closed(p_model=0.75, realized_pnl=-1.0, position_id=4),
    ]
    by_lower = {b.lower: b for b in calibration_report(positions)}

    assert by_lower[0.6].count == 3
    assert by_lower[0.6].win_rate == pytest.approx(2 / 3)
    assert by_lower[0.7].count == 1
    assert by_lower[0.7].win_rate == 0.0
    assert by_lower[0.5].count == 0


def test_mean_return_is_realized_pnl_over_cost_basis() -> None:
    # cost = 0.50 * 10 = $5; +$1 → +20%, -$2 → -40%; mean = -10%.
    positions = [
        _closed(p_model=0.55, realized_pnl=1.0, position_id=1),
        _closed(p_model=0.55, realized_pnl=-2.0, position_id=2),
    ]
    bucket = next(b for b in calibration_report(positions) if b.lower == 0.5)
    assert bucket.mean_return == pytest.approx(-0.10)


def test_no_side_is_bucketed_by_the_held_side_probability() -> None:
    """A NO position on p_model=0.2 is a 0.8 bet on the side actually held —
    bucketing the raw 0.2 would drop it out of the report entirely."""
    positions = [_closed(p_model=0.2, side="no", realized_pnl=1.0)]
    by_lower = {b.lower: b for b in calibration_report(positions)}
    assert by_lower[0.8].count == 1


def test_open_and_unlabelled_positions_are_excluded() -> None:
    """Only a closed position has an outcome; only a labelled one has a
    prediction. Anything else would bias the report."""
    unlabelled = _closed(p_model=0.7, realized_pnl=1.0, position_id=2)
    unlabelled = PositionRecord(**{**unlabelled.__dict__, "entry_p_model": None})
    positions = [_open(p_model=0.7), unlabelled]
    assert all(b.count == 0 for b in calibration_report(positions))


def test_top_bucket_includes_certainty() -> None:
    """p=1.0 belongs in the last bucket, not off the end of it."""
    by_lower = {b.lower: b for b in calibration_report([_closed(p_model=1.0, realized_pnl=1.0)])}
    assert by_lower[0.9].count == 1


def test_bucket_boundary_belongs_to_the_upper_bucket() -> None:
    by_lower = {b.lower: b for b in calibration_report([_closed(p_model=0.70, realized_pnl=1.0)])}
    assert by_lower[0.7].count == 1
    assert by_lower[0.6].count == 0


def test_zero_cost_basis_does_not_divide_by_zero() -> None:
    positions = [_closed(p_model=0.65, realized_pnl=0.0, entry=0.0, position_id=1)]
    bucket = next(b for b in calibration_report(positions) if b.lower == 0.6)
    assert bucket.count == 1
    # Unmeasurable basis: excluded from mean_return rather than counted as a
    # fake 0.0 return that dilutes the bucket's average.
    assert bucket.mean_return is None


def test_return_is_measured_against_the_opened_cost_basis_not_the_residual(store) -> None:
    """Buy 10 @ 0.40 ($4.00), sell 9.4 @ 0.55, then close the 0.6 remainder at
    0.0. Realized is $1.17 — a +29% trade. Measuring it against the *residual*
    0.6 shares ($0.24) reports +488% and makes an uncalibrated model look
    spectacular."""
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
        entry_p_model=0.75,
        entry_confidence="medium",
        entry_edge=0.10,
    )
    store.record_sell(
        held.position_id,
        sold_qty=9.4,
        sell_price=0.55,
        ts=150.0,
        close_reason="take_profit",
    )
    store.close_position(
        held.position_id,
        sell_price=0.0,
        ts=200.0,
        close_reason="settlement",
    )

    positions = store.list_positions(10)
    assert positions[0].realized_pnl == pytest.approx(1.17)
    buckets = calibration_report(positions, store.buy_cost_basis([p.id for p in positions]))

    bucket = next(b for b in buckets if b.lower == 0.7)
    assert bucket.count == 1
    assert bucket.mean_return == pytest.approx(0.2925)


# ---------- reconciled closes carry no measurable outcome ----------


def test_reconciled_closes_are_excluded() -> None:
    """A reconciled close records ``realized_pnl = 0`` by construction: the
    position was exited outside the ledger and the real exit price cannot be
    attributed back to it (see ``ReconciliationMonitor``). Counting that zero
    scores every such trade as a loss, so a bucket full of reconciled rows
    reads as a badly calibrated model rather than as missing data."""
    positions = [
        _closed(p_model=0.85, realized_pnl=0.0, position_id=1, close_reason="reconciled"),
        _closed(p_model=0.85, realized_pnl=0.0, position_id=2, close_reason="reconciled"),
        _closed(p_model=0.85, realized_pnl=5.0, position_id=3),
    ]
    buckets = calibration_report(positions)
    top = [b for b in buckets if b.lower == 0.8][0]
    assert top.count == 1
    assert top.win_rate == pytest.approx(1.0)


@pytest.mark.parametrize(
    "close_reason", ["settlement", "take_profit", "stop_loss", "peak_drawdown", "manual"]
)
def test_real_close_reasons_are_counted(close_reason: str) -> None:
    positions = [_closed(p_model=0.85, realized_pnl=5.0, close_reason=close_reason)]
    top = [b for b in calibration_report(positions) if b.lower == 0.8][0]
    assert top.count == 1


def test_a_null_close_reason_is_still_counted() -> None:
    """Only ``reconciled`` is fabricated; an unlabelled close is not."""
    positions = [_closed(p_model=0.85, realized_pnl=5.0, close_reason=None)]
    top = [b for b in calibration_report(positions) if b.lower == 0.8][0]
    assert top.count == 1

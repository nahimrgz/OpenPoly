"""Calibration — is the analyzer's stated probability worth anything?

The entry section sizes and gates off ``p_model``. Whether that number is
*calibrated* — whether the trades it opened at "70%" actually won about 70% of
the time — is the question that has to be answered before ``p_model`` is
allowed to influence position size at all (see
``EdgeThresholdConfig.size_edge_multiplier_max``). It cannot be answered from
the analyzer log: that ring evicts a call within a few hundred news events,
long before the position it opened closes. It is answered here, from the
``entry_p_model`` frozen onto the position row at open time.

The report is a plain bucketing, deliberately: no smoothing, no fitted curve,
no confidence intervals. A bucket whose win rate sits near its own midpoint,
with enough closed positions behind it to mean anything, is the whole signal.

The frame is the *held side's* probability over its full ``[0, 1]`` range.
Each closed position is one forecast — "the side I bought wins with
probability q" — scored by whether it won, so the held-side frame is the one in
which forecast and outcome refer to the same event. v0 bought the side
``p_model`` favoured, so q was always ≥ 0.5 and the report bucketed only
``[0.5, 1]``; entry v1 picks the side by *edge* (``p_model = 0.55`` with a NO
ask of 0.25 buys NO at q = 0.45), so a q below 0.5 is now a real forecast and
dropping it biased the report toward the entries where model and price agreed.
(Mapping everything to the YES frame instead would need the market's
resolution, which an early exit never observes; the held-side win is what the
ledger actually records.)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from openpoly.portfolio import PositionRecord
from openpoly.sections.entry._types import held_side_probability

# Bucket boundaries over the *held side's* probability, full range (see the
# module docstring for why it is not ≥ 0.5 any more).
BUCKET_EDGES: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
# Digits a probability is rounded to before bucketing — far below any
# meaningful model precision, far above float noise.
_BUCKET_ROUND_DIGITS = 9

# Close reasons whose ``realized_pnl`` is not a measured outcome. A
# ``reconciled`` close books 0 by construction — the position was exited
# outside the ledger and the real exit price cannot be attributed back to it
# (see ``ReconciliationMonitor``) — so scoring it counts a trade whose result
# is unknown as a loss, and a run of them reads as a miscalibrated model
# rather than as missing data. Every other reason (``settlement``,
# ``take_profit``, ``stop_loss``, ``peak_drawdown``, manual) closed at a price
# that actually happened and is counted.
UNMEASURED_CLOSE_REASONS: frozenset[str] = frozenset({"reconciled"})


@dataclass(frozen=True)
class CalibrationBucket:
    """One probability bucket ``[lower, upper)`` — the top one includes 1.0.

    ``win_rate`` is the fraction of the bucket's closed positions with positive
    realized PnL; compare it against the bucket midpoint. ``mean_return`` is the
    average of ``realized_pnl / cost_basis``, where the cost basis is what was
    paid to *open* the position — it answers a different question (is the edge
    worth trading) and can disagree with the win rate.
    Both are None for an empty bucket rather than a misleading 0.0;
    ``mean_return`` is also None when no row in the bucket has a measurable
    (positive) cost basis.
    """

    lower: float
    upper: float
    count: int
    win_rate: float | None
    mean_return: float | None


def _held_side_probability(record: PositionRecord) -> float | None:
    """The model's probability for the side actually held.

    A NO position on ``p_model=0.2`` is a 0.8 bet on the outcome it bought, so
    bucketing the raw 0.2 would misfile it. Returns None when the value
    falls outside [0, 1] (or is not a number), which no entry section can
    produce — such a row is a data defect and is left out (and counted as
    ``excluded``) rather than quietly clamped into a bucket it does not
    belong to.
    """
    p_model = record.entry_p_model
    if p_model is None:
        return None
    held = held_side_probability(p_model, record.side)
    if not (BUCKET_EDGES[0] <= held <= BUCKET_EDGES[-1]):  # also rejects NaN
        return None
    return held


def _bucket_index(probability: float) -> int:
    """Index of the bucket owning ``probability``; boundaries belong to the
    upper bucket and 1.0 belongs to the last one.

    The probability is rounded first: a NO holding's ``1 - p_model`` carries
    float noise (``1 - 0.8`` is ``0.19999999999999996``) that would otherwise
    drop it one bucket below its edge."""
    probability = round(probability, _BUCKET_ROUND_DIGITS)
    for index in range(len(BUCKET_EDGES) - 2, -1, -1):
        if probability >= BUCKET_EDGES[index]:
            return index
    return 0


@dataclass(frozen=True)
class CalibrationReport:
    """The buckets plus ``excluded``: closed, labelled, measured positions
    that could not be bucketed (a held-side probability outside [0, 1] — a
    data defect). Open, unlabelled and unmeasured positions are not
    "excluded": they are simply not forecasts with a known outcome."""

    buckets: list[CalibrationBucket]
    excluded: int


def calibration_summary(
    positions: Iterable[PositionRecord],
    cost_basis: Mapping[int, float] | None = None,
) -> CalibrationReport:
    """Bucket closed, labelled positions by held-side probability.

    Excluded: still-open positions (no outcome yet), positions without an
    ``entry_p_model`` (opened manually, by reconciliation, or before the column
    existed), and positions closed for a reason in
    ``UNMEASURED_CLOSE_REASONS`` — counting any of them would bias the very
    number being measured.

    ``cost_basis`` maps position id → what was paid to open it, from
    ``PortfolioStore.buy_cost_basis``. It is what ``mean_return`` divides by,
    and it has to come from the fill ledger: ``record_sell`` decrements
    ``PositionRecord.qty`` on a partial sell while ``realized_pnl`` keeps
    accruing on the whole position, so ``avg_entry_price * qty`` measures the
    full gain against a residual sliver of the stake (buy 10 @ 0.40, sell 9.4
    @ 0.55, close 0.6 → +488% reported for a +29% trade). A position missing
    from the mapping falls back to ``avg_entry_price * qty``, which is exact
    for any position that was never partially sold.

    Always returns one bucket per ``BUCKET_EDGES`` pair, in order, including
    empty ones: a gap in the coverage is itself worth seeing.
    """
    basis_by_id: Mapping[int, float] = cost_basis or {}
    wins: list[int] = [0] * (len(BUCKET_EDGES) - 1)
    counts: list[int] = [0] * (len(BUCKET_EDGES) - 1)
    returns: list[float] = [0.0] * (len(BUCKET_EDGES) - 1)
    measured: list[int] = [0] * (len(BUCKET_EDGES) - 1)
    excluded = 0

    for record in positions:
        if record.status != "closed" or record.realized_pnl is None:
            continue
        if record.close_reason in UNMEASURED_CLOSE_REASONS:
            continue
        if record.entry_p_model is None:
            continue
        probability = _held_side_probability(record)
        if probability is None:
            excluded += 1
            continue
        index = _bucket_index(probability)
        counts[index] += 1
        if record.realized_pnl > 0:
            wins[index] += 1
        basis = basis_by_id.get(record.id, record.avg_entry_price * record.qty)
        if basis > 0:
            returns[index] += record.realized_pnl / basis
            measured[index] += 1
        # basis <= 0 → the return is unmeasurable: leave it out of mean_return
        # instead of diluting the bucket with a fake 0.0. The row still counts
        # toward count / win_rate, which need no basis.

    buckets = [
        CalibrationBucket(
            lower=BUCKET_EDGES[i],
            upper=BUCKET_EDGES[i + 1],
            count=counts[i],
            win_rate=(wins[i] / counts[i]) if counts[i] else None,
            mean_return=(returns[i] / measured[i]) if measured[i] else None,
        )
        for i in range(len(counts))
    ]
    return CalibrationReport(buckets=buckets, excluded=excluded)

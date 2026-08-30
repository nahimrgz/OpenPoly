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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from openpoly.portfolio import PositionRecord

# Bucket boundaries over the *held side's* probability, which is always ≥ 0.5
# by construction (the entry section picks the side p_model favours).
BUCKET_EDGES: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


@dataclass(frozen=True)
class CalibrationBucket:
    """One probability bucket ``[lower, upper)`` — the top one includes 1.0.

    ``win_rate`` is the fraction of the bucket's closed positions with positive
    realized PnL; compare it against the bucket midpoint. ``mean_return`` is the
    average of ``realized_pnl / cost_basis``, where the cost basis is what was
    paid to *open* the position — it answers a different question (is the edge
    worth trading) and can disagree with the win rate.
    Both are None for an empty bucket rather than a misleading 0.0.
    """

    lower: float
    upper: float
    count: int
    win_rate: float | None
    mean_return: float | None


def _held_side_probability(record: PositionRecord) -> float | None:
    """The model's probability for the side actually held.

    A NO position on ``p_model=0.2`` is a 0.8 bet on the outcome it bought, so
    bucketing the raw 0.2 would drop it out of the report entirely. Returns
    None when the value falls outside [0.5, 1.0], which the entry section
    cannot produce — such a row is a data defect and is left out rather than
    quietly clamped into a bucket it does not belong to.
    """
    p_model = record.entry_p_model
    if p_model is None:
        return None
    held = p_model if record.side == "yes" else 1.0 - p_model
    if held < BUCKET_EDGES[0] or held > BUCKET_EDGES[-1]:
        return None
    return held


def _bucket_index(probability: float) -> int:
    """Index of the bucket owning ``probability``; boundaries belong to the
    upper bucket and 1.0 belongs to the last one."""
    for index in range(len(BUCKET_EDGES) - 2, -1, -1):
        if probability >= BUCKET_EDGES[index]:
            return index
    return 0


def calibration_report(
    positions: Iterable[PositionRecord],
    cost_basis: Mapping[int, float] | None = None,
) -> list[CalibrationBucket]:
    """Bucket closed, labelled positions by held-side probability.

    Excluded: still-open positions (no outcome yet) and positions without an
    ``entry_p_model`` (opened manually, by reconciliation, or before the column
    existed) — counting either would bias the very number being measured.

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

    for record in positions:
        if record.status != "closed" or record.realized_pnl is None:
            continue
        probability = _held_side_probability(record)
        if probability is None:
            continue
        index = _bucket_index(probability)
        counts[index] += 1
        if record.realized_pnl > 0:
            wins[index] += 1
        basis = basis_by_id.get(record.id, record.avg_entry_price * record.qty)
        returns[index] += record.realized_pnl / basis if basis > 0 else 0.0

    return [
        CalibrationBucket(
            lower=BUCKET_EDGES[i],
            upper=BUCKET_EDGES[i + 1],
            count=counts[i],
            win_rate=(wins[i] / counts[i]) if counts[i] else None,
            mean_return=(returns[i] / counts[i]) if counts[i] else None,
        )
        for i in range(len(counts))
    ]

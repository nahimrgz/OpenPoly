"""Depth-guarded book prices — one rule shared by the entry and the exit.

A resting level-1 quote can be a single minimum-size probe sitting far from
fair value. Marking (or gating) on it produced false stop-outs on the exit and
let entries through whose real bid sat far below the quoted one. Both sides of
the system therefore read a book through ``first_level_with_size``: the first
level carrying at least ``MIN_LEVEL_SIZE`` shares — a price someone is actually
committed to at a size larger than the venue minimum.
"""

from __future__ import annotations

# Minimum resting size (in shares) for a level to count as the book's price.
# Polymarket's minimum order notional is $1, which at the 0.20–0.80 prices this
# system trades is 1.25–5 shares — so any level below ~5 shares can be a single
# minimum-size probe or dust order rather than a price anyone is committed to.
# Requiring 5 shares means the price is always backed by at least one order
# larger than the venue minimum, which is what makes a stop-loss fired off it
# an executable price rather than an artifact.
MIN_LEVEL_SIZE = 5.0


def first_level_with_size(
    levels: list[tuple[float, float]],
    min_size: float = MIN_LEVEL_SIZE,
) -> float | None:
    """The price of the first level (best-first) carrying ``min_size`` or
    more, or None when no level qualifies — every alternative (the mid, the
    dust quote) is a price nothing can actually trade at."""
    for price, size in levels:
        if size >= min_size:
            return price
    return None


def taker_fee_per_share(rate: float, price: float) -> float:
    """USDC fee a taker pays per share traded at ``price``.

    Polymarket charges ``shares × rate × p × (1 − p)``
    (docs.polymarket.com/trading/fees): symmetric around 0.50, where it
    peaks. Makers pay nothing; ``rate`` is the market's ``taker_fee_rate``."""
    return rate * price * (1.0 - price)


def sweep(levels: list[tuple[float, float]], qty: float) -> tuple[float, float]:
    """Take ``qty`` from ``levels`` best-first.

    Returns ``(notional, remaining)``: the price-weighted notional of what the
    levels covered, and the part of ``qty`` they could not cover (``<= 0`` when
    fully covered)."""
    remaining = qty
    notional = 0.0
    for price, size in levels:
        take = min(remaining, size)
        notional += take * price
        remaining -= take
        if remaining <= 0:
            break
    return notional, remaining

"""Order sizing — the one venue rule both executors obey.

``PaperExecutor`` and ``LiveExecutor`` used to carry independent fill models:
paper floored a fill at $1.00 and never quantized, live floored at $1.10 and
floored qty to whole shares. Paper is the simulation of live, so a paper fill
that live would have rejected is a lie about the strategy's realized behavior.
Both now size through this module.

The rules are the Polymarket V2 CLOB rules, read off the SDK's own
``ROUNDING_CONFIG`` (``py_clob_client_v2.order_builder.builder``):

* **Two size decimals.** Every tick size in that table — 0.1, 0.01, 0.001,
  0.0001 — carries ``size=2``, and the builder itself rounds the derived
  maker/taker *amount* to its own ``amount`` decimals (4–6). So the client only
  has to floor the size to 2 decimals; it must not additionally demand that
  ``qty * price`` land on clean cents. That extra demand was the bug: at a
  3-decimal price (0.999, 0.993 — the 0.001-tick regime above 0.96 where
  winners exit) no whole-share qty aligns to a cent, so a perfectly sellable
  9-share position quantized to 0 and was treated as unsellable dust.
* **One share minimum.** A remainder below one share is not worth placing —
  it cannot clear the venue's $1 minimum at any price ≤ 1.0. It quantizes to
  0, and the sell side leaves that remainder OPEN (see
  ``dust_remainder_skip``) rather than writing it off.
* **Minimum notional.** The server minimum order is $1.00. We floor at $1.10
  so price/rounding wiggle at the edge cannot trip a server rejection.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from openpoly.execution.types import ExecResult

if TYPE_CHECKING:
    from openpoly.portfolio import HeldPosition

logger = logging.getLogger(__name__)

# $1.00 server minimum + a $0.10 buffer for price/rounding wiggle.
MIN_NOTIONAL_USD = 1.10

# ``RoundConfig.size`` is 2 for every tick size in the SDK's ROUNDING_CONFIG,
# so the tick size is not needed to floor a size. ``MIN_SELLABLE_QTY`` in
# openpoly/portfolio/store.py is the ``10**-SIZE_DECIMALS`` twin of this
# constant, duplicated there so the portfolio layer keeps no execution import.
SIZE_DECIMALS = 2

# One share is the smallest remainder worth placing: below it no price <= 1.0
# can clear the venue's $1 minimum notional. This is the single definition of
# "dust" — the exit monitor uses it to skip a position before evaluating it,
# and the executors use it (via ``quantize_size`` / ``dust_remainder_skip``) as
# the defensive fallback. If they disagreed, the monitor would keep producing
# sells the executors can only ever skip.
MIN_SELL_SHARES = 1.0

# Positions already warned about as an unsellable remainder — the exit monitor
# retries every tick and one WARNING per tick would be pure noise.
_dust_warned: set[int] = set()


def is_dust_qty(qty: float) -> bool:
    """True when ``qty`` is below the venue's one-share sell minimum.

    Callers that hold a position (rather than a candidate order size) ask this
    directly instead of round-tripping through ``quantize_size``, which needs a
    price they may not have.
    """
    return qty < MIN_SELL_SHARES


def quantize_size(qty: float, price: float) -> float:
    """Floor ``qty`` to a size the venue accepts.

    ``price`` is unused: the SDK allows 2 size decimals at every tick size and
    rounds the resulting maker/taker amount itself, so the price cannot make a
    size invalid. It stays in the signature because sizing is a per-(qty,
    price) venue question and both call sites read as such.

    Returns 0.0 only for a genuine remainder below one share — never for a qty
    the venue would have accepted.
    """
    if is_dust_qty(qty):
        return 0.0
    scale = 10**SIZE_DECIMALS
    # round() before floor() so binary representation (5.56 * 100 =
    # 555.99999999999994) cannot silently shave a hundredth off the size.
    return math.floor(round(qty * scale, 6)) / scale


def dust_remainder_skip(position: "HeldPosition") -> ExecResult:
    """Skip a sell whose remaining qty is below one share.

    A partial sell can leave less than one share open, which is not a placeable
    order — but it is not worthless either: the settlement monitor closes the
    row at the resolution price (1.0 on the winning side), so the remainder is
    worth ``qty * resolution_price``. Closing it at 0.0 instead booked a fake
    realized loss and orphaned tokens that were still in the wallet.

    So the position stays OPEN and the sell skips. The cost is honest and
    bounded: the row counts toward the open-position list and ``heat_cap_usd``
    until the market resolves. Warns once per position — the exit monitor
    retries every tick.
    """
    if position.position_id not in _dust_warned:
        _dust_warned.add(position.position_id)
        logger.warning(
            "dust remainder: position %d (%s %s) has %.6f shares left — below "
            "one share, not a placeable order; left open for settlement to close",
            position.position_id,
            position.market_id,
            position.side,
            position.qty,
        )
    return ExecResult.skip("dust_remainder")

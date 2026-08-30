"""In-flight close registry — which position ids the exit monitor is selling.

Three runtime loops can close the same position: the exit monitor (threshold
sell), the settlement monitor (resolved market) and the reconciliation monitor
(flat on-chain). They used to be effectively serialized by the event loop,
because the exit monitor's ``execute_sell`` ran inline. It no longer does: the
live sell is offloaded with ``asyncio.to_thread``, which hands the loop back
for the seconds the on-chain order takes. In that window the other two loops
can run, see a position the wallet has already emptied, and call
``close_position`` on it first. The exit monitor then fails to persist the real
fill (``ValueError: position N is closed``, five retries deep) and the actual
exit price and realized PnL are lost.

The fix is deliberately small: the exit monitor registers a position id here
for exactly as long as its sell is in flight, and the other two monitors skip
any id in the set for that tick. They are periodic sweeps — skipping is free,
they simply reconsider the position on the next tick, by which time the exit
monitor has either persisted its close or left the position open on purpose.

Process-local and single-threaded by construction: every mutation happens on
the event loop thread (the sell itself runs in a worker, the registry calls
around it do not), so a plain ``set`` needs no lock.
"""

from __future__ import annotations

_closing: set[int] = set()


def mark_closing(position_id: int) -> None:
    """Register ``position_id`` as being sold by the exit monitor."""
    _closing.add(position_id)


def clear_closing(position_id: int) -> None:
    """Deregister ``position_id`` — always from a ``finally``, so a failed sell
    can never leave a position permanently unreconcilable."""
    _closing.discard(position_id)


def is_closing(position_id: int) -> bool:
    """True while the exit monitor has a sell in flight for ``position_id``."""
    return position_id in _closing


def closing_ids() -> frozenset[int]:
    """Snapshot of the ids currently being sold (diagnostics / tests)."""
    return frozenset(_closing)


def reset_for_tests() -> None:
    """Drop every registered id — the set is process-global, so a test that
    leaves one behind (a cancelled sell, a fake executor that raised) would
    make the other monitors skip that position for the rest of the session.
    Called by an autouse fixture in ``tests/conftest.py``; never in runtime."""
    _closing.clear()

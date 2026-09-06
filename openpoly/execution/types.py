"""Shared types for the execution layer — keeps ExecResult import-cycle-free
between PaperExecutor and ExecutorDispatcher."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecResult:
    """Outcome of an executor call. On ``filled`` the ``price`` / ``qty`` /
    ``position_id`` fields are set; on a skip ``skip_reason`` carries a stable
    label and the fill fields stay None.

    ``resting_order_id`` names an order of ours the venue may still hold on the
    book because no cancel could be confirmed. It is set on skips AND on fills
    (a partial fill is persisted regardless — it happened on-chain), so a caller
    can back off instead of posting a second order on top of the first."""

    filled: bool
    skip_reason: str | None = None
    price: float | None = None
    qty: float | None = None
    position_id: int | None = None
    resting_order_id: str | None = None

    @classmethod
    def skip(cls, reason: str, *, resting_order_id: str | None = None) -> "ExecResult":
        return cls(filled=False, skip_reason=reason, resting_order_id=resting_order_id)

    @classmethod
    def ok(
        cls,
        *,
        price: float,
        qty: float,
        position_id: int,
        resting_order_id: str | None = None,
    ) -> "ExecResult":
        return cls(
            filled=True,
            price=price,
            qty=qty,
            position_id=position_id,
            resting_order_id=resting_order_id,
        )

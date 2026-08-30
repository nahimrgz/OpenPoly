"""Synchronous transactional store over the fill ledger + position table.

The portfolio is state that must never be lost — unlike the write-behind
``order_book`` / ``news`` sinks, every call here commits synchronously inside
one transaction. openPoly is one-shot per (market, side): a position is exactly
one buy fill, later one sell fill; ``open_position`` / ``close_position`` write
the fill and the position-projection row together.

The store is stateless — it holds only a session factory and touches the DB
only when a method is called, so it is safe to construct before ``init_db``.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from openpoly.db.tables import FillRow, PositionRow
from openpoly.portfolio.models import (
    CloseReason,
    Fill,
    HeldPosition,
    PositionRecord,
    Side,
)

# The venue's size precision: Polymarket accepts 2 size decimals, so 0.01
# shares is the smallest quantity that can be expressed as an order size at
# all. A residual below it is unsellable by construction — not float noise to
# be tolerated but a quantity no exit can ever clear — so ``record_sell``
# treats it as fully sold. Deliberately duplicated from ``SIZE_DECIMALS`` in
# openpoly/execution/sizing.py (10**-2): the portfolio layer is below
# execution and must not import it. tests/test_execution_sizing.py pins the
# two together.
MIN_SELLABLE_QTY = 0.01


def _to_held(row: PositionRow) -> HeldPosition:
    return HeldPosition(
        position_id=row.id,
        market_id=row.market_id,
        side=row.side,  # type: ignore[arg-type]
        token_id=row.token_id,
        condition_id=row.condition_id,
        qty=row.qty,
        avg_entry_price=row.avg_entry_price,
        opened_at=row.opened_at,
    )


def _to_record(row: PositionRow) -> PositionRecord:
    return PositionRecord(
        id=row.id,
        market_id=row.market_id,
        side=row.side,  # type: ignore[arg-type]
        token_id=row.token_id,
        condition_id=row.condition_id,
        qty=row.qty,
        avg_entry_price=row.avg_entry_price,
        status=row.status,  # type: ignore[arg-type]
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        close_reason=row.close_reason,
        realized_pnl=row.realized_pnl,
        entry_p_model=row.entry_p_model,
        entry_confidence=row.entry_confidence,
        entry_edge=row.entry_edge,
    )


def _to_fill(row: FillRow) -> Fill:
    return Fill(
        id=row.id,
        ts=row.ts,
        market_id=row.market_id,
        side=row.side,  # type: ignore[arg-type]
        action=row.action,  # type: ignore[arg-type]
        price=row.price,
        qty=row.qty,
        fee=row.fee,
        position_id=row.position_id,
        news_id=row.news_id,
        trigger=row.trigger,
        order_id=row.order_id,  # NEW
        tx_hash=row.tx_hash,  # NEW
    )


class PortfolioStore:
    """Repository over ``fill`` + ``position``. Construct with a session
    factory; every method opens its own short transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def open_position(
        self,
        *,
        market_id: str,
        side: Side,
        token_id: str,
        condition_id: str,
        price: float,
        qty: float,
        ts: float,
        news_id: str | None = None,
        order_id: str | None = None,  # NEW
        tx_hash: str | None = None,  # NEW
        entry_p_model: float | None = None,
        entry_confidence: str | None = None,
        entry_edge: float | None = None,
    ) -> HeldPosition:
        """Open a position: insert the position-projection row + its buy fill in
        one transaction. Raises ``IntegrityError`` if an open position for
        (market_id, side) already exists — the partial unique index backstop.

        ``entry_p_model`` / ``entry_confidence`` / ``entry_edge`` are the entry
        decision's own inputs, stored on the position so the outcome can later
        be joined to the belief (calibration). They default to None: a manual
        or reconciled open has no entry decision behind it.
        """
        with self._session_factory() as session:
            pos = PositionRow(
                market_id=market_id,
                side=side,
                token_id=token_id,
                condition_id=condition_id,
                qty=qty,
                avg_entry_price=price,
                status="open",
                opened_at=ts,
                closed_at=None,
                close_reason=None,
                realized_pnl=None,
                entry_p_model=entry_p_model,
                entry_confidence=entry_confidence,
                entry_edge=entry_edge,
            )
            session.add(pos)
            session.flush()  # populate pos.id for the fill's position_id
            session.add(
                FillRow(
                    ts=ts,
                    market_id=market_id,
                    side=side,
                    action="buy",
                    price=price,
                    qty=qty,
                    fee=0.0,
                    position_id=pos.id,
                    news_id=news_id,
                    trigger=None,
                    order_id=order_id,  # NEW
                    tx_hash=tx_hash,  # NEW
                )
            )
            session.commit()
            return _to_held(pos)

    def close_position(
        self,
        position_id: int,
        *,
        sell_price: float,
        ts: float,
        close_reason: CloseReason,
        trigger: str | None = None,
        order_id: str | None = None,  # NEW
        tx_hash: str | None = None,  # NEW
    ) -> PositionRecord:
        """Close a position: insert the sell fill + flip the position row to
        ``closed`` with its realized PnL, in one transaction. Always closes the
        full quantity (one-shot model — no partial close). Raises ``ValueError``
        if the position is missing or not open.
        """
        with self._session_factory() as session:
            pos = session.get(PositionRow, position_id)
            if pos is None:
                raise ValueError(f"position {position_id} not found")
            if pos.status != "open":
                raise ValueError(f"position {position_id} is {pos.status}, not open")
            realized = (sell_price - pos.avg_entry_price) * pos.qty  # fee = 0
            session.add(
                FillRow(
                    ts=ts,
                    market_id=pos.market_id,
                    side=pos.side,
                    action="sell",
                    price=sell_price,
                    qty=pos.qty,
                    fee=0.0,
                    position_id=pos.id,
                    news_id=None,
                    trigger=trigger,
                    order_id=order_id,  # NEW
                    tx_hash=tx_hash,  # NEW
                )
            )
            pos.status = "closed"
            pos.closed_at = ts
            pos.close_reason = close_reason
            # Accrue (not overwrite): a position partially sold via record_sell
            # already carries realized PnL on the sold portion.
            pos.realized_pnl = (pos.realized_pnl or 0.0) + realized
            session.commit()
            return _to_record(pos)

    def record_sell(
        self,
        position_id: int,
        *,
        sold_qty: float,
        sell_price: float,
        ts: float,
        close_reason: CloseReason,
        trigger: str | None = None,
        order_id: str | None = None,
        tx_hash: str | None = None,
    ) -> PositionRecord:
        """Record a (possibly partial) sell against an open position.

        Closes the position only when ``sold_qty`` covers the full remaining
        qty; a partial fill reduces ``qty`` and leaves the position OPEN so the
        next exit tick sells the remainder — otherwise the unsold tokens are
        stranded on-chain as an orphan (the orphaned-remainder bug). Realized PnL accrues
        across partials. Raises ``ValueError`` if missing or not open.

        A residual below ``MIN_SELLABLE_QTY`` counts as fully sold: no exit
        tick can ever clear it, so leaving the row open leaves it open
        forever.
        """
        with self._session_factory() as session:
            pos = session.get(PositionRow, position_id)
            if pos is None:
                raise ValueError(f"position {position_id} not found")
            if pos.status != "open":
                raise ValueError(f"position {position_id} is {pos.status}, not open")
            sold = min(sold_qty, pos.qty)
            session.add(
                FillRow(
                    ts=ts,
                    market_id=pos.market_id,
                    side=pos.side,
                    action="sell",
                    price=sell_price,
                    qty=sold,
                    fee=0.0,
                    position_id=pos.id,
                    news_id=None,
                    trigger=trigger,
                    order_id=order_id,
                    tx_hash=tx_hash,
                )
            )
            pos.realized_pnl = (pos.realized_pnl or 0.0) + (sell_price - pos.avg_entry_price) * sold
            residual = pos.qty - sold
            if residual < MIN_SELLABLE_QTY:
                # Fully sold, or all that is left is a residue the venue has no
                # order size for. The residue is dropped rather than carried:
                # at any price ≤ 1.0 it is worth under $0.01, and keeping the
                # row open for it costs a heat-cap slot plus an exit decision
                # every tick until the market resolves. ``realized_pnl`` stays
                # as accrued from the legs actually sold — the dropped residue
                # is not booked as a loss.
                pos.qty = sold
                pos.status = "closed"
                pos.closed_at = ts
                pos.close_reason = close_reason
            else:
                pos.qty = residual
            session.commit()
            return _to_record(pos)

    def get_open_position(self, market_id: str, side: Side) -> HeldPosition | None:
        """The open position for (market_id, side), or None."""
        with self._session_factory() as session:
            row = session.execute(
                select(PositionRow).where(
                    PositionRow.market_id == market_id,
                    PositionRow.side == side,
                    PositionRow.status == "open",
                )
            ).scalar_one_or_none()
            return _to_held(row) if row is not None else None

    def get_open_positions(self) -> list[HeldPosition]:
        """Every open position."""
        with self._session_factory() as session:
            rows = (
                session.execute(select(PositionRow).where(PositionRow.status == "open"))
                .scalars()
                .all()
            )
            return [_to_held(r) for r in rows]

    def get_position(self, position_id: int) -> PositionRecord | None:
        """The position with this id — any status (open or closed) — or None."""
        with self._session_factory() as session:
            row = session.get(PositionRow, position_id)
            return _to_record(row) if row is not None else None

    def list_positions(self, limit: int = 100) -> list[PositionRecord]:
        """Recent positions (open + closed), newest first."""
        with self._session_factory() as session:
            rows = (
                session.execute(select(PositionRow).order_by(PositionRow.id.desc()).limit(limit))
                .scalars()
                .all()
            )
            return [_to_record(r) for r in rows]

    def list_fills(self, limit: int = 100) -> list[Fill]:
        """Recent fills (the ledger tail), newest first."""
        with self._session_factory() as session:
            rows = (
                session.execute(select(FillRow).order_by(FillRow.id.desc()).limit(limit))
                .scalars()
                .all()
            )
            return [_to_fill(r) for r in rows]

    def buy_cost_basis(self, position_ids: Sequence[int]) -> dict[int, float]:
        """What was actually paid to open each position — the sum of its BUY
        fill notional, keyed by position id.

        ``PositionRow.qty`` cannot answer this: ``record_sell`` decrements it,
        so after a partial sell the row carries the *residual* while
        ``realized_pnl`` carries the gain on the whole thing. Measuring a
        return against that residual inflates it without bound (see
        ``openpoly.analytics.calibration``). The fill ledger is append-only, so
        the opening cost is always recoverable from it.

        Positions with no BUY fill (reconciled or manually created rows) are
        simply absent from the mapping.
        """
        if not position_ids:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FillRow.position_id,
                    func.sum(FillRow.price * FillRow.qty),
                )
                .where(FillRow.action == "buy")
                .where(FillRow.position_id.in_(list(position_ids)))
                .group_by(FillRow.position_id)
            ).all()
            return {int(pid): float(total or 0.0) for pid, total in rows}

    def news_id_for_position(self, position_id: int) -> str | None:
        """Look up the news_id that triggered this position's BUY fill.

        PositionRecord doesn't denormalize news_id (it lives on the fill
        ledger row), but PositionDetail UI / analyzer-log lookup wants to
        cross-reference the LLM call by news_id. Returns ``None`` when no
        BUY fill matches (e.g. manually-opened paper position, or the
        position id doesn't exist)."""
        with self._session_factory() as session:
            stmt = (
                select(FillRow.news_id)
                .where(FillRow.position_id == position_id)
                .where(FillRow.action == "buy")
                .order_by(FillRow.id.asc())
                .limit(1)
            )
            return session.execute(stmt).scalar_one_or_none()

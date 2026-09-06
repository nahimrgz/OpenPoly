"""LiveExecutor — submit crossing GTC orders to Polymarket V2 CLOB and settle
whatever did not fill immediately.

Same ``ExecResult`` contract as ``PaperExecutor`` — failures map to
``skip(reason)``, never raise. The order type is GTC at the level-1 price (see
the note at the order call for why not FAK); the executor then treats every
order the server answered as fill-or-cancel: unless the response reports the
whole size filled, the order is cancelled and its final matched size re-read.

POST /order answers with a ``status`` (order lifecycle docs): ``matched`` —
filled immediately; ``live`` — resting on the book; ``delayed`` — marketable
but held in the venue's matching-delay window (250 ms taker delay on
crypto/finance markets, configured seconds on sports markets) and NOT
cancellable while pending; ``unmatched`` — placed on the book after that window
expired without a match. None of these means "not placed" (a rejection is
``success: false``), so every one of them goes through the same settle step,
which retries the cancel across the delay window. The cancel endpoints answer
200 with ``{"canceled": [...], "not_canceled": {id: reason}}``: a refusal is a
body, not an exception, and is read as one.

A lost response (the ``except`` branch around the post) is the remaining
window: the order id is never learned, so nothing can be cancelled by id and
the CTF balance delta is the only signal. There is no retry of the order itself
(slice C design doc §3 D2 / D3).

Auth model is Polymarket V2 DepositWallet:
  * signer EOA = ``wallet.private_key_ref`` (resolved at factory time)
  * funder    = ``wallet.funder_address`` (the DepositWallet contract)
  * sig_type  = 3 (POLY_1271) — server validates via EIP-1271 on funder

A prior project's production verified the following exact pattern
works against V2 CLOB; deviations have hit dead bugs (see py-clob-client-v2
issues #64/#70/#76). Do NOT change without re-testing live:
  * Cloudflare patch applied before any SDK import (see ``clob_patch``)
  * ``derive_api_key()`` — NOT ``create_or_derive_api_key()``
  * ``create_and_post_order(...)`` — combined call
  * ``PartialCreateOrderOptions(neg_risk=...)`` passed on every order
  * ``update_balance_allowance()`` before each trade (collateral for BUY,
    conditional + token_id for SELL)

The ``_ClobClient`` Protocol lets tests pass a fake without instantiating
the real v2 client (which would hit the network at init).

**No client-side order idempotency.** py-clob-client-v2 exposes no client
order id: ``OrderArgsV2`` carries only ``builder_code`` and a bytes32
``metadata`` field (neither is queryable), and the read side filters orders by
the *server-assigned* id only (``OpenOrderParams.id`` / ``get_order(order_id)``)
— an id we learn from the very response a lost order loses. There is therefore
no way to ask "did the order I just sent land?" by an id we chose. The only
signal available is the wallet's CTF balance delta, so a pre-order balance read
is not an optimisation here, it is the entire recovery mechanism: without it a
lost response after a real fill becomes an untracked on-chain position. Both
order paths consequently refuse to place an order they could not confirm —
BUY skips with ``ctf_balance_unavailable``, SELL with ``ctf_cache_not_synced``.
Revisit if the SDK ever gains a client-supplied order id.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

# Cloudflare patch MUST be applied before any other SDK import. The patch
# module re-exports the SDK symbols we need so this single import covers
# both concerns.
from openpoly.execution.clob_patch import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)
from openpoly.execution.sizing import MIN_NOTIONAL_USD, dust_remainder_skip, quantize_size
from openpoly.execution.types import ExecResult
from openpoly.markets.manager import manager as market_source_manager
from openpoly.portfolio import CloseReason, HeldPosition, PortfolioStore
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

logger = logging.getLogger(__name__)


def _parse_amount(raw: Any) -> float | None:
    """One amount field from an order response. Absent or empty is a clean 0.0;
    anything unparseable is None — unknown, never a silent zero, so the caller
    can keep the fields that did parse and say which did not.

    ``NaN`` and the infinities parse without raising, so ``float()`` alone is
    not the validation this claims to be: a non-finite amount makes every
    comparison downstream False (neither over-size nor a miss) and divides into
    an infinite price. They are unusable values, so they are None like the rest.
    """
    if raw is None or raw == "":
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_order_id(raw: Any) -> str | None:
    """The ``orderID`` of an order response, as a string we can put in a cancel
    payload, or None. The venue documents a string; a whole number is kept too,
    because an id we can still send is worth a cancel attempt and dropping one
    leaves the order on the book with nothing to cancel it by.

    Everything else is None, and each for its own reason: a container or an
    empty value is not an id at all; a bool is an int in Python and ``str()``
    would turn it into nonsense; a float is a *different* id once stringified
    (``12345.0``, not ``12345``), and cancelling the wrong id is worse than
    reporting this one as unreachable.
    """
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw:
        return str(raw)
    return None


CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
SIGTYPE_POLY_1271 = 3

# Order size + min-notional live in ``openpoly.execution.sizing`` so the paper
# executor obeys exactly the same venue rules (see that module).
_CTF_DECIMALS = 6  # CTF / Polymarket shares are 1e6 base units
_CTF_POLL_ATTEMPTS = 5  # SELL right after BUY can hit cache lag; ~5s total
_CTF_POLL_SLEEP = 1.0
# Settle loop: cancel what did not fill, re-reading the order whenever the venue
# refuses (an order in its matching-delay window cannot be cancelled yet).
# 16 × 0.5 s ≈ 8 s covers the 250 ms taker delay and typical seconds-long sports
# windows; exhausting it surfaces as ``live_cancel_failed`` plus a RESTING ORDER
# ALERT rather than as a silent miss. The attempt count alone does NOT bound the
# loop in time — each attempt issues up to two requests whose only limit is the
# SDK's httpx default — so a wall-clock deadline bounds it as well, and whichever
# runs out first ends the loop.
_SETTLE_ATTEMPTS = 16
_SETTLE_SLEEP = 0.5
_SETTLE_DEADLINE_S = 8.0
# GET /data/order ``status`` values after which nothing can rest on the book.
# ``MATCHED`` is NOT one of them: the venue reports it for a PARTIAL match too,
# so believing it would abandon the unmatched remainder of a partially filled
# order on the book. A match ends the settle only through the size comparison.
_ORDER_DONE_STATUSES = frozenset({"CANCELED", "CANCELLED"})
# An on-chain fill is irreversible; persisting it (open for BUY, close for SELL)
# must survive a transient write failure (locked SQLite, brief error) or the
# ledger drifts from the wallet: a phantom-open position whose tokens are gone,
# or tokens no exit monitor manages.
_PERSIST_ATTEMPTS = 5
_PERSIST_SLEEP = 0.5
# Budget, honestly: the settle loop ends at min(16 attempts, 8 s of wall clock),
# and is followed by one final order read plus — only when that read fails — the
# balance-confirm fallback (≈ 4 s of sleeps). With the CTF poll (≈ 4 s) and the
# persist retries (≈ 2 s) that is ≈ 18 s of waiting worst case, which must stay
# inside the exit monitor's 30 s in-flight drain (``runtime/exit_monitor.py``
# INFLIGHT_DRAIN_TIMEOUT_SECONDS). What is NOT bounded here is a single request:
# each one is limited only by the SDK's httpx default timeout, so the totals hold
# only as long as that default does — an explicit client timeout is the gap.

_T = TypeVar("_T")


@dataclass(frozen=True)
class _Fill:
    """What the venue actually did with one order: final matched size, the
    price to book it at, the identifiers the ledger keeps, and whether part of
    the order may still be resting (no cancel could be confirmed)."""

    qty: float
    price: float
    tx_hash: str | None
    order_id: str | None
    resting: bool = False


@dataclass(frozen=True)
class _OrderResponse:
    """The POST /order body, parsed once and validated by construction, so no
    field of it can reach the ledger — or an exception — in a shape the venue
    never documented. ``shares`` / ``usdc`` are None when unparseable (see
    ``_parse_amount``); ``tx_hash`` is None unless it is the string it is
    supposed to be, and ``order_id`` None unless it is an id we could still
    send (see ``_parse_order_id``)."""

    order_id: str | None
    shares: float | None
    usdc: float | None
    tx_hash: str | None

    @classmethod
    def parse(cls, resp: dict[str, Any], *, shares_key: str, usdc_key: str) -> _OrderResponse:
        hashes = resp.get("transactionsHashes")
        # A bare string is indexable, so ``[0]`` on one would store its first
        # character as the transaction hash.
        first = hashes[0] if isinstance(hashes, (list, tuple)) and hashes else None
        return cls(
            order_id=_parse_order_id(resp.get("orderID")),
            shares=_parse_amount(resp.get(shares_key)),
            usdc=_parse_amount(resp.get(usdc_key)),
            tx_hash=first if isinstance(first, str) and first else None,
        )


class _ClobClient(Protocol):
    def create_and_post_order(
        self,
        order_args: OrderArgs,
        options: PartialCreateOrderOptions,
        order_type: Any,
    ) -> dict[str, Any]: ...
    def update_balance_allowance(self, params: BalanceAllowanceParams) -> Any: ...
    def get_balance_allowance(self, params: BalanceAllowanceParams) -> dict[str, Any]: ...
    def cancel_order(self, payload: OrderPayload) -> Any: ...
    def get_order(self, order_id: str) -> dict[str, Any]: ...


class LiveExecutor:
    """V2 CLOB executor — crossing GTC, anything short of a full fill cancelled
    and re-read. Construct via ``build_live_executor``."""

    def __init__(
        self,
        *,
        portfolio: PortfolioStore,
        clob_client: _ClobClient,
    ) -> None:
        self._store = portfolio
        self._clob = clob_client

    def _post_order(self, order_args: OrderArgs, *, neg_risk: bool) -> dict[str, Any]:
        """POST the order (GTC — see the note in ``execute_buy``) and return the
        parsed answer. A 200 with a non-JSON body reaches here as raw text (the
        SDK's http helper returns ``resp.text`` then): whether the order landed
        is unknown, so it is raised and takes the lost-response path."""
        resp = self._clob.create_and_post_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(neg_risk=neg_risk),
            order_type=OrderType.GTC,
        )
        if not isinstance(resp, dict):
            raise TypeError(f"non-dict order response: {type(resp).__name__}")
        return resp

    def _read_order(self, order_id: str) -> tuple[float, str] | None:
        """``GET /data/order``: (size matched so far, upper-cased status), or
        None when the read fails — logged here, interpreted by the caller.

        A ``size_matched`` that will not parse is a failed read, not a fill of
        zero: this is the source the settle trusts once an over-size POST amount
        is discarded, and a non-finite value cannot even be clamped
        (``min(nan, size)`` is ``nan``), so it would reach the ledger unbounded.
        """
        try:
            order = self._clob.get_order(order_id)
            matched = _parse_amount(order.get("size_matched"))
            status = str(order.get("status") or "").upper()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_order for %s failed: %s", order_id, exc)
            return None
        if matched is None:
            logger.error(
                "get_order for %s returned an unusable size_matched (%r) — "
                "treating the order as unreadable",
                order_id,
                order.get("size_matched"),
            )
            return None
        return matched, status

    def _cancel_with_retry(
        self, order_id: str, size: float
    ) -> tuple[str | None, float | None, bool]:
        """Cancel ``order_id``, retrying across the venue's matching-delay window.

        Returns ``(refusal, seen, settled)``. ``refusal`` is None once nothing
        rests: the venue acknowledged the cancel, or the order itself reports it
        fully matched / already cancelled. Otherwise it is the last refusal
        reason after the loop ran out of attempts or of wall clock, whichever
        came first (``_SETTLE_ATTEMPTS`` / ``_SETTLE_DEADLINE_S``).

        ``seen`` is the LARGEST ``size_matched`` any successful read reported,
        None when no read succeeded. It is a maximum and never a last-write-wins
        value: a fill the venue already reported cannot be undone by a later
        acknowledged cancel or contradicted by a lagging read, so dropping it
        would persist less than the wallet actually holds.

        ``settled`` says a read ENDED the loop by showing the order terminal
        (fully matched, or a cancelled status): only then is ``seen`` the venue's
        last word. Every other ending — acknowledged cancel, exhausted budget,
        reads that never landed — leaves the caller one fresh read to do.
        """
        reason = "cancel never attempted"
        seen: float | None = None
        start = time.monotonic()
        for attempt in range(_SETTLE_ATTEMPTS):
            try:
                res = self._clob.cancel_order(OrderPayload(orderID=order_id))
            except Exception as exc:  # noqa: BLE001
                res = {"not_canceled": {order_id: f"{type(exc).__name__}: {exc}"}}
            if not isinstance(res, dict):
                res = {"not_canceled": {order_id: f"unexpected response {type(res).__name__}"}}
            # Both halves are read defensively: the acknowledgement must be a
            # list holding our id (a bare string would make ``in`` a substring
            # test and acknowledge an order we never sent), and the refusal must
            # be a mapping. An unexpected shape is a refusal, not an exception —
            # raising here would abandon the order on the book.
            acked = res.get("canceled")
            if isinstance(acked, list) and order_id in acked:
                return None, seen, False
            refusals = res.get("not_canceled")
            if isinstance(refusals, Mapping):
                reason = str(refusals.get(order_id, "refused without a reason"))
            else:
                reason = f"unexpected cancel body shape ({type(refusals).__name__})"
            state = self._read_order(order_id)
            if state is not None:
                matched, status = state
                # Clamped like the balance source is: an order cannot match more
                # than it asked for, and the caller books this straight down.
                matched = min(matched, size)
                seen = matched if seen is None else max(seen, matched)
                # The size comparison — not a ``MATCHED`` status, which the
                # venue also reports on a partial — is what proves nothing rests.
                if seen >= size - 1e-9 or status in _ORDER_DONE_STATUSES:
                    return None, seen, True
            logger.info(
                "cancel attempt %d/%d for %s refused (%s)",
                attempt + 1,
                _SETTLE_ATTEMPTS,
                order_id,
                reason,
            )
            # Whichever budget runs out first ends the loop: slow round-trips
            # must not stretch one settle past the exit monitor's drain window.
            remaining = _SETTLE_DEADLINE_S - (time.monotonic() - start)
            if remaining <= 0 or attempt >= _SETTLE_ATTEMPTS - 1:
                break
            time.sleep(min(_SETTLE_SLEEP, remaining))
        return reason, seen, False

    def _settle_resting_remainder(
        self,
        order_id: str | None,
        reported_qty: float,
        size: float,
        *,
        confirm: Callable[[], float],
    ) -> tuple[float, str | None]:
        """Cancel whatever of this order is still resting and return
        ``(final_matched_qty, failure)``.

        ``reported_qty`` is the immediate fill from the POST response, already
        vetted by the caller; a full fill returns at once. ``confirm`` is the CTF
        balance-delta fallback for when the order cannot be re-read. ``failure``
        is None, or one of the skip reasons: ``"live_cancel_failed"`` — something
        may still rest (no order id, or every cancel refused / raised and no
        later read showed the order terminal), logged as a RESTING ORDER ALERT;
        ``"live_fill_unknown"`` — nothing filled as far as could be told, but
        neither the order read nor the balance could establish it. The returned
        qty is the LARGEST fill any source reported and is never less than
        ``reported_qty``.
        """
        if reported_qty >= size - 1e-9:
            return reported_qty, None  # full fill — nothing resting, no round-trip
        if not order_id:
            logger.error(
                "RESTING ORDER ALERT: %.4f of %.4f filled but the response carried no "
                "order id — nothing to cancel by; reverse-reconciliation will flag it",
                reported_qty,
                size,
            )
            return reported_qty, "live_cancel_failed"

        failure: str | None = None
        reason, seen, settled = self._cancel_with_retry(order_id, size)
        final = seen or 0.0
        if not settled:
            # No read ended the loop, so the venue's last word is still unread —
            # an acknowledged cancel, an exhausted budget and reads that never
            # landed are alike here. This read is the only place a fill that
            # raced the cancel, or one a stale in-loop read missed, is visible,
            # and the only thing that can show the order is no longer resting.
            state = self._read_order(order_id)
            if state is not None:
                fresh, status = state
                final = max(final, min(fresh, size))  # clamped, as in the loop
                # Only this order's own read can clear the alert, and only by
                # showing the whole order matched or a terminal status.
                # ``reported_qty`` has no say in it: the early return above
                # already proved it short of ``size``.
                if final >= size - 1e-9 or status in _ORDER_DONE_STATUSES:
                    reason = None  # the order is terminal — nothing rests after all
            else:
                logger.error(
                    "order %s could not be re-read after settle — confirming the fill "
                    "through the CTF balance instead",
                    order_id,
                )
                try:
                    confirmed = min(confirm(), size)
                except Exception as cexc:  # noqa: BLE001
                    logger.error("balance confirmation for %s failed too: %s", order_id, cexc)
                    confirmed = 0.0
                if confirmed > 0:
                    logger.error(
                        "fill for %s established through the CTF balance: %.4f",
                        order_id,
                        confirmed,
                    )
                elif reported_qty <= 0 and not seen:
                    # No source could establish a fill — a read of 0.0 counts
                    # for nothing here, it predates the cancel. Not "no match":
                    # the caller must not treat this as a clean miss.
                    failure = "live_fill_unknown"
                # The balance never clears a refused cancel, however much it
                # accounts for: it is a wallet-wide delta against a pre-order
                # baseline, attributed to no order id, so an EARLIER order's
                # remainder filling in this poll window is indistinguishable
                # from this one filling. A false alert on an order the wallet
                # suggests is filled costs a reconciliation look; suppressing a
                # true one leaves an answered order resting unannounced.
                final = max(final, confirmed)
        if reason is not None:
            logger.error(
                "RESTING ORDER ALERT: could not cancel %s within %d attempts / %.1fs "
                "(last: %s) — remainder may fill untracked; reverse-reconciliation "
                "will flag it",
                order_id,
                _SETTLE_ATTEMPTS,
                _SETTLE_DEADLINE_S,
                reason,
            )
            failure = "live_cancel_failed"
        return max(final, reported_qty), failure

    def _resolve_fill(
        self,
        resp: dict[str, Any],
        *,
        side: str,
        size: float,
        limit_price: float,
        confirm: Callable[[], float],
    ) -> _Fill | ExecResult:
        """Turn a successful POST /order response into what actually filled, or
        into the ``ExecResult.skip`` to return. This is where the venue's own
        account of the order is either believed or thrown away; the settle step
        below is handed only numbers that survived that, and runs whenever what
        did survive is short of ``size``."""
        # ``makingAmount`` is what our order gives, ``takingAmount`` what it
        # receives: a BUY receives shares (and gives pUSD), a SELL the reverse.
        shares_key, usdc_key = (
            ("takingAmount", "makingAmount") if side == "buy" else ("makingAmount", "takingAmount")
        )
        parsed = _OrderResponse.parse(resp, shares_key=shares_key, usdc_key=usdc_key)
        order_id = parsed.order_id
        no_fill_reason = "live_no_match"
        # Each amount stands on its own: a garbage price field must not erase a
        # share count the venue reported, or an executed fill would be dropped
        # from the ledger.
        bad = [k for k, v in ((shares_key, parsed.shares), (usdc_key, parsed.usdc)) if v is None]
        shares, usdc = parsed.shares or 0.0, parsed.usdc or 0.0
        if shares > size + 1e-9:
            # More filled than the order asked for is not a fill to book down.
            # It is a number the venue cannot mean, so it earns no trust at all:
            # drop it and let the order read or the wallet say what happened.
            # Booking it down to ``size`` instead would pin a full-size floor
            # under the answer — a phantom position against an empty wallet,
            # which blocks re-entry and can never be sold.
            logger.error(
                "%s response reported %.4f filled for an order of %.4f (%s) — "
                "discarding it; the settle establishes the fill instead",
                side,
                shares,
                size,
                order_id,
            )
            shares = 0.0
            bad.append(shares_key)
        if bad:
            logger.error(
                "%s response carried unusable %s for order %s — settling and "
                "recording only what could be read",
                side,
                " and ".join(bad),
                order_id,
            )
            no_fill_reason = "live_unparseable"
        qty, failure = self._settle_resting_remainder(order_id, shares, size, confirm=confirm)
        resting_id = order_id if failure == "live_cancel_failed" else None
        if qty <= 0:
            return ExecResult.skip(failure or no_fill_reason, resting_order_id=resting_id)
        if shares > 0 and usdc > 0:
            # The raced-extra portion (if any) filled at our limit price, so
            # pricing it at the response average is conservative-enough (≤1 tick).
            price = usdc / shares
        else:
            # No usable amounts for this fill (nothing had crossed at response
            # time, or a field is missing): the real price is unknown. Record
            # at our limit — BUY cost can only be ≤ it, SELL proceeds ≥ it.
            logger.warning(
                "%s: %.4f filled but the response carried no usable amounts — "
                "recording fill at limit price %.4f (order=%s)",
                side,
                qty,
                limit_price,
                order_id,
            )
            price = limit_price
        return _Fill(
            qty=qty,
            price=price,
            tx_hash=parsed.tx_hash,
            order_id=order_id,
            resting=resting_id is not None,
        )

    def _fill_from_balance(
        self, confirm: Callable[[], float], *, size: float, limit_price: float
    ) -> _Fill | None:
        """The fill as the CTF balance tells it, for when the venue's own account
        of the order is unavailable (lost response): the balance delta clamped
        to ``size``, booked at our limit — BUY cost can only be ≤ it, SELL
        proceeds ≥ it — with no order id or tx hash to keep. None when nothing
        moved."""
        qty = min(confirm(), size)
        if qty <= 0:
            return None
        return _Fill(qty=qty, price=limit_price, tx_hash=None, order_id=None)

    def _persist_irreversible(self, write: Callable[[], _T], *, what: str) -> _T:
        """Run a ledger write for a fill that already happened on-chain, retrying
        transient failures; re-raises after the last attempt so the caller can
        log the loss with its own context."""
        for attempt in range(_PERSIST_ATTEMPTS - 1):
            try:
                return write()
            except Exception as exc:  # noqa: BLE001 — on-chain fill already happened
                logger.warning("%s attempt %d failed: %s; retrying", what, attempt + 1, exc)
                time.sleep(_PERSIST_SLEEP)
        return write()

    def _persist_open(
        self,
        intent: OrderIntent,
        fill: _Fill,
        *,
        token_id: str,
        condition_id: str,
        ts: float,
        news_id: str | None,
    ) -> ExecResult:
        """Persist an already-executed on-chain buy (``_persist_irreversible``);
        on final failure log CRITICAL and skip — never raise."""
        try:
            held = self._persist_irreversible(
                lambda: self._store.open_position(
                    market_id=intent.market_id,
                    side=intent.side,
                    token_id=token_id,
                    condition_id=condition_id,
                    price=fill.price,
                    qty=fill.qty,
                    ts=ts,
                    news_id=news_id,
                    order_id=fill.order_id,
                    tx_hash=fill.tx_hash,
                    entry_p_model=intent.p_model,
                    entry_confidence=intent.confidence,
                    entry_edge=intent.edge,
                ),
                what=f"open_position {intent.market_id} {intent.side}",
            )
        except Exception as exc:  # noqa: BLE001 — on-chain fill already happened
            logger.error(
                "CRITICAL: on-chain buy filled (order=%s qty=%.4f @ %.4f) but "
                "open_position failed after %d attempts for %s %s: %s — the wallet "
                "holds tokens no ledger row manages; reconciliation will flag them",
                fill.order_id,
                fill.qty,
                fill.price,
                _PERSIST_ATTEMPTS,
                intent.market_id,
                intent.side,
                exc,
            )
            return ExecResult.skip(f"open_persist_failed:{type(exc).__name__}")
        logger.info(
            "live buy filled: %s %s qty=%.4f @ %.4f order=%s tx=%s",
            intent.market_id,
            intent.side,
            fill.qty,
            fill.price,
            fill.order_id,
            (fill.tx_hash[:10] + "…" if fill.tx_hash else None),
        )
        return ExecResult.ok(
            price=fill.price,
            qty=fill.qty,
            position_id=held.position_id,
            resting_order_id=fill.order_id if fill.resting else None,
        )

    def get_collateral_balance_raw(self) -> int | None:
        """Wallet USDC (collateral) balance in raw 1e6 units — None when the
        read fails. Read-only; serves the wallet-balance dashboard endpoint."""
        try:
            self._clob.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            ba = self._clob.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return int(ba.get("balance", 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("collateral balance read failed: %s", exc)
            return None

    # ---------- lost-response confirmation (R5) ----------

    def _read_ctf_balance_raw(self, token_id: str) -> int | None:
        """Refresh + read the wallet's CTF balance for ``token_id`` (raw 1e6
        units). None when the read fails — callers treat that as 'unknown'.

        A balance field that is absent, null, or not a number is *unknown*, not
        zero. Returning 0 for it defeated the ``ctf_balance_unavailable`` guard
        entirely: the caller saw a successfully read baseline of 0, posted the
        order, and — if the response was then lost — confirmed the fill against
        a baseline that had never been read, turning the first parseable read
        into an invented fill delta.
        """
        try:
            self._clob.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
            )
            ba = self._clob.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
            )
            return int(ba.get("balance"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            logger.warning("CTF balance unparseable for %s: %s", token_id, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("CTF balance read failed: %s", exc)
            return None

    def _confirm_lost_order_qty(self, token_id: str, pre_raw: int, direction: str) -> float:
        """After a lost order response, poll the CTF balance to see whether the
        order actually filled. A network exception from create_and_post_order
        does NOT mean no fill — the server may have matched the order and only
        the response was lost (a confirmed live drift incident). Returns the filled token
        qty inferred from the balance delta (0.0 = no change observed)."""
        for attempt in range(_CTF_POLL_ATTEMPTS):
            now_raw = self._read_ctf_balance_raw(token_id)
            if now_raw is not None:
                delta = pre_raw - now_raw if direction == "drop" else now_raw - pre_raw
                if delta > 0:
                    return delta / (10**_CTF_DECIMALS)
            if attempt < _CTF_POLL_ATTEMPTS - 1:
                time.sleep(_CTF_POLL_SLEEP)
        return 0.0

    def execute_buy(self, intent: OrderIntent, *, news_id: str | None, ts: float) -> ExecResult:
        catalog = market_source_manager.store
        market = catalog.get(intent.market_id)
        if market is None:
            return ExecResult.skip("market_not_found")
        token_id = market.yes_token_id if intent.side == "yes" else market.no_token_id
        if token_id is None:
            return ExecResult.skip("no_token")

        if self._store.get_open_position(intent.market_id, intent.side) is not None:
            return ExecResult.skip("position_exists")

        # Quantize qty + check min notional against server rules verified
        # 2026-05-24. Both are pre-flight: cheaper to skip locally than to
        # eat a 400 round-trip + clutter logs with rejections.
        size = quantize_size(intent.qty, intent.price)
        notional = size * intent.price
        if notional < MIN_NOTIONAL_USD:
            return ExecResult.skip("min_notional_below_floor")

        # Refresh CLOB collateral allowance cache before signing (a prior project pattern).
        # Failures are non-fatal — the order may still succeed if the cache is
        # already warm — but we log so a real allowance issue surfaces in logs.
        try:
            self._clob.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("update_balance_allowance(COLLATERAL) failed: %s", exc)

        # Pre-order CTF balance — the baseline for lost-response confirmation
        # below, and the ONLY one available: the SDK has no client order id to
        # query a lost order by (see module header). Without the baseline a
        # lost response after a real fill becomes an untracked on-chain
        # position, so we refuse to place the order rather than trade blind.
        pre_raw = self._read_ctf_balance_raw(token_id)
        if pre_raw is None:
            logger.error(
                "buy aborted for %s %s: CTF balance unreadable, so a lost order "
                "response could not be confirmed — refusing to place an "
                "unconfirmable order",
                intent.market_id,
                intent.side,
            )
            return ExecResult.skip("ctf_balance_unavailable")

        # GTC + crossing the spread acts like an aggressive market order, and
        # the settle step cancels whatever did not fill. GTC (not FAK)
        # because a prior project's production verified it end-to-end on
        # 2026-05-05 / smoke verified again 2026-05-24, while FAK failed then
        # for a reason never diagnosed. sizing.py's two-decimal floor does not
        # address that (the SDK builder applies the same floor to every order
        # type), so FAK is not a known-safe swap: switching requires a live
        # smoke. Do not change without one.
        def confirm() -> float:
            # The balance delta against the baseline read above — the only
            # signal when the venue's account of the order is unavailable.
            return self._confirm_lost_order_qty(token_id, pre_raw, "rise")

        fill: _Fill | ExecResult | None
        try:
            resp = self._post_order(
                OrderArgs(token_id=token_id, price=intent.price, size=size, side=Side.BUY),
                neg_risk=market.neg_risk,
            )
        except Exception as exc:  # noqa: BLE001
            # The order may have filled despite the lost response — confirm via
            # the balance before declaring failure (R5 at-least-once).
            fill = self._fill_from_balance(confirm, size=size, limit_price=intent.price)
            if fill is None:
                logger.error("live buy submit failed (%s): %s", type(exc).__name__, exc)
                return ExecResult.skip(f"live_error:{type(exc).__name__}")
            logger.warning(
                "buy response lost (%s) but CTF balance rose %.4f — "
                "recording fill at limit price %.4f",
                type(exc).__name__,
                fill.qty,
                fill.price,
            )
        else:
            if not resp.get("success"):
                return ExecResult.skip(f"live_rejected:{resp.get('errorMsg', 'unknown')}")
            fill = self._resolve_fill(
                resp, side="buy", size=size, limit_price=intent.price, confirm=confirm
            )
            if isinstance(fill, ExecResult):
                return fill
        return self._persist_open(
            intent,
            fill,
            token_id=token_id,
            condition_id=market.condition_id,
            ts=ts,
            news_id=news_id,
        )

    def execute_sell(
        self,
        position: HeldPosition,
        *,
        close_reason: CloseReason,
        ts: float,
        trigger: str | None = None,
    ) -> ExecResult:
        catalog = market_source_manager.store
        market = catalog.get(position.market_id)
        if market is None:
            return ExecResult.skip("market_not_found")

        book = catalog.get_order_book(position.token_id)
        if book is None or not book.bids:
            return ExecResult.skip("no_bid_liquidity")
        bid_price = book.bids[0][0]

        # Quantize SELL size symmetrically with BUY so the size stays within
        # server precision.
        size = quantize_size(position.qty, bid_price)
        if size <= 0:
            # Below one share: not a placeable order. The remainder still
            # settles at the resolution price, so leave the row open rather
            # than writing it off at 0.
            return dust_remainder_skip(position)

        # Poll CTF balance — handles cache lag when SELL fires shortly after
        # BUY (live smoke testing saw ~3-5s lag). update_balance_allowance is
        # the documented refresh trigger; we then read to confirm.
        need_raw = int(size * (10**_CTF_DECIMALS))
        synced = False
        pre_raw = 0  # balance at gate-sync time — lost-response baseline
        for attempt in range(_CTF_POLL_ATTEMPTS):
            have_raw = self._read_ctf_balance_raw(position.token_id)
            if have_raw is not None and have_raw >= need_raw:
                synced = True
                pre_raw = have_raw
                break
            if attempt < _CTF_POLL_ATTEMPTS - 1:
                time.sleep(_CTF_POLL_SLEEP)
        if not synced:
            return ExecResult.skip("ctf_cache_not_synced")

        def confirm() -> float:
            return self._confirm_lost_order_qty(position.token_id, pre_raw, "drop")

        fill: _Fill | ExecResult | None
        try:
            resp = self._post_order(
                OrderArgs(token_id=position.token_id, price=bid_price, size=size, side=Side.SELL),
                neg_risk=market.neg_risk,
            )
        except Exception as exc:  # noqa: BLE001
            # The order may have filled despite the lost response — confirm via
            # the balance before declaring failure (R5 at-least-once).
            fill = self._fill_from_balance(confirm, size=size, limit_price=bid_price)
            if fill is None:
                logger.error("live sell submit failed (%s): %s", type(exc).__name__, exc)
                return ExecResult.skip(f"live_error:{type(exc).__name__}")
            logger.warning(
                "sell response lost (%s) but CTF balance dropped %.4f — "
                "recording fill at limit price %.4f",
                type(exc).__name__,
                fill.qty,
                fill.price,
            )
        else:
            if not resp.get("success"):
                return ExecResult.skip(f"live_rejected:{resp.get('errorMsg', 'unknown')}")
            fill = self._resolve_fill(
                resp, side="sell", size=size, limit_price=bid_price, confirm=confirm
            )
            if isinstance(fill, ExecResult):
                return fill
        return self._persist_sell(position, fill, ts=ts, close_reason=close_reason, trigger=trigger)

    def _persist_sell(
        self,
        position: HeldPosition,
        fill: _Fill,
        *,
        ts: float,
        close_reason: CloseReason,
        trigger: str | None,
    ) -> ExecResult:
        """Persist an already-executed on-chain sell (``_persist_irreversible``).
        Records the ACTUAL filled qty: a GTC sell can partially fill, and
        record_sell keeps the position open with the remainder in that case.
        On final failure log CRITICAL and skip — never raise."""
        try:
            self._persist_irreversible(
                lambda: self._store.record_sell(
                    position.position_id,
                    sold_qty=fill.qty,
                    sell_price=fill.price,
                    ts=ts,
                    close_reason=close_reason,
                    trigger=trigger,
                    order_id=fill.order_id,
                    tx_hash=fill.tx_hash,
                ),
                what=f"close_position {position.position_id}",
            )
        except Exception as exc:  # noqa: BLE001 — on-chain fill already happened
            logger.error(
                "CRITICAL: on-chain sell filled (order=%s tx=%s) but close_position "
                "failed after %d attempts for position %d: %s — tokens are gone; "
                "leaving open for reconciliation",
                fill.order_id,
                fill.tx_hash,
                _PERSIST_ATTEMPTS,
                position.position_id,
                exc,
            )
            return ExecResult.skip(f"close_persist_failed:{type(exc).__name__}")
        logger.info(
            "live sell filled: %s %s qty=%.4f @ %.4f (position %d, %s) order=%s",
            position.market_id,
            position.side,
            fill.qty,
            fill.price,
            position.position_id,
            close_reason,
            fill.order_id,
        )
        return ExecResult.ok(
            price=fill.price,
            qty=fill.qty,
            position_id=position.position_id,
            resting_order_id=fill.order_id if fill.resting else None,
        )


# ---------- factory ----------


def build_live_executor(
    wallet,  # WalletSpec, typing avoided to skip cyclic import
    portfolio: PortfolioStore,
) -> LiveExecutor:
    """Construct a LiveExecutor from a WalletSpec + PortfolioStore.

    Resolves ``wallet.private_key_ref`` to the EOA signer key, binds the
    v2 ClobClient to ``wallet.funder_address`` (the DepositWallet) with
    POLY_1271 sig type, then derives + sets L2 API creds.

    Raises on any failure (lifespan catches, logs, and leaves dispatcher's
    live=None).
    """
    from openpoly.execution.clob_patch import ClobClient
    from openpoly.news.secrets import resolve

    private_key = resolve(wallet.private_key_ref)

    clob = ClobClient(
        CLOB_HOST,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=SIGTYPE_POLY_1271,
        funder=wallet.funder_address,
    )
    creds = clob.derive_api_key()
    clob.set_api_creds(creds)
    # The event is INFO; its payload is not. The L2 API key is a live trading
    # credential and never appears at any level — a prefix of a credential is
    # still a piece of one, and it bought nothing that "ready" does not say.
    # The signer / funder addresses are public on chain but still identify the
    # operator's wallet, so they sit at DEBUG: reachable when someone is
    # deliberately debugging, absent from the log file and from every pasted
    # snippet by default.
    logger.info("live executor ready")
    logger.debug(
        "live executor bound: signer=%s funder=%s",
        clob.get_address(),
        wallet.funder_address[:10] + "…",
    )
    return LiveExecutor(portfolio=portfolio, clob_client=clob)

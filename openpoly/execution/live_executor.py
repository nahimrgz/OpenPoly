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
from typing import Any, Protocol, TypeGuard, TypeVar

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
from openpoly.execution.sizing import (
    MAX_TOKEN_PRICE,
    MIN_NOTIONAL_USD,
    MIN_TOKEN_PRICE,
    dust_remainder_skip,
    quantize_size,
)
from openpoly.execution.types import ExecResult
from openpoly.markets.manager import manager as market_source_manager
from openpoly.portfolio import CloseReason, HeldPosition, PortfolioStore
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

logger = logging.getLogger(__name__)


def _parse_amount(raw: Any) -> float | None:
    """One amount field from an order response. Absent or empty is a clean 0.0;
    anything unparseable is None — unknown, never a silent zero, so the caller
    can keep the fields that did parse and say which did not.

    ``float()`` alone is not that validation. ``NaN`` and the infinities parse
    without raising and then make every comparison downstream False; and an
    integer too large for a double — which ``json.loads`` hands over verbatim
    for an unquoted literal — raises ``OverflowError``, an ``ArithmeticError``
    a ``ValueError`` clause does not catch. Both are None like the rest.
    """
    if raw is None or raw == "":
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
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


def _in_price_band(price: float | None) -> TypeGuard[float]:
    """A price the venue could have produced: inside the band, which no
    non-finite value can be."""
    return price is not None and MIN_TOKEN_PRICE <= price <= MAX_TOKEN_PRICE


def _bookable_price(
    price: float | None, *, fallback: float, side: str, order_id: str | None
) -> float:
    """The price to book a fill at, guaranteed a price the venue could mean.

    Every ``_Fill`` price comes through here — the quotient the venue implies,
    our own limit when that quotient is unusable, and the limit again on the
    balance-confirmed path. Nothing downstream would catch a bad one:
    ``open_position`` validates nothing and the exit section rejects only a
    non-positive basis, so an out-of-band basis survives into every exit
    decision and into realized P&L.

    ``price`` is None when no quotient could be computed; the caller has already
    said so in its own words, since "the response carried no amounts" and "the
    venue's own quotient is nonsense" are different operator stories.

    A fill is never dropped over a price. When the limit is out of band too — a
    SELL's is the live best bid, which nothing upstream validates — the fill is
    booked at the WORST edge for the side rather than the nearest one: a BUY at
    the ceiling (the highest cost it could have paid), a SELL at the floor (the
    least it could have received). Clamping to the nearest edge instead would
    hand a SELL the most favourable price there is, inventing realized profit —
    and fabricated profit resets the entry kill switch's consecutive-loss walk.
    A non-finite value is out of band by the same comparison, so it never
    survives to become a basis.
    """
    if _in_price_band(price):
        return price
    if price is not None:
        logger.error(
            "%s: %r is not a price the venue could produce (outside [%.4f, %.1f]) — "
            "recording the limit %.4f instead (order=%s)",
            side,
            price,
            MIN_TOKEN_PRICE,
            MAX_TOKEN_PRICE,
            fallback,
            order_id,
        )
    if _in_price_band(fallback):
        return fallback
    worst = MAX_TOKEN_PRICE if side == "buy" else MIN_TOKEN_PRICE
    logger.error(
        "CRITICAL: %s: the limit %r is not a price the venue could produce either — "
        "the order book itself is wrong-scale; booking the worst edge %.4f rather "
        "than a flattering one (order=%s)",
        side,
        fallback,
        worst,
        order_id,
    )
    return worst


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
        raw_id = resp.get("orderID")
        order_id = _parse_order_id(raw_id)
        if order_id is None and raw_id:
            # Only a value that is both present and non-empty is the venue
            # naming the order; ``""``/``0``/``False`` name nothing. This one
            # named it and we cannot address it, which is worth saying here,
            # where the raw value is in hand.
            logger.error(
                "the order response named the order %r (%s), a shape no cancel "
                "payload can carry — it cannot be cancelled or handed to a caller",
                raw_id,
                type(raw_id).__name__,
            )
        return cls(
            order_id=order_id,
            shares=_parse_amount(resp.get(shares_key)),
            usdc=_parse_amount(resp.get(usdc_key)),
            tx_hash=first if isinstance(first, str) and first else None,
        )


# ---------- the settle's evidence model ----------
#
# Several sources can say how much of one order filled, they can disagree, and
# any of them can be nonsense. Rather than fold them together as they arrive —
# which is how a maximum here and a clamp there ended up contradicting each
# other — each is recorded as evidence with a strength, and the answer is
# resolved once, at the end, by one rule.
#
# Plausibility first, for every source alike: a quantity above the order's own
# size, or below zero, is not a quantity. It is a wrong-scale or garbage field,
# so it is discarded and logged — never booked down to the size, because
# clamping pins a full-size floor under the answer and opens a phantom position
# against an empty wallet.
#
#   EXACT     the venue's own count of an order that can no longer fill (a
#             cancelled status, or a matched size equal to the order). Nothing
#             more can arrive, so this is the final answer.
#   AT_LEAST  a lower bound: the POST body's immediate cross, a read taken while
#             the order was still live, or a CTF balance delta. More may have
#             filled since.
#
#   1. EXACT wins outright. A lower bound never floors it — when one exceeds an
#      EXACT count the two answers disagree, which is reported, not averaged.
#   2. Otherwise the largest plausible lower bound wins.
#   3. With no evidence at all the quantity is UNKNOWN, which is not zero and
#      must never be reported as a clean miss.
#
# Zero is admitted as evidence only when it means something. A lower bound of
# zero taken while the order was still working says "at least nothing yet",
# which is nothing — more can arrive after the read. Two things make a zero
# real: an EXACT count (the venue counted, and the count was nought), and a
# reading taken once the order was already off the book (``zero_is_real``),
# where nothing more can arrive.
#
# An acknowledged cancel puts the order off the book but says nothing about how
# much filled, and the data API lags behind the matching engine — so a read
# taken after an acknowledgement corroborates, it does not overrule a cross the
# venue already reported. Only its own terminal answer does.


_POST_BODY = "the POST response"
_ORDER_READ = "the order read"
_WALLET = "the CTF balance"


def _plausible(
    qty: float | None, size: float, *, source: str, order_id: str | None
) -> float | None:
    """``qty`` when an order of ``size`` could have filled it, else None.

    The model's first rule, and its only implementation: every source asks here
    rather than repeating the comparison. A discarded number is logged, so it is
    never silently a zero, and never booked down to ``size`` — clamping pins a
    full-size floor under the answer and opens a phantom position against an
    empty wallet.
    """
    if qty is None:
        return None
    if not math.isfinite(qty) or qty < 0 or qty > size + 1e-9:
        logger.error(
            "%s reported %r filled for an order of %.4f (%s) — that is not a "
            "quantity this order could have, so it is discarded rather than "
            "booked down; the remaining sources establish the fill",
            source,
            qty,
            size,
            order_id,
        )
        return None
    return qty


@dataclass(frozen=True)
class _Evidence:
    """One source's account of how much of an order filled.

    ``settled`` marks a cross the venue stamped with a transaction hash: still a
    lower bound (more may have filled since), but one the chain has already
    recorded, so a later count cannot take it back.
    """

    qty: float
    source: str
    settled: bool = False


class _FillEvidence:
    """Collects what each source said and resolves it by strength — one EXACT
    count if the venue ever gave one, otherwise the largest lower bound. See the
    model note above."""

    def __init__(self, size: float, *, order_id: str | None = None) -> None:
        self._size = size
        self._order_id = order_id
        # At most one EXACT can exist: the read that produces it ends the settle.
        self._exact: _Evidence | None = None
        self._bounds: list[_Evidence] = []

    def vet(self, qty: float | None, *, source: str) -> float | None:
        """The model's plausibility rule for one number, without recording it."""
        return _plausible(qty, self._size, source=source, order_id=self._order_id)

    def add(
        self,
        qty: float | None,
        *,
        source: str,
        exact: bool = False,
        zero_is_real: bool = False,
        settled: bool = False,
    ) -> None:
        """Record what one source said. Vetting happens here, so no source can
        reach the answer without passing the plausibility rule, and the note
        above says which zeros count — ``zero_is_real`` for a reading taken once
        the order was already off the book, ``settled`` for a cross the venue
        stamped with a transaction hash."""
        qty = self.vet(qty, source=source)
        if qty is None or (qty <= 0 and not (exact or zero_is_real)):
            return
        if exact:
            self._exact = _Evidence(qty=qty, source=source)
        else:
            self._bounds.append(_Evidence(qty=qty, source=source, settled=settled))

    def add_read(self, matched: float | None, status: str, *, zero_is_real: bool = False) -> bool:
        """Record one ``GET /data/order`` answer; return whether it shows the
        order can no longer fill.

        The order matters: an implausible ``size_matched`` is discarded FIRST, so
        a number the venue cannot mean can never make the order look fully
        matched. A cancelled status proves terminality without any size at all;
        a match proves it only through the size, since the venue reports
        ``MATCHED`` for a PARTIAL match too and believing that status would
        abandon the unmatched remainder on the book.
        """
        matched = self.vet(matched, source=_ORDER_READ)
        terminal = status in _ORDER_DONE_STATUSES or (
            matched is not None and matched >= self._size - 1e-9
        )
        self.add(matched, source=_ORDER_READ, exact=terminal, zero_is_real=zero_is_real)
        return terminal

    def resolve(self) -> float | None:
        """The quantity to book, or None when no source could establish one."""
        if self._exact is not None:
            answer = self._exact.qty
            for bound in self._bounds:
                if bound.qty <= answer + 1e-9:
                    continue
                # The two answers disagree. An EXACT count normally wins, but
                # two kinds of bound outlast it, and for the same reason: the
                # data API lags the matching engine, so a smaller "final" count
                # is the lag rather than a reversal. A cross the chain already
                # stamped with a transaction hash happened, whatever a later
                # read says. And an earlier reading OF THIS ORDER counts what
                # this order matched — ``size_matched`` only ever grows — so a
                # later reading below it is the same lag seen twice. Every other
                # bound is a different source's guess and cannot outlast the
                # count.
                earlier_reading = bound.source == self._exact.source
                logger.error(
                    "%s disagrees with %s about order %s: %.4f against a final "
                    "count of %.4f — booking %s",
                    bound.source,
                    self._exact.source,
                    self._order_id,
                    bound.qty,
                    self._exact.qty,
                    "the settled cross, which the chain already recorded"
                    if bound.settled
                    else "the larger reading, since this order's matched size only grows"
                    if earlier_reading
                    else "the venue's count of this order",
                )
                if bound.settled or earlier_reading:
                    answer = max(answer, bound.qty)
            return answer
        if self._bounds:
            return max(e.qty for e in self._bounds)
        return None


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

    def _read_order(self, order_id: str) -> tuple[float | None, str] | None:
        """``GET /data/order``: (size matched so far, upper-cased status), or
        None when the call itself failed — logged here, interpreted by the
        caller.

        The size is None when it is missing, blank, or will not parse — unknown,
        never a fill of zero. An answer without the field at all is not an order
        document, and reading it as a confirmed zero would suppress the balance
        fallback, the only remaining way to see a fill that raced the cancel.
        (In a POST body absent legitimately means zero; that is why the
        distinction lives here and not in ``_parse_amount``.)

        The status still comes back either way. It answers a different question —
        whether anything can still be resting — and throwing it away over a
        missing size spends the whole retry budget on an order the venue has
        already reported cancelled.
        """
        try:
            order = self._clob.get_order(order_id)
            raw_matched = order.get("size_matched")
            matched = _parse_amount(raw_matched)
            status = str(order.get("status") or "").upper()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_order for %s failed: %s", order_id, exc)
            return None
        # Absent and blank alike: the parser reads both as a clean zero, which is
        # right for a POST body and wrong here — it would be the venue counting
        # nought.
        if raw_matched is None or raw_matched == "" or matched is None:
            logger.error(
                "get_order for %s answered without a usable size_matched (%r) — "
                "the size stays unknown; the status is still %r",
                order_id,
                raw_matched,
                status,
            )
            return None, status
        return matched, status

    def _cancel_with_retry(self, order_id: str, evidence: _FillEvidence) -> tuple[str | None, bool]:
        """Cancel ``order_id``, retrying across the venue's matching-delay window
        and recording what each read said into ``evidence``.

        Returns ``(refusal, counted)``. ``refusal`` is None once the venue says
        the order is off the book — it acknowledged the cancel, or a read showed
        it terminal. Otherwise it is the last refusal reason after the loop ran
        out of attempts or of wall clock, whichever came first
        (``_SETTLE_ATTEMPTS`` / ``_SETTLE_DEADLINE_S``).

        ``terminal`` says a read showed the order can no longer fill, so the
        caller needs no further read. An acknowledged cancel is not that: it
        settles whether anything rests, not how much filled.
        """
        reason = "cancel never attempted"
        start = time.monotonic()
        for attempt in range(_SETTLE_ATTEMPTS):
            try:
                res = self._clob.cancel_order(OrderPayload(orderID=order_id))
            except Exception as exc:  # noqa: BLE001
                res = {"not_canceled": {order_id: f"{type(exc).__name__}: {exc}"}}
            if not isinstance(res, dict):
                res = {"not_canceled": {order_id: f"unexpected response {type(res).__name__}"}}
            # Both halves of the body are normalised once, through the same
            # coercion the id itself went through: a venue that answers with a
            # numeric ``orderID`` echoes it numerically here too, in either half,
            # and ``"12345" in [12345]`` is False — which would read every
            # attempt as a refusal and end in a false alert for an order the
            # venue had in fact cancelled. The acknowledgement must be a list
            # (a bare string would make membership a substring test and
            # acknowledge an order we never sent); anything else is no
            # acknowledgement.
            raw_acked = res.get("canceled")
            acked = (
                {_parse_order_id(e) for e in raw_acked} if isinstance(raw_acked, list) else set()
            )
            if order_id in acked:
                # The venue removed the order. That is its own statement about
                # this id, so nothing rests — a later read still saying LIVE is
                # lag in its data API, not a contradiction. How much filled is a
                # separate question, and the caller's fresh read answers it.
                return None, False
            raw_refusals = res.get("not_canceled")
            if raw_refusals is None or isinstance(raw_refusals, Mapping):
                # An absent key, and a mapping that does not name our id, are the
                # same well-formed refusal that gives no reason. Only a present
                # value of the wrong type is a malformed body, and saying so of
                # the others sends the operator hunting a venue bug that is not
                # there. A refusal is never an exception: raising would abandon
                # the order on the book.
                refusals = {_parse_order_id(k): v for k, v in (raw_refusals or {}).items()}
                reason = str(refusals.get(order_id, "refused without a reason"))
            else:
                reason = f"unexpected cancel body shape ({type(raw_refusals).__name__})"
            state = self._read_order(order_id)
            if state is not None and evidence.add_read(*state):
                return None, True
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
        return reason, False

    @staticmethod
    def _wallet_delta(
        confirm: Callable[[], float], size: float, *, order_id: str | None
    ) -> float | None:
        """What the wallet says this order filled, booked down to ``size``.

        The wallet is the one source clamped rather than discarded, and the
        reason is the same wherever it is asked — the settle, when the order
        itself could not be re-read, and the lost-response path, where there is
        no order id to read by. An order for exactly ``size`` was sent, so a
        delta above it still means at least ``size`` was ours, with an EARLIER
        order's remainder settling in the same window explaining the excess.
        Discarding it is the worse error: the wallet is the last source to
        speak, so nothing else can ever see the fill — the buy leaves tokens no
        ledger row manages, the sell leaves the row open with the tokens already
        spent — and a larger delta would book less than a smaller one. A delta
        that is not a quantity at all, negative or non-finite, is still
        discarded, because that is not a reading.

        Both callers ask here rather than restating the rule, so a later
        "simplification" of one of them cannot quietly turn the clamp back into
        a discard on one path only. That has already happened twice.
        """
        moved = confirm()
        if not math.isfinite(moved) or moved < 0:
            logger.error(
                "%s reported %r moved for order %s — that is not a quantity, so "
                "it establishes nothing",
                _WALLET,
                moved,
                order_id,
            )
            return None
        if moved > size + 1e-9:
            logger.error(
                "%s moved %.4f for an order of %.4f (%s) — more than this order "
                "could be, so the wallet moved for more than one reason; booking "
                "this order's own size and leaving the excess to reconciliation",
                _WALLET,
                moved,
                size,
                order_id,
            )
        return min(moved, size)

    def _settle_resting_remainder(
        self,
        order_id: str | None,
        reported_qty: float,
        size: float,
        *,
        confirm: Callable[[], float],
        cross_settled: bool = False,
    ) -> tuple[float | None, bool]:
        """Cancel whatever of this order is still resting and return the two
        facts it established: ``(qty, rests)``.

        ``qty`` is resolved by the evidence model above from every source that
        spoke, and is None when none of them could — unknown, which is not zero
        and must never be named a clean miss. ``rests`` says part of the order
        may still be on the book: no order id to cancel by, or every cancel
        refused and no later read showing it terminal. Both are facts, not
        labels; the caller names the outcome.

        ``cross_settled`` says the response stamped its cross with a transaction
        hash: the chain already recorded that much, so a later count cannot take
        it back.
        """
        if reported_qty >= size - 1e-9:
            return reported_qty, False  # full fill — nothing resting, no round-trip

        evidence = _FillEvidence(size, order_id=order_id)
        evidence.add(reported_qty, source=_POST_BODY, settled=cross_settled)
        refusal: str | None = None
        # A zero from a read taken once the order was off the book: held back
        # rather than dropped, and admitted at the end if nothing else speaks.
        off_book_zero = False
        if not order_id:
            # Nothing can be cancelled, so part of the order may rest either way.
            # The alert is the only channel for that — but the wallet is still
            # the one signal left about how MUCH filled, and this was the single
            # path that returned without ever asking it.
            logger.error(
                "RESTING ORDER ALERT: %.4f of %.4f filled but the response carried no "
                "usable order id — nothing to cancel by; reverse-reconciliation will "
                "flag it",
                reported_qty,
                size,
            )
            rests = True  # nothing was cancelled, so nothing is off the book
        else:
            refusal, terminal = self._cancel_with_retry(order_id, evidence)
            if not terminal:
                # The venue's final count is not in hand: an acknowledged cancel,
                # an exhausted budget and reads that never landed are alike here.
                # This read is the only place a fill that raced the cancel is
                # visible, and the only thing that can show a refused order is no
                # longer resting.
                state = self._read_order(order_id)
                if state is None:
                    logger.error(
                        "order %s could not be re-read after settle — confirming the "
                        "fill through the CTF balance instead",
                        order_id,
                    )
                # An acknowledged cancel puts the order off the book but the data
                # API lags the matching engine, so a zero read moments later
                # proves nothing — exactly as the same zero read inside the loop
                # proves nothing. Only the read's own terminal answer is
                # evidence; anything else leaves the wallet its turn.
                elif evidence.add_read(*state):
                    refusal = None  # the order is terminal — nothing rests after all
                off_book_zero = not refusal and state is not None and state[0] == 0.0
            rests = refusal is not None
        qty = evidence.resolve()
        if qty is None:
            # No source could say. The wallet is the last channel — and only for
            # the quantity: it is a delta against a pre-order baseline attributed
            # to no order id, so an EARLIER order's remainder filling in this
            # poll window is indistinguishable from this one filling. It never
            # decides whether anything rests. A false alert on an order the
            # wallet suggests is filled costs a reconciliation look; suppressing
            # a true one leaves an answered order resting unannounced.
            try:
                confirmed = self._wallet_delta(confirm, size, order_id=order_id)
            except Exception as cexc:  # noqa: BLE001
                logger.error("balance confirmation for %s failed too: %s", order_id, cexc)
                confirmed = None
            if confirmed:
                logger.error(
                    "fill for %s established through the CTF balance: %.4f",
                    order_id,
                    confirmed,
                )
            evidence.add(confirmed, source=_WALLET)
            qty = evidence.resolve()
        if qty is None and off_book_zero:
            # Last of all: the order is off the book and its own read said
            # nought. That zero could not be trusted to PREEMPT the wallet — the
            # data API lags, so a fill racing the cancel would not be in it yet —
            # but once the wallet has spoken and established nothing either, two
            # sources agree that nothing filled. That is a clean miss, not the
            # absence of an answer.
            qty = 0.0

        if refusal is not None:
            logger.error(
                "RESTING ORDER ALERT: could not cancel %s within %d attempts / %.1fs "
                "(last: %s) — remainder may fill untracked; reverse-reconciliation "
                "will flag it",
                order_id,
                _SETTLE_ATTEMPTS,
                _SETTLE_DEADLINE_S,
                refusal,
            )
        return qty, rests

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
        # The cross is asked against the model's own plausibility rule rather
        # than compared here a second time: a share count the order cannot have
        # filled is discarded, and the settle establishes the fill instead.
        shares = _plausible(parsed.shares, size, source=_POST_BODY, order_id=order_id)
        if shares is None and parsed.shares is not None:
            bad.append(shares_key)
        shares, usdc = shares or 0.0, parsed.usdc or 0.0
        if bad:
            logger.error(
                "%s response carried unusable %s for order %s — settling and "
                "recording only what could be read",
                side,
                " and ".join(bad),
                order_id,
            )
            no_fill_reason = "live_unparseable"
        qty, resting = self._settle_resting_remainder(
            order_id,
            shares,
            size,
            confirm=confirm,
            cross_settled=parsed.tx_hash is not None,
        )
        # The settle reports facts; the names are chosen here. A may-rest outcome
        # is reported on a fill exactly as it is on a skip: a partial fill whose
        # remainder could not be cancelled is still an order the venue may hold,
        # and the caller must not post a second one on top of it. (When the
        # response named no usable id there is nothing to hand back, so the
        # RESTING ORDER ALERT is the only channel.)
        if resting:
            no_fill_reason = "live_cancel_failed"
        elif qty is None:
            no_fill_reason = "live_fill_unknown"
        if not qty:
            return ExecResult.skip(
                no_fill_reason,
                resting_order_id=order_id if resting else None,
            )
        if shares > 0 and usdc > 0:
            # The raced-extra portion (if any) filled at our limit price, so
            # pricing it at the response average is conservative-enough (≤1 tick).
            price: float | None = usdc / shares
        else:
            # Nothing had crossed at response time, or a field is unusable: the
            # real price is unknown. Our limit is the conservative stand-in —
            # BUY cost can only be ≤ it, SELL proceeds ≥ it.
            logger.warning(
                "%s: %.4f filled but the response carried no usable amounts — "
                "falling back to the limit price (order=%s)",
                side,
                qty,
                order_id,
            )
            price = None
        return _Fill(
            qty=qty,
            price=_bookable_price(price, fallback=limit_price, side=side, order_id=order_id),
            tx_hash=parsed.tx_hash,
            order_id=order_id,
            resting=resting,
        )

    def _fill_from_balance(
        self, confirm: Callable[[], float], *, side: str, size: float, limit_price: float
    ) -> _Fill | ExecResult | None:
        """The fill as the CTF balance tells it, for when the venue's own account
        of the order is unavailable (lost response): the balance delta booked at
        our limit — BUY cost can only be ≤ it, SELL proceeds ≥ it — with no order
        id or tx hash to keep. None when nothing moved; flagged as resting when
        only part of the order moved.

        The clamp and the discard rules both live in ``_wallet_delta``; this
        path only differs in what an unusable reading means to it. There the
        venue's own account of the order is already gone, so a delta that
        establishes nothing leaves this order's fate unknown rather than merely
        unmeasured.
        """
        qty = self._wallet_delta(confirm, size, order_id=None)
        if qty is None:
            return ExecResult.skip("live_fill_unknown")
        if qty <= 0:
            return None
        # A delta short of the whole order is positive proof that a remainder is
        # still on the book — and this path never learned an order id, so nothing
        # can cancel it. That is an alert, not a finished trade.
        resting = qty < size - 1e-9
        if resting:
            logger.error(
                "RESTING ORDER ALERT: the lost response's order filled only %.4f of "
                "%.4f — the remainder rests with no id this executor ever learned; "
                "reverse-reconciliation will flag it",
                qty,
                size,
            )
        price = _bookable_price(None, fallback=limit_price, side=side, order_id=None)
        return _Fill(qty=qty, price=price, tx_hash=None, order_id=None, resting=resting)

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

        # Same rule as the SELL below: the order is signed at this price, which
        # the entry section read off the book's asks and guards only against a
        # non-positive one.
        if not _in_price_band(intent.price):
            logger.error(
                "buy aborted for %s %s: %r is not a price the venue could produce "
                "(outside [%.4f, %.1f]) — refusing to sign against a wrong-scale book",
                intent.market_id,
                intent.side,
                intent.price,
                MIN_TOKEN_PRICE,
                MAX_TOKEN_PRICE,
            )
            return ExecResult.skip("price_out_of_band")

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
            fill = self._fill_from_balance(confirm, side="buy", size=size, limit_price=intent.price)
            if isinstance(fill, ExecResult):
                return fill
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
        # The order is SIGNED at this price. Refusing afterwards only declines to
        # RECORD a price we already traded at, so a book the venue could not have
        # produced stops the trade here — nothing upstream validates its levels.
        if not _in_price_band(bid_price):
            logger.error(
                "sell aborted for position %d: the book's best bid %r is not a price "
                "the venue could produce (outside [%.4f, %.1f]) — refusing to sign "
                "against a wrong-scale book",
                position.position_id,
                bid_price,
                MIN_TOKEN_PRICE,
                MAX_TOKEN_PRICE,
            )
            return ExecResult.skip("price_out_of_band")

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
            fill = self._fill_from_balance(confirm, side="sell", size=size, limit_price=bid_price)
            if isinstance(fill, ExecResult):
                return fill
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

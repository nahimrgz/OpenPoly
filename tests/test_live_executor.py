"""Tests for LiveExecutor — crossing-GTC submission through a faked _ClobClient.

Every order the server answers is settled: whatever did not fill immediately is
cancelled, across a bounded retry that outlives the venue's matching delay.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.execution.clob_patch import PolyApiException
from openpoly.execution.live_executor import (
    _PERSIST_ATTEMPTS,
    _PERSIST_SLEEP,
    _SETTLE_ATTEMPTS,
    _SETTLE_SLEEP,
    LiveExecutor,
    _bookable_price,
)
from openpoly.execution.sizing import MAX_TOKEN_PRICE, MIN_TOKEN_PRICE
from openpoly.markets.manager import manager as market_source_manager
from openpoly.markets.models import OrderBook, normalize_gamma_market
from openpoly.markets.store import MarketStore, PollSummary
from openpoly.portfolio import PortfolioStore
from openpoly.sections.entry.edge_threshold_v0 import OrderIntent


def _refused(order_id: str = "0x1", reason: str = "order is pending") -> dict[str, Any]:
    """DELETE /order body when the venue keeps the order (200, not an exception)."""
    return {"canceled": [], "not_canceled": {order_id: reason}}


def _cancelled(order_id: str = "0x1") -> dict[str, Any]:
    """DELETE /order body when the venue acknowledges the cancel."""
    return {"canceled": [order_id], "not_canceled": {}}


class _FakeClob:
    """Records calls; returns canned responses set per test.

    ``ctf_balance_raw`` controls what get_balance_allowance returns for
    CONDITIONAL queries (the SELL CTF-cache poll). Set high (default 1e18)
    so SELL tests pass immediately; tests for cache-lag use 0.
    """

    def __init__(
        self,
        *,
        order_response: Any = None,
        exception: Exception | None = None,
        allowance_update_raises: bool = False,
        ctf_balance_raw: int = 10**18,
        ctf_balance_sequence: list[Any] | None = None,
        cancel_responses: list[Any] | None = None,
        order_status: Any = None,
    ) -> None:
        # ``None`` → the default full fill; anything else (including a non-dict
        # such as "") is returned verbatim, so a 200-with-garbage body can be
        # modelled.
        self._response = (
            {
                "success": True,
                "orderID": "0xDEAD",
                "status": "matched",
                "makingAmount": "5.0",
                "takingAmount": "10.0",
                "transactionsHashes": ["0xCAFE"],
            }
            if order_response is None
            else order_response
        )
        self._exception = exception
        self._allowance_update_raises = allowance_update_raises
        self._ctf_balance_raw = ctf_balance_raw
        # When set, successive CONDITIONAL balance reads consume this list
        # (last value sticks) — models balance changing across the pre-order
        # gate read and the post-exception confirmation polls.
        # (an Exception entry is raised — models the read going dark).
        self._ctf_balance_sequence = list(ctf_balance_sequence) if ctf_balance_sequence else None
        # Successive cancel_order calls consume this list (last value sticks);
        # an Exception entry is raised. None → the venue acknowledges every cancel.
        self._cancel_responses = list(cancel_responses) if cancel_responses else None
        # get_order answers: one dict, or a list consumed per call (last value
        # sticks; an Exception entry is raised). Default "0" so the final qty
        # falls back to the reported fill (max(matched, reported)).
        if order_status is None:
            order_status = {"size_matched": "0"}
        self._order_status = (
            list(order_status) if isinstance(order_status, list) else [order_status]
        )
        self.posted: list[dict[str, Any]] = []
        self.allowance_updates: list[Any] = []
        self.cancelled: list[str] = []
        self.order_reads = 0
        self.balance_reads = 0

    def create_and_post_order(self, order_args, options, order_type):
        self.posted.append({"order_args": order_args, "options": options, "order_type": order_type})
        if self._exception is not None:
            raise self._exception
        return self._response

    def update_balance_allowance(self, params):
        self.allowance_updates.append(params)
        # Scoped to the pre-signing COLLATERAL refresh: the CONDITIONAL read is
        # the lost-response confirmation baseline, whose failure is a separate
        # (and fatal) case — an Exception entry in ``ctf_balance_sequence``.
        if self._allowance_update_raises and params.asset_type == "COLLATERAL":
            raise RuntimeError("cache refresh failed")

    @staticmethod
    def _next(seq: list[Any]) -> Any:
        """Consume ``seq`` one entry per call; the last entry sticks."""
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def cancel_order(self, payload):
        self.cancelled.append(payload.orderID)
        if self._cancel_responses is None:
            return _cancelled(payload.orderID)
        res = self._next(self._cancel_responses)
        if isinstance(res, Exception):
            raise res
        return res

    def get_order(self, order_id):
        self.order_reads += 1
        res = self._next(self._order_status)
        if isinstance(res, Exception):
            raise res
        return res

    def get_balance_allowance(self, params):
        # CONDITIONAL queries return the CTF balance the SELL poll checks;
        # COLLATERAL queries don't matter for these tests.
        self.balance_reads += 1
        if self._ctf_balance_sequence is not None:
            val = self._next(self._ctf_balance_sequence)
            if isinstance(val, Exception):
                raise val
            return {"balance": str(val), "allowances": {}}
        return {"balance": str(self._ctf_balance_raw), "allowances": {}}


@pytest.fixture(autouse=True)
def _isolate_market_store():
    saved = market_source_manager.store
    market_source_manager.store = MarketStore()
    yield
    market_source_manager.store = saved


@pytest.fixture
def store(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    yield PortfolioStore(make_session_factory(engine))
    engine.dispose()


def _market(market_id: str = "m1", *, neg_risk: bool = False):
    raw = {
        "id": market_id,
        "conditionId": f"0x{market_id}",
        "question": "Q?",
        "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
        "negRisk": neg_risk,
    }
    m = normalize_gamma_market(raw, event={"id": "e", "title": "E", "tags": []})
    assert m is not None
    return m


def _populate(market, *books: OrderBook) -> None:
    s = market_source_manager.store
    s.replace([market], PollSummary(ts=1.0, fetched=1, kept=1, reason_counts={}))
    s.set_order_books(list(books))


def _intent(market_id="m1", side="yes", price=0.5, qty=10.0) -> OrderIntent:
    return OrderIntent(market_id=market_id, side=side, price=price, qty=qty)


def _zero_fill_response(status: str, order_id: str | None = "0x1") -> dict[str, Any]:
    """POST /order answer for an order that crossed nothing: ``live`` rests,
    ``delayed`` sits in the venue's matching-delay window, ``unmatched`` rests
    after that window expired. ``order_id=None`` drops the id altogether."""
    resp: dict[str, Any] = {
        "success": True,
        "status": status,
        "makingAmount": "0",
        "takingAmount": "0",
    }
    if order_id is not None:
        resp["orderID"] = order_id
    return resp


def _held(store: PortfolioStore, m, **overrides: Any):
    """Seed one open position on ``m`` (10 shares @ 0.40 by default)."""
    kw: dict[str, Any] = dict(
        market_id=m.market_id,
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    kw.update(overrides)
    return store.open_position(**kw)


def _settle_waits(no_sleep: list[float]) -> list[float]:
    """Only the settle loop's waits — ``no_sleep`` also records the CTF poll's
    and the persist retries'."""
    return [w for w in no_sleep if w == _SETTLE_SLEEP]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch) -> list[float]:
    """Patch the executor's ``time.sleep`` (settle retries, CTF polls, persist
    retries) for every test and record the requested delays; tests that assert
    on the delays request it by name."""
    import openpoly.execution.live_executor as le_mod

    calls: list[float] = []
    monkeypatch.setattr(le_mod.time, "sleep", lambda secs=0.0, *_a, **_k: calls.append(secs))
    return calls


def _locked_db_error() -> OperationalError:
    """The one class of failure ``_persist_irreversible`` treats as transient:
    SQLite lock contention surfaced through SQLAlchemy."""
    return OperationalError("stmt", {}, Exception("database is locked"))


class _FlakyStore:
    """Wraps a PortfolioStore; ``method`` raises for the first ``fail_times``
    calls (``None`` = always), then delegates. Defaults to modeling a
    transient DB write failure (``OperationalError``, e.g. SQLite lock
    contention) *after* an irreversible on-chain fill — the window that
    leaves the ledger out of step with the wallet if the persist is dropped
    instead of retried. Pass ``make_exc`` to model a permanent failure
    (``ValueError``, ``IntegrityError``) instead."""

    def __init__(
        self,
        inner: PortfolioStore,
        *,
        method: str,
        fail_times: int | None,
        make_exc: Callable[[], Exception] = _locked_db_error,
    ) -> None:
        self._inner = inner
        self._method = method
        self._remaining = fail_times
        self._make_exc = make_exc
        self.attempts = 0

    def __getattr__(self, name: str) -> Any:
        if name == self._method:
            return self._flaky
        return getattr(self._inner, name)

    def _flaky(self, *args: Any, **kwargs: Any):
        self.attempts += 1
        if self._remaining is None or self._remaining > 0:
            if self._remaining is not None:
                self._remaining -= 1
            raise self._make_exc()
        return getattr(self._inner, self._method)(*args, **kwargs)


# ---------- execute_buy ----------


def test_buy_success_records_actual_fill(store) -> None:
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xORDER",
            "status": "matched",
            "makingAmount": "4.0",  # pUSD paid
            "takingAmount": "10.0",  # tokens received
            "transactionsHashes": ["0xTX"],
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n1", ts=100.0)
    assert r.filled is True
    assert r.price == pytest.approx(0.4)
    assert r.qty == pytest.approx(10.0)
    # GTC (verified) order type was passed
    assert str(clob.posted[0]["order_type"]).endswith("GTC")
    # BUY quantized the qty (10.0 was already integer, kept as-is)
    assert clob.posted[0]["order_args"].size == 10.0
    # Collateral allowance refresh + the CONDITIONAL pre-read (the
    # lost-response confirmation baseline) both ran before the order
    assert [p.asset_type for p in clob.allowance_updates] == [
        "COLLATERAL",
        "CONDITIONAL",
    ]
    fills = store.list_fills(limit=5)
    assert any(f.order_id == "0xORDER" and f.tx_hash == "0xTX" for f in fills)


def test_buy_skips_when_below_min_notional(store) -> None:
    """qty * price < $1.10 floor → skip without posting (server min is $1.00)."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    # qty=2 × price=0.5 = $1.00, below the $1.10 floor
    r = le.execute_buy(_intent(qty=2.0, price=0.5), news_id="n", ts=1.0)
    assert r.skip_reason == "min_notional_below_floor"
    assert clob.posted == []


def test_buy_quantizes_fractional_qty_down(store) -> None:
    """qty=5.567 → floors to 5.56; the SDK allows 2 size decimals."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    le.execute_buy(_intent(qty=5.567, price=0.50), news_id="n", ts=1.0)
    assert clob.posted[0]["order_args"].size == pytest.approx(5.56)


def test_buy_market_not_in_catalog_skips(store) -> None:
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(market_id="nonexistent"), news_id="n", ts=1.0)
    assert r.filled is False and r.skip_reason == "market_not_found"
    assert clob.posted == []


def test_buy_duplicate_position_skips(store) -> None:
    m = _market("m1")
    _populate(m)
    store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.4,
        qty=10.0,
        ts=50.0,
        news_id="prior",
    )
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=100.0)
    assert r.filled is False and r.skip_reason == "position_exists"
    assert clob.posted == []


def test_buy_clob_network_error_skips(store) -> None:
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(exception=ConnectionError("RPC down"))
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_error:ConnectionError"


def test_buy_definitive_rejection_skips_without_balance_confirm(store) -> None:
    """A ``PolyApiException`` with a non-None ``status_code`` means the venue
    answered and refused (bad precision, min-size, closed-only mode, ...) —
    the order was never placed, so there is nothing to confirm via the CTF
    balance. Polling it anyway would be dishonest about what happened and
    would burn the lost-response recovery path on an order that never
    existed."""
    m = _market("m1")
    _populate(m)
    exc = PolyApiException(error_msg={"error": "invalid price"})
    exc.status_code = 400
    clob = _FakeClob(exception=exc)
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason is not None
    assert r.skip_reason.startswith("live_rejected:")
    assert "invalid price" in r.skip_reason
    # Exactly the pre-order baseline read (1) — no additional balance-confirm
    # poll must follow a definitive rejection.
    assert clob.balance_reads == 1
    assert store.get_open_position("m1", "yes") is None


def test_buy_response_success_false_skips(store) -> None:
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(order_response={"success": False, "errorMsg": "price not tick-aligned"})
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_rejected:price not tick-aligned"


@pytest.mark.parametrize("status", ["live", "unmatched"])
def test_buy_zero_fill_is_cancelled(store, status) -> None:
    """A GTC that crossed nothing is placed and RESTS — ``live`` right away,
    ``unmatched`` after the venue's delay window expired without a match (per
    the order lifecycle docs it is NOT "not placed"). Left alone it fills later
    at a stale limit with no ledger row — the orphan incident the partial-fill
    cancel exists for — so zero match cancels too."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(order_response=_zero_fill_response(status))
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.skip_reason == "live_no_match"
    assert r.resting_order_id is None  # the cancel was acknowledged
    assert clob.cancelled == ["0x1"]
    assert store.get_open_position("m1", "yes") is None


def test_buy_zero_match_raced_fill_after_cancel_is_recorded(store) -> None:
    """The resting order can fill between the response and the cancel. The
    post-cancel ``get_order`` re-read is the only place that fill is visible;
    it must become a position (at the limit price — actual cost ≤ limit)."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        order_status={"size_matched": "10.0"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=10.0)
    r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(intent.price)
    assert clob.cancelled == ["0x1"]
    held = store.get_open_position("m1", "yes")
    assert held is not None
    assert held.qty == pytest.approx(10.0)
    assert held.avg_entry_price == pytest.approx(intent.price)
    assert any(f.order_id == "0x1" for f in store.list_fills(limit=5))


def test_buy_neg_risk_flag_passed_to_options(store) -> None:
    m = _market("m1", neg_risk=True)
    _populate(m)
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    le.execute_buy(_intent(), news_id="n", ts=1.0)
    posted = clob.posted[0]
    assert posted["options"].neg_risk is True


def test_buy_allowance_refresh_failure_is_non_fatal(store) -> None:
    """The pre-signing COLLATERAL allowance refresh is best-effort — the order
    should still attempt (unlike the CTF baseline read, which is not)."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(allowance_update_raises=True)
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is True  # default fake response is a fill
    assert len(clob.posted) == 1


# ---------- execute_sell ----------


def _book(token_id: str, bid: float = 0.55, ask: float = 0.56) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        ts=1.0,
        bids=[(bid, 100.0)],
        asks=[(ask, 100.0)],
    )


def test_sell_success_closes_position(store) -> None:
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xSELL",
            "status": "matched",
            "makingAmount": "10.0",  # tokens sold (SELL)
            "takingAmount": "5.5",  # pUSD received
            "transactionsHashes": ["0xSTX"],
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="take_profit", ts=200.0)
    assert r.filled is True
    assert r.price == pytest.approx(0.55)
    assert r.qty == pytest.approx(10.0)
    # GTC (verified) order type was passed
    assert str(clob.posted[0]["order_type"]).endswith("GTC")
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "closed"
    assert rec.close_reason == "take_profit"
    fills = store.list_fills(limit=5)
    sell_fill = next(f for f in fills if f.action == "sell")
    assert sell_fill.order_id == "0xSELL"
    assert sell_fill.tx_hash == "0xSTX"
    # CTF allowance refresh was called at least once (poll loop, first attempt OK)
    assert len(clob.allowance_updates) >= 1


def test_sell_skips_when_ctf_cache_never_syncs(store) -> None:
    """If CTF balance never reaches position.qty within poll window → skip."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    # CTF balance always 0 — cache "stuck"
    clob = _FakeClob(ctf_balance_raw=0)
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.skip_reason == "ctf_cache_not_synced"
    assert clob.posted == []  # never attempted to POST


def test_sell_market_gone_from_catalog_skips(store) -> None:
    m = _market("m1")
    _populate(m, _book(m.yes_token_id))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    market_source_manager.store = MarketStore()  # empty catalog
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="take_profit", ts=200.0)
    assert r.skip_reason == "market_not_found"


def test_sell_empty_bids_skips(store) -> None:
    m = _market("m1")
    book = OrderBook(token_id=m.yes_token_id, ts=1.0, bids=[], asks=[(0.6, 100.0)])
    _populate(m, book)
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="take_profit", ts=200.0)
    assert r.skip_reason == "no_bid_liquidity"
    assert clob.posted == []


@pytest.mark.parametrize("status", ["live", "unmatched"])
def test_sell_zero_fill_is_cancelled(store, status) -> None:
    """SELL twin: a sell that crossed nothing rests at our bid and would sell
    the position later behind the ledger's back. Cancel it; the position stays
    open and untouched."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id))
    held = _held(store, m)
    clob = _FakeClob(order_response=_zero_fill_response(status))
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.skip_reason == "live_no_match"
    assert r.resting_order_id is None
    assert clob.cancelled == ["0x1"]
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"
    assert rec.qty == pytest.approx(10.0)


def test_sell_zero_match_raced_fill_after_cancel_is_recorded(store) -> None:
    """A sell that filled between the response and the cancel: the re-read
    reports 4 of 10 sold. Persist that at the limit (bid) price — proceeds can
    only be ≥ bid — and keep the position open with the unsold 6."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        order_status={"size_matched": "4.0"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(4.0)
    assert r.price == pytest.approx(0.55)
    assert clob.cancelled == ["0x1"]
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"
    assert rec.qty == pytest.approx(6.0)
    assert any(f.order_id == "0x1" for f in store.list_fills(limit=5))


# ---------- lost-response confirmation (R5: at-least-once hardening) ----------
#
# A network exception from create_and_post_order does NOT mean the order
# didn't fill — the server may have matched it and only the response was
# lost (a confirmed live drift incident). The executor must confirm via
# the CTF balance before declaring failure.


def test_sell_network_error_but_balance_dropped_records_fill(store) -> None:
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    # Gate read sees 10 tokens (synced); post-exception confirm sees 0 → sold.
    clob = _FakeClob(
        exception=RuntimeError("request exception"),
        ctf_balance_sequence=[10_000_000, 0],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(0.55)  # recorded at the limit (bid) price
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "closed"


def test_sell_network_error_partial_drop_records_partial(store, caplog) -> None:
    """A delta short of the whole order is positive proof that a remainder is
    still on the book — and this path never learned an order id, so nothing can
    cancel it. That is an alert, not a finished trade."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m, qty=18.0)
    # Gate sees 18; confirm sees 3 → 15 sold, 3 unsold remain open.
    clob = _FakeClob(
        exception=RuntimeError("request exception"),
        ctf_balance_sequence=[18_000_000, 3_000_000],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(15.0)
    assert "RESTING ORDER ALERT" in caplog.text
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"
    assert rec.qty == pytest.approx(3.0)


# A raised post and a 200 whose body is not JSON (the SDK hands back the raw
# text) are the same event: the order's fate is unknown until the balance says.
_LOST_RESPONSE = {
    "exception": {"exception": RuntimeError("request exception")},
    "non-json-200": {"order_response": ""},
    # A transport failure (DNS, connection refused, timeout, ...): the SDK's
    # own signal for this is a ``PolyApiException`` with ``status_code is
    # None`` — must NOT be mistaken for a definitive rejection.
    "poly-api-transport": {"exception": PolyApiException(error_msg="Request exception!")},
}
_LOST_RESPONSE_REASON = {
    "exception": "live_error:RuntimeError",
    "non-json-200": "live_error:TypeError",
    "poly-api-transport": "live_error:PolyApiException",
}


@pytest.mark.parametrize("case", list(_LOST_RESPONSE))
def test_buy_lost_response_without_balance_change_skips(store, case) -> None:
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(ctf_balance_sequence=[0, 0], **_LOST_RESPONSE[case])
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n1", ts=100.0)
    assert r.filled is False
    assert r.skip_reason == _LOST_RESPONSE_REASON[case]
    assert store.get_open_position("m1", "yes") is None


@pytest.mark.parametrize("case", list(_LOST_RESPONSE))
def test_buy_lost_response_with_balance_rise_records_fill_at_limit(store, case) -> None:
    m = _market("m1")
    _populate(m)
    # Pre-order read 0; post-exception confirm 10 tokens → the buy filled.
    clob = _FakeClob(ctf_balance_sequence=[0, 10_000_000], **_LOST_RESPONSE[case])
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n1", ts=100.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(0.5)  # recorded at the limit price
    assert store.get_open_position("m1", "yes") is not None


@pytest.mark.parametrize("case", list(_LOST_RESPONSE))
def test_sell_lost_response_without_balance_change_skips(store, case) -> None:
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m)
    clob = _FakeClob(
        ctf_balance_sequence=[10_000_000, 10_000_000],  # never drops
        **_LOST_RESPONSE[case],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.skip_reason == _LOST_RESPONSE_REASON[case]
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"


def test_sell_definitive_rejection_skips_without_balance_confirm(store) -> None:
    """Mirrors the buy case: a ``PolyApiException`` with a non-None
    ``status_code`` means the venue refused the sell outright — no balance
    poll, and the position must stay open exactly as it was, not recorded as
    (even partially) sold."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m)
    exc = PolyApiException(error_msg="closed only mode")
    exc.status_code = 400
    clob = _FakeClob(exception=exc)
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is False
    assert r.skip_reason is not None
    assert r.skip_reason.startswith("live_rejected:")
    assert "closed only mode" in r.skip_reason
    # Exactly the CTF-sync poll's one read — no additional balance-confirm
    # poll must follow a definitive rejection.
    assert clob.balance_reads == 1
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"
    assert rec.qty == pytest.approx(held.qty)


def test_sell_partial_fill_keeps_position_open(store) -> None:
    """A partial on-chain fill must reduce the open qty, not mark the whole
    position closed — closing it strands the unsold remainder on-chain as an
    orphan (the orphaned-remainder bug)."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=18.0,
        ts=100.0,
        news_id="n",
    )
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xSELL",
            "status": "matched",
            "makingAmount": "15.0",  # only 15 of 18 filled on-chain
            "takingAmount": "8.25",
            "transactionsHashes": ["0xSTX"],
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(15.0)
    rec = store.get_position(held.position_id)
    assert rec is not None
    assert rec.status == "open"  # remainder stays open, NOT closed
    assert rec.qty == pytest.approx(3.0)  # 18 - 15
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 15.0)


def test_sell_retries_db_close_after_transient_failure(store) -> None:
    """The on-chain sell is irreversible. A transient close_position failure
    (``OperationalError``) must be retried so the position is not left
    phantom-open while its tokens are already gone — the root cause of stuck
    phantom-open positions."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xSELL",
            "status": "matched",
            "makingAmount": "10.0",
            "takingAmount": "5.5",
            "transactionsHashes": ["0xSTX"],
        }
    )
    flaky = _FlakyStore(store, method="record_sell", fail_times=1)
    le = LiveExecutor(portfolio=flaky, clob_client=clob)
    r = le.execute_sell(held, close_reason="take_profit", ts=200.0)
    assert r.filled is True
    assert flaky.attempts == 2  # failed once, retried, then persisted
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "closed"
    assert rec.close_reason == "take_profit"


# ---------- get_collateral_balance_raw (wallet-balance W2) ----------


def test_collateral_balance_raw_reads_clob(store) -> None:
    clob = _FakeClob(ctf_balance_raw=162_199_200)
    le = LiveExecutor(portfolio=store, clob_client=clob)
    assert le.get_collateral_balance_raw() == 162_199_200
    # refresh-then-read, COLLATERAL asset type
    assert clob.allowance_updates[-1].asset_type == "COLLATERAL"


def test_collateral_balance_raw_error_is_none(store) -> None:
    class _Broken:
        def update_balance_allowance(self, params):
            raise RuntimeError("clob down")

        def get_balance_allowance(self, params):
            raise RuntimeError("clob down")

        def create_and_post_order(self, *a, **k):
            raise AssertionError("not used")

    le = LiveExecutor(portfolio=store, clob_client=_Broken())
    assert le.get_collateral_balance_raw() is None


# ---------- cancel-on-partial-fill (O1: resting-remainder hygiene) ----------
#
# A GTC order that partially fills leaves its remainder resting on the book;
# later fills against it are invisible to openPoly (a live orphan incident: a partially-filled
# GTC buy whose resting remainder filled later untracked). After a partial
# fill the executor must cancel the remainder and record the order's FINAL
# matched size.


def test_buy_partial_fill_cancels_resting_remainder(store) -> None:
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xPART",
            "status": "matched",
            "makingAmount": "6.0",  # pUSD paid
            "takingAmount": "12.0",  # only 12 of 30 filled immediately
            "transactionsHashes": ["0xTX"],
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=30.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(12.0)
    assert clob.cancelled == ["0xPART"]  # remainder cancelled, nothing rests
    assert r.resting_order_id is None
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(12.0)


def test_buy_partial_fill_records_late_matched_qty(store) -> None:
    """A fill racing the cancel: get_order's final size_matched (13) beats the
    response-reported 12 — record 13 so the extra token isn't orphaned."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xPART",
            "status": "matched",
            "makingAmount": "6.0",
            "takingAmount": "12.0",
            "transactionsHashes": ["0xTX"],
        },
        order_status={"size_matched": "13.0"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=30.0), news_id="n", ts=1.0)
    assert r.qty == pytest.approx(13.0)


def test_buy_full_fill_does_not_cancel(store) -> None:
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xFULL",
            "status": "matched",
            "makingAmount": "5.0",
            "takingAmount": "10.0",
            "transactionsHashes": ["0xTX"],
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert clob.cancelled == []  # nothing resting — no cancel round-trip


def test_buy_cancel_failure_records_reported_qty(store) -> None:
    """Every cancel attempt raising must not lose the recorded fill — record
    the reported qty and name the order that may still rest."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xPART",
            "status": "matched",
            "makingAmount": "6.0",
            "takingAmount": "12.0",
            "transactionsHashes": ["0xTX"],
        },
        cancel_responses=[RuntimeError("cancel failed")],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=30.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(12.0)
    assert r.resting_order_id == "0xPART"


def test_sell_partial_fill_cancels_resting_remainder(store) -> None:
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=18.0,
        ts=100.0,
        news_id="n",
    )
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xSPART",
            "status": "matched",
            "makingAmount": "15.0",  # 15 of 18 sold immediately
            "takingAmount": "8.25",
            "transactionsHashes": ["0xSTX"],
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is True
    assert clob.cancelled == ["0xSPART"]  # the unsold 3 don't rest


# ---------- dust remainder (below one share: not sellable, still valuable) ----------


def test_sell_skips_a_sub_one_share_remainder_and_leaves_it_open(store) -> None:
    """A partial sell can leave < 1 share open, which is not a placeable order.
    It is still worth its resolution price at settlement, so the sell skips and
    the row stays open — writing it off at 0.0 would book a fake loss and
    orphan the tokens."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n",
    )
    # A prior partial sell leaves 0.6 shares open.
    store.record_sell(
        held.position_id,
        sold_qty=9.4,
        sell_price=0.55,
        ts=150.0,
        close_reason="take_profit",
    )
    remainder = store.get_open_position("m1", "yes")
    assert remainder is not None and remainder.qty == pytest.approx(0.6)

    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(remainder, close_reason="take_profit", ts=200.0)

    assert r.filled is False
    assert r.skip_reason == "dust_remainder"
    assert clob.posted == []  # nothing placeable was ever sent
    rec = store.get_position(held.position_id)
    assert rec is not None
    assert rec.status == "open"
    assert rec.qty == pytest.approx(0.6)
    # Only the earlier partial's gain is realized — the remainder is not a loss.
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 9.4)


def test_sell_does_not_skip_a_whole_share_position(store) -> None:
    """A position of 2 shares is a placeable order — it sells, it is not dust."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = store.open_position(
        market_id="m1",
        side="yes",
        token_id=m.yes_token_id,
        condition_id=m.condition_id,
        price=0.40,
        qty=2.0,
        ts=100.0,
        news_id="n",
    )
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xSELL",
            "makingAmount": "2.0",
            "takingAmount": "1.1",
            "transactionsHashes": ["0xSTX"],
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="take_profit", ts=200.0)

    assert r.filled is True
    assert r.price == pytest.approx(0.55)
    assert len(clob.posted) == 1
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.close_reason == "take_profit"


# ---------- order idempotency fallback (no client order id in the SDK) ----------


def test_buy_refuses_to_post_without_a_ctf_baseline(store) -> None:
    """Without a pre-order CTF balance there is no way to tell a lost response
    from a real fill, and the SDK offers no client order id to query by — so a
    fill would become an untracked position. Refuse to place the order."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(ctf_balance_sequence=[RuntimeError("balance read failed")])
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)

    assert r.filled is False
    assert r.skip_reason == "ctf_balance_unavailable"
    assert clob.posted == []
    assert store.get_open_position("m1", "yes") is None


def test_live_buy_persists_the_entry_signals_from_the_intent(store) -> None:
    """Same calibration contract as paper — a live fill must be joinable to the
    belief that opened it."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xORDER",
            "makingAmount": "4.0",
            "takingAmount": "10.0",
        }
    )
    intent = OrderIntent(
        market_id="m1",
        side="yes",
        price=0.5,
        qty=10.0,
        p_model=0.61,
        confidence="medium",
        edge=0.11,
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(intent, news_id="n1", ts=100.0)
    assert r.filled is True
    rec = store.get_position(r.position_id)
    assert rec is not None
    assert rec.entry_p_model == 0.61
    assert rec.entry_confidence == "medium"
    assert rec.entry_edge == 0.11


def test_buy_refuses_when_the_ctf_balance_is_unparseable(store) -> None:
    """A balance field that is present but not a number is *unknown*, not zero.

    Returning 0 made the pre-order baseline look successfully read, so the
    ``ctf_balance_unavailable`` guard never fired — and a lost response after a
    real fill would then be confirmed against a fabricated baseline of 0,
    inventing a fill delta out of the first successful read.
    """

    class _GarbageBalance:
        def __init__(self) -> None:
            self.posted: list[dict] = []

        def update_balance_allowance(self, params) -> None:
            pass

        def get_balance_allowance(self, params):
            return {"balance": "not-a-number", "allowances": {}}

        def create_and_post_order(self, *a, **k):
            raise AssertionError("must not post without a baseline")

    _populate(_market("m1"))
    clob = _GarbageBalance()
    r = LiveExecutor(portfolio=store, clob_client=clob).execute_buy(_intent(), news_id="n", ts=1.0)

    assert r.filled is False
    assert r.skip_reason == "ctf_balance_unavailable"
    assert clob.posted == []


def test_read_ctf_balance_raw_returns_none_for_a_missing_balance_field(store) -> None:
    class _NoBalanceKey:
        def update_balance_allowance(self, params) -> None:
            pass

        def get_balance_allowance(self, params):
            return {"balance": None, "allowances": {}}

    le = LiveExecutor(portfolio=store, clob_client=_NoBalanceKey())
    assert le._read_ctf_balance_raw("tok") is None


# ---------- settle: every answered order is cancelled unless fully filled ----------
#
# Facts (docs.polymarket.com, order lifecycle + manage orders): ``delayed`` is a
# marketable order held in the venue's matching-delay window and cannot be
# cancelled while pending; ``unmatched`` rests on the book after that window;
# every cancel endpoint answers 200 with ``{"canceled": [...], "not_canceled":
# {id: reason}}`` — a refusal is not an exception.


def test_buy_delayed_order_retries_cancel_until_window_expires(store, no_sleep) -> None:
    """A ``delayed`` order refuses the first cancel (pending window); the
    executor waits and retries instead of reading one ``size_matched: 0`` and
    forgetting an order that goes on to match."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("delayed"),
        cancel_responses=[_refused(), _cancelled()],
        order_status={"size_matched": "0", "status": "LIVE"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.skip_reason == "live_no_match"
    assert clob.cancelled == ["0x1", "0x1"]
    assert len(_settle_waits(no_sleep)) == 1  # one wait between the refusal and the retry
    assert store.get_open_position("m1", "yes") is None


def test_buy_cancel_refused_every_attempt_reports_cancel_failed(store, no_sleep, caplog) -> None:
    """Every attempt refused with the order still LIVE: the bounded loop gives
    up with a distinct reason, a RESTING ORDER ALERT and the resting order's id
    — not the ``live_no_match`` a clean cancel returns, so callers do not retry
    blind on top of a resting order."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status={"size_matched": "0", "status": "LIVE"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.skip_reason == "live_cancel_failed"
    assert r.resting_order_id == "0x1"
    assert len(clob.cancelled) == _SETTLE_ATTEMPTS
    settle_waits = _settle_waits(no_sleep)
    assert len(settle_waits) == _SETTLE_ATTEMPTS - 1
    assert "RESTING ORDER ALERT" in caplog.text
    assert store.get_open_position("m1", "yes") is None


def test_buy_cancel_refused_because_already_matched_records_full_fill(store) -> None:
    """The cancel is refused with "order already matched" and the in-loop read
    shows the whole size filled: nothing rests, the fill — invisible in the
    zero-amount response — is recorded at the limit price, and that read is the
    final word (no second GET for a number already in hand)."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused(reason="order already matched")],
        order_status={"size_matched": "10.0", "status": "MATCHED"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=10.0)
    r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(intent.price)
    assert len(clob.cancelled) == 1  # refused, but the re-read settled it
    assert clob.order_reads == 1  # two round-trips in total
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(10.0)


@pytest.mark.parametrize(
    "order_status",
    [RuntimeError("order lookup failed"), {"status": "LIVE"}],
    ids=["read-raises", "no-size_matched"],
)
def test_buy_unreadable_size_falls_back_to_balance_delta(store, order_status) -> None:
    """A clean cancel, then the size cannot be read — the call raised, or the
    answer carried no ``size_matched`` and so is not an order document. Either
    way the fill that may have raced the cancel is confirmed through the CTF
    balance (the lost-response mechanism, whose baseline is already in hand)
    instead of being dropped."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        order_status=[order_status],
        ctf_balance_sequence=[0, 10_000_000],  # baseline 0; confirm sees 10 shares
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=10.0)
    r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(intent.price)
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(10.0)


def test_buy_get_order_failure_and_balance_failure_is_fill_unknown(store) -> None:
    """Neither the order read nor the balance read works after the cancel: the
    executor cannot say whether anything filled and must not claim
    ``live_no_match``."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        order_status=[RuntimeError("order lookup failed")],
        ctf_balance_sequence=[0, RuntimeError("clob down")],  # baseline ok, then dark
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_fill_unknown"
    assert clob.cancelled == ["0x1"]
    assert store.get_open_position("m1", "yes") is None


def test_buy_zero_fill_without_order_id_is_cancel_failed(store, caplog) -> None:
    """An accepted zero-fill answer with no order id cannot be cancelled by id.
    That is a resting-order alert, not a silent ``live_no_match``."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(order_response=_zero_fill_response("live", order_id=None))
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.skip_reason == "live_cancel_failed"
    assert clob.cancelled == []
    assert "RESTING ORDER ALERT" in caplog.text


def test_buy_unparseable_amounts_still_cancel(store) -> None:
    """Garbage amount fields must not skip the settle step: the order id is
    read first, the order is cancelled, and only then is the skip reported."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(order_response={**_zero_fill_response("live"), "makingAmount": "n/a"})
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.skip_reason == "live_unparseable"
    assert clob.cancelled == ["0x1"]


@pytest.mark.parametrize("making", ["", "n/a"])  # absent, then unparseable
def test_buy_unusable_usdc_amount_records_shares_at_limit_price(store, making) -> None:
    """Shares reported but no usable USDC amount: one unreadable field must not
    discard the other, and a 0.0 price must never reach the ledger (the exit
    section skips a position whose entry price is invalid). Fall back to the
    limit like every other unknown-price fill."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0x1",
            "status": "matched",
            "takingAmount": "10",
            "makingAmount": making,
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=10.0)
    r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(intent.price)
    assert clob.cancelled == []  # full fill — nothing to settle
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(10.0)
    assert held.avg_entry_price == pytest.approx(intent.price)


def test_buy_retries_open_position_after_transient_failure(store) -> None:
    """The on-chain buy is irreversible, exactly like the sell: a locked SQLite
    file (``OperationalError``) must not turn a confirmed fill into an
    unmanaged wallet position."""
    m = _market("m1")
    _populate(m)
    flaky = _FlakyStore(store, method="open_position", fail_times=2)
    le = LiveExecutor(portfolio=flaky, clob_client=_FakeClob())
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is True
    assert flaky.attempts == 3
    assert store.get_open_position("m1", "yes") is not None


def test_buy_open_position_persistent_failure_never_raises(store, caplog) -> None:
    """Even a persistently locked DB (every attempt raises ``OperationalError``)
    exhausts the retry budget and then skips instead of raising."""
    m = _market("m1")
    _populate(m)
    flaky = _FlakyStore(store, method="open_position", fail_times=None)
    le = LiveExecutor(portfolio=flaky, clob_client=_FakeClob())
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "open_persist_failed:OperationalError"
    assert flaky.attempts == _PERSIST_ATTEMPTS
    assert "CRITICAL" in caplog.text


def test_buy_open_position_value_error_is_not_retried(store, no_sleep, caplog) -> None:
    """A ``ValueError`` (logic-level: position not found / already closed) is
    permanent — retrying it can never succeed, so it must propagate on the
    very first attempt with no sleep in between."""
    m = _market("m1")
    _populate(m)
    flaky = _FlakyStore(
        store,
        method="open_position",
        fail_times=None,
        make_exc=lambda: ValueError("position already open"),
    )
    le = LiveExecutor(portfolio=flaky, clob_client=_FakeClob())
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "open_persist_failed:ValueError"
    assert flaky.attempts == 1
    assert _PERSIST_SLEEP not in no_sleep
    assert "CRITICAL" in caplog.text


def test_buy_open_position_integrity_error_is_not_retried(store, no_sleep, caplog) -> None:
    """An ``IntegrityError`` (a genuine duplicate against the partial unique
    index) fails identically on every attempt — retrying it is pointless, so
    it must propagate on the very first attempt with no sleep in between."""
    m = _market("m1")
    _populate(m)
    flaky = _FlakyStore(
        store,
        method="open_position",
        fail_times=None,
        make_exc=lambda: IntegrityError("stmt", {}, Exception("UNIQUE constraint failed")),
    )
    le = LiveExecutor(portfolio=flaky, clob_client=_FakeClob())
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "open_persist_failed:IntegrityError"
    assert flaky.attempts == 1
    assert _PERSIST_SLEEP not in no_sleep
    assert "CRITICAL" in caplog.text


def test_sell_delayed_order_retries_cancel_until_window_expires(store) -> None:
    m = _market("m1")
    _populate(m, _book(m.yes_token_id))
    held = _held(store, m)
    clob = _FakeClob(
        order_response=_zero_fill_response("delayed"),
        cancel_responses=[_refused(), _cancelled()],
        order_status={"size_matched": "0", "status": "LIVE"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.skip_reason == "live_no_match"
    assert clob.cancelled == ["0x1", "0x1"]
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"
    assert rec.qty == pytest.approx(10.0)


def test_sell_get_order_failure_and_balance_failure_is_fill_unknown(store) -> None:
    m = _market("m1")
    _populate(m, _book(m.yes_token_id))
    held = _held(store, m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        order_status=[RuntimeError("order lookup failed")],
        ctf_balance_sequence=[10_000_000, RuntimeError("clob down")],  # gate ok, then dark
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is False
    assert r.skip_reason == "live_fill_unknown"
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"
    assert rec.qty == pytest.approx(10.0)


@pytest.mark.parametrize(
    "taking",
    ["", "n/a", pytest.param(10**400, id="overflowing-int")],
)
def test_sell_unusable_usdc_amount_records_shares_at_limit_price(store, taking) -> None:
    """SELL twin: shares are ``makingAmount`` here. Booking a 0.0 sale would
    fabricate a total loss; record at the bid (proceeds can only be ≥ bid)."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xS",
            "status": "matched",
            "makingAmount": "10",
            "takingAmount": taking,
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="take_profit", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(0.55)
    assert clob.cancelled == []
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "closed"
    assert rec.realized_pnl == pytest.approx((0.55 - 0.40) * 10.0)


# ---------- resting signal: an unconfirmed cancel is typed, not just logged ----------
#
# ``ExecResult.resting_order_id`` names an order of ours the venue may still
# hold after every cancel attempt was refused or raised. It is set on skips AND
# on fills (a partial fill is persisted regardless — it happened on-chain), so
# a caller can back off instead of posting a second order on top of it.


def test_buy_partial_fill_with_unconfirmed_cancel_reports_resting_order(store) -> None:
    """12 of 30 filled, every cancel refused with the order still LIVE: the
    known fill is persisted AND the result names the order that may rest."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xPART",
            "status": "matched",
            "makingAmount": "6.0",
            "takingAmount": "12.0",
            "transactionsHashes": ["0xTX"],
        },
        cancel_responses=[_refused("0xPART")],
        order_status={"size_matched": "12.0", "status": "LIVE"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=30.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(12.0)
    assert r.resting_order_id == "0xPART"
    assert len(clob.cancelled) == _SETTLE_ATTEMPTS
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(12.0)


def test_sell_partial_fill_with_unconfirmed_cancel_reports_resting_order(store) -> None:
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m, qty=18.0)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0xSPART",
            "status": "matched",
            "makingAmount": "15.0",
            "takingAmount": "8.25",
            "transactionsHashes": ["0xSTX"],
        },
        cancel_responses=[_refused("0xSPART")],
        order_status={"size_matched": "15.0", "status": "LIVE"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(15.0)
    assert r.resting_order_id == "0xSPART"
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"
    assert rec.qty == pytest.approx(3.0)


# ---------- settle bounds + the venue's last word on the fill ----------
#
# The settle loop is bounded by BOTH a retry count and a wall clock, and every
# ending except a read that already showed the order terminal is followed by one
# fresh read: an acknowledged cancel does not undo a fill the venue reported
# mid-loop, and an exhausted budget is not proof that something still rests.


def _stepping_clock(step: float):
    """A ``time.monotonic`` stand-in that jumps ``step`` seconds per call —
    models round-trips slow enough to eat the settle budget."""
    ticks = itertools.count(0.0, step)
    return lambda: next(ticks)


def test_settle_stops_at_the_wall_clock_deadline(store, monkeypatch, no_sleep) -> None:
    """Attempts alone do not bound the settle: 16 attempts of two un-timed HTTP
    calls each can outlast the exit monitor's 30 s in-flight drain. With every
    round-trip costing 3 s the loop must give up on the deadline, long before
    the attempt count, and still report the order as possibly resting."""
    import openpoly.execution.live_executor as le_mod

    step = 3.0
    monkeypatch.setattr(le_mod.time, "monotonic", _stepping_clock(step))
    expected = math.ceil(le_mod._SETTLE_DEADLINE_S / step)
    assert expected < _SETTLE_ATTEMPTS  # the deadline, not the count, bounds this run
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status={"size_matched": "0", "status": "LIVE"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert len(clob.cancelled) == expected
    settle_waits = _settle_waits(no_sleep)
    assert len(settle_waits) == expected - 1  # never a wait after the last attempt
    assert r.skip_reason == "live_cancel_failed"
    assert r.resting_order_id == "0x1"


def test_settle_keeps_the_largest_matched_size_seen_mid_loop(store) -> None:
    """A refused cancel whose read reported 20 of 30 matched, then an
    acknowledged cancel and a dead post-settle read: the 20 the venue already
    reported must survive the acknowledgement (a later cancel cannot un-fill
    it), so 20 is persisted — not the 12 of the POST response."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0x1",
            "status": "matched",
            "makingAmount": "6.0",
            "takingAmount": "12.0",
        },
        cancel_responses=[_refused(), _cancelled()],
        order_status=[
            {"size_matched": "20.0", "status": "LIVE"},
            RuntimeError("order lookup failed"),
        ],
        ctf_balance_sequence=[0, 0],  # the balance lags: it confirms nothing
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=30.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(20.0)
    assert r.resting_order_id is None  # the cancel was acknowledged
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(20.0)


def test_settle_falls_back_to_the_balance_after_a_stale_in_loop_read(store) -> None:
    """One successful in-loop read (0 matched) must not suppress the fallbacks:
    every later read goes dark and every cancel is refused, so the CTF balance
    is the only source left — and it says 10 filled."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status=[
            {"size_matched": "0", "status": "LIVE"},
            RuntimeError("order lookup failed"),
        ],
        ctf_balance_sequence=[0, 10_000_000],  # baseline 0; confirm sees 10 shares
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=10.0)
    r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(intent.price)
    assert r.resting_order_id == "0x1"  # no cancel was ever acknowledged
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(10.0)


def test_settle_exhausted_but_fresh_read_shows_a_full_fill_does_not_alert(store, caplog) -> None:
    """The budget ran out with the order still LIVE, but the post-loop read
    shows it fully matched: nothing rests, so no RESTING ORDER ALERT and no
    resting id — the alert belongs after that read, not before it."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status=[{"size_matched": "0", "status": "LIVE"}] * _SETTLE_ATTEMPTS
        + [{"size_matched": "10.0", "status": "MATCHED"}],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=10.0)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.resting_order_id is None
    assert len(clob.cancelled) == _SETTLE_ATTEMPTS
    assert "RESTING ORDER ALERT" not in caplog.text


def test_settle_keeps_retrying_a_partially_matched_order(store) -> None:
    """``MATCHED`` on a GET is reported for a partial match too: 12 of 30 with
    the cancel refused is an order still resting, so the loop must retry instead
    of walking away from the untouched 18."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused(), _cancelled()],
        order_status={"size_matched": "12.0", "status": "MATCHED"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=30.0), news_id="n", ts=1.0)
    assert clob.cancelled == ["0x1", "0x1"]  # the partial MATCHED did not end it
    assert r.filled is True
    assert r.qty == pytest.approx(12.0)
    assert r.resting_order_id is None
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(12.0)


def test_settle_alerts_when_a_partially_matched_order_cannot_be_cancelled(store, caplog) -> None:
    """Same partial ``MATCHED``, but no cancel is ever acknowledged: the 18 that
    never filled is still on the book, so the fill is persisted AND the order is
    named as resting with the alert — not silently abandoned."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status={"size_matched": "12.0", "status": "MATCHED"},
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(price=0.5, qty=30.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(12.0)
    assert r.resting_order_id == "0x1"
    assert len(clob.cancelled) == _SETTLE_ATTEMPTS
    assert "RESTING ORDER ALERT" in caplog.text
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(12.0)


# ---------- a malformed response must never cost a fill ----------
#
# Everything below is a venue answer the executor cannot take at face value.
# None of them may end with tokens on-chain and no ledger row, and none of them
# may raise out of execute_buy / execute_sell.


# A share count larger than the order is not a fill to book down — it is a
# number the venue cannot mean, so it buys no trust at all. Discarding it lets
# the settle establish the truth from the order read or the wallet; clamping it
# to the size would pin a full-size floor under the answer and open a phantom
# position against an empty wallet, which blocks re-entry and can never be sold.


def test_buy_over_size_report_is_discarded_not_booked(store) -> None:
    """Nothing else can establish a fill, so the outcome is unknown — never a
    full-size row."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "10000000",  # raw 1e6 units, not shares
            "makingAmount": "5000000",
        },
        order_status=[RuntimeError("dark")],
        ctf_balance_sequence=[0, RuntimeError("clob down")],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_fill_unknown"
    assert clob.cancelled == ["0x1"]  # settled despite the "full fill"
    assert store.get_open_position("m1", "yes") is None


def test_buy_over_size_report_books_what_the_order_read_establishes(store) -> None:
    """The read is the trustworthy source: 6 of 10, not the 1e7 the response
    claimed and not the 10 a clamp would have pinned."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "10000000",
            "makingAmount": "5000000",
        },
        cancel_responses=[_cancelled()],
        order_status=[{"size_matched": "6", "status": "LIVE"}],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=10.0)
    r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(6.0)
    assert r.price == pytest.approx(intent.price)  # no usable amounts → the limit
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(6.0)


def test_sell_over_size_report_is_discarded_not_booked(store) -> None:
    """SELL twin: shares are ``makingAmount`` here. Booking the discarded report
    would close a position whose tokens the wallet may still hold — leave it
    open for the next pass instead."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id))
    held = _held(store, m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "makingAmount": "10000000",  # raw 1e6 units, not shares
            "takingAmount": "5500000",
        },
        order_status=[RuntimeError("dark")],
        ctf_balance_sequence=[10_000_000, RuntimeError("clob down")],  # gate ok, then dark
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is False
    assert r.skip_reason == "live_fill_unknown"
    assert clob.cancelled == ["0x1"]  # settled despite the "full fill"
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"
    assert rec.qty == pytest.approx(10.0)


@pytest.mark.parametrize(
    "cancels",
    [[_cancelled()], [_refused(), _cancelled()]],
    ids=["post-loop-read", "in-loop-read"],
)
def test_buy_over_size_order_read_is_discarded_not_clamped(store, cancels) -> None:
    """The plausibility rule binds every source alike. A ``size_matched`` bigger
    than the order is the same nonsense as an over-size POST amount, so it earns
    the same treatment: discarded, not booked down. Clamping it to the size
    would pin a full-size floor under the answer — the phantom position against
    an empty wallet that the POST-side discard exists to prevent, arriving
    through the read instead."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=cancels,
        order_status=[{"size_matched": "999", "status": "LIVE"}],
        ctf_balance_sequence=[0, RuntimeError("clob down")],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_fill_unknown"
    assert store.get_open_position("m1", "yes") is None


# ``NaN`` and the infinities are floats, so they parse — and then defeat every
# comparison they touch. They are unusable amounts, not numbers to reason with.


def test_buy_non_finite_order_read_is_not_a_fill(store) -> None:
    """The clamp cannot bound what does not compare: ``min(nan, size)`` is
    ``nan``. A ``size_matched`` the venue cannot mean has to be an unreadable
    order, not a fill — otherwise it reaches the ledger through the very source
    the discard promotes to source of truth."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_cancelled("0x1")],
        order_status=[{"size_matched": "NaN", "status": "LIVE"}],
        ctf_balance_sequence=[0, 0],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_fill_unknown"
    assert store.get_open_position("m1", "yes") is None


@pytest.mark.parametrize(
    "taking",
    ["NaN", pytest.param(10**400, id="overflowing-int")],
)
def test_buy_unusable_share_count_is_never_a_clean_miss(store, taking) -> None:
    """Neither over-size nor ``<= 0``, so an unusable share count would walk past
    the discard and past the clean-miss guard alike and report a miss nothing had
    established. ``NaN`` parses; an int too large for a double raises an
    ``OverflowError`` a ``ValueError`` clause would miss."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": taking,
            "makingAmount": "5",
        },
        order_status=[RuntimeError("dark")],
        ctf_balance_sequence=[0, RuntimeError("clob down")],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_fill_unknown"
    assert store.get_open_position("m1", "yes") is None


@pytest.mark.parametrize("making", ["Infinity", "1e400"])  # literal, then an overflow
def test_buy_non_finite_usdc_amount_records_shares_at_limit_price(store, making) -> None:
    """A non-finite pUSD amount divides into an infinite entry price the ledger
    would then carry forever. Book it at the limit like every other unusable
    amount — the shares themselves filled and must still be recorded."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "10",
            "makingAmount": making,
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=10.0)
    r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.price == pytest.approx(intent.price)
    held = store.get_open_position("m1", "yes")
    assert held is not None
    assert math.isfinite(held.avg_entry_price)
    assert held.avg_entry_price == pytest.approx(intent.price)


# Every field of the POST body is read through one validated view, so a shape
# the venue never documented lands nowhere: not as an exception out of
# execute_buy, and not as a plausible-looking value in the ledger.


def test_buy_non_list_tx_hashes_does_not_raise(store) -> None:
    """``transactionsHashes`` as a bare int is not subscriptable."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "10",
            "makingAmount": "5",
            "transactionsHashes": 5,
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    fills = store.list_fills(limit=5)
    assert fills and fills[0].tx_hash is None


def test_buy_string_tx_hashes_is_not_indexed_into(store) -> None:
    """A bare string is indexable, so ``[0]`` would store "0" as the hash."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "10",
            "makingAmount": "5",
            "transactionsHashes": "0xCAFE",
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    fills = store.list_fills(limit=5)
    assert fills and fills[0].tx_hash is None


# A whole number is still an id we can send, so it is worth a cancel attempt.


def test_sell_non_list_tx_hashes_does_not_raise(store) -> None:
    """SELL twin: ``transactionsHashes`` as a bare int is not subscriptable."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "makingAmount": "10",
            "takingAmount": "5.5",
            "transactionsHashes": 5,
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="take_profit", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "closed"
    sell_fill = next(f for f in store.list_fills(limit=5) if f.action == "sell")
    assert sell_fill.tx_hash is None


def test_buy_numeric_order_id_is_still_cancelled(store) -> None:
    """``orderID`` as a JSON number is coerced, not discarded."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("live"),
            "orderID": 12345,
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert clob.cancelled == ["12345"]
    assert r.skip_reason == "live_no_match"
    assert store.get_open_position("m1", "yes") is None


def test_buy_numeric_order_id_is_named_as_resting_when_no_cancel_lands(store, caplog) -> None:
    """...and when no cancel is ever acknowledged it is the id the caller backs
    off on — dropping it would leave the caller nothing to name."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("live"),
            "orderID": 12345,
        },
        cancel_responses=[_refused("12345")],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.skip_reason == "live_cancel_failed"
    assert r.resting_order_id == "12345"
    assert len(clob.cancelled) == _SETTLE_ATTEMPTS
    assert "RESTING ORDER ALERT" in caplog.text


# A container, on the other hand, is a shape no payload can carry.
_UNSENDABLE_ORDER_ID = {"dict": {"a": 1}, "list": ["0x1"]}


@pytest.mark.parametrize("case", list(_UNSENDABLE_ORDER_ID))
def test_buy_non_scalar_order_id_is_treated_as_no_order_id(store, caplog, case) -> None:
    """An id we cannot cancel by is no id: the alert path, not an exception. The
    value still reaches the alert — the venue named this order, and dropping the
    name leaves nothing to look it up by."""
    m = _market("m1")
    _populate(m)
    raw = _UNSENDABLE_ORDER_ID[case]
    clob = _FakeClob(order_response={**_zero_fill_response("live"), "orderID": raw})
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_cancel_failed"
    assert r.resting_order_id is None  # there is no id to name
    assert clob.cancelled == []  # nothing to cancel by
    assert "RESTING ORDER ALERT" in caplog.text
    assert repr(raw) in caplog.text  # ...but the value the venue sent survives
    assert type(raw).__name__ in caplog.text
    assert store.get_open_position("m1", "yes") is None


# Every shape here is a 200 the executor must read as a refusal, with the
# operator-facing reason each one earns.
_BAD_CANCEL_BODY = {
    "not_canceled-is-a-list": (
        {**_refused(), "not_canceled": ["0x1"]},
        "unexpected cancel body shape",
    ),
    # ``in`` would substring-match; and with no ``not_canceled`` key at all.
    "canceled-is-a-string": ({"canceled": "0x1abc"}, "refused without a reason"),
    "no-not_canceled": ({"canceled": []}, "refused without a reason"),
}


@pytest.mark.parametrize("case", list(_BAD_CANCEL_BODY))
def test_buy_bad_cancel_body_is_a_refusal(store, caplog, case) -> None:
    m = _market("m1")
    _populate(m)
    body, expected_reason = _BAD_CANCEL_BODY[case]
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[body],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_cancel_failed"
    assert r.resting_order_id == "0x1"
    assert len(clob.cancelled) == _SETTLE_ATTEMPTS  # refused, so it kept retrying
    assert "RESTING ORDER ALERT" in caplog.text
    assert expected_reason in caplog.text


def test_buy_stale_zero_read_does_not_become_a_clean_miss(store) -> None:
    """An in-loop read of 0.0 is not evidence of no fill: it predates the
    cancel. With the fresh read and the balance both dark, the outcome is
    unknown, not a miss."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused(), _cancelled()],
        order_status=[{"size_matched": "0", "status": "LIVE"}, RuntimeError("dark")],
        ctf_balance_sequence=[0, RuntimeError("clob down")],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert r.skip_reason == "live_fill_unknown"
    assert store.get_open_position("m1", "yes") is None


def test_buy_balance_showing_a_full_fill_does_not_clear_the_resting_alert(store, caplog) -> None:
    """Every cancel refused and every read dark, and the wallet shows the whole
    size filled — but the CTF balance is wallet-wide and attributed to no order
    id, so it cannot prove THIS order stopped resting: an earlier order's
    remainder filling in the same poll window looks identical. The fill is
    recorded (it happened on-chain) and the order is still named as resting."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("delayed"),
        cancel_responses=[_refused()],
        order_status=[RuntimeError("dark")],
        ctf_balance_sequence=[0, 10_000_000],  # baseline, then +10 shares
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    assert r.resting_order_id == "0x1"
    assert "RESTING ORDER ALERT" in caplog.text


# ---------- a price the venue cannot mean is never the basis ----------


@pytest.mark.parametrize(
    "making",
    ["5000000", "5.0000001"],  # raw 1e6 units; and a hair over 1.0 per share
    ids=["wrong-scale", "just-over-one"],
)
def test_buy_out_of_domain_price_falls_back_to_the_limit(store, making) -> None:
    """A wrong-scale money field divides into a price the venue cannot mean, and
    nothing downstream catches it: the store validates nothing and the exit
    section rejects only a non-positive basis."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "5",
            "makingAmount": making,
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    intent = _intent(price=0.5, qty=5.0)
    r = le.execute_buy(intent, news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(5.0)
    assert r.price == pytest.approx(intent.price)
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.avg_entry_price == pytest.approx(intent.price)


def test_sell_out_of_domain_price_falls_back_to_the_bid(store) -> None:
    """SELL twin: shares are ``makingAmount``, pUSD ``takingAmount``."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "makingAmount": "10",
            "takingAmount": "5500000",  # raw 1e6 units, not pUSD
        }
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="take_profit", ts=200.0)
    assert r.filled is True
    assert r.price == pytest.approx(0.55)
    sell_fill = next(f for f in store.list_fills(limit=5) if f.action == "sell")
    assert sell_fill.price == pytest.approx(0.55)


# Both entry points now refuse to sign against an out-of-band book price, so
# neither of these paths can reach ``_bookable_price``'s limit fallback any
# more: what used to be "book the conservative edge" is "do not trade on it".
# The edge itself stays pinned by the unit tests above, as defence in depth for
# any future caller.


# ---------- the settle reads what the venue actually said ----------


def test_buy_numeric_order_id_acknowledgement_is_recognised(store, caplog) -> None:
    """A venue that names an order numerically echoes it numerically in the
    cancel body too, and ``"12345" in [12345]`` is False."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={**_zero_fill_response("live"), "orderID": 12345},
        cancel_responses=[{"canceled": [12345], "not_canceled": {}}],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(), news_id="n", ts=1.0)
    assert len(clob.cancelled) == 1  # acknowledged at once, no retry budget spent
    assert r.skip_reason == "live_no_match"
    assert r.resting_order_id is None
    assert "RESTING ORDER ALERT" not in caplog.text


def test_buy_terminal_status_without_size_ends_the_loop_at_once(store) -> None:
    """A cancelled order is cancelled whether or not the answer carried a size:
    discarding the whole read over a missing size spends the entire retry
    budget on an order that provably stopped resting after one attempt."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status=[{"status": "CANCELED"}],  # terminal, but no size_matched
        ctf_balance_sequence=[0, 0],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert len(clob.cancelled) == 1  # the status settled it; no budget spent
    assert r.resting_order_id is None  # nothing rests after a cancelled status
    assert r.skip_reason == "live_fill_unknown"  # the size was never established


# ---------- the settle's evidence model ----------
#
# The model itself (strengths, plausibility, resolution) is documented in
# openpoly/execution/live_executor.py; each test below names the rule it pins.


def test_settle_exact_count_beats_a_larger_lower_bound(store, caplog) -> None:
    """The response claimed 5 crossed; the order itself, cancelled, says 2. The
    venue's account of THIS order is the answer — a lower bound never floors it
    — and the disagreement is reported rather than settled in our favour."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "5",
            "makingAmount": "2.5",
        },
        cancel_responses=[_refused()],
        order_status=[{"size_matched": "2", "status": "CANCELED"}],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(2.0)
    assert "disagrees" in caplog.text
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(2.0)


def test_settle_exact_zero_is_a_clean_miss_and_the_wallet_is_not_asked(store) -> None:
    """The order is cancelled having matched nothing. That is the venue's own
    count, so it stands: a wallet that rose in the same window rose for some
    other reason, and attributing it here would book a position this order did
    not open."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status=[{"size_matched": "0", "status": "CANCELED"}],
        ctf_balance_sequence=[0, 7_000_000],  # would say 7 if it were consulted
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "live_no_match"
    assert r.resting_order_id is None
    assert store.get_open_position("m1", "yes") is None


def test_settle_exact_count_never_falls_below_an_earlier_reading_of_the_order(
    store, caplog
) -> None:
    """``size_matched`` counts what one order matched, so it only ever grows. A
    later reading below an earlier one is the data API lagging behind the
    matching engine, not the venue taking a fill back — so the cancelled read's
    nought cannot erase the 4 shares an earlier read of the SAME order already
    reported."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused(), _cancelled()],
        order_status=[
            {"size_matched": "4", "status": "LIVE"},
            {"size_matched": "0", "status": "CANCELED"},
        ],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(4.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(4.0)


@pytest.mark.parametrize(
    ("moved", "expected"),
    [
        pytest.param(8_000_000, 8.0, id="inside-the-order"),
        pytest.param(15_000_000, 10.0, id="over-size-books-the-order"),
    ],
)
def test_settle_books_an_over_size_wallet_delta_down_to_the_order(store, moved, expected) -> None:
    """The wallet is the one source booked down rather than discarded, and the
    settle must say so as loudly as the lost-response path does: an order for
    exactly this size was sent, so a bigger delta still means at least this size
    was ours. Discarding it drops a real fill — and would make a LARGER delta
    book LESS than a smaller one, which is the shape of a bug, not a rule."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status=[RuntimeError("dark")],
        ctf_balance_sequence=[0, moved],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(expected)
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(expected)


def test_sell_settle_books_an_over_size_wallet_drop_down_to_the_order(store) -> None:
    """SELL twin: the wallet held more of the token than this order sold, so the
    drop measured across the poll window is larger than the order. Discarding it
    would leave the row open with the tokens already gone."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id))
    held = _held(store, m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_refused()],
        order_status=[RuntimeError("dark")],
        # Gate syncs at 25 shares held, then the window shows a 15-share drop.
        ctf_balance_sequence=[25_000_000, 10_000_000],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "closed"


def test_settle_keeps_a_reported_cross_when_nothing_else_can_speak(store) -> None:
    """The POST body reported 3 of 10 crossing and then every later source went
    dark. The cross is a lower bound, not a count — but it is the venue's own
    word that 3 shares moved on-chain, so it is booked. Reporting the outcome
    unknown here would leave a real fill with no ledger row, which is the one
    thing this path must never do."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "3",
            "makingAmount": "1.5",
        },
        order_status=[RuntimeError("dark")],
        ctf_balance_sequence=[0, RuntimeError("clob down")],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(3.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(3.0)


# ---------- a price outside the venue's band is booked conservatively ----------
#
# Clamping to the nearest edge is not neutral: for a SELL the ceiling is the
# most favourable price there is, so a wrong-scale book would fabricate realized
# profit — and fabricated profit resets the entry kill switch's consecutive-loss
# walk. Each side is booked at ITS worst edge instead.


@pytest.mark.parametrize(
    ("price", "fallback"),
    [(None, 550_000.0), (float("nan"), float("nan"))],
    ids=["wrong-scale-limit", "non-finite"],
)
@pytest.mark.parametrize(
    ("side", "expected"),
    [("buy", MAX_TOKEN_PRICE), ("sell", MIN_TOKEN_PRICE)],
)
def test_bookable_price_falls_to_the_conservative_edge(price, fallback, side, expected) -> None:
    """Neither the quotient nor the limit is a price the venue could produce —
    including when neither is even a number, which the band rejects too."""
    booked = _bookable_price(price, fallback=fallback, side=side, order_id="0x1")
    assert booked == pytest.approx(expected)
    assert math.isfinite(booked)


def test_buy_lost_response_over_size_balance_delta_books_the_order(store, caplog) -> None:
    """R1: on the lost-response path an order for exactly ``size`` was sent, so a
    wallet delta ABOVE the size still means at least the whole order is ours —
    an earlier order's remainder settling in the same window explains the
    excess. Discarding it leaves tokens no ledger row manages, which is the
    worse error, so this one source is booked down to the size and the excess is
    reported."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        exception=RuntimeError("request exception"),
        ctf_balance_sequence=[0, 999_000_000],  # +999 shares for an order of 10
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(10.0)
    assert "999" in caplog.text  # the excess is not swallowed


def test_sell_lost_response_over_size_balance_delta_books_the_order(store) -> None:
    """R1, sell side: discarding here is worse still — the row stays open with
    the tokens already gone, so every later tick fails the balance gate until
    reconciliation closes it at a fabricated zero P&L."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=0.55))
    held = _held(store, m, qty=10.0)
    clob = _FakeClob(
        exception=RuntimeError("request exception"),
        ctf_balance_sequence=[999_000_000, 0],  # gate syncs, then -999 shares
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is True
    assert r.qty == pytest.approx(10.0)
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "closed"


def test_settle_keeps_a_settled_cross_over_a_lagging_exact_zero(store) -> None:
    """R2: a transaction hash is the venue's own proof that a match settled. When
    only the status propagates to the data API, its ``size_matched: 0`` must not
    erase a cross the response already reported and stamped with a hash."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            "success": True,
            "orderID": "0x1",
            "status": "matched",
            "takingAmount": "5",
            "makingAmount": "2.5",
            "transactionsHashes": ["0xabc"],
        },
        cancel_responses=[_cancelled()],
        order_status=[{"size_matched": "0", "status": "CANCELED"}],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(5.0)
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(5.0)


def test_settle_post_ack_zero_is_not_evidence_so_the_balance_speaks(store) -> None:
    """R3: the data API lags the matching engine, so a ``size_matched: 0`` read
    moments after an acknowledged cancel proves nothing — exactly as the same
    zero read inside the loop proves nothing. The wallet gets its turn."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live"),
        cancel_responses=[_cancelled()],
        order_status=[{"size_matched": "0", "status": "LIVE"}],
        ctf_balance_sequence=[0, 5_000_000],  # 5 shares crossed before the ack
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(5.0)


def test_read_order_blank_size_matched_is_unknown_not_a_counted_zero(store) -> None:
    """R4: an order document with a blank count is not an order document. The
    parser maps an empty string to a clean zero, which is right for a POST body
    and wrong here — it would be the venue counting nought."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "3",
            "makingAmount": "1.5",
        },
        cancel_responses=[_cancelled()],
        order_status=[{"size_matched": "", "status": "CANCELED"}],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(3.0)  # the cross survives; the blank counts nothing


def test_settle_without_an_order_id_still_asks_the_balance(store, caplog) -> None:
    """R5: with no id to cancel by, the CTF balance is the ONLY remaining signal
    — and it was the one path that never asked it. The alert stays either way:
    nothing can be cancelled, so the remainder may still rest."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response=_zero_fill_response("live", order_id=None),
        ctf_balance_sequence=[0, 6_000_000],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    with caplog.at_level("ERROR", logger="openpoly.execution.live_executor"):
        r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(6.0)
    assert "RESTING ORDER ALERT" in caplog.text
    held = store.get_open_position("m1", "yes")
    assert held is not None and held.qty == pytest.approx(6.0)


def test_sell_out_of_band_bid_is_never_posted(store) -> None:
    """C3: the order is signed at the level-1 bid. If that bid is not a price the
    venue could produce, the book is not trustworthy and the trade must not be
    made on it — refusing after signing only declines to RECORD a price we
    already traded at."""
    m = _market("m1")
    _populate(m, _book(m.yes_token_id, bid=550_000.0))
    held = _held(store, m, qty=10.0)
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_sell(held, close_reason="stop_loss", ts=200.0)
    assert r.filled is False
    assert r.skip_reason == "price_out_of_band"
    assert clob.posted == []  # nothing was signed
    rec = store.get_position(held.position_id)
    assert rec is not None and rec.status == "open"


def test_buy_out_of_band_price_is_never_posted(store) -> None:
    """C3, buy side: the intent's price is the level-1 ask the entry section
    read, and that section guards only against a non-positive one."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob()
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=550_000.0, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is False
    assert r.skip_reason == "price_out_of_band"
    assert clob.posted == []


def test_buy_partial_fill_that_may_rest_is_flagged_on_the_fill_path(store) -> None:
    """C2: a partial fill whose remainder could not be cancelled is still an
    order the venue may hold. That has to reach the caller through the FILL
    path, not only through the skip path, or the next tick posts a second order
    on top of the first."""
    m = _market("m1")
    _populate(m)
    clob = _FakeClob(
        order_response={
            **_zero_fill_response("matched"),
            "takingAmount": "4",
            "makingAmount": "2",
        },
        cancel_responses=[_refused()],
        order_status=[{"size_matched": "4", "status": "LIVE"}],
    )
    le = LiveExecutor(portfolio=store, clob_client=clob)
    r = le.execute_buy(_intent(price=0.5, qty=10.0), news_id="n", ts=1.0)
    assert r.filled is True
    assert r.qty == pytest.approx(4.0)
    assert r.resting_order_id == "0x1"

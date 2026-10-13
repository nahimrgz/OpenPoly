"""Entry-side freshness and v1 side-choice fixes.

* ``ensure_fresh`` judges each token's age from its own fetch against the
  decision time: one slow sibling refresh no longer ages a side that was
  refreshed seconds earlier into ``stale_book``.
* Entry v1 leaves a stale side out of its side choice (``FreshBooks``), so the
  stale side cannot win on its cached price and veto the decision.
* v1 reports each side's real skip reason, not a blanket "no order book".
* A dust remainder does not block the opposite side.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time as _time

from openpoly.markets.manager import MarketSourceManager
from openpoly.markets.models import OrderBook
from openpoly.markets.store import book_clock
from openpoly.sections._base import SectionInput
from openpoly.sections.entry.edge_threshold_v1 import (
    EdgeThresholdConfigV1,
    EdgeThresholdEntryV1,
    FreshBooks,
    OrderIntent,
)
from tests.test_section_entry_edge import (  # noqa: F401 — autouse fixture
    _book,
    _FakePortfolio,
    _isolate_market_store,
    _market,
    _rec,
)
from tests.test_section_entry_edge import _populate as _populate_market
from tests.test_section_entry_edge_v1 import _ar


def _run(inst: EdgeThresholdEntryV1, payload: object):
    return inst.run(SectionInput(tick_type="event", payload=payload))


# ---------- ensure_fresh ----------


async def test_ages_are_honest_after_a_slow_sibling() -> None:
    """Ages are measured when ensure_fresh returns: a side refreshed at t=0
    really is ~0.3s old once the slow sibling finishes, so it is reported as
    such (the caller excludes it as stale instead of trading an old book)."""

    async def fetch(token_id: str) -> OrderBook:
        if token_id == "slow":
            await asyncio.sleep(0.3)
        return OrderBook(token_id=token_id, ts=1.0, bids=[(0.4, 10)], asks=[(0.41, 10)])

    mgr = MarketSourceManager(book_fetcher=fetch)
    ages = await mgr.ensure_fresh(["fast", "slow"], 0.2)
    assert ages["fast"] is not None and ages["fast"] >= 0.25
    assert ages["slow"] is not None and ages["slow"] <= 0.2


async def test_a_failed_refresh_still_reads_stale() -> None:
    async def fetch(token_id: str) -> OrderBook:
        raise RuntimeError("venue down")

    mgr = MarketSourceManager(book_fetcher=fetch)
    mgr.store.put_order_book(
        OrderBook(token_id="t", ts=1.0, bids=[], asks=[]), fetched_at=book_clock() - 30.0
    )
    ages = await mgr.ensure_fresh(["t", "missing"], 5.0)
    assert ages["t"] is not None and ages["t"] >= 30.0
    assert ages["missing"] is None


# ---------- v1 side choice with stale sides ----------


def test_stale_side_is_excluded_and_the_fresh_side_still_trades() -> None:
    """YES has the larger edge on its cached (stale) book; NO is fresh. v1
    used to pick YES, which the orchestrator then refused — losing NO too."""
    _populate_market(_market(), _book("yes-m1", 0.39, 0.40), _book("no-m1", 0.39, 0.40))
    inst = EdgeThresholdEntryV1(EdgeThresholdConfigV1())
    # p 0.70: YES edge 0.30, NO edge -0.10 → YES would win on price.
    out = _run(inst, FreshBooks(_ar(0.70), frozenset({"no-m1"})))
    assert out.verdict == "skip"  # NO's own edge fails min_edge …
    assert out.signals.get("side") == "no"  # … but it was the side considered
    # With a NO edge that passes, the fresh side is traded.
    out = _run(inst, FreshBooks(_ar(0.30), frozenset({"no-m1"})))
    assert out.verdict == "ok"
    assert isinstance(out.payload, OrderIntent) and out.payload.side == "no"


def test_plain_payload_keeps_considering_both_sides() -> None:
    _populate_market(_market(), _book("yes-m1", 0.39, 0.40), _book("no-m1", 0.39, 0.40))
    inst = EdgeThresholdEntryV1(EdgeThresholdConfigV1())
    out = _run(inst, _ar(0.70))
    assert isinstance(out.payload, OrderIntent) and out.payload.side == "yes"
    # The fresh-token set does not leak into the next decision.
    out = _run(inst, FreshBooks(_ar(0.70), frozenset({"no-m1"})))
    out = _run(inst, _ar(0.70))
    assert isinstance(out.payload, OrderIntent) and out.payload.side == "yes"


def test_skip_reason_names_each_sides_real_reason() -> None:
    """Regression: every per-side failure collapsed to "no order book"."""
    dust_bids = OrderBook(token_id="no-m1", ts=1.0, bids=[(0.20, 1.0)], asks=[(0.25, 50.0)])
    _populate_market(_market(), dust_bids)
    inst = EdgeThresholdEntryV1(EdgeThresholdConfigV1())
    out = _run(inst, _ar(0.40))
    assert out.verdict == "skip"
    assert out.reason == "yes: no order book; no: no executable bid"
    out = _run(inst, FreshBooks(_ar(0.40), frozenset()))
    assert out.reason == "stale_book"


# ---------- opposite-side gate ignores dust ----------


def test_dust_remainder_does_not_block_the_opposite_side() -> None:
    _populate_market(_market(), _book("yes-m1", 0.49, 0.50))
    dust = dataclasses.replace(_rec("m1", "no", opened_at=_time.time() - 7200), qty=0.4)
    portfolio = _FakePortfolio([dust])
    inst = EdgeThresholdEntryV1(EdgeThresholdConfigV1(), portfolio_provider=lambda: portfolio)
    assert _run(inst, _ar(0.70)).verdict == "ok"

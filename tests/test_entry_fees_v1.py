"""Entry v1 prices the taker fee into the edge.

Polymarket charges takers ``shares × rate × p × (1 − p)`` USDC
(docs.polymarket.com/trading/fees), so one share bought at ``ask`` costs
``ask + rate × ask × (1 − ask)``. v1 ranks the sides and gates ``min_edge`` on
the edge net of that fee; the shadow pick mirrors the same side choice.
"""

from __future__ import annotations

import pytest

from openpoly.analytics.shadow import choose_shadow_side
from openpoly.markets.models import normalize_gamma_market
from openpoly.markets.pricing import taker_fee_per_share
from openpoly.sections._base import SectionInput
from openpoly.sections.entry.edge_threshold_v1 import (
    EdgeThresholdConfigV1,
    EdgeThresholdEntryV1,
    OrderIntent,
)
from tests.test_section_entry_edge import (  # noqa: F401 — autouse fixture
    _book,
    _isolate_market_store,
)
from tests.test_section_entry_edge import _populate as _populate_market
from tests.test_section_entry_edge_v1 import _ar


def _fee_market(rate: float | None):
    raw: dict[str, object] = {
        "id": "m1",
        "conditionId": "0xm1",
        "question": "Will X happen?",
        "clobTokenIds": '["yes-m1", "no-m1"]',
    }
    if rate is not None:
        raw["feesEnabled"] = rate > 0
        raw["feeSchedule"] = {"rate": rate, "exponent": 1, "takerOnly": True}
    m = normalize_gamma_market(raw, event={"id": "e1", "title": "E", "tags": []})
    assert m is not None
    assert m.taker_fee_rate == rate
    return m


def _run(inst: EdgeThresholdEntryV1, payload: object):
    return inst.run(SectionInput(tick_type="event", payload=payload))


def test_fee_per_share_follows_the_venue_formula() -> None:
    # Docs example: 100 crypto shares at 0.50 pay $1.75.
    assert taker_fee_per_share(0.07, 0.50) * 100 == pytest.approx(1.75)
    assert taker_fee_per_share(0.04, 0.30) == pytest.approx(taker_fee_per_share(0.04, 0.70))
    assert taker_fee_per_share(0.0, 0.42) == 0.0


def test_edge_is_net_of_the_taker_fee() -> None:
    _populate_market(_fee_market(0.04), _book("yes-m1", 0.59, 0.60), _book("no-m1", 0.39, 0.40))
    out = _run(EdgeThresholdEntryV1(EdgeThresholdConfigV1()), _ar(0.70))
    net = 0.70 - 0.60 - 0.04 * 0.60 * 0.40
    assert out.verdict == "ok"
    assert isinstance(out.payload, OrderIntent)
    assert out.payload.side == "yes"
    assert out.payload.edge == pytest.approx(net)
    assert out.signals["edge"] == round(net, 4)
    assert out.signals["taker_fee_rate"] == 0.04


def test_fee_pushes_a_thin_edge_below_min_edge() -> None:
    # Gross 0.055 clears min_edge 0.05; net of the 0.0096 fee it does not.
    _populate_market(_fee_market(0.04), _book("yes-m1", 0.59, 0.60), _book("no-m1", 0.39, 0.40))
    out = _run(EdgeThresholdEntryV1(EdgeThresholdConfigV1()), _ar(0.655))
    assert out.verdict == "skip"
    assert out.reason == "edge below min_edge"


def test_fee_can_change_the_chosen_side() -> None:
    # Gross edges are both 0.10; the NO fee is lower (0.04 × 0.30 × 0.70 <
    # 0.04 × 0.50 × 0.50), so NO has the larger net edge.
    _populate_market(_fee_market(0.04), _book("yes-m1", 0.49, 0.50), _book("no-m1", 0.29, 0.30))
    out = _run(EdgeThresholdEntryV1(EdgeThresholdConfigV1()), _ar(0.60))
    assert isinstance(out.payload, OrderIntent)
    assert out.payload.side == "no"


def test_unknown_fee_rate_is_not_traded() -> None:
    _populate_market(_fee_market(None), _book("yes-m1", 0.59, 0.60), _book("no-m1", 0.39, 0.40))
    out = _run(EdgeThresholdEntryV1(EdgeThresholdConfigV1()), _ar(0.80))
    assert out.verdict == "skip"
    assert out.reason == "unknown taker fee"


def test_zero_fee_market_keeps_the_gross_edge() -> None:
    _populate_market(_fee_market(0.0), _book("yes-m1", 0.59, 0.60), _book("no-m1", 0.39, 0.40))
    out = _run(EdgeThresholdEntryV1(EdgeThresholdConfigV1()), _ar(0.70))
    assert isinstance(out.payload, OrderIntent)
    assert out.payload.edge == pytest.approx(0.10)


def test_shadow_side_uses_the_net_edge() -> None:
    yes = ("yes-m1", _book("yes-m1", 0.49, 0.50))
    no = ("no-m1", _book("no-m1", 0.29, 0.30))
    quote = choose_shadow_side(0.60, yes=yes, no=no, fee_rate=0.04)
    assert quote.side == "no"
    assert quote.edge == pytest.approx(0.40 - 0.30 - 0.04 * 0.30 * 0.70)

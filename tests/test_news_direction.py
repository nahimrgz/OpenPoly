"""The analyzer says which way the news pushes YES; entry can refuse the rest.

The analyzer never sees the market price, so ``p_yes`` is an absolute level.
Entry compares that level with the price, which can buy NO on news that made
YES more likely: the side then reflects the model's prior against the market,
not the news. ``news_direction`` keeps the direction, and the entry gate
``require_news_direction`` only trades on the side the news pushes.
"""

from __future__ import annotations

from typing import Any

import pytest

from openpoly.sections._base import SectionInput
from openpoly.sections.analyzer.llm_v0 import AnalysisResult, LLMAnalyzerConfig, LLMAnalyzerV0
from openpoly.sections.analyzer.llm_v1 import SUBMIT_ANALYSIS_TOOL_V1
from openpoly.sections.entry.edge_threshold_v1 import (
    EdgeThresholdConfigV1,
    EdgeThresholdEntryV1,
    OrderIntent,
)
from tests.test_section_analyzer_llm import FakeLLMClient, _candidates
from tests.test_section_analyzer_llm_v1 import FakeLLMClientV1, _result
from tests.test_section_analyzer_llm_v1 import _run as _analyze
from tests.test_section_entry_edge import (  # noqa: F401 — autouse fixture
    _book,
    _isolate_market_store,
    _market,
)
from tests.test_section_entry_edge import _populate as _populate_market


def _ar(p_model: float, direction: Any = None) -> AnalysisResult:
    return AnalysisResult(
        market_id="m1", p_model=p_model, confidence="medium", news_direction=direction
    )


def _enter(res: AnalysisResult, **config: Any):
    inst = EdgeThresholdEntryV1(EdgeThresholdConfigV1(**config))
    return inst.run(SectionInput(tick_type="event", payload=res))


def _books_favoring_no() -> None:
    # p_model 0.15 at YES 0.68 / NO 0.31: the edge is on NO.
    _populate_market(_market(), _book("yes-m1", 0.67, 0.68), _book("no-m1", 0.30, 0.31))


def _books_favoring_yes() -> None:
    # p_model 0.70 at YES 0.50 / NO 0.49: the edge is on YES.
    _populate_market(_market(), _book("yes-m1", 0.49, 0.50), _book("no-m1", 0.48, 0.49))


# ---------- analyzer ----------


def test_news_direction_is_a_required_field_decided_before_p_yes() -> None:
    schema: dict[str, Any] = SUBMIT_ANALYSIS_TOOL_V1["input_schema"]  # type: ignore[assignment]
    props = list(schema["properties"])
    assert props.index("news_direction") < props.index("p_yes")
    assert "news_direction" in schema["required"]
    assert schema["properties"]["news_direction"]["enum"] == ["raises_yes", "lowers_yes", "unclear"]


def test_prompt_explains_news_direction() -> None:
    fake = FakeLLMClientV1(result=_result(news_direction="raises_yes"))
    _analyze(fake, _candidates("m1"))
    assert "news_direction" in (fake.last_system or "")


@pytest.mark.parametrize("direction", ["raises_yes", "lowers_yes", "unclear"])
def test_analysis_carries_the_direction(direction: str) -> None:
    fake = FakeLLMClientV1(result=_result(news_direction=direction))
    out = _analyze(fake, _candidates("m1"))
    assert out.verdict == "ok"
    assert isinstance(out.payload, AnalysisResult)
    assert out.payload.news_direction == direction
    assert out.signals["news_direction"] == direction


@pytest.mark.parametrize("direction", ["sideways", 1, None])
def test_invalid_direction_is_not_kept(direction: Any) -> None:
    fake = FakeLLMClientV1(result=_result(news_direction=direction))
    out = _analyze(fake, _candidates("m1"))
    assert out.verdict == "ok"
    assert isinstance(out.payload, AnalysisResult)
    assert out.payload.news_direction is None


def test_analyzer_v0_has_no_direction() -> None:
    fake = FakeLLMClient(
        result={"selected_index": 1, "p_yes": 0.7, "confidence": "medium", "rationale": "r"}
    )
    out = LLMAnalyzerV0(LLMAnalyzerConfig(), llm_client=fake).run(  # type: ignore[arg-type]
        SectionInput(tick_type="event", payload=_candidates("m1"))
    )
    assert isinstance(out.payload, AnalysisResult)
    assert out.payload.news_direction is None


# ---------- entry ----------


def test_gate_is_off_by_default_and_keeps_the_edge_side() -> None:
    _books_favoring_no()
    out = _enter(_ar(0.15, "raises_yes"))
    assert out.verdict == "ok"
    assert isinstance(out.payload, OrderIntent)
    assert out.payload.side == "no"


def test_gate_refuses_buying_no_on_news_that_raises_yes() -> None:
    _books_favoring_no()
    out = _enter(_ar(0.15, "raises_yes"), require_news_direction=True)
    assert out.verdict == "skip"
    assert out.reason == "against news direction"
    assert out.payload is None
    assert out.signals["side"] == "no"
    assert out.signals["news_direction"] == "raises_yes"


def test_gate_refuses_buying_yes_on_news_that_lowers_yes() -> None:
    _books_favoring_yes()
    out = _enter(_ar(0.70, "lowers_yes"), require_news_direction=True)
    assert out.verdict == "skip"
    assert out.reason == "against news direction"


def test_gate_trades_when_the_edge_agrees_with_the_news() -> None:
    _books_favoring_yes()
    yes = _enter(_ar(0.70, "raises_yes"), require_news_direction=True)
    assert isinstance(yes.payload, OrderIntent)
    assert yes.payload.side == "yes"

    _books_favoring_no()
    no = _enter(_ar(0.15, "lowers_yes"), require_news_direction=True)
    assert isinstance(no.payload, OrderIntent)
    assert no.payload.side == "no"


@pytest.mark.parametrize("direction", ["unclear", None])
def test_gate_refuses_a_direction_it_cannot_read(direction: Any) -> None:
    _books_favoring_yes()
    out = _enter(_ar(0.70, direction), require_news_direction=True)
    assert out.verdict == "skip"
    assert out.reason == "unclear news direction"

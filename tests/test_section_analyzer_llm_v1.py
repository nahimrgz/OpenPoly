"""Tests for LLMAnalyzerV1 — the self-check gets a field to be written in.

Reuses the fakes from the v0 analyzer tests; v0's result validation (index,
confidence, p_yes) is unchanged and covered there.
"""

from __future__ import annotations

from typing import Any

from openpoly.llm import LLMError
from openpoly.sections._base import SectionInput
from openpoly.sections._registry import scan
from openpoly.sections.analyzer.llm_v1 import (
    SUBMIT_ANALYSIS_TOOL_V1,
    AnalysisResult,
    LLMAnalyzerConfig,
    LLMAnalyzerV1,
)
from tests.test_section_analyzer_llm import FakeLLMClient, _candidates


class FakeLLMClientV1(FakeLLMClient):
    """v0's fake plus the ``max_tokens`` keyword v1 passes."""

    last_max_tokens: int | None = None

    def analyze(  # type: ignore[override]
        self, *, system: str, user: str, tool: dict[str, Any], max_tokens: int = 1024
    ) -> dict[str, Any]:
        self.last_max_tokens = max_tokens
        return super().analyze(system=system, user=user, tool=tool)


def _result(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "reasoning": "Q1: new. Q2: not resolved. Q3: cites the filing.",
        "selected_index": 1,
        "p_yes": 0.7,
        "confidence": "medium",
        "rationale": "Filing confirms it.",
    }
    base.update(over)
    return base


def _run(fake: FakeLLMClient, payload: object, config: LLMAnalyzerConfig | None = None):
    return LLMAnalyzerV1(config or LLMAnalyzerConfig(), llm_client=fake).run(  # type: ignore[arg-type]
        SectionInput(tick_type="event", payload=payload)
    )


def test_v1_in_default_catalog_next_to_v0() -> None:
    names = {e.name: e for e in scan() if e.type == "analyzer"}
    assert "LLMAnalyzerV0" in names
    assert "LLMAnalyzerV1" in names
    assert names["LLMAnalyzerV1"].version == "1.1.0"


def test_reasoning_is_the_first_required_tool_property() -> None:
    schema: dict[str, Any] = SUBMIT_ANALYSIS_TOOL_V1["input_schema"]  # type: ignore[assignment]
    assert list(schema["properties"])[0] == "reasoning"
    assert schema["properties"]["reasoning"]["type"] == "string"
    assert "reasoning" in schema["required"]


def test_prompt_puts_the_self_check_inside_reasoning() -> None:
    fake = FakeLLMClientV1(result=_result())
    _run(fake, _candidates("m1"))
    assert fake.last_tool is SUBMIT_ANALYSIS_TOOL_V1
    system = fake.last_system or ""
    for marker in ("reasoning", "Q1", "Q2", "Q3"):
        assert marker in system
    # The response cap is raised for the longer tool input.
    assert fake.last_max_tokens is not None and fake.last_max_tokens > 1024


def test_ok_carries_reasoning_as_a_signal() -> None:
    fake = FakeLLMClientV1(result=_result())
    out = _run(fake, _candidates("m1", "m2"))
    assert out.verdict == "ok"
    assert isinstance(out.payload, AnalysisResult)
    assert out.payload.market_id == "m1"
    assert out.payload.p_model == 0.7
    assert out.payload.rationale == "Filing confirms it."
    assert out.signals["reasoning"].startswith("Q1: new.")


def test_abstain_still_carries_reasoning() -> None:
    fake = FakeLLMClientV1(result=_result(selected_index=0, reasoning="Q1: rewrite."))
    out = _run(fake, _candidates("m1"))
    assert out.verdict == "skip"
    assert out.signals["reasoning"] == "Q1: rewrite."


def test_missing_reasoning_is_tolerated() -> None:
    """The schema requires it, but a gateway may not enforce that — a missing
    field yields an empty signal, not a failed analysis."""
    result = _result()
    del result["reasoning"]
    out = _run(FakeLLMClientV1(result=result), _candidates("m1"))
    assert out.verdict == "ok"
    assert out.signals["reasoning"] == ""


def test_below_min_confidence_skips() -> None:
    fake = FakeLLMClientV1(result=_result(confidence="low"))
    out = _run(fake, _candidates("m1"))
    assert out.verdict == "skip"
    assert out.reason == "below min_confidence"


def test_llm_error_is_an_error_verdict() -> None:
    fake = FakeLLMClientV1(raise_exc=LLMError("boom"))
    out = _run(fake, _candidates("m1"))
    assert out.verdict == "error"

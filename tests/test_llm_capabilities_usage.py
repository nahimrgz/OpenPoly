"""LLMClient model capabilities, structural failures and token usage.

* ``temperature`` only for an allowlist of legacy models; newer ones 400 on it.
* Forced ``tool_choice`` only where it is accepted; elsewhere ``auto`` plus an
  instruction, with the structural retry as the safety net.
* ``output_config.effort`` for models that support it (Haiku 5.5 & co).
* Thinking blocks are skipped; a ``max_tokens`` cut-off missing a required
  field is a structural failure (retried with a higher cap), never a partial
  result. A complete call missing a required field is returned as-is.
* Usage is summed across every attempt, with an estimated cost.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openpoly.llm import LLMClient, LLMError, LLMUsage, model_capabilities, model_price
from openpoly.llm.client import _MAX_TOKENS_CEILING
from openpoly.sections._base import SectionInput
from openpoly.sections.analyzer.llm_v1 import (
    SUBMIT_ANALYSIS_TOOL_V1,
    LLMAnalyzerConfigV1,
    LLMAnalyzerV1,
)
from tests.test_llm_client import _Block, _Messages, _patch
from tests.test_section_analyzer_llm import _candidates

_REQUIRED_TOOL: dict[str, Any] = {
    "name": "submit_analysis",
    "description": "d",
    "input_schema": {
        "type": "object",
        "properties": {"reasoning": {"type": "string"}, "p_yes": {"type": "number"}},
        "required": ["reasoning", "p_yes"],
    },
}


class _UsageResponse:
    def __init__(
        self,
        content: list[_Block],
        stop_reason: str = "tool_use",
        *,
        inp: int = 100,
        out: int = 50,
        cache_read: int | None = None,
        cache_write: int | None = None,
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )


def _ok(payload: dict[str, Any], **kw: Any) -> _UsageResponse:
    return _UsageResponse([_Block("tool_use", name="submit_analysis", tool_input=payload)], **kw)


def _client(model: str, effort: str | None = None) -> LLMClient:
    return LLMClient(
        api_key_ref="env:OPENPOLY_TEST_LLM_KEY",
        model=model,
        temperature=0.2,
        effort=effort,  # type: ignore[arg-type]
    )


# ---------- capability helper ----------


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-6",
        "claude-sonnet-4-20250514",
        "claude-opus-4-6",
        "claude-opus-4-1",
        "claude-3-5-sonnet-20241022",
        "claude-3-haiku-20240307",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ],
)
def test_temperature_allowlist_accepts_legacy_models(model: str) -> None:
    caps = model_capabilities(model)
    assert caps.temperature is True
    assert caps.forced_tool_choice is True


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-1",
        "some-gateway-model",
    ],
)
def test_temperature_is_omitted_for_newer_and_unknown_models(model: str) -> None:
    assert model_capabilities(model).temperature is False


@pytest.mark.parametrize(
    ("model", "forced"),
    [
        ("claude-opus-5-5", False),
        ("claude-sonnet-5-5", False),
        ("claude-fable-5-1", False),
        ("claude-haiku-5-5", True),
        ("claude-sonnet-5", True),
        ("claude-opus-5", True),
        ("claude-opus-4-7", True),
        ("claude-opus-4-8", True),
        ("claude-haiku-4-5", True),
        ("unknown-model", False),
    ],
)
def test_forced_tool_choice_capability(model: str, forced: bool) -> None:
    assert model_capabilities(model).forced_tool_choice is forced


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("claude-haiku-5-5", True),
        ("claude-opus-4-5", True),
        ("claude-opus-4-7", True),
        ("claude-sonnet-4-6", True),
        ("claude-sonnet-5-5", True),
        ("claude-fable-5-1", True),
        ("claude-haiku-4-5", False),
        ("claude-sonnet-4-5", False),
        ("claude-3-5-sonnet-20241022", False),
        ("unknown-model", False),
    ],
)
def test_effort_capability(model: str, effort: bool) -> None:
    assert model_capabilities(model).effort is effort


# ---------- request shaping ----------


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"])
def test_models_rejecting_forced_tool_choice_get_auto_plus_an_instruction(
    monkeypatch, model: str
) -> None:
    messages = _Messages([_ok({"reasoning": "r", "p_yes": 0.6})])
    _patch(monkeypatch, messages)
    _client(model).analyze(system="SYS", user="u", tool=_REQUIRED_TOOL)
    kw = messages.calls[0]
    assert kw["tool_choice"]["type"] == "auto"
    assert "temperature" not in kw
    # The cached block is untouched; the instruction follows it.
    assert kw["system"][0] == {
        "type": "text",
        "text": "SYS",
        "cache_control": {"type": "ephemeral"},
    }
    assert "submit_analysis" in kw["system"][1]["text"]


@pytest.mark.parametrize(
    "model", ["claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5"]
)
def test_no_temperature_for_models_that_reject_it(monkeypatch, model: str) -> None:
    messages = _Messages([_ok({"reasoning": "r", "p_yes": 0.6})])
    _patch(monkeypatch, messages)
    _client(model).analyze(system="s", user="u", tool=_REQUIRED_TOOL)
    assert "temperature" not in messages.calls[0]


def test_haiku_5_5_gets_effort_and_forced_tool_choice(monkeypatch) -> None:
    messages = _Messages([_ok({"reasoning": "r", "p_yes": 0.6})])
    _patch(monkeypatch, messages)
    _client("claude-haiku-5-5", effort="low").analyze(system="s", user="u", tool=_REQUIRED_TOOL)
    kw = messages.calls[0]
    assert kw["output_config"] == {"effort": "low"}
    # The docs state Haiku 5.5 accepts a forced tool_choice.
    assert kw["tool_choice"] == {"type": "tool", "name": "submit_analysis"}
    assert "temperature" not in kw


def test_effort_is_not_sent_to_models_without_it(monkeypatch) -> None:
    messages = _Messages([_ok({"reasoning": "r", "p_yes": 0.6})])
    _patch(monkeypatch, messages)
    _client("claude-haiku-4-5", effort="low").analyze(system="s", user="u", tool=_REQUIRED_TOOL)
    kw = messages.calls[0]
    assert "output_config" not in kw
    assert kw["tool_choice"] == {"type": "tool", "name": "submit_analysis"}
    assert kw["temperature"] == 0.2


def test_thinking_blocks_are_skipped(monkeypatch) -> None:
    resp = _UsageResponse(
        [
            _Block("thinking"),
            _Block("text"),
            _Block("tool_use", name="submit_analysis", tool_input={"reasoning": "r", "p_yes": 0.4}),
        ]
    )
    _patch(monkeypatch, _Messages([resp]))
    out = _client("claude-haiku-5-5").analyze(system="s", user="u", tool=_REQUIRED_TOOL)
    assert out == {"reasoning": "r", "p_yes": 0.4}


# ---------- structural failures ----------


def test_max_tokens_cutoff_is_retried_with_a_higher_cap(monkeypatch) -> None:
    """Regression: a response truncated at max_tokens returned a partial dict
    (only ``reasoning``) that the analyzer recorded as an abstain."""
    messages = _Messages(
        [
            _ok({"reasoning": "half a thou"}, stop_reason="max_tokens"),
            _ok({"reasoning": "r", "p_yes": 0.6}),
        ]
    )
    _patch(monkeypatch, messages)
    result = _client("claude-haiku-4-5").analyze_detailed(
        system="s", user="u", tool=_REQUIRED_TOOL, max_tokens=2048
    )
    assert result.data == {"reasoning": "r", "p_yes": 0.6}
    assert [c["max_tokens"] for c in messages.calls] == [2048, 4096]


def test_max_tokens_cap_never_exceeds_the_ceiling(monkeypatch) -> None:
    messages = _Messages(
        [
            _ok({"reasoning": "x"}, stop_reason="max_tokens"),
            _ok({"reasoning": "x"}, stop_reason="max_tokens"),
        ]
    )
    _patch(monkeypatch, messages)
    with pytest.raises(LLMError, match="max_tokens"):
        _client("claude-haiku-4-5").analyze(
            system="s", user="u", tool=_REQUIRED_TOOL, max_tokens=_MAX_TOKENS_CEILING
        )
    assert [c["max_tokens"] for c in messages.calls] == [_MAX_TOKENS_CEILING] * 2


def test_complete_call_missing_a_required_field_is_returned_as_is(monkeypatch) -> None:
    messages = _Messages([_ok({"reasoning": "r"})])
    _patch(monkeypatch, messages)
    result = _client("claude-haiku-4-5").analyze_detailed(system="s", user="u", tool=_REQUIRED_TOOL)
    assert result.data == {"reasoning": "r"}
    assert result.usage.attempts == 1


# ---------- usage ----------


def test_usage_is_summed_across_attempts_and_priced(monkeypatch) -> None:
    messages = _Messages(
        [
            _UsageResponse([_Block("text")], stop_reason="end_turn", inp=1000, out=200),
            _ok(
                {"reasoning": "r", "p_yes": 0.6}, inp=1000, out=300, cache_read=500, cache_write=40
            ),
        ]
    )
    _patch(monkeypatch, messages)
    usage = (
        _client("claude-haiku-4-5")
        .analyze_detailed(system="s", user="u", tool=_REQUIRED_TOOL)
        .usage
    )
    assert usage.attempts == 2
    assert usage.input_tokens == 2000
    assert usage.output_tokens == 500
    assert usage.cache_read_input_tokens == 500
    assert usage.cache_creation_input_tokens == 40
    # haiku-4-5: $1 in / $5 out per MTok; cache read 0.1x, write 1.25x.
    expected = (2000 * 1 + 500 * 0.1 + 40 * 1.25 + 500 * 5) / 1_000_000
    assert usage.cost_usd == pytest.approx(expected)
    assert usage.to_dict()["cost_usd"] == pytest.approx(expected)


def test_pricing_table() -> None:
    p44 = model_price("claude-haiku-4-5")
    p55 = model_price("claude-haiku-5-5")
    assert p44 is not None and (p44.input, p44.output) == (1.0, 5.0)
    assert p55 is not None and (p55.input, p55.output) == (0.10, 0.50)
    assert model_price("claude-sonnet-4-6") is None
    assert LLMUsage(model="mystery", input_tokens=10).cost_usd is None


# ---------- analyzer v1 ----------


class _DetailedFake:
    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    def analyze_detailed(self, **kw: Any):  # noqa: ANN201
        if self._exc is not None:
            raise self._exc
        return self._result


def test_v1_reports_usage_as_a_signal() -> None:
    from openpoly.llm import LLMResult

    usage = LLMUsage(model="claude-haiku-5-5", input_tokens=900, output_tokens=120, attempts=1)
    fake = _DetailedFake(
        LLMResult(
            data={
                "reasoning": "Q1 Q2 Q3",
                "selected_index": 1,
                "p_yes": 0.7,
                "confidence": "medium",
                "rationale": "r",
            },
            usage=usage,
        )
    )
    out = LLMAnalyzerV1(LLMAnalyzerConfigV1(), llm_client=fake).run(  # type: ignore[arg-type]
        SectionInput(tick_type="event", payload=_candidates("m1"))
    )
    assert out.verdict == "ok"
    assert out.signals["llm_usage"]["output_tokens"] == 120
    assert out.signals["llm_usage"]["cost_usd"] == pytest.approx(
        (900 * 0.10 + 120 * 0.50) / 1_000_000
    )


def test_v1_truncated_call_is_an_error_not_an_abstain(monkeypatch) -> None:
    messages = _Messages(
        [
            _ok({"reasoning": "cut"}, stop_reason="max_tokens"),
            _ok({"reasoning": "cut again"}, stop_reason="max_tokens"),
        ]
    )
    _patch(monkeypatch, messages)
    cfg = LLMAnalyzerConfigV1(api_key_ref="env:OPENPOLY_TEST_LLM_KEY")
    out = LLMAnalyzerV1(cfg).run(SectionInput(tick_type="event", payload=_candidates("m1")))
    assert out.verdict == "error"
    assert "max_tokens" in (out.reason or "")
    # The failed attempts still cost tokens and are reported.
    assert out.signals["llm_usage"]["attempts"] == 2


def test_v1_config_effort_defaults_low_and_reaches_the_request(monkeypatch) -> None:
    messages = _Messages(
        [
            _ok(
                {
                    "reasoning": "r",
                    "selected_index": 0,
                    "p_yes": 0.5,
                    "confidence": "low",
                    "rationale": "none",
                }
            )
        ]
    )
    _patch(monkeypatch, messages)
    cfg = LLMAnalyzerConfigV1(api_key_ref="env:OPENPOLY_TEST_LLM_KEY", llm_model="claude-haiku-5-5")
    assert cfg.llm_effort == "low"
    assert LLMAnalyzerConfigV1().llm_model == "claude-haiku-4-5"  # default unchanged
    LLMAnalyzerV1(cfg).run(SectionInput(tick_type="event", payload=_candidates("m1")))
    kw = messages.calls[0]
    assert kw["output_config"] == {"effort": "low"}
    assert kw["tools"] == [SUBMIT_ANALYSIS_TOOL_V1]

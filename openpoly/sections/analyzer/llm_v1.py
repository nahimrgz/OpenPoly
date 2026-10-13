"""LLM analyzer v1 — the self-check actually happens.

Same contract as ``llm_v0`` (input ``MarketCandidates``, output
``AnalysisResult``, the price deliberately withheld); see that module for the
design. v1 fixes one thing.

v0's prompt asks the model to "work the self-check" (Q1-Q3) *before*
answering, but ``LLMClient`` forces ``tool_choice`` to ``submit_analysis``: the
model's first output token is already inside the tool call, so there is no
text turn in which that check could be written. The self-check was an
instruction with nowhere to run.

v1 gives it a place inside the forced call. The tool schema's FIRST property is
a required ``reasoning`` string, and the prompt tells the model to answer Q1-Q3
there before filling ``p_yes`` / ``confidence``. JSON properties are generated
in schema order, so the reasoning tokens are produced before the numbers they
are meant to inform.

The reasoning is surfaced as the ``reasoning`` signal; the orchestrator records
it on the analyzer log entry next to ``rationale`` and logs it at DEBUG.

v1 also reports the call's token usage (every attempt, retries included) as the
``llm_usage`` signal, and adds ``llm_effort`` (``LLMAnalyzerConfigV1``): sent
as ``output_config.effort`` to models that accept it. ``claude-haiku-5-5``
runs thinking by default (effort ``medium``) and thinking tokens bill as
output, so v1 defaults to ``low``. The default model stays
``claude-haiku-4-5``; ``claude-haiku-5-5`` is a supported choice. What each
model accepts (temperature, forced tool choice, effort) is decided by
``openpoly.llm.client.model_capabilities``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from pydantic import Field

from openpoly.embedding.models import MarketCandidates
from openpoly.llm import LLMError
from openpoly.llm.client import LLMUsage, ToolDefinition
from openpoly.sections._base import SectionInput, SectionOutput
from openpoly.sections.analyzer.llm_v0 import (
    AnalysisResult,
    LLMAnalyzerConfig,
    LLMAnalyzerV0,
    _build_system,
    _build_user,
    _interpret,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AnalysisResult",
    "LLMAnalyzerConfig",
    "LLMAnalyzerConfigV1",
    "LLMAnalyzerV1",
    "SUBMIT_ANALYSIS_TOOL_V1",
]


class LLMAnalyzerConfigV1(LLMAnalyzerConfig):
    llm_model: str = Field(
        default="claude-haiku-4-5",
        description=(
            "Model id sent to the API. On the official Anthropic endpoint use "
            "a Claude id — claude-haiku-4-5 (default), claude-haiku-5-5 "
            "(cheaper; thinks by default, tuned by llm_effort), "
            "claude-sonnet-4-6, claude-opus-4-7, …; on a third-party gateway "
            "use whatever id that gateway publishes."
        ),
    )
    temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description=(
            "Sampling temperature; sent only to models that accept one "
            "(claude-haiku-4-5, Sonnet / Opus up to 4.6, 3.x) and omitted for "
            "the rest, which reject a non-default value."
        ),
    )
    llm_effort: Literal["low", "medium", "high"] = Field(
        default="low",
        description=(
            "Effort sent as output_config.effort to models that support it "
            "(e.g. claude-haiku-5-5, Opus 4.5+, Sonnet 4.6+); ignored for the "
            "others. claude-haiku-5-5 thinks by default and thinking tokens "
            "bill as output — low keeps the analyzer cheap and fast."
        ),
    )


# The reasoning field makes the tool input several times longer than v0's
# (index + probability + one-line rationale), so the response cap is raised to
# keep a verbose self-check from truncating the call before ``p_yes``.
_MAX_TOKENS = 2048

_SYSTEM_PROMPT = """You are a prediction-market analyst. You get a news item \
and a shortlist of candidate markets (pre-filtered by semantic similarity —
recall-oriented; some may not be moved by the news).

Answer by calling submit_analysis. Fill its fields IN ORDER — `reasoning`
comes first and must be written before you decide any number.

  reasoning      — your working, written out. Identify the market you think
                   the news moves (or why none is), then answer each check
                   explicitly, labelled Q1 / Q2 / Q3:
                     Q1. Is this genuinely new information, or a wire rewrite
                         of something already public?
                     Q2. Could the event have already resolved or shifted
                         before this news?
                     Q3. If your p_yes is a strong claim, can you cite
                         specific checkable evidence? If not, downgrade
                         confidence.
                   Keep it concise — a few sentences per check.
  selected_index — the 1-based index of the ONE market the news materially
                   moves, or 0 if none. Abstaining is expected; do not force
                   a pick.
  p_yes          — YOUR probability (0-1) that the selected market resolves
                   YES, in light of the news and your reasoning.
  confidence     — HIGH only with primary-source confirmation; LOW for stale
                   news (>2h old), ambiguous resolution, or a directional
                   guess; MEDIUM otherwise. It must agree with your Q3 answer.
  rationale      — one or two sentences summarizing the decision.

"Materially moved" = the news changes the outcome or its probability, not
mere topical overlap. You are not given the current market price — do not
anchor to one; reason from the news and the market's resolution question."""


SUBMIT_ANALYSIS_TOOL_V1: ToolDefinition = {
    "name": "submit_analysis",
    "description": (
        "Write the Q1-Q3 self-check in `reasoning` first, then report which "
        "candidate market the news moves and its YES probability."
    ),
    "input_schema": {
        "type": "object",
        # ``reasoning`` is deliberately the first property: the model emits
        # tool input in schema order, so this is where the self-check is
        # written before the numbers.
        "properties": {
            "reasoning": {
                "type": "string",
                "description": (
                    "The Q1 / Q2 / Q3 self-check, written out before deciding "
                    "selected_index, p_yes and confidence."
                ),
            },
            "selected_index": {
                "type": "integer",
                "description": "1-based index of the moved market, or 0 if none.",
            },
            "p_yes": {
                "type": "number",
                "description": "Probability (0-1) the selected market resolves YES.",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "rationale": {
                "type": "string",
                "description": "One or two sentences.",
            },
        },
        "required": ["reasoning", "selected_index", "p_yes", "confidence", "rationale"],
    },
}


class LLMAnalyzerV1(LLMAnalyzerV0):
    """v1 analyzer. Inherits construction and the lazy client from v0;
    the prompt, tool schema and ``run`` are v1's own."""

    SECTION_TYPE = "analyzer"
    SECTION_VERSION = "1.1.0"
    REQUIRES = ["llm", "market_data"]
    Config = LLMAnalyzerConfigV1

    config: LLMAnalyzerConfigV1

    def _client_kwargs(self) -> dict[str, Any]:
        return {**super()._client_kwargs(), "effort": self.config.llm_effort}

    def run(self, input: SectionInput) -> SectionOutput:
        candidates = input.payload
        if not isinstance(candidates, MarketCandidates) or not candidates.candidates:
            return SectionOutput(payload=None, verdict="skip", reason="no market candidates")

        client = self._client()
        usage: LLMUsage | None = None
        kwargs: dict[str, Any] = {
            "system": _build_system(self.config.extra_guidance, base=_SYSTEM_PROMPT),
            "user": _build_user(candidates),
            "tool": SUBMIT_ANALYSIS_TOOL_V1,
            "max_tokens": _MAX_TOKENS,
        }
        try:
            # ``analyze_detailed`` also returns the usage; an injected client
            # without it (a test double) still works through ``analyze``.
            detailed = getattr(client, "analyze_detailed", None)
            if detailed is not None:
                llm_result = detailed(**kwargs)
                result, usage = llm_result.data, llm_result.usage
            else:
                result = client.analyze(**kwargs)
        except LLMError as exc:
            return SectionOutput(
                payload=None,
                verdict="error",
                reason=repr(exc)[:200],
                signals=_usage_signal(exc.usage),
            )

        raw_reasoning = result.get("reasoning")
        reasoning = raw_reasoning.strip() if isinstance(raw_reasoning, str) else ""
        logger.debug("analyzer reasoning for news %s: %s", candidates.news.id, reasoning)
        # Every outcome carries the reasoning — an abstain or a
        # below-min-confidence skip is exactly when it is worth reading.
        return _interpret(
            result,
            candidates,
            self.config.min_confidence,
            extra_signals={"reasoning": reasoning, **_usage_signal(usage)},
        )

    @staticmethod
    def CONTRACT_TEST() -> None:
        # payload=None → skip before any LLM call: the registry scan stays
        # light and needs no API key.
        inst = LLMAnalyzerV1(LLMAnalyzerConfigV1())
        out = inst.run(SectionInput(tick_type="event", payload=None))
        assert out.verdict == "skip"

        schema = cast(dict[str, Any], SUBMIT_ANALYSIS_TOOL_V1["input_schema"])
        assert next(iter(schema["properties"])) == "reasoning"
        assert "reasoning" in schema["required"]


def _usage_signal(usage: LLMUsage | None) -> dict[str, Any]:
    """The ``llm_usage`` signal (the orchestrator records it on the analyzer
    log entry and the persisted usage row); empty when nothing was spent."""
    return {"llm_usage": usage.to_dict()} if usage is not None else {}

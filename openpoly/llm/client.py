"""Anthropic API client wrapper for section impls.

A thin **sync** wrapper over ``anthropic.Anthropic``: it resolves the API key
from an ``api_key_ref``, asks for a single structured tool call, and returns
that tool's input dict. An optional ``base_url`` routes the call through an
Anthropic-compatible third-party gateway (e.g. yunwu) instead of the
official endpoint. The Anthropic SDK retries transient HTTP errors (429 / 5xx /
connection) itself; this wrapper additionally retries a *structurally* bad
response — one with no ``tool_use`` block (e.g. a refusal), or one cut off
at ``max_tokens`` before every required field was written — up to
``_STRUCTURAL_RETRIES`` attempts in all before raising ``LLMError``. A
complete (non-truncated) tool call missing a schema-required field is returned
as-is: the section interprets it (an abstain carries no probability).

Request parameters depend on the model (``model_capabilities``): newer models
reject a non-default ``temperature`` and a forced ``tool_choice`` with a 400,
and only some accept ``output_config.effort``. Response content blocks other
than ``tool_use`` (thinking blocks, text) are ignored.

Token usage of every attempt — retries included — is summed into one
``LLMUsage`` (``analyze_detailed``), with an estimated USD cost for models in
``MODEL_PRICING``.

The call blocks (network I/O); the orchestrator runs the section on a worker
thread so it does not stall the event loop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeVar

import anthropic
from anthropic.types import ToolParam

from openpoly.news.secrets import SecretsError, resolve

logger = logging.getLogger(__name__)

# The structured tool output is small (an index, a probability, a one-line
# rationale); this caps the response generously.
_MAX_TOKENS = 1024
# A response cut off at ``max_tokens`` is retried with twice the cap, up to
# this ceiling (a thinking model spends output tokens before the tool call).
_MAX_TOKENS_CEILING = 8192
# Attempts in all when the response is structurally unusable (the SDK handles
# HTTP retries): the first call plus one retry.
_STRUCTURAL_RETRIES = 2
# Per-request timeout — a small completion should never take this long.
_TIMEOUT_SECONDS = 60.0

Effort = Literal["low", "medium", "high"]

# A tool definition as the SDK types it (``name`` / ``description`` /
# ``input_schema``); re-exported so section impls type their tool constants
# without importing the SDK themselves.
ToolDefinition = ToolParam

# Minimal tool for the connectivity probe (``ping``): its only job is to make
# the model emit one tool_use block.
_PING_TOOL: ToolDefinition = {
    "name": "ack",
    "description": "Acknowledge the connectivity check.",
    "input_schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    },
}


# ---------- model capabilities ----------


@dataclass(frozen=True)
class ModelCapabilities:
    """What one model accepts in a Messages request.

    * ``temperature`` — a non-default sampling temperature. Rejected (400)
      from Opus 4.7 / Sonnet 5 / Haiku 5.5 on.
    * ``forced_tool_choice`` — ``tool_choice: {type: "tool"}``. Rejected (400)
      by Opus 5.5 / Sonnet 5.5 / Fable 5.1 / Mythos 5.1; accepted by Opus 5,
      Sonnet 5, Fable 5, Haiku 5.5 and older models (per the Claude API
      docs). Where it is rejected or undocumented the wrapper sends
      ``{type: "auto"}`` plus an instruction to call the tool.
    * ``effort`` — ``output_config.effort``.
    """

    temperature: bool
    forced_tool_choice: bool
    effort: bool


# Families: claude-<family>-<major>[-<minor>] (4.x and later) and the legacy
# claude-<major>[-<minor>]-<family> (3.x). Ids are normalized first: a vendor
# prefix up to the last ``/`` (``anthropic/claude-…``, a gateway's naming) is
# dropped and dots become dashes (``claude-opus-4.7`` → ``claude-opus-4-7``).
# A trailing date suffix (claude-sonnet-4-5-20250929) or a provider prefix
# (us.anthropic.claude-…) does not change the family; a minor is at most two
# digits so a date is never read as one.
_FAMILIES = "opus|sonnet|haiku|fable|mythos"
_NEW_ID = re.compile(rf"claude-({_FAMILIES})-(\d{{1,2}})(?:-(\d{{1,2}}))?(?!\d)")
_LEGACY_ID = re.compile(rf"claude-(\d{{1,2}})(?:-(\d{{1,2}}))?-({_FAMILIES})")

# Unknown ids (a gateway's own naming) get the conservative set: no
# temperature (sending one is a 400 on every new model), auto tool choice
# (valid on every model) and no effort.
_UNKNOWN = ModelCapabilities(temperature=False, forced_tool_choice=False, effort=False)

# 5-generation models the Claude API docs state accept a forced tool_choice
# (Opus 5.5 / Sonnet 5.5 / Fable 5.1 / Mythos 5.1 reject it with a 400; the
# rest of the generation is undocumented and stays on auto + instruction).
_FORCED_TOOL_CHOICE_5X: frozenset[tuple[str, int, int]] = frozenset(
    {("opus", 5, 0), ("sonnet", 5, 0), ("fable", 5, 0), ("haiku", 5, 5)}
)


def _normalize_model_id(model: str) -> str:
    """Lower-cased id without a vendor prefix, with dotted versions dashed."""
    return model.strip().lower().rsplit("/", 1)[-1].replace(".", "-")


def _parse_model(model: str) -> tuple[str, int, int] | None:
    """``(family, major, minor)`` of a Claude model id, or None."""
    m = _normalize_model_id(model)
    new = _NEW_ID.search(m)
    if new is not None:
        return new.group(1), int(new.group(2)), int(new.group(3) or 0)
    legacy = _LEGACY_ID.search(m)
    if legacy is not None:
        return legacy.group(3), int(legacy.group(1)), int(legacy.group(2) or 0)
    return None


def model_capabilities(model: str) -> ModelCapabilities:
    """The request parameters ``model`` accepts — the one place the wrapper
    decides what to send (see ``ModelCapabilities``).

    * temperature: an allowlist of legacy models known to accept it —
      3.x, ``claude-haiku-4-5``, ``claude-sonnet-4-*`` up to 4.6 and
      ``claude-opus-4-*`` up to 4.6.
    * forced tool choice: those plus Opus 4.7 / 4.8 and the 5-generation
      models documented to accept it (``_FORCED_TOOL_CHOICE_5X``).
      Everything else — Opus 5.5, Sonnet 5.5, Fable 5.1, Mythos, later
      generations, unknown ids — gets ``auto`` plus an instruction.
    * effort: Opus 4.5+, Sonnet 4.6+, Haiku 5+, Fable, Mythos.
    """
    parsed = _parse_model(model)
    if parsed is None:
        return _UNKNOWN
    family, major, minor = parsed
    if major <= 3:
        return ModelCapabilities(temperature=True, forced_tool_choice=True, effort=False)
    if family in ("fable", "mythos") or major >= 5:
        return ModelCapabilities(
            temperature=False,
            forced_tool_choice=parsed in _FORCED_TOOL_CHOICE_5X,
            effort=True,
        )
    # major == 4
    if family == "haiku":
        return ModelCapabilities(temperature=True, forced_tool_choice=True, effort=False)
    if family == "sonnet":
        return ModelCapabilities(
            temperature=minor <= 6, forced_tool_choice=minor <= 6, effort=minor >= 6
        )
    # opus 4.x
    return ModelCapabilities(
        temperature=minor <= 6,
        forced_tool_choice=minor <= 8,
        effort=minor >= 5,
    )


# ---------- usage / pricing ----------


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float


# Prompt-cache multipliers on the input price (5-minute cache writes).
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25

# Keyed by (family, major, minor). Haiku 5.5's rate is for prompts up to 100K
# tokens — far above an analyzer call.
MODEL_PRICING: dict[tuple[str, int, int], ModelPrice] = {
    ("haiku", 4, 5): ModelPrice(input=1.00, output=5.00),
    ("haiku", 5, 5): ModelPrice(input=0.10, output=0.50),
}


def model_price(model: str) -> ModelPrice | None:
    """The price of ``model`` per ``MODEL_PRICING``, None when unknown."""
    parsed = _parse_model(model)
    return MODEL_PRICING.get(parsed) if parsed is not None else None


_T = TypeVar("_T")


def int_or(value: object, default: _T) -> int | _T:
    """``value`` when it is an int (a bool is not), else ``default`` — the
    coercion for one field of an ``llm_usage`` signal dict."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def cost_or_none(value: object) -> float | None:
    """The ``cost_usd`` of an ``llm_usage`` signal dict as a float, None when
    it is not a number (an unpriced model)."""
    return float(value) if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class LLMUsage:
    """Token usage of one ``analyze`` call, summed over every attempt.

    ``output_tokens`` includes thinking tokens (they bill as output).
    ``cost_usd`` is an estimate from ``MODEL_PRICING`` — None for a model
    not in the table."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    attempts: int = 0

    @property
    def cost_usd(self) -> float | None:
        price = model_price(self.model)
        if price is None:
            return None
        per_token_in = price.input / 1_000_000
        return (
            self.input_tokens * per_token_in
            + self.cache_read_input_tokens * per_token_in * _CACHE_READ_MULTIPLIER
            + self.cache_creation_input_tokens * per_token_in * _CACHE_WRITE_MULTIPLIER
            + self.output_tokens * price.output / 1_000_000
        )

    def plus(self, response_usage: object) -> LLMUsage:
        """This usage with one response's ``usage`` added (one more attempt).
        Fields the response does not carry count as 0."""

        def _int(name: str) -> int:
            value = getattr(response_usage, name, None)
            return value if isinstance(value, int) else 0

        return LLMUsage(
            model=self.model,
            input_tokens=self.input_tokens + _int("input_tokens"),
            output_tokens=self.output_tokens + _int("output_tokens"),
            cache_read_input_tokens=self.cache_read_input_tokens + _int("cache_read_input_tokens"),
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + _int("cache_creation_input_tokens"),
            attempts=self.attempts + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "cost_usd": self.cost_usd}


@dataclass(frozen=True)
class LLMResult:
    """A usable tool call's input plus the usage it took to get it."""

    data: dict[str, Any]
    usage: LLMUsage


class LLMError(Exception):
    """An LLM call failed — key resolution, the API, or a malformed response.

    ``usage`` carries the tokens the failed call still spent (None when no
    request was made)."""

    def __init__(self, message: str, *, usage: LLMUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


def _tool_instruction(tool_name: str) -> str:
    """Steers an unforced (``auto``) tool choice to the one tool."""
    return (
        f"Respond only by calling the {tool_name} tool exactly once, with every "
        "required field filled in. Do not answer in plain text."
    )


def _missing_required(tool: ToolDefinition, data: dict[str, Any]) -> list[str]:
    schema = tool.get("input_schema")
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(required, list):
        return []
    return [name for name in required if name not in data]


class LLMClient:
    """Sync Anthropic wrapper that runs one tool-call analysis.

    Construction touches nothing; the API key is resolved and the underlying
    ``anthropic.Anthropic`` client is built lazily on the first ``analyze`` —
    so a client can be constructed before the secret store is reachable.
    """

    def __init__(
        self,
        *,
        api_key_ref: str,
        model: str,
        temperature: float,
        base_url: str = "",
        effort: Effort | None = None,
    ) -> None:
        self._api_key_ref = api_key_ref
        self._model = model
        self._temperature = temperature
        self._base_url = base_url
        # Sent as ``output_config.effort`` to models that accept it; None
        # leaves the model's own default.
        self._effort = effort
        self._client: anthropic.Anthropic | None = None

    def _ensure_client(self) -> anthropic.Anthropic:
        if self._client is None:
            try:
                api_key = resolve(self._api_key_ref)
            except SecretsError as exc:
                raise LLMError(
                    f"could not resolve api_key_ref {self._api_key_ref!r}: {exc}"
                ) from exc
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "max_retries": 2,
                "timeout": _TIMEOUT_SECONDS,
            }
            # Empty base_url → omit it, so the SDK keeps its own default (and
            # still honors an ANTHROPIC_BASE_URL env var); non-empty → route
            # through the given Anthropic-compatible gateway (e.g. yunwu).
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _request_params(self, *, system: str, user: str, tool: ToolDefinition) -> dict[str, Any]:
        """Everything but ``max_tokens``, shaped for this model."""
        caps = model_capabilities(self._model)
        tool_name = tool["name"]
        # cache_control marks the (static) system prompt cacheable; it is a
        # no-op below the model's minimum cacheable prefix.
        system_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        params: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
        }
        if caps.forced_tool_choice:
            params["tool_choice"] = {"type": "tool", "name": tool_name}
        else:
            # Forced tool use is a 400 here: let the model choose, tell it
            # which tool to use, and rely on the structural retry if it does
            # not. The instruction follows the cached block so the cached
            # prefix is unchanged.
            params["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
            system_blocks.append({"type": "text", "text": _tool_instruction(tool_name)})
        params["system"] = system_blocks
        if caps.temperature:
            params["temperature"] = self._temperature
        if caps.effort and self._effort is not None:
            params["output_config"] = {"effort": self._effort}
        return params

    def analyze(
        self,
        *,
        system: str,
        user: str,
        tool: ToolDefinition,
        max_tokens: int = _MAX_TOKENS,
    ) -> dict[str, Any]:
        """Run one completion that calls ``tool``; return its input dict
        (``analyze_detailed`` without the usage)."""
        return self.analyze_detailed(
            system=system, user=user, tool=tool, max_tokens=max_tokens
        ).data

    def analyze_detailed(
        self,
        *,
        system: str,
        user: str,
        tool: ToolDefinition,
        max_tokens: int = _MAX_TOKENS,
    ) -> LLMResult:
        """Run one completion that calls ``tool``; return its input dict and
        the usage summed over every attempt.

        ``tool`` is a full tool definition (``name`` / ``description`` /
        ``input_schema``). ``max_tokens`` caps the response; a tool whose input
        carries free-form reasoning needs more than the default.

        A response is structurally unusable — and retried, up to
        ``_STRUCTURAL_RETRIES`` attempts in all — when it carries no
        ``tool_use`` block for ``tool``, or when it stopped at ``max_tokens``
        before writing every ``required`` field of the tool schema (a
        truncated tool input is partial, not an answer; the retry doubles the
        cap). A complete call missing a required field is returned as-is —
        the section decides what it means (an abstain needs no ``p_yes``).
        Raises ``LLMError`` (with the usage spent) on key / API failure or
        when no attempt is usable.
        """
        client = self._ensure_client()
        tool_name = tool["name"]
        params = self._request_params(system=system, user=user, tool=tool)
        usage = LLMUsage(model=self._model)
        cap = max_tokens

        last_error: str | None = None
        for attempt in range(1, _STRUCTURAL_RETRIES + 1):
            try:
                response = client.messages.create(max_tokens=cap, **params)
            except anthropic.APIError as exc:
                # The SDK already retried transient errors; this is terminal.
                raise LLMError(
                    f"Anthropic API call failed: {exc!r}",
                    usage=usage if usage.attempts else None,
                ) from exc
            usage = usage.plus(getattr(response, "usage", None))
            stop_reason = getattr(response, "stop_reason", None)

            data: dict[str, Any] | None = None
            # Thinking (and any text) blocks are skipped: only the tool call
            # is the answer.
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                    data = dict(block.input)
                    break

            truncated = stop_reason == "max_tokens"
            if data is not None and not (truncated and _missing_required(tool, data)):
                return LLMResult(data=data, usage=usage)
            if truncated:
                last_error = f"response truncated at max_tokens={cap}"
                cap = min(cap * 2, _MAX_TOKENS_CEILING)
            else:
                last_error = f"no '{tool_name}' tool call (stop_reason={stop_reason})"
            logger.warning("LLM attempt %d/%d: %s", attempt, _STRUCTURAL_RETRIES, last_error)

        raise LLMError(f"LLM returned no usable tool call: {last_error}", usage=usage)

    def ping(self) -> None:
        """Probe connectivity with one minimal tool call.

        Reuses ``analyze`` so the probe walks exactly the path a real call
        takes — key resolution, ``base_url`` routing, model-id validity, and
        the tool-call parameters the analyzer depends on. Raises ``LLMError``
        on any failure; returns ``None`` on success.
        """
        self.analyze(
            system="Connectivity check.",
            user="Call the ack tool with ok set to true.",
            tool=_PING_TOOL,
        )

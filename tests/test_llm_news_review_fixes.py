"""Regression tests for the second review's LLM / news / analytics findings.

* S7 — a non-truncated tool call missing schema-required fields is returned
  as-is (the section interprets it); only a ``max_tokens`` cut-off that lacks
  required fields is a structural failure.
* S8 — the entry kill switch walks closed positions in close order
  (``closed_at``), not in the store's id order.
* S9 — a malformed news frame is dropped, never kills the WS task.
* S11 — gateway model ids (vendor prefixes, dotted versions, legacy order)
  parse to the right family / version, capabilities and price.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time as _time
from typing import Any

import pytest

from openpoly.llm import LLMClient, LLMError, model_capabilities, model_price
from openpoly.llm.client import _parse_model
from openpoly.news.ring_buffer import NewsRingBuffer
from openpoly.news.ws_client import NewsWSClient, default_parse
from openpoly.sections._base import SectionInput
from openpoly.sections.analyzer.llm_v0 import LLMAnalyzerConfig, LLMAnalyzerV0
from openpoly.sections.analyzer.llm_v1 import LLMAnalyzerConfigV1, LLMAnalyzerV1
from openpoly.sections.entry.edge_threshold_v0 import EdgeThresholdConfig, EdgeThresholdEntryV0
from tests.test_llm_capabilities_usage import _REQUIRED_TOOL, _ok
from tests.test_llm_client import _Messages, _patch
from tests.test_section_entry_edge import (
    _ar,
    _book,
    _closed_rec,
    _FakePortfolio,
    _market,
    _populate,
    _run,
)
from tests.test_section_entry_edge import (
    _isolate_market_store as _isolate_market_store,  # autouse fixture
)
from tests.test_section_analyzer_llm import _candidates

_KEY = "env:OPENPOLY_TEST_LLM_KEY"


def _client(model: str = "claude-haiku-4-5") -> LLMClient:
    return LLMClient(api_key_ref=_KEY, model=model, temperature=0.2)


# ---------- S7: required-field check ----------


@pytest.mark.parametrize(
    ("analyzer", "payload"),
    [
        (LLMAnalyzerV0(LLMAnalyzerConfig(api_key_ref=_KEY)), {"selected_index": 0}),
        (
            LLMAnalyzerV0(LLMAnalyzerConfig(api_key_ref=_KEY)),
            {"selected_index": 0, "rationale": "nothing material"},
        ),
        (
            LLMAnalyzerV1(LLMAnalyzerConfigV1(api_key_ref=_KEY)),
            {"reasoning": "Q1 no", "selected_index": 0, "rationale": "nothing material"},
        ),
    ],
)
def test_abstain_without_p_yes_is_a_skip_after_one_call(
    monkeypatch, analyzer: Any, payload: dict[str, Any]
) -> None:
    messages = _Messages([_ok(payload)])
    _patch(monkeypatch, messages)
    out = analyzer.run(SectionInput(tick_type="event", payload=_candidates("m1")))
    assert out.verdict == "skip"
    assert out.reason == "no actionable market"
    assert len(messages.calls) == 1


def test_pick_missing_only_rationale_still_trades(monkeypatch) -> None:
    messages = _Messages(
        [_ok({"reasoning": "Q1 yes", "selected_index": 1, "p_yes": 0.7, "confidence": "high"})]
    )
    _patch(monkeypatch, messages)
    cfg = LLMAnalyzerConfigV1(api_key_ref=_KEY)
    out = LLMAnalyzerV1(cfg).run(SectionInput(tick_type="event", payload=_candidates("m1")))
    assert out.verdict == "ok"
    assert out.payload is not None and out.payload.p_model == pytest.approx(0.7)
    assert out.payload.rationale == ""
    assert len(messages.calls) == 1


def test_non_truncated_call_missing_required_fields_is_returned_as_is(monkeypatch) -> None:
    messages = _Messages([_ok({"reasoning": "r"})])
    _patch(monkeypatch, messages)
    result = _client().analyze_detailed(system="s", user="u", tool=_REQUIRED_TOOL)
    assert result.data == {"reasoning": "r"}
    assert result.usage.attempts == 1


def test_truncated_call_missing_fields_is_retried_with_a_doubled_cap_then_errors(
    monkeypatch,
) -> None:
    messages = _Messages(
        [
            _ok({"reasoning": "cut"}, stop_reason="max_tokens"),
            _ok({"reasoning": "cut again"}, stop_reason="max_tokens"),
        ]
    )
    _patch(monkeypatch, messages)
    with pytest.raises(LLMError, match="max_tokens") as info:
        _client().analyze(system="s", user="u", tool=_REQUIRED_TOOL, max_tokens=1024)
    assert [c["max_tokens"] for c in messages.calls] == [1024, 2048]
    assert info.value.usage is not None and info.value.usage.attempts == 2


def test_truncated_call_with_every_required_field_is_usable(monkeypatch) -> None:
    messages = _Messages([_ok({"reasoning": "r", "p_yes": 0.6}, stop_reason="max_tokens")])
    _patch(monkeypatch, messages)
    result = _client().analyze_detailed(system="s", user="u", tool=_REQUIRED_TOOL)
    assert result.data == {"reasoning": "r", "p_yes": 0.6}
    assert len(messages.calls) == 1


# ---------- S11: gateway model ids ----------


@pytest.mark.parametrize(
    ("model", "parsed"),
    [
        ("anthropic/claude-opus-4.7", ("opus", 4, 7)),
        ("claude-opus-4.7", ("opus", 4, 7)),
        ("claude-3.5-sonnet", ("sonnet", 3, 5)),
        ("anthropic/claude-3.5-sonnet", ("sonnet", 3, 5)),
        ("claude-haiku-4.5", ("haiku", 4, 5)),
        ("openrouter/anthropic/claude-haiku-4.5", ("haiku", 4, 5)),
        ("claude-3-5-sonnet-20241022", ("sonnet", 3, 5)),
        ("claude-3-7-sonnet-latest", ("sonnet", 3, 7)),
        ("claude-sonnet-4-20250514", ("sonnet", 4, 0)),
        ("claude-sonnet-4-5-20250929", ("sonnet", 4, 5)),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", ("sonnet", 4, 5)),
        ("claude-haiku-5-5", ("haiku", 5, 5)),
        ("claude-fable-5-1", ("fable", 5, 1)),
        ("some-gateway-model", None),
    ],
)
def test_parse_model_handles_gateway_ids(model: str, parsed: tuple[str, int, int] | None) -> None:
    assert _parse_model(model) == parsed


def test_dotted_opus_4_7_gets_its_real_capabilities() -> None:
    # Half-matched as opus 4.0 it was sent a temperature (a 400 on 4.7).
    caps = model_capabilities("anthropic/claude-opus-4.7")
    assert caps.temperature is False
    assert caps.forced_tool_choice is True
    assert caps.effort is True


def test_dotted_legacy_and_new_ids_get_their_capabilities() -> None:
    assert model_capabilities("claude-3.5-sonnet").temperature is True
    assert model_capabilities("claude-haiku-4.5").forced_tool_choice is True


def test_pricing_uses_the_same_normalization() -> None:
    assert model_price("anthropic/claude-haiku-4.5") == model_price("claude-haiku-4-5")
    assert model_price("claude-haiku-4.5") is not None
    assert model_price("vendor/claude-haiku-5.5") == model_price("claude-haiku-5-5")


@pytest.mark.parametrize(
    ("model", "forced"),
    [
        # Documented as accepting forced tool_choice (claude-api skill,
        # tool-use-concepts / tool-use.md / Haiku 5.5 migration notes).
        ("claude-haiku-5-5", True),
        ("claude-opus-5", True),
        ("claude-sonnet-5", True),
        ("claude-fable-5", True),
        # Documented as rejecting it with a 400.
        ("claude-opus-5-5", False),
        ("claude-sonnet-5-5", False),
        ("claude-fable-5-1", False),
        ("claude-mythos-5-1", False),
        # Not documented either way: auto + instruction.
        ("claude-mythos-5", False),
        ("claude-opus-6", False),
    ],
)
def test_5x_forced_tool_choice_follows_the_docs(model: str, forced: bool) -> None:
    assert model_capabilities(model).forced_tool_choice is forced


# ---------- S8: kill switch close order ----------


def test_kill_streak_follows_close_order_not_id_order() -> None:
    """Position 1 was opened first but closed last: by close order the two
    newest closes are losses (streak 2), while id order put the win first."""
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    now = _time.time()
    # The store returns rows newest-id first.
    records = [
        _closed_rec(
            market_id="m3", side="yes", closed_at=now - 300, realized_pnl=2.0, position_id=3
        ),
        _closed_rec(
            market_id="m2", side="yes", closed_at=now - 200, realized_pnl=-1.0, position_id=2
        ),
        _closed_rec(
            market_id="m1",
            side="yes",
            closed_at=now - 100,
            realized_pnl=-1.0,
            position_id=1,
            opened_at=now - 5000,
        ),
    ]
    inst = EdgeThresholdEntryV0(
        EdgeThresholdConfig(kill_max_consecutive_losses=2),
        portfolio_provider=lambda: _FakePortfolio(records),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "skip"
    assert out.reason == "kill_consecutive_losses"
    assert out.signals["streak"] == 2


def test_kill_drawdown_curve_follows_close_order_not_id_order() -> None:
    """Chronological closes +5 then -4 → peak 5, now 1, drawdown 4. Walked in
    id order (-4 then +5) the curve never dropped below its start."""
    _populate(_market(), _book("no-m1", bid=0.40, ask=0.42))
    now = _time.time()
    records = [
        _closed_rec(
            market_id="m2", side="yes", closed_at=now - 300, realized_pnl=5.0, position_id=2
        ),
        _closed_rec(
            market_id="m1",
            side="yes",
            closed_at=now - 100,
            realized_pnl=-4.0,
            position_id=1,
            opened_at=now - 5000,
        ),
    ]
    inst = EdgeThresholdEntryV0(
        EdgeThresholdConfig(kill_max_drawdown_usd=3.0),
        portfolio_provider=lambda: _FakePortfolio(records),
    )
    out = _run(inst, _ar(p_model=0.30))
    assert out.verdict == "skip"
    assert out.reason == "kill_drawdown"
    assert out.signals["drawdown_usd"] == 4.0
    assert out.signals["peak_usd"] == 5.0


# ---------- S9: malformed news frames ----------

_HUGE_INT_FRAME = '{"id": "x", "n": ' + "9" * 5000 + "}"  # int digit limit → ValueError
_DEEP_FRAME = "[" * 200_000  # → RecursionError
_BAD_UTF8_FRAME = b"\xff\xfe\xfa"  # → UnicodeDecodeError


def _good_frame(news_id: str) -> str:
    return json.dumps({"id": news_id, "content": "c", "published_at": _time.time()})


class _FakeWS:
    """An ``async with`` / ``async for`` stand-in for a websockets connection
    that yields the queued frames, then blocks until cancelled."""

    def __init__(self, frames: list[str | bytes]) -> None:
        self._frames = list(frames)

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def __aiter__(self) -> _FakeWS:
        return self

    async def __anext__(self) -> str | bytes:
        if self._frames:
            return self._frames.pop(0)
        await asyncio.Event().wait()
        raise StopAsyncIteration


@pytest.mark.parametrize("frame", [_HUGE_INT_FRAME, _DEEP_FRAME], ids=["huge_int", "deep"])
def test_default_parse_returns_none_on_any_parse_error(frame: str) -> None:
    assert default_parse(frame) is None


async def test_malformed_frames_are_dropped_and_the_stream_continues(monkeypatch) -> None:
    from openpoly.news import ws_client as wsc_mod

    frames: list[str | bytes] = [
        _HUGE_INT_FRAME,
        _DEEP_FRAME,
        _BAD_UTF8_FRAME,
        _good_frame("after"),
    ]
    monkeypatch.setattr(wsc_mod.websockets, "connect", lambda *a, **k: _FakeWS(frames))
    events: list[tuple[str, str | None]] = []
    buf = NewsRingBuffer(maxsize=10)
    client = NewsWSClient(
        endpoint="ws://fake",
        buffer=buf,
        ping_interval=None,
        on_event=lambda k, d: events.append((k, d)),
    )
    task = asyncio.create_task(client.run_forever())
    try:
        for _ in range(100):
            if len(buf) >= 1:
                break
            await asyncio.sleep(0.01)
        assert not task.done(), task.exception() if task.done() else None
    finally:
        client.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert [it.id for it in buf.snapshot()] == ["after"]
    assert [k for k, _ in events].count("parse_error") == 3


async def test_unexpected_loop_error_reconnects_and_updates_state(monkeypatch) -> None:
    from openpoly.news import ws_client as wsc_mod

    calls = 0

    def connect(*_a: object, **_k: object) -> _FakeWS:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return _FakeWS([_good_frame("second")])

    monkeypatch.setattr(wsc_mod.websockets, "connect", connect)
    events: list[tuple[str, str | None]] = []
    buf = NewsRingBuffer(maxsize=10)
    client = NewsWSClient(
        endpoint="ws://fake",
        buffer=buf,
        initial_backoff=0.01,
        ping_interval=None,
        on_event=lambda k, d: events.append((k, d)),
    )
    task = asyncio.create_task(client.run_forever())
    try:
        for _ in range(200):
            if len(buf) >= 1:
                break
            await asyncio.sleep(0.01)
    finally:
        client.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    kinds = [k for k, _ in events]
    assert kinds[:3] == ["connecting", "disconnected", "reconnect_attempt"]
    assert "boom" in (events[1][1] or "")
    assert [it.id for it in buf.snapshot()] == ["second"]


def test_manager_state_leaves_connected_after_an_unexpected_error() -> None:
    from openpoly.news.manager import NewsSourceManager

    mgr = NewsSourceManager()
    mgr._state = "connecting"
    mgr.record_event("connected", "ws://fake")
    # The client reports an unexpected loop error as a disconnect.
    mgr.record_event("disconnected", "RuntimeError('boom')")
    assert mgr.status().state == "connecting"

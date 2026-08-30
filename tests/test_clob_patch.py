"""Tests for openpoly.execution.clob_patch — the Cloudflare header patch.

The module monkey-patches the SDK's HTTP helper at import time and re-exports
the SDK surface the executor uses. Both halves are load-bearing and neither was
covered: a patch that silently stopped applying (an SDK refactor of
``http_helpers.request``) would fail only against live Cloudflare, and a
re-export that disappeared would fail only at live-executor construction.

No network: the transport is replaced with a recorder.
"""

from __future__ import annotations

import pytest
from py_clob_client_v2.http_helpers import helpers as v2_helpers

from openpoly.execution import clob_patch

BROWSER_UA_PREFIX = "Mozilla/5.0"


# ---------- the patch is installed ----------


def test_sdk_request_is_the_patched_callable() -> None:
    """Import order matters: everything else imports the SDK through this
    module precisely so the helper is already swapped."""
    assert v2_helpers.request is clob_patch._patched_request


# ---------- header injection ----------


class _Recorder:
    """Stands in for ``_orig_request`` — records what the patch handed down."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"ok": True}


def test_patch_injects_browser_headers(monkeypatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(clob_patch, "_orig_request", rec)

    out = v2_helpers.request(
        "https://clob.polymarket.com/order",
        "POST",
        {"Accept": "application/json"},
        {"body": 1},
    )

    assert out == {"ok": True}
    args, _kwargs = rec.calls[0]
    headers = args[2]
    assert headers["User-Agent"].startswith(BROWSER_UA_PREFIX)
    assert headers["Origin"] == "https://polymarket.com"
    assert headers["Referer"] == "https://polymarket.com/"
    # The caller's own headers survive, and the caller's dict is not mutated.
    assert headers["Accept"] == "application/json"
    assert args[0] == "https://clob.polymarket.com/order"
    assert args[1] == "POST"
    assert args[3] == {"body": 1}


def test_patch_does_not_override_caller_supplied_headers(monkeypatch) -> None:
    """``setdefault``, not assignment — a caller that deliberately sets one of
    these keeps it."""
    rec = _Recorder()
    monkeypatch.setattr(clob_patch, "_orig_request", rec)

    v2_helpers.request("https://x", "GET", {"Origin": "https://example.test"})

    assert rec.calls[0][0][2]["Origin"] == "https://example.test"


def test_patch_leaves_a_none_headers_slot_populated(monkeypatch) -> None:
    """The SDK passes ``headers=None`` on unauthenticated GETs; the patch must
    still fill the slot rather than skip injection."""
    rec = _Recorder()
    monkeypatch.setattr(clob_patch, "_orig_request", rec)

    v2_helpers.request("https://x", "GET", None)

    headers = rec.calls[0][0][2]
    assert headers["Origin"] == "https://polymarket.com"


def test_patch_passes_through_when_headers_are_not_positional(monkeypatch) -> None:
    """Guarded on arity so a future SDK call site passing headers by keyword is
    forwarded untouched instead of being silently mangled."""
    rec = _Recorder()
    monkeypatch.setattr(clob_patch, "_orig_request", rec)

    v2_helpers.request("https://x", "GET")

    args, _kwargs = rec.calls[0]
    assert args == ("https://x", "GET")


def test_browser_headers_reach_the_wire(monkeypatch) -> None:
    """End-to-end through the real SDK ``request``, with only the httpx client
    replaced: Origin / Referer are what actually get sent.

    ``User-Agent`` is deliberately NOT asserted here — the SDK's own
    ``_overload_headers`` assigns ``py_clob_client_v2`` over whatever the
    patch set, so the browser UA does not survive to the wire. See the module
    docstring of ``clob_patch``.
    """

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"ok": True}

    class _Client:
        def __init__(self) -> None:
            self.headers: dict | None = None

        def request(self, *, method, url, headers, params, **_kw):
            self.headers = headers
            return _Resp()

    client = _Client()
    monkeypatch.setattr(v2_helpers, "_http_client", client)

    out = v2_helpers.request("https://clob.polymarket.com/book", "GET", {})

    assert out == {"ok": True}
    assert client.headers is not None
    assert client.headers["Origin"] == "https://polymarket.com"
    assert client.headers["Referer"] == "https://polymarket.com/"


# ---------- re-exported SDK surface ----------


def test_reexports_cover_every_symbol_the_executor_imports() -> None:
    """live_executor imports these from clob_patch (never from the SDK
    directly) so the patch is guaranteed to be in place first."""
    expected = {
        "AssetType",
        "BalanceAllowanceParams",
        "ClobClient",
        "OrderArgs",
        "OrderPayload",
        "OrderType",
        "PartialCreateOrderOptions",
        "Side",
    }
    assert expected <= set(clob_patch.__all__)
    for name in expected:
        assert getattr(clob_patch, name, None) is not None


def test_reexported_types_are_constructible_as_the_executor_uses_them() -> None:
    """The exact call shapes in live_executor — a signature drift in the SDK
    should fail here, not on the first live order."""
    args = clob_patch.OrderArgs(
        token_id="t1",
        price=0.55,
        size=10.0,
        side=clob_patch.Side.BUY,
    )
    assert args.token_id == "t1"
    assert args.size == 10.0

    collateral = clob_patch.BalanceAllowanceParams(asset_type=clob_patch.AssetType.COLLATERAL)
    assert collateral.asset_type == "COLLATERAL"
    conditional = clob_patch.BalanceAllowanceParams(
        asset_type=clob_patch.AssetType.CONDITIONAL, token_id="t1"
    )
    assert conditional.token_id == "t1"

    assert clob_patch.OrderPayload(orderID="0xABC").orderID == "0xABC"
    assert clob_patch.PartialCreateOrderOptions(neg_risk=True).neg_risk is True
    assert str(clob_patch.OrderType.GTC).endswith("GTC")
    assert clob_patch.Side.SELL != clob_patch.Side.BUY
    assert callable(clob_patch.ClobClient)


def test_reexported_clob_client_exposes_the_methods_the_executor_calls() -> None:
    """No network: attribute presence on the class, not an instance."""
    for method in (
        "create_and_post_order",
        "update_balance_allowance",
        "get_balance_allowance",
        "cancel_order",
        "get_order",
        "derive_api_key",
        "set_api_creds",
        "get_address",
    ):
        assert callable(getattr(clob_patch.ClobClient, method, None)), method


def test_reexports_are_the_sdk_objects() -> None:
    import py_clob_client_v2

    assert clob_patch.ClobClient is py_clob_client_v2.ClobClient
    assert clob_patch.Side is py_clob_client_v2.Side


@pytest.mark.parametrize("name", ["OrderArgs", "OrderPayload"])
def test_reexported_dataclasses_reject_unknown_fields(name: str) -> None:
    """Guards against a silently renamed field: passing a stale kwarg must
    raise rather than be ignored."""
    with pytest.raises(TypeError):
        getattr(clob_patch, name)(definitely_not_a_field=1)

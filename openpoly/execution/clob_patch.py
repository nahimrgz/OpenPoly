"""Cloudflare-bypass patch for ``py_clob_client_v2`` HTTP transport.

Polymarket's CLOB sits behind Cloudflare bot protection. Without browser-like
headers the SDK's requests get challenged or 403'd. A prior project's production
verified this exact patch in 2026-05 (handoff doc pitfall #6). Import this
module BEFORE any other ``py_clob_client_v2`` import — the monkey-patch
must be in place before any SDK module wires its own reference to the
helper. Re-exports the SDK symbols callers need so a single import covers
both the patch and the client surface.

Known limitation (observed 08/30/2026, not changed): only ``Origin`` and
``Referer`` reach the wire. The SDK's own ``_overload_headers`` runs inside
``request`` and *assigns* ``User-Agent = "py_clob_client_v2"``, overwriting the
browser UA this patch sets beforehand. Live has been working against Cloudflare
on Origin/Referer alone, and the pattern above is the one verified in
production, so this is documented rather than "fixed" blind — forcing the UA
after ``_overload_headers`` would need a live re-test to justify.
"""

from __future__ import annotations

import logging

from py_clob_client_v2.http_helpers import helpers as _v2_helpers

logger = logging.getLogger(__name__)

_orig_request = _v2_helpers.request

# Once-guard for the unexpected-call-shape warning below: if an SDK bump ever
# changes the call convention, every request would otherwise emit one line.
_warned_shape_mismatch = False


def _patched_request(*args, **kwargs):
    # SDK calls request(method, url, headers, ...); inject browser headers
    # only when the headers slot is positionally present so we don't shadow
    # any future SDK call site that passes them differently.
    args = list(args)
    if len(args) >= 3:
        headers = dict(args[2] or {})
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        )
        headers.setdefault("Origin", "https://polymarket.com")
        headers.setdefault("Referer", "https://polymarket.com/")
        args[2] = headers
    else:
        # Never reached with the pinned SDK (every helper passes headers
        # positionally — tests/test_clob_patch.py pins both shapes). Reaching
        # it means an SDK bump changed the call convention and the Cloudflare
        # headers are NOT being injected: say so once instead of failing open
        # silently while live orders start 403ing.
        global _warned_shape_mismatch
        if not _warned_shape_mismatch:
            _warned_shape_mismatch = True
            logger.warning(
                "clob_patch: request() called without a positional headers "
                "slot — browser headers not injected (SDK call shape changed?)"
            )
    return _orig_request(*args, **kwargs)


_v2_helpers.request = _patched_request


# Re-export the SDK surface so callers don't risk importing the SDK without
# the patch in place.
from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, Side  # noqa: E402
from py_clob_client_v2.clob_types import (  # noqa: E402
    AssetType,
    BalanceAllowanceParams,
    OrderPayload,
    PartialCreateOrderOptions,
)

__all__ = [
    "AssetType",
    "BalanceAllowanceParams",
    "ClobClient",
    "OrderArgs",
    "OrderPayload",
    "OrderType",
    "PartialCreateOrderOptions",
    "Side",
]

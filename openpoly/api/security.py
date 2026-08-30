"""API hardening — shared-secret token on mutating routes + Host allowlist.

The backend is designed to bind loopback (see ``docs/deploy``), and that was
the whole of its access control: every route was open to anything that could
reach the socket. That is thinner than it looks. ``POST /api/system/mode``
flips paper→live, ``PUT /api/wallet/config`` repoints the signing key, and
``POST /api/secrets/local`` writes the secret store — so "anything that can
reach the socket" includes any other process on the host, anything sharing the
SSH tunnel, and any web page the operator has open that can be talked into
issuing a cross-origin request to ``127.0.0.1`` under a hostname that resolves
there (DNS rebinding).

Two independent guards, because they answer different questions:

* **Token** — *who is calling?* An optional shared secret in the
  ``X-OpenPoly-Token`` header, checked by a dependency on every route that
  mutates state (POST / PUT / DELETE / PATCH). Reads stay open: they expose no
  secret values and gating them would break the canvas' polling for no gain.
* **Host allowlist** — *what name did they use to get here?* A browser can be
  induced to send a request to a loopback service, but it cannot forge the
  ``Host`` header. Refusing every host that is not loopback or explicitly
  allowed is what makes the rebinding path a 421 instead of a live-mode switch.

Leaving the token unset keeps the local development workflow (backend on
127.0.0.1, Vite proxy in front of it) working exactly as before — but it is
logged loudly at startup, and it **hard-blocks the switch to live mode**: real
funds behind an unauthenticated endpoint is not a default anyone should be able
to reach by omission.

CORS is deliberately not configured here. The frontend is served through Vite's
proxy (same origin), so no cross-origin allowance is needed, and adding a
permissive one would hand back exactly what the Host allowlist just took away.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from openpoly.news.secrets import SecretsError, resolve

logger = logging.getLogger(__name__)

API_TOKEN_ENV = "OPENPOLY_API_TOKEN"
API_TOKEN_HEADER = "X-OpenPoly-Token"
ALLOWED_HOSTS_ENV = "OPENPOLY_ALLOWED_HOSTS"

# HTTP methods that change state. Everything here is guarded; GET / HEAD /
# OPTIONS are not.
MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# Always accepted, with or without the allowlist: these are the names the
# operator's own machine uses to reach a loopback-bound backend.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

# Prefixes that mark the value as a ``*_ref`` (see ``openpoly.news.secrets``)
# rather than the literal token. Anything else is used verbatim, so a literal
# token containing a colon still works.
_REF_SCHEMES = ("env:", "local:", "vault:", "keychain:")

_ALLOWLIST_WILDCARD = "*"

# One-shot guard for the startup warning: the lifespan may run more than once
# in a process (tests, reload) and the warning is meant to be seen, not spammed.
_warned_no_token = False


# ---------- token ----------


def _raw_token_setting() -> str:
    return os.environ.get(API_TOKEN_ENV, "").strip()


def api_token_configured() -> bool:
    """True when the operator has set a token — regardless of whether it
    currently resolves."""
    return bool(_raw_token_setting())


def resolve_api_token() -> str | None:
    """The expected token value, or None when unset, **unresolvable, or empty**.

    Callers must treat None as "deny" whenever ``api_token_configured()`` is
    True: a ref that stopped resolving (deleted secret, typo in the unit file)
    must not silently degrade into the open dev mode.

    An empty resolution is the same failure wearing a worse disguise. ``env:FOO``
    with ``FOO`` exported empty resolves to ``""`` without raising, and an empty
    expected token *matches an empty header* — every mutating route would open
    to any caller willing to send the header blank. Empty and whitespace-only
    values are therefore invalid, never usable secrets.
    """
    raw = _raw_token_setting()
    if not raw:
        return None
    if raw.startswith(_REF_SCHEMES):
        try:
            value = resolve(raw)
        except (SecretsError, NotImplementedError) as exc:
            logger.error(
                "%s is a secret ref that does not resolve (%s) — every mutating "
                "route will be refused until it does",
                API_TOKEN_ENV,
                exc,
            )
            return None
    else:
        value = raw
    if not value.strip():
        logger.error(
            "%s resolves to an empty value — every mutating route will be "
            "refused, and live mode with it, until it holds a real secret",
            API_TOKEN_ENV,
        )
        return None
    return value


def api_token_ok() -> bool:
    """True when a token is configured **and** currently usable.

    The one question both guards ask, so "configured" and "checkable" can never
    drift apart: the route dependency uses it to decide whether a request can be
    authenticated at all, and the live-mode gate uses it to decide whether real
    funds may go behind this API.
    """
    return api_token_configured() and resolve_api_token() is not None


def _tokens_match(supplied: str, expected: str) -> bool:
    """Constant-time compare of two token strings.

    Compared as UTF-8 bytes: ``hmac.compare_digest`` raises ``TypeError`` on
    ``str`` inputs holding non-ASCII code points, so a header with one accented
    character would otherwise be a 500 rather than a refusal — and would rule
    out a non-ASCII token entirely. Encoding never fails on ``str``.
    """
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def require_api_token(
    supplied: str | None = Header(default=None, alias=API_TOKEN_HEADER),
) -> None:
    """FastAPI dependency guarding one mutating route.

    No token configured → pass (loopback dev mode). Otherwise the header must
    be present and match, compared with ``hmac.compare_digest`` so a wrong
    token cannot be recovered a byte at a time from response timing. A token
    that is configured but does not resolve to a usable value denies everything
    — including the empty header that an empty expected value would accept.
    """
    if not api_token_configured():
        return
    expected = resolve_api_token()
    if expected is None or supplied is None or not _tokens_match(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_token",
                "message": f"missing or invalid {API_TOKEN_HEADER} header",
            },
        )


def log_startup_security_state() -> None:
    """Say once, at startup, whether the API is authenticated."""
    global _warned_no_token
    if api_token_configured():
        logger.info("API token configured — mutating routes require %s", API_TOKEN_HEADER)
        return
    if _warned_no_token:
        return
    _warned_no_token = True
    logger.warning(
        "%s is not set: every mutating API route is open to anything that can "
        "reach this socket. Fine for loopback development; set it before "
        "exposing the backend, and note that live mode is refused without it.",
        API_TOKEN_ENV,
    )


def reset_startup_warning_for_tests() -> None:
    """Test hook — re-arm the one-shot startup warning."""
    global _warned_no_token
    _warned_no_token = False


# ---------- Host allowlist ----------


def _hostname(raw: str) -> str:
    """Host header → bare hostname, port stripped, lowercased.

    IPv6 literals arrive bracketed (``[::1]:8000``); the brackets are kept so
    the value matches how the allowlist and ``LOOPBACK_HOSTS`` spell it.
    """
    value = raw.strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    if value.count(":") > 1:
        # Bare (unbracketed) IPv6 literal — no port to strip.
        return value
    return value.split(":", 1)[0]


def allowed_hosts() -> set[str]:
    """Loopback plus whatever ``OPENPOLY_ALLOWED_HOSTS`` adds (comma-separated).

    Read per request rather than cached: the value is a deployment knob, and a
    cache here would mean a restart to fix a lockout.
    """
    extra = os.environ.get(ALLOWED_HOSTS_ENV, "")
    parsed = {part.strip().lower() for part in extra.split(",") if part.strip()}
    return set(LOOPBACK_HOSTS) | parsed


def host_allowed(raw_host: str) -> bool:
    """Whether a request carrying this ``Host`` header may proceed.

    An absent/empty Host is allowed: it carries no name to misdirect through
    (HTTP/1.1 requires one, so in practice this is only reached by a local
    HTTP/1.0 client).
    """
    if _ALLOWLIST_WILDCARD in {p.strip() for p in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",")}:
        return True
    host = _hostname(raw_host)
    if not host:
        return True
    return host in allowed_hosts()


class HostAllowlistMiddleware:
    """Reject requests whose ``Host`` is neither loopback nor allowlisted.

    A plain ASGI middleware rather than ``BaseHTTPMiddleware``: it has to run
    before anything else touches the request, and it never needs the body.
    Answers **421 Misdirected Request** — the status that means "this server is
    not the one for that authority", which is precisely the situation.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        raw_host = Headers(scope=scope).get("host", "")
        if host_allowed(raw_host):
            await self.app(scope, receive, send)
            return
        logger.warning("rejected request with disallowed Host header: %r", raw_host[:100])
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {
                "error": "host_not_allowed",
                "message": (
                    f"Host {_hostname(raw_host)!r} is not allowed; add it to "
                    f"{ALLOWED_HOSTS_ENV} if this backend is meant to serve it"
                ),
            },
            status_code=421,
        )
        await response(scope, receive, send)

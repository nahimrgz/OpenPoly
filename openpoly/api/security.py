"""API hardening — shared-secret token, Host allowlist, cross-origin write guard.

The backend is designed to bind loopback (see ``docs/deploy``), and that was
the whole of its access control: every route was open to anything that could
reach the socket. That is thinner than it looks. ``POST /api/system/mode``
flips paper→live, ``PUT /api/wallet/config`` repoints the signing key, and
``POST /api/secrets/local`` writes the secret store — so "anything that can
reach the socket" includes any other process on the host, anything sharing the
SSH tunnel, and any web page the operator has open that can be talked into
issuing a cross-origin request to ``127.0.0.1`` under a hostname that resolves
there (DNS rebinding).

Three independent guards, because they answer different questions:

* **Token** — *who is calling?* An optional shared secret in the
  ``X-OpenPoly-Token`` header, checked by a dependency on every route that
  mutates state (POST / PUT / DELETE / PATCH). Reads stay open: they expose no
  secret values and gating them would break the canvas' polling for no gain.
* **Host allowlist** — *what name did they use to get here?* A browser can be
  induced to send a request to a loopback service, but it cannot forge the
  ``Host`` header. Refusing every host that is not loopback or explicitly
  allowed is what makes the rebinding path a 421 instead of a live-mode switch.
* **Cross-origin write guard** — *whose page issued this?* The Host allowlist
  admits loopback by design, and a body-less ``POST`` is a CORS *simple
  request*: the browser sends it, and only refuses the caller sight of the
  *response*. ``fetch('http://127.0.0.1:8000/api/positions/close-all',
  {method:'POST'})`` from any page the operator has open therefore reaches the
  route and bulk-closes the book, with the token unset (the loopback default)
  or — worse — with it set, because a browser attaches no header it was not
  asked to and the request still runs if the token is unset. The response never
  reaching the attacker does not undo the sell. So every mutating request that
  a browser labels cross-origin is refused before it reaches a route:
  ``Sec-Fetch-Site: cross-site`` is refused outright without consulting the
  ``Origin``; ``same-origin`` and ``none`` pass; ``same-site`` and any value
  this module does not know settle nothing and fall through to the ``Origin``,
  which must name the same authority — host **and** port — as the request's own
  ``Host``, or be listed in ``OPENPOLY_ALLOWED_HOSTS``. Loopback is *not*
  admitted port-free on that fall-through: it is the one authority every local
  page shares, so a free pass there is what would let ``localhost:3000`` drive
  ``localhost:8000``. See ``cross_origin_write_refusal`` for the exact order.

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

from openpoly.news.secrets import SecretsError, is_secret_ref, resolve

logger = logging.getLogger(__name__)

API_TOKEN_ENV = "OPENPOLY_API_TOKEN"
API_TOKEN_HEADER = "X-OpenPoly-Token"
ALLOWED_HOSTS_ENV = "OPENPOLY_ALLOWED_HOSTS"

# Fetch-metadata + Origin, the two headers a browser attaches to a cross-origin
# write. Lower-case because Starlette's ``Headers`` lookup is case-insensitive
# but the constants are also used in log lines and tests.
SEC_FETCH_SITE_HEADER = "sec-fetch-site"
SEC_FETCH_SITE_CROSS = "cross-site"
ORIGIN_HEADER = "origin"

# The only ``Sec-Fetch-Site`` values that name *this* origin. ``same-site`` is
# deliberately absent: it means "same registrable domain", which every
# ``localhost:<port>`` shares with every other, so it vouches for nothing a
# mutating request can be trusted on. It falls through to the Origin check.
SEC_FETCH_SITE_SELF = frozenset({"same-origin", "none"})

# Ports implied by a scheme, so ``http://host`` and ``Host: host`` compare equal.
_DEFAULT_PORTS = {"http": "80", "https": "443"}
_IMPLIED_PORTS = frozenset(_DEFAULT_PORTS.values())

# HTTP methods that change state. Everything here is guarded; GET / HEAD /
# OPTIONS are not.
MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# Always accepted, with or without the allowlist: these are the names the
# operator's own machine uses to reach a loopback-bound backend.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

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
    # Ref-vs-literal is the resolver's own call (openpoly.news.secrets):
    # a scheme known there but not here would have turned the ref into the
    # literal expected token — every request 401s, with no "does not
    # resolve" log to explain why.
    if is_secret_ref(raw):
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


# ---------- cross-origin write guard ----------


def _split_authority(raw: str) -> tuple[str, str | None] | None:
    """``host[:port]`` → ``(hostname, port or None)``, or ``None`` for no host.

    Shares ``_hostname``'s spelling of an IPv6 literal, so ``[::1]:8000`` splits
    into ``("[::1]", "8000")`` and a bare ``::1`` keeps all of its colons.
    """
    value = raw.strip().lower()
    host = _hostname(value)
    if not host:
        return None
    rest = value[len(host) :]
    if rest.startswith(":"):
        port = rest[1:]
        return (host, port) if port else (host, None)
    return host, None


def _origin_authority(raw_origin: str) -> tuple[str, str | None] | None:
    """``Origin`` header → ``(hostname, port)``, the port filled in from the
    scheme when the header omits it. ``None`` when the value names no origin.

    ``null`` — what a sandboxed iframe, a ``data:`` document and some redirect
    chains send — and any syntactically broken value both yield ``None``. The
    header was present and does not name an origin this backend serves, which
    is a refusal; treating it as "no Origin at all" would make the sandbox the
    way around the guard.
    """
    value = raw_origin.strip()
    if not value or value.lower() == "null":
        return None
    scheme, sep, rest = value.partition("://")
    if not sep:
        return None
    parts = _split_authority(rest.split("/", 1)[0])
    if parts is None:
        return None
    host, port = parts
    return host, port if port is not None else _DEFAULT_PORTS.get(scheme.strip().lower())


def _same_authority(origin_parts: tuple[str, str | None], raw_host: str) -> bool:
    """Whether an Origin and the request's ``Host`` name the same authority.

    Host and port both, which is the whole point: ``localhost:3000`` and
    ``localhost:8000`` are one *site* but two *origins*, and only the origin
    boundary is the one a page cannot cross.
    """
    host_parts = _split_authority(raw_host)
    if host_parts is None:
        return False
    if origin_parts[0] != host_parts[0]:
        return False
    if host_parts[1] is None:
        # No port on the Host header: the request arrived on the scheme's
        # default port, so an Origin naming that default is the same origin.
        return origin_parts[1] is None or origin_parts[1] in _IMPLIED_PORTS
    return origin_parts[1] == host_parts[1]


def _origin_allowlisted(origin_parts: tuple[str, str | None]) -> bool:
    """Whether ``OPENPOLY_ALLOWED_HOSTS`` names this origin explicitly.

    Only the operator's own entries count — loopback is *not* implied here the
    way it is for the ``Host`` check. Loopback is the one authority every local
    page shares, so admitting it by default is what let a page on
    ``localhost:3000`` write to ``localhost:8000``. An entry may be a bare host
    (any port of it) or ``host:port`` (that origin only).
    """
    entries = {part.strip().lower() for part in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",")}
    if _ALLOWLIST_WILDCARD in entries:
        return True
    host, port = origin_parts
    if host in entries:
        return True
    return port is not None and f"{host}:{port}" in entries


def cross_origin_write_refusal(method: str, headers: Headers) -> str | None:
    """Why this mutating request is refused, or ``None`` when it may proceed.

    Read the two headers in the order of how much they know:

    1. ``Sec-Fetch-Site`` is set by the browser and cannot be set by page
       script. ``cross-site`` is the browser saying, unforgeably, that the
       initiator was another site — refuse, without consulting the Origin.
       ``same-origin`` and ``none`` (a typed-in URL) name this very origin and
       settle the question the other way. ``same-site`` settles nothing: it
       only says the registrable domain matches, and *every* ``localhost:<port>``
       is same-site with every other, so a page on a dev server one port over
       would otherwise drive this backend. It — and any value this code does not
       know — falls through to the Origin check below. Failing open on an
       unknown label would make the next header value the browsers add a hole.
    2. ``Origin`` decides the fall-through and is the only signal older clients
       send. It passes when it names the same authority (host **and** port,
       with a scheme's default port implied) as the request's own ``Host``, or
       when the operator listed it in ``OPENPOLY_ALLOWED_HOSTS``.

    Neither header present → pass. That is ``curl``, a systemd timer, a Python
    client, an old browser — none of which carry a hostile page's authority.
    The guard's job is to stop a *browser* being used as a confused deputy, and
    a browser always sends at least one of the two on a cross-origin write.

    The reason is returned rather than a bare bool because the two refusals are
    fixed differently, and a 403 that names the wrong knob sends the operator
    to edit a setting that would not have changed the answer.
    """
    if method.upper() not in MUTATING_METHODS:
        return None
    site = headers.get(SEC_FETCH_SITE_HEADER)
    if site is not None:
        value = site.strip().lower()
        if value == SEC_FETCH_SITE_CROSS:
            return (
                "this browser labelled the request cross-site "
                "(Sec-Fetch-Site: cross-site): a page on another site may not "
                "issue state changes here. Drive this backend from its own UI, "
                "or from a client that is not a browser."
            )
        if value in SEC_FETCH_SITE_SELF:
            return None
    origin = headers.get(ORIGIN_HEADER)
    if origin is None:
        return None
    parts = _origin_authority(origin)
    if parts is not None and (
        _same_authority(parts, headers.get("host", "")) or _origin_allowlisted(parts)
    ):
        return None
    return (
        "mutating requests must come from this backend's own origin (host and "
        f"port both). Origin {origin.strip()[:100]!r} does not match Host "
        f"{headers.get('host', '')[:100]!r}; add that origin's host — or "
        f"host:port — to {ALLOWED_HOSTS_ENV} if it is meant to drive this "
        "backend."
    )


def cross_origin_write(method: str, headers: Headers) -> bool:
    """Whether this request is a state change issued from another origin."""
    return cross_origin_write_refusal(method, headers) is not None


class HostAllowlistMiddleware:
    """Reject a request whose ``Host`` is neither loopback nor allowlisted, and
    any mutating request a browser labels cross-origin.

    A plain ASGI middleware rather than ``BaseHTTPMiddleware``: it has to run
    before anything else touches the request, and it never needs the body.

    Two different refusals, because they are two different mistakes.
    **421 Misdirected Request** means "this server is not the one for that
    authority" — the DNS-rebinding shape. **403 Forbidden** means "this server
    is the right one, but that page may not write to it" — the cross-site
    ``fetch`` shape, which reaches a correct Host by design.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        raw_host = headers.get("host", "")
        if not host_allowed(raw_host):
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
            return
        if scope["type"] == "http":
            refusal = cross_origin_write_refusal(scope.get("method", ""), headers)
            if refusal is not None:
                logger.warning(
                    "rejected cross-origin %s %s (Origin=%r, Sec-Fetch-Site=%r)",
                    scope.get("method", ""),
                    scope.get("path", ""),
                    headers.get(ORIGIN_HEADER, "")[:100],
                    headers.get(SEC_FETCH_SITE_HEADER, "")[:40],
                )
                # The body carries the reason for *this* decision: the two
                # refusal paths are fixed differently, and the allowlist cannot
                # undo a cross-site label.
                response = JSONResponse(
                    {"error": "cross_origin_write", "message": refusal},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

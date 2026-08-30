"""API hardening — shared-secret token on mutating routes + Host allowlist.

The backend binds loopback and holds a wallet private-key ref, a secret store,
and a paper→live switch behind routes that had no authentication at all. These
tests pin both halves of the fix: nothing that mutates state is reachable
without the configured token, and nothing is reachable under a Host the
operator did not allow (the DNS-rebinding shape against a loopback service).
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from openpoly.api.main import app
from openpoly.api.security import (
    ALLOWED_HOSTS_ENV,
    API_TOKEN_ENV,
    API_TOKEN_HEADER,
    MUTATING_METHODS,
    log_startup_security_state,
    require_api_token,
    reset_startup_warning_for_tests,
)

TOKEN = "s3cret-token"

# A mutating route with no side effect worth isolating: stopping an already
# stopped market source is a no-op that still proves the dependency ran.
MUTATING_PATH = "/api/market/source/stop"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    reset_startup_warning_for_tests()
    yield
    reset_startup_warning_for_tests()


# ---------- token on mutating routes ----------


def test_mutating_route_rejected_without_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    r = client.post(MUTATING_PATH)
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_api_token"


def test_mutating_route_rejected_with_wrong_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    r = client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: "nope"})
    assert r.status_code == 401


def test_mutating_route_passes_with_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    r = client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: TOKEN})
    assert r.status_code == 200


def test_mutating_route_open_when_no_token_configured(client: TestClient) -> None:
    """Loopback dev mode: an unset token keeps the local workflow working."""
    r = client.post(MUTATING_PATH)
    assert r.status_code == 200


def test_get_routes_are_unaffected_by_the_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/market/source/status").status_code == 200


def test_token_may_be_a_secret_ref(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The value follows the same ``*_ref`` indirection as every other secret,
    so the token never has to sit in the systemd unit's env in plaintext."""
    monkeypatch.setenv("OPENPOLY_TEST_TOKEN_HOLDER", TOKEN)
    monkeypatch.setenv(API_TOKEN_ENV, "env:OPENPOLY_TEST_TOKEN_HOLDER")
    assert client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: TOKEN}).status_code == 200
    assert client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: "env:x"}).status_code == 401


def test_unresolvable_token_ref_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured-but-unresolvable token must deny, never fall back to the
    open dev mode — that would turn a typo into a silently unauthenticated
    backend."""
    monkeypatch.delenv("OPENPOLY_MISSING_TOKEN", raising=False)
    monkeypatch.setenv(API_TOKEN_ENV, "env:OPENPOLY_MISSING_TOKEN")
    assert client.post(MUTATING_PATH).status_code == 401
    assert client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: TOKEN}).status_code == 401


def test_blank_token_env_is_treated_as_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, "   ")
    assert client.post(MUTATING_PATH).status_code == 200


def test_every_mutating_route_declares_the_token_dependency() -> None:
    """The guard is per-route, so a route added later without it is a hole.
    This test is what closes that: it enumerates the app, not a hand-list."""
    missing = [
        f"{sorted(route.methods & MUTATING_METHODS)} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.methods & MUTATING_METHODS
        and not any(d.dependency is require_api_token for d in route.dependencies)
    ]
    assert missing == []


# ---------- startup warning ----------


def test_startup_warns_once_when_no_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="openpoly.api.security"):
        log_startup_security_state()
        log_startup_security_state()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert API_TOKEN_ENV in warnings[0].getMessage()


def test_startup_does_not_warn_when_token_configured(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    with caplog.at_level(logging.WARNING, logger="openpoly.api.security"):
        log_startup_security_state()
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


# ---------- live mode needs a token ----------


def test_live_mode_refused_without_token(client: TestClient) -> None:
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "api_token_required"


def test_live_mode_not_refused_for_that_reason_with_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a token configured the live switch reaches its real preflight —
    it still fails on the unconfigured wallet, but never on the token."""
    from openpoly.api.portfolio_routes import get_portfolio_store

    class _NoPositions:
        def get_open_positions(self):
            return []

    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    app.dependency_overrides[get_portfolio_store] = _NoPositions
    try:
        r = client.post(
            "/api/system/mode",
            json={"mode": "live"},
            headers={API_TOKEN_HEADER: TOKEN},
        )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code != 403
    assert r.json()["detail"]["error"] == "wallet_not_configured"


def test_paper_mode_switch_is_not_gated(client: TestClient) -> None:
    r = client.post("/api/system/mode", json={"mode": "paper"})
    assert r.status_code == 200


# ---------- Host allowlist ----------


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "localhost:8000"])
def test_loopback_hosts_are_always_allowed(host: str) -> None:
    r = TestClient(app, base_url=f"http://{host}").get("/api/health")
    assert r.status_code == 200


@pytest.mark.parametrize(
    "host", ["localhost", "LOCALHOST", "127.0.0.1", "[::1]", "[::1]:18000", "::1", ""]
)
def test_loopback_host_headers_pass_the_check(host: str) -> None:
    """Checked at the predicate: Starlette's TestClient cannot build a URL for
    a bracketed IPv6 authority, so the header shapes are asserted directly."""
    from openpoly.api.security import host_allowed

    assert host_allowed(host) is True


def test_unknown_host_is_rejected() -> None:
    r = TestClient(app, base_url="http://evil.example.com").get("/api/health")
    assert r.status_code == 421
    assert r.json()["error"] == "host_not_allowed"


def test_allowlist_env_admits_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "openpoly.example.com, other.example.com")
    r = TestClient(app, base_url="http://openpoly.example.com").get("/api/health")
    assert r.status_code == 200
    r = TestClient(app, base_url="http://third.example.com").get("/api/health")
    assert r.status_code == 421


def test_allowlist_wildcard_admits_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "*")
    r = TestClient(app, base_url="http://anything.example.com").get("/api/health")
    assert r.status_code == 200


def test_host_check_applies_to_mutating_routes_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    r = TestClient(app, base_url="http://evil.example.com").post(MUTATING_PATH)
    assert r.status_code == 421


def test_port_is_ignored_when_matching_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "openpoly.example.com")
    r = TestClient(app, base_url="http://openpoly.example.com:18000").get("/api/health")
    assert r.status_code == 200


# ---------- a configured token that resolves to nothing must fail closed ----------

# Holder for the "set but empty" shape: an env var that exists with an empty
# value resolves without raising, so it reaches the comparison as "" — and an
# empty expected value matches an empty supplied header.
EMPTY_HOLDER = "OPENPOLY_TEST_EMPTY_TOKEN_HOLDER"


def test_token_ref_resolving_to_empty_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env:FOO`` with FOO set-but-empty is a broken deployment, not an open
    one: an empty expected token would match an empty supplied header and let
    every caller through."""
    monkeypatch.setenv(EMPTY_HOLDER, "")
    monkeypatch.setenv(API_TOKEN_ENV, f"env:{EMPTY_HOLDER}")
    assert client.post(MUTATING_PATH).status_code == 401
    assert client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: ""}).status_code == 401
    assert client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: TOKEN}).status_code == 401


def test_token_ref_resolving_to_whitespace_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EMPTY_HOLDER, "   ")
    monkeypatch.setenv(API_TOKEN_ENV, f"env:{EMPTY_HOLDER}")
    assert client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: "   "}).status_code == 401


def test_resolve_api_token_returns_none_for_an_empty_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openpoly.api.security import resolve_api_token

    monkeypatch.setenv(EMPTY_HOLDER, "")
    monkeypatch.setenv(API_TOKEN_ENV, f"env:{EMPTY_HOLDER}")
    assert resolve_api_token() is None


def test_api_token_ok_distinguishes_unset_from_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``api_token_ok`` is the single question both the route guard and the
    live-mode gate ask: is there a token that can actually be checked?"""
    from openpoly.api.security import api_token_ok

    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    assert api_token_ok() is False
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    assert api_token_ok() is True
    monkeypatch.setenv(EMPTY_HOLDER, "")
    monkeypatch.setenv(API_TOKEN_ENV, f"env:{EMPTY_HOLDER}")
    assert api_token_ok() is False
    monkeypatch.delenv("OPENPOLY_MISSING_TOKEN", raising=False)
    monkeypatch.setenv(API_TOKEN_ENV, "env:OPENPOLY_MISSING_TOKEN")
    assert api_token_ok() is False


def test_live_mode_refused_when_the_token_resolves_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live switch must treat an unusable token as no token at all."""
    monkeypatch.setenv(EMPTY_HOLDER, "")
    monkeypatch.setenv(API_TOKEN_ENV, f"env:{EMPTY_HOLDER}")
    r = client.post("/api/system/mode", json={"mode": "live"}, headers={API_TOKEN_HEADER: ""})
    assert r.status_code in (401, 403)


# ---------- non-ASCII tokens / headers ----------

# All code points below U+0100, so the value survives the latin-1 round trip
# HTTP header values use on the wire.
NON_ASCII_TOKEN = "s3cret-tökén"


def test_non_ascii_header_is_refused_not_a_server_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hmac.compare_digest`` rejects non-ASCII ``str`` inputs; comparing the
    encoded bytes keeps a hostile header a 401 instead of a 500."""
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    r = client.post(MUTATING_PATH, headers={API_TOKEN_HEADER: "tökén".encode("utf-8")})
    assert r.status_code == 401


def test_non_ascii_token_matches_an_identical_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ASCII token still authenticates — the guard must refuse the wrong
    value, not every value it cannot compare.

    Asserted against the dependency rather than through the test client: HTTP
    header values are latin-1 on the wire and the client transport re-encodes
    them as UTF-8, so an end-to-end version would be pinning that transcoding
    instead of the comparison under test.
    """
    monkeypatch.setenv(API_TOKEN_ENV, NON_ASCII_TOKEN)
    require_api_token(supplied=NON_ASCII_TOKEN)  # must not raise
    with pytest.raises(HTTPException) as excinfo:
        require_api_token(supplied="s3cret-tökèn")
    assert excinfo.value.status_code == 401


# ---------- a restored live mode is re-checked at startup ----------


def _write_runtime(path, mode: str) -> None:
    path.write_text(json.dumps({"wallet": None, "exec_mode": mode, "updated_at": 1.0}))


class _StubEmbeddingManager:
    """Stand-in for the process-wide embedding manager during the lifespan.

    Its warm loop is the one long-lived task in startup whose stop Event is
    created once per process rather than per start, so driving the real one
    from a test's event loop leaves a landmine for the next lifespan test.
    Nothing here is under test, so it is replaced outright.
    """

    async def start(self, **_kwargs: object) -> None:
        return None

    async def stop(self) -> None:
        return None


async def test_startup_forces_paper_when_restored_live_has_no_usable_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """runtime.json survives the deployment that lost its token: restoring
    ``live`` behind an unauthenticated API would spend real funds for anything
    that can reach the socket."""
    import openpoly.api.main as main_mod
    from openpoly.wallet.runtime_state import RuntimeState

    path = tmp_path / "runtime.json"
    _write_runtime(path, "live")
    rs = RuntimeState(path)
    monkeypatch.setattr(main_mod, "runtime_state", rs)
    monkeypatch.setattr(main_mod, "embedding_manager", _StubEmbeddingManager())
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)

    with caplog.at_level(logging.ERROR, logger="openpoly.api.main"):
        async with main_mod.lifespan(app):
            pass

    assert rs.exec_mode == "paper"
    assert json.loads(path.read_text())["exec_mode"] == "paper"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] != []


async def test_startup_forces_paper_in_memory_when_runtime_json_is_unwritable(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The demotion must fail CLOSED. If the state file cannot be rewritten the
    process still must not route orders through the live executor, so the
    in-memory mode is forced to paper regardless of persistence. Disk keeps
    saying live, which only means the same demotion re-runs next boot."""
    import openpoly.api.main as main_mod
    from openpoly.wallet.runtime_state import RuntimeState

    path = tmp_path / "runtime.json"
    _write_runtime(path, "live")
    rs = RuntimeState(path)

    def boom(self: RuntimeState) -> None:
        raise OSError("simulated read-only state directory")

    monkeypatch.setattr(RuntimeState, "_save", boom)
    monkeypatch.setattr(main_mod, "runtime_state", rs)
    monkeypatch.setattr(main_mod, "embedding_manager", _StubEmbeddingManager())
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)

    with caplog.at_level(logging.CRITICAL, logger="openpoly.api.main"):
        async with main_mod.lifespan(app):
            pass

    assert rs.exec_mode == "paper"
    assert json.loads(path.read_text())["exec_mode"] == "live"
    assert [r for r in caplog.records if r.levelno >= logging.CRITICAL] != []


def test_restored_live_survives_a_configured_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard demotes an unauthenticated live restore, not every live one."""
    import openpoly.api.main as main_mod
    from openpoly.wallet.runtime_state import RuntimeState

    path = tmp_path / "runtime.json"
    _write_runtime(path, "live")
    rs = RuntimeState(path)
    rs.load()
    monkeypatch.setattr(main_mod, "runtime_state", rs)
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)

    main_mod._demote_restored_live_without_token()

    assert rs.exec_mode == "live"
    assert json.loads(path.read_text())["exec_mode"] == "live"


def test_restored_live_is_demoted_when_the_token_ref_resolves_empty(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openpoly.api.main as main_mod
    from openpoly.wallet.runtime_state import RuntimeState

    path = tmp_path / "runtime.json"
    _write_runtime(path, "live")
    rs = RuntimeState(path)
    rs.load()
    monkeypatch.setattr(main_mod, "runtime_state", rs)
    monkeypatch.setenv(EMPTY_HOLDER, "")
    monkeypatch.setenv(API_TOKEN_ENV, f"env:{EMPTY_HOLDER}")

    main_mod._demote_restored_live_without_token()

    assert rs.exec_mode == "paper"
    assert json.loads(path.read_text())["exec_mode"] == "paper"


def test_restored_paper_is_left_alone(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import openpoly.api.main as main_mod
    from openpoly.wallet.runtime_state import RuntimeState

    path = tmp_path / "runtime.json"
    _write_runtime(path, "paper")
    rs = RuntimeState(path)
    rs.load()
    monkeypatch.setattr(main_mod, "runtime_state", rs)
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)

    main_mod._demote_restored_live_without_token()

    assert rs.exec_mode == "paper"

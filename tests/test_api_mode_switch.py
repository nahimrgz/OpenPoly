"""Endpoint tests for POST /api/system/mode (Polymarket V2 DepositWallet)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import openpoly.api.wallet_routes as wallet_routes
from openpoly.api.main import app
from openpoly.api.portfolio_routes import get_portfolio_store
from openpoly.api.security import API_TOKEN_HEADER
from openpoly.db.engine import init_db, make_engine, make_session_factory
from openpoly.portfolio import PortfolioStore
from openpoly.wallet.runtime_state import RuntimeState, WalletSpec

# Anvil's deterministic dev key #0 — public, well-known, safe to bake into tests.
TEST_PRIVKEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_FUNDER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

# V2 exchange addresses — must match the preflight thresholds in wallet_routes.
STANDARD_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEGRISK_V2 = "0xe2222d279d744050d28e00520010520000310F59"
MAX_UINT = str(2**256 - 1)

# Shared secret the fixture configures so the live switch clears its token gate.
TEST_API_TOKEN = "mode-switch-test-token"


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, PortfolioStore, RuntimeState]:
    monkeypatch.setenv("OPENPOLY_RUNTIME_STATE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("OPENPOLY_POLYMARKET_PK", TEST_PRIVKEY)
    # Live mode is refused outright while the API has no shared secret (see
    # openpoly.api.security). These tests are about the *wallet* preflight, so
    # the token gate is satisfied here; the gate itself is covered in
    # tests/test_api_security.py. The header is not needed on top of it — the
    # per-route dependency compares only when a token is configured, and the
    # client below sends it.
    monkeypatch.setenv("OPENPOLY_API_TOKEN", TEST_API_TOKEN)

    engine = make_engine(f"sqlite:///{tmp_path}/portfolio.db")
    init_db(engine)
    store = PortfolioStore(make_session_factory(engine))
    app.dependency_overrides[get_portfolio_store] = lambda: store

    rs = RuntimeState()
    rs.load()
    monkeypatch.setattr(wallet_routes, "runtime_state", rs)

    yield (
        TestClient(app, headers={API_TOKEN_HEADER: TEST_API_TOKEN}),
        store,
        rs,
    )

    app.dependency_overrides.clear()
    engine.dispose()


def _open_position(store: PortfolioStore) -> None:
    store.open_position(
        market_id="m1",
        side="yes",
        token_id="t1",
        condition_id="0xm1",
        price=0.40,
        qty=10.0,
        ts=100.0,
        news_id="n1",
    )


def _set_wallet(rs: RuntimeState) -> None:
    rs.set_wallet(
        WalletSpec(
            private_key_ref="env:OPENPOLY_POLYMARKET_PK",
            funder_address=TEST_FUNDER,
        )
    )


def _patch_build_with_fake(monkeypatch: pytest.MonkeyPatch, fake_clob) -> None:
    """Replace build_live_executor with a stub returning a LiveExecutor wrapping fake_clob."""
    from openpoly.execution.live_executor import LiveExecutor

    def fake_build(wallet, portfolio):
        return LiveExecutor(portfolio=portfolio, clob_client=fake_clob)

    monkeypatch.setattr(wallet_routes, "build_live_executor", fake_build)


def _allowance_dict(*, standard: str = MAX_UINT, negrisk: str = MAX_UINT) -> dict:
    return {STANDARD_V2: standard, NEGRISK_V2: negrisk}


def test_short_circuit_when_already_target(env) -> None:
    client, _store, _rs = env
    r = client.post("/api/system/mode", json={"mode": "paper"})
    assert r.status_code == 200
    assert r.json() == {"mode": "paper"}


def test_unknown_mode_returns_422(env) -> None:
    client, _store, _rs = env
    r = client.post("/api/system/mode", json={"mode": "yolo"})
    assert r.status_code == 422  # pydantic validation


def test_paper_to_live_blocked_by_open_positions(env) -> None:
    client, store, rs = env
    _set_wallet(rs)
    _open_position(store)
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["error"] == "open_positions"
    assert body["count"] == 1


def test_paper_to_live_blocked_without_wallet(env) -> None:
    client, _store, _rs = env
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "wallet_not_configured"


def test_paper_to_live_blocked_when_secret_missing(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, rs = env
    _set_wallet(rs)
    monkeypatch.delenv("OPENPOLY_POLYMARKET_PK", raising=False)
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "wallet_secret_missing"


def test_paper_to_live_blocked_when_private_key_invalid(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, rs = env
    _set_wallet(rs)
    monkeypatch.setenv("OPENPOLY_POLYMARKET_PK", "not-a-valid-private-key")
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "bad_private_key"


def test_paper_to_live_success_when_funded_and_approved(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, rs = env
    _set_wallet(rs)

    class _ClobOK:
        def get_balance_allowance(self, params):
            return {"balance": "5000000", "allowances": _allowance_dict()}

    _patch_build_with_fake(monkeypatch, _ClobOK())
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 200, r.text
    assert r.json() == {"mode": "live"}
    assert rs.exec_mode == "live"


def test_live_to_paper_blocked_by_open_positions(env) -> None:
    client, store, rs = env
    _set_wallet(rs)
    rs.set_mode("live")
    _open_position(store)
    r = client.post("/api/system/mode", json={"mode": "paper"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "open_positions"


def test_live_to_paper_success_persists(env) -> None:
    client, _store, rs = env
    _set_wallet(rs)
    rs.set_mode("live")
    assert rs.exec_mode == "live"

    r = client.post("/api/system/mode", json={"mode": "paper"})
    assert r.status_code == 200, r.text
    assert r.json() == {"mode": "paper"}
    assert rs.exec_mode == "paper"


def test_switch_to_live_blocked_by_pusd_insufficient(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, rs = env
    _set_wallet(rs)

    class _ClobLowBalance:
        def get_balance_allowance(self, params):
            return {"balance": "500000", "allowances": _allowance_dict()}  # 0.5 pUSD

    _patch_build_with_fake(monkeypatch, _ClobLowBalance())
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "pusd_insufficient"


def test_switch_to_live_blocked_by_standard_v2_not_approved(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, rs = env
    _set_wallet(rs)

    class _ClobStdMissing:
        def get_balance_allowance(self, params):
            return {
                "balance": "5000000",
                "allowances": _allowance_dict(standard="0"),
            }

    _patch_build_with_fake(monkeypatch, _ClobStdMissing())
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "standard_v2_not_approved"


def test_switch_to_live_blocked_by_negrisk_v2_not_approved(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, rs = env
    _set_wallet(rs)

    class _ClobNegMissing:
        def get_balance_allowance(self, params):
            return {
                "balance": "5000000",
                "allowances": _allowance_dict(negrisk="0"),
            }

    _patch_build_with_fake(monkeypatch, _ClobNegMissing())
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "negrisk_v2_not_approved"


def test_switch_to_live_blocked_by_rpc_unreachable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, rs = env
    _set_wallet(rs)

    class _ClobBlowsUp:
        def get_balance_allowance(self, params):
            raise ConnectionError("clob.polymarket.com unreachable")

    _patch_build_with_fake(monkeypatch, _ClobBlowsUp())
    r = client.post("/api/system/mode", json={"mode": "live"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "rpc_unreachable"


# ---------- the switch must hand the executor to the dispatcher ----------


class _FakeClob:
    """Only the one call the live preflight makes."""

    def get_balance_allowance(self, params):  # noqa: ANN001, ANN201
        return {"balance": "5000000", "allowances": _allowance_dict()}


class _RecordingLiveExecutor:
    """Stand-in for ``build_live_executor``'s return value.

    The route touches exactly two things on it: ``_clob`` (the preflight
    balance/allowance read) and, after the fix, the dispatcher hand-off. Its
    ``execute_buy`` is what proves a trade actually reached the live side.
    """

    def __init__(self) -> None:
        self._clob = _FakeClob()
        self.buys: list[str] = []

    def execute_buy(self, intent, *, news_id, ts):  # noqa: ANN001, ANN201
        from openpoly.execution import ExecResult

        self.buys.append(intent.market_id)
        return ExecResult.ok(price=intent.price, qty=intent.qty, position_id=1)

    def execute_sell(self, position, *, close_reason, ts, trigger=None):  # noqa: ANN001, ANN201
        raise AssertionError("not exercised by this test")

    def get_collateral_balance_raw(self) -> int | None:
        return None


def test_switch_to_live_wires_the_executor_into_the_dispatcher(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wallet configured *after* boot must be able to trade.

    ``executor.configure_live`` otherwise ran only in the boot lifespan, behind
    ``runtime_state.wallet is not None``. Configure the wallet later, flip the
    mode, and the route built a live executor, validated it, and threw it
    away — so every dispatch skipped with ``live_not_ready`` until the process
    was restarted. This drives the route and then asks the dispatcher to route
    a real buy.
    """
    import openpoly.execution.dispatcher as dispatcher_module
    from openpoly.execution import executor
    from openpoly.sections.entry.edge_threshold_v0 import OrderIntent

    client, _store, rs = env
    # In production these are the same module-level singleton; the fixture
    # swaps only the routes' reference, so point the dispatcher at the same
    # RuntimeState the route is about to flip.
    monkeypatch.setattr(dispatcher_module, "runtime_state", rs)
    _set_wallet(rs)  # wallet configured after boot
    live = _RecordingLiveExecutor()
    monkeypatch.setattr(wallet_routes, "build_live_executor", lambda wallet, portfolio: live)
    # The dispatcher is a process-wide singleton — start from the state a boot
    # without a wallet leaves behind, and put back whatever was there.
    saved_live = executor._live
    executor._live = None

    try:
        r = client.post("/api/system/mode", json={"mode": "live"})
        assert r.status_code == 200, r.text

        result = executor.execute_buy(
            OrderIntent(market_id="m1", side="yes", price=0.42, qty=10.0),
            news_id="n1",
            ts=1.0,
        )
    finally:
        executor._live = saved_live

    assert result.skip_reason != "live_not_ready"
    assert result.filled is True
    assert live.buys == ["m1"]

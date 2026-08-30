"""Credential fragments must not reach INFO logs.

The live-executor factory and the wallet-config route both used to announce the
signer address, a funder prefix and an API-key prefix at INFO — the level that
actually lands in ``/var/log/openpoly.out`` and in every pasted debug snippet.
The API-key fragment is gone entirely (it is a live trading credential, and a
prefix of one is still a prefix of one); the addresses moved to DEBUG, where
they are available when someone is deliberately looking.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import openpoly.api.wallet_routes as wallet_routes
from openpoly.api.main import app
from openpoly.execution.live_executor import build_live_executor
from openpoly.wallet.runtime_state import RuntimeState, WalletSpec

# Anvil's deterministic dev key #0 — public, well-known, safe to bake into tests.
TEST_PRIVKEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_SIGNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TEST_FUNDER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
FAKE_API_KEY = "apikey-0123456789abcdef"


class _FakeCreds:
    api_key = FAKE_API_KEY
    api_secret = "secret"
    api_passphrase = "passphrase"


class _FakeClobClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def derive_api_key(self):
        return _FakeCreds()

    def set_api_creds(self, _creds) -> None:
        pass

    def get_address(self) -> str:
        return TEST_SIGNER


def _build(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENPOLY_POLYMARKET_PK", TEST_PRIVKEY)
    monkeypatch.setattr("openpoly.execution.clob_patch.ClobClient", _FakeClobClient)
    wallet = WalletSpec(
        private_key_ref="env:OPENPOLY_POLYMARKET_PK",
        funder_address=TEST_FUNDER,
    )
    return build_live_executor(wallet, None)  # type: ignore[arg-type]


# ---------- live executor factory ----------


def test_build_live_executor_logs_no_credentials_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="openpoly.execution.live_executor"):
        _build(monkeypatch)
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert TEST_SIGNER not in emitted
    assert TEST_FUNDER[:10] not in emitted
    assert FAKE_API_KEY[:8] not in emitted
    # The event itself is still announced — only its payload changed.
    assert "live executor ready" in emitted


def test_build_live_executor_logs_addresses_at_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="openpoly.execution.live_executor"):
        _build(monkeypatch)
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert TEST_SIGNER in emitted
    assert TEST_FUNDER[:10] in emitted


def test_build_live_executor_never_logs_the_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Not even a fragment, and not even at DEBUG: it is a live credential."""
    with caplog.at_level(logging.DEBUG, logger="openpoly.execution.live_executor"):
        _build(monkeypatch)
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_API_KEY[:6] not in emitted


# ---------- wallet config route ----------


@pytest.fixture
def wallet_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPENPOLY_RUNTIME_STATE", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("OPENPOLY_POLYMARKET_PK", TEST_PRIVKEY)
    fresh = RuntimeState()
    fresh.load()
    monkeypatch.setattr(wallet_routes, "runtime_state", fresh)
    return TestClient(app)


def _put(client: TestClient):
    return client.put(
        "/api/wallet/config",
        json={
            "private_key_ref": "env:OPENPOLY_POLYMARKET_PK",
            "funder_address": TEST_FUNDER,
        },
    )


def test_wallet_config_logs_no_addresses_at_info(
    wallet_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="openpoly.api.wallet_routes"):
        assert _put(wallet_client).status_code == 200
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert TEST_SIGNER not in emitted
    assert TEST_FUNDER[:10] not in emitted
    assert "wallet config updated" in emitted


def test_wallet_config_logs_addresses_at_debug(
    wallet_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="openpoly.api.wallet_routes"):
        assert _put(wallet_client).status_code == 200
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    assert TEST_SIGNER in emitted
    assert TEST_FUNDER[:10] in emitted

"""Tests for scripts/daily_report.py — the daily Telegram summary."""

from __future__ import annotations

import importlib.util
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "daily_report.py"


@pytest.fixture
def report(monkeypatch):
    spec = importlib.util.spec_from_file_location("daily_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    responses = {
        "/api/system/mode": {"mode": "paper"},
        "/api/portfolio/equity": {"summary": {}},
        "/api/positions?limit=500": {"positions": []},
        "/api/analyzer/usage": {"last_7_days": {}},
        "/api/news/source/status": {"snapshot": {"state": "connected"}},
        "/api/market/source/status": {"snapshot": {"state": "running"}},
    }
    monkeypatch.setattr(module, "api", lambda _base, path: responses.get(path, {}))
    return module


@pytest.fixture
def mexico_city_tz():
    """Run the test with a non-UTC local timezone, restoring the exact
    previous TZ (set or unset) and re-applying it afterwards."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Mexico_City"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


def test_report_date_is_utc_whatever_the_host_timezone(report, mexico_city_tz) -> None:
    # 03:00 UTC on Oct 27 is still Oct 26 in Mexico City.
    now = datetime(2026, 10, 27, 3, 0, tzinfo=UTC).timestamp()
    text = report.build_report("http://x", now)
    assert "OpenPoly — 27 oct" in text.splitlines()[0]

"""Regression: a numeric epoch string must never be parsed as an ISO date.

Python 3.13's ``datetime.fromisoformat`` accepts ISO *basic* format, so an
epoch-ms string such as ``"1792013060023.0"`` parsed as the year 1792 once the
wall clock passed 2026-10-14 20:35Z (13-digit ms values whose leading digits
read as a valid basic date). The clock is frozen inside that window here so the
test is not calendar-dependent.
"""

from __future__ import annotations

import json

import pytest

from openpoly.news import ws_client
from openpoly.news.ws_client import _to_epoch, default_parse

# 2026-10-14 21:24:50Z — inside the window where fromisoformat misreads epochs.
_FROZEN_NOW = 1792013090.0


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(ws_client.time, "time", lambda: _FROZEN_NOW)
    return _FROZEN_NOW


def test_epoch_ms_string_in_the_iso_basic_window_is_read_as_a_number(frozen_clock: float) -> None:
    sec = frozen_clock - 30
    raw = json.dumps({"id": "n", "published_at": str(sec * 1000)})
    item = default_parse(raw)
    assert item is not None
    assert item.published_at == pytest.approx(sec)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1792013060023.0", 1792013060.023),
        ("1792013060023", 1792013060.023),
        ("1792013060", 1792013060.0),
        ("1792013060.5", 1792013060.5),
        (" 1792013060 ", 1792013060.0),
        ("+1792013060", 1792013060.0),
    ],
)
def test_numeric_strings_are_epochs(value: str, expected: float) -> None:
    assert _to_epoch(value, fallback=-1.0) == pytest.approx(expected)


def test_iso_strings_still_parse_as_utc() -> None:
    assert _to_epoch("2026-10-14T20:44:20+00:00", fallback=-1.0) == pytest.approx(1792010660.0)
    assert _to_epoch("2026-10-14T20:44:20Z", fallback=-1.0) == pytest.approx(1792010660.0)


def test_garbage_falls_back() -> None:
    assert _to_epoch("not a date", fallback=-1.0) == -1.0

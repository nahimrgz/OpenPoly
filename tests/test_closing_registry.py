"""Tests for the in-flight close registry.

The registry is a process-global set. An id left behind by one test makes the
settlement and reconciliation monitors silently skip that position in every
later test in the session, so it must be resettable — and the reset has to run
automatically, not by remembering to call it.
"""

from __future__ import annotations

from openpoly.runtime.closing_registry import closing_ids, mark_closing, reset_for_tests


def test_reset_for_tests_clears_every_registered_id() -> None:
    mark_closing(1)
    mark_closing(2)
    reset_for_tests()
    assert closing_ids() == frozenset()


def test_a_leaked_id_does_not_reach_the_next_test() -> None:
    assert closing_ids() == frozenset()
    mark_closing(99)  # deliberately leaked — the next test proves it is cleared


def test_the_registry_starts_empty() -> None:
    assert closing_ids() == frozenset()

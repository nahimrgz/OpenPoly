"""Entry types and helpers shared between the runtime, analytics and entry impls.

Kept out of the versioned impl files so the orchestrator does not import a
specific entry version to build the payload it hands every entry section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from openpoly.sections.analyzer.llm_v0 import AnalysisResult

__all__ = ["FreshBooks", "Side", "candidate_sides", "held_side_probability"]

Side = Literal["yes", "no"]


def candidate_sides(side_lock: bool) -> tuple[Side, ...]:
    """The sides an entry may buy: YES only under ``side_lock``, else both."""
    return ("yes",) if side_lock else ("yes", "no")


def held_side_probability(p_model: float, side: str) -> float:
    """The model's probability for ``side``: ``p_model`` is P(YES), so a NO
    holding is a ``1 - p_model`` bet on the outcome it bought."""
    return p_model if side == "yes" else 1.0 - p_model


@dataclass(frozen=True)
class FreshBooks:
    """Entry payload for sections declaring ``ACCEPTS_FRESH_BOOKS``: the
    analysis plus the tokens whose books the orchestrator found within the
    freshness budget for this decision."""

    analysis: AnalysisResult
    fresh_tokens: frozenset[str]

"""Which section impl the runtime builds, per section type — and the builder.

The registry lists every impl it discovers — ``llm_v0`` and ``llm_v1`` both
appear in ``/api/sections/catalog``. The canvas has one node per section type,
so it needs to know which of those entries the backend runs; otherwise it
renders (and seeds a node's config from) whichever entry happens to come first.
``/api/sections/catalog`` marks the entry named here ``active``.

``ACTIVE_SECTION_IMPLS`` is also what ``build_section`` constructs, so the
orchestrator, the exit monitor and the canvas reload path cannot drift from
what the catalog reports.
"""

from __future__ import annotations

import importlib
from typing import Any

# section type -> (module, class name)
ACTIVE_SECTION_IMPLS: dict[str, tuple[str, str]] = {
    "news_source": ("openpoly.sections.news_source.tradingnews_ws", "TradingNewsWSSource"),
    "market_source": ("openpoly.sections.market_source.polymarket", "PolymarketSource"),
    "embedding": ("openpoly.sections.embedding.minilm_v0", "EmbeddingFilterV0"),
    "analyzer": ("openpoly.sections.analyzer.llm_v1", "LLMAnalyzerV1"),
    "entry": ("openpoly.sections.entry.edge_threshold_v1", "EdgeThresholdEntryV1"),
    "exit": ("openpoly.sections.exit.threshold_v1", "ThresholdExitV1"),
    "database": ("openpoly.sections.database.sqlite", "SqliteDatabase"),
}


def is_active(section_type: str, module: str, name: str) -> bool:
    """True when (module, name) is the impl the runtime builds for this type."""
    return ACTIVE_SECTION_IMPLS.get(section_type) == (module, name)


def active_class(section_type: str) -> type[Any]:
    """Import and return the active impl class for ``section_type``."""
    module_name, class_name = ACTIVE_SECTION_IMPLS[section_type]
    cls: type[Any] = getattr(importlib.import_module(module_name), class_name)
    return cls


def build_section(section_type: str) -> Any:
    """Build the active impl for ``section_type``.

    The config is the impl's own ``Config``, read from the persisted canvas
    node (falling back to defaults on a missing / invalid node). The entry
    section additionally gets the executor's portfolio as a lazy provider: the
    executor's PortfolioStore is configured *after* the sections are built, so
    entry calls the closure per ``run()`` instead of holding the store; it
    returns None until then.
    """
    from openpoly.runtime.orchestrator import _canvas_config

    cls = active_class(section_type)
    config = _canvas_config(cls.Config, section_type)
    if section_type == "entry":
        from openpoly.execution import executor

        return cls(
            config,
            portfolio_provider=lambda: getattr(
                getattr(executor, "_paper", executor), "_portfolio", None
            ),
        )
    return cls(config)

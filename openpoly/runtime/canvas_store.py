"""Canvas template persistence — the bridge from the React canvas to the runtime.

The canvas (React Flow) is the user's config surface: each section node carries
a config dict. The frontend mirrors the whole template here via
``PUT /api/canvas/template`` so the backend pipeline can build its sections from
the user's canvas config instead of bare defaults.

Plain JSON at ``~/.openpoly/canvas.json``; ``OPENPOLY_CANVAS_STORE`` overrides
the path (tests, isolation). Same trust model as the secret store — single-user,
local, paper.

Slice canvas-sync v2: ``compute_rev`` derives an HTTP-ETag-style content hash
from the template's canonical JSON. Frontend reads ``rev`` from the GET
response and sends it back in ``If-Match`` on PUT; backend rejects stale revs
with 409. No counter to persist — the hash is the version.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENV_OVERRIDE = "OPENPOLY_CANVAS_STORE"


def _path() -> Path:
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".openpoly" / "canvas.json"


def compute_rev(template: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of ``template`` — 64-char hex. Stateless
    rev: same content → same hash, so equality checks on a freshly-saved file
    are stable across processes / restarts. Used as an HTTP ETag analog."""
    canonical = json.dumps(template, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_template(template: dict[str, Any]) -> str:
    """Atomically persist the canvas template (same-dir tmp + os.replace).
    Returns the rev of what was just written (caller uses to echo back to the
    frontend or for diff bookkeeping)."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    os.replace(tmp, path)
    return compute_rev(template)


def load_template() -> dict[str, Any] | None:
    """Read the persisted template; ``None`` when nothing has been saved — or
    the file is unreadable, since a corrupt canvas must not break startup."""
    path = _path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to read canvas store at %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def load_template_with_rev() -> tuple[dict[str, Any], str] | None:
    """Convenience: load + compute_rev in one call. Returns ``None`` when no
    template persisted (caller maps to HTTP 404)."""
    t = load_template()
    if t is None:
        return None
    return t, compute_rev(t)


# The canvas template version this backend understands — mirrors the
# frontend's ``TEMPLATE_VERSION`` (``frontend/src/canvas/templateIO.ts``).
CANVAS_TEMPLATE_VERSION = 4

# Entry v1's cooldown default; v3 canvases persisted entry v0's 0 instead.
_ENTRY_V1_COOLDOWN_MINUTES = 60


def upgrade_template(template: dict[str, Any]) -> dict[str, Any]:
    """Apply the canvas migrations the backend's section configs depend on —
    the same steps the frontend runs on load — and return the upgraded copy
    (the input is not mutated).

    v3 → v4: a canvas saved while entry v0 was active persisted v0's
    ``same_market_cooldown_minutes: 0``, and that explicit 0 overrode entry
    v1's default of 60, silently disabling the cooldown. An explicit 0 on an
    entry node of a pre-v4 canvas is lifted to 60. The template's ``version``
    is the marker that makes this a one-time step: once the frontend saves
    the upgraded (v4) canvas, a 0 the user sets afterwards is kept.

    Only the runtime's view is upgraded (``section_config``); the stored file
    and ``GET /api/canvas/template`` stay as written, and the frontend
    migrates — and on its next save persists — the same change itself.
    """
    version = template.get("version")
    if not isinstance(version, int) or version >= CANVAS_TEMPLATE_VERSION:
        return template
    nodes = template.get("nodes")
    upgraded_nodes: list[Any] = []
    for node in nodes if isinstance(nodes, list) else []:
        if (
            isinstance(node, dict)
            and node.get("sectionType") == "entry"
            and isinstance(node.get("config"), dict)
            and node["config"].get("same_market_cooldown_minutes") == 0
        ):
            node = {
                **node,
                "config": {
                    **node["config"],
                    "same_market_cooldown_minutes": _ENTRY_V1_COOLDOWN_MINUTES,
                },
            }
        upgraded_nodes.append(node)
    upgraded = {**template, "version": CANVAS_TEMPLATE_VERSION}
    if isinstance(nodes, list):
        upgraded["nodes"] = upgraded_nodes
    return upgraded


def section_config(section_type: str) -> dict[str, Any]:
    """The config dict of the first canvas node of ``section_type``, read
    from the upgraded template (``upgrade_template``).

    Returns ``{}`` when no template is persisted or no such node exists — the
    caller then builds the section from its own defaults.
    """
    template = load_template()
    if not template:
        return {}
    template = upgrade_template(template)
    nodes = template.get("nodes")
    if not isinstance(nodes, list):
        return {}
    for node in nodes:
        if isinstance(node, dict) and node.get("sectionType") == section_type:
            cfg = node.get("config")
            return dict(cfg) if isinstance(cfg, dict) else {}
    return {}

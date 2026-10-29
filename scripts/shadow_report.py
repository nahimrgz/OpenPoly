"""Print the shadow-pick report: what the ``min_confidence`` filter turned down.

Each analyzer pick discarded for low confidence is persisted with the side and
ask the entry would have used (``shadow_pick``). The report marks those picks
at +6h / +24h / +48h / latest against the order-book snapshots, grouped by
confidence, next to the real closed positions grouped by entry confidence. See
``openpoly/analytics/shadow.py`` for the method.

Two sources:

    python3 scripts/shadow_report.py                          # backend API
    python3 scripts/shadow_report.py --url http://host:8000   # another backend
    uv run python scripts/shadow_report.py --db openpoly.db   # a DB file directly

``--url`` needs only the standard library (``OPENPOLY_API_URL`` is the
default, else http://127.0.0.1:8000). ``--db`` imports openPoly, so run it with
``uv run``. ``--json`` prints the raw report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any


def _from_api(base: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base.rstrip('/')}/api/analytics/shadow", timeout=30) as r:
        return json.loads(r.read())


def _from_db(path: str) -> dict[str, Any]:
    from pathlib import Path

    from sqlalchemy import inspect

    from openpoly.analytics.shadow import shadow_report
    from openpoly.db.engine import make_engine, make_session_factory
    from openpoly.portfolio import PortfolioStore

    if not Path(path).is_file():
        raise SystemExit(f"no such database file: {path}")
    engine = make_engine(f"sqlite:///{path}")
    if "shadow_pick" not in inspect(engine).get_table_names():
        raise SystemExit(
            f"{path} has no shadow_pick table yet: start the backend once on it "
            "so its migrations run"
        )
    factory = make_session_factory(engine)
    store = PortfolioStore(factory)
    positions = store.list_positions(5000)
    return shadow_report(
        factory,
        positions=positions,
        cost_basis=store.buy_cost_basis([p.id for p in positions]),
    )


def _pct(x: float | None) -> str:
    return "     —" if x is None else f"{x * 100:+6.1f}%"


def render(report: dict[str, Any]) -> str:
    lines = ["Shadow picks (discarded by min_confidence), return vs the ask entry would pay"]
    keys = [f"{h}h" for h in report["horizons_hours"]] + ["latest"]
    if not report["shadow"]:
        lines.append("  no shadow picks recorded yet")
    for conf, g in sorted(report["shadow"].items()):
        lines.append(f"  {conf}: {g['picks']} picks")
        for k in keys:
            h = g["horizons"][k]
            win = "  —" if h["win_rate"] is None else f"{h['win_rate'] * 100:3.0f}%"
            lines.append(
                f"    {k:>6}  n={h['count']:<4} mean {_pct(h['mean_return'])}  "
                f"win {win}  pending {h['pending']}  no data {h['no_data']}"
            )
    lines.append("Real closed positions by entry confidence (realized / cost)")
    if not report["real"]:
        lines.append("  none")
    for conf, r in sorted(report["real"].items()):
        win = "  —" if r["win_rate"] is None else f"{r['win_rate'] * 100:3.0f}%"
        lines.append(f"  {conf:>8}  n={r['count']:<4} mean {_pct(r['mean_return'])}  win {win}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--url", default=os.environ.get("OPENPOLY_API_URL", "http://127.0.0.1:8000"))
    src.add_argument("--db", help="SQLite file to read directly (run with uv run)")
    ap.add_argument("--json", action="store_true", help="print the raw JSON report")
    args = ap.parse_args(argv)
    report = _from_db(args.db) if args.db else _from_api(args.url)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

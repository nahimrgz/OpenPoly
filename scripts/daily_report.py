"""Daily Telegram summary of the last 24h of openPoly activity.

Reads the local backend API and sends one message through the Telegram Bot API. Standard library
only, so it runs with a plain ``python3`` from cron.

Config comes from the environment, falling back to the repo's ``.env``:

    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     chat to send to
    OPENPOLY_API_URL     backend base URL (default http://127.0.0.1:8000)

If the backend is down the script still sends a message saying so, so a
missing summary never looks like a quiet day.

Dates are UTC, like the rest of the repo. The message text is Spanish on
purpose: it is a personal notification for the operator, not UI shipped with
openPoly, so it is a deliberate exception to the English-only rule.

Usage:
    python3 scripts/daily_report.py            # send
    python3 scripts/daily_report.py --dry-run  # print instead of sending

Cron (07:00 every day):
    0 7 * * * cd ~/Documentos/Github/OpenPoly && python3 scripts/daily_report.py >> ~/.openpoly/daily_report.log 2>&1
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAY = 24 * 3600
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def load_env() -> None:
    """Fill os.environ from the repo's .env without overriding real env vars."""
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def api(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return json.load(resp)


def money(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}${abs(x):.2f}"


def build_report(base: str, now: float) -> str:
    since = now - DAY
    d = datetime.fromtimestamp(now, UTC)
    mode = api(base, "/api/system/mode").get("mode", "?")
    mode_label = (
        "simulación, sin dinero real"
        if mode == "paper"
        else "⚠️ DINERO REAL"
        if mode == "live"
        else mode
    )
    lines = [f"📊 <b>OpenPoly — {d.day} {MONTHS[d.month - 1]}</b> ({mode_label})", ""]

    equity = api(base, "/api/portfolio/equity").get("summary", {})
    positions = api(base, "/api/positions?limit=500").get("positions", [])
    closed = [
        p for p in positions if p["status"] == "closed" and (p.get("closed_at") or 0) >= since
    ]
    open_now = [p for p in positions if p["status"] == "open"]
    exposure = sum(p["qty"] * p["avg_entry_price"] for p in open_now)
    wins = sum(1 for p in closed if (p.get("realized_pnl") or 0) > 0)

    # The headline: winning or losing overall, open bets valued at today's price.
    total = equity.get("total", 0.0)
    realized, unrealized = equity.get("realized", 0.0), equity.get("unrealized", 0.0)
    if not positions:
        lines.append("⚪ Todavía no ha hecho ninguna apuesta")
    elif abs(total) < 0.005:
        lines.append("⚪ <b>Vas en ceros</b>: ni ganas ni pierdes")
    elif total > 0:
        lines.append(f"🟢 <b>Vas ganando {money(total)}</b> en total")
    else:
        lines.append(f"🔴 <b>Vas perdiendo {money(total)}</b> en total")
    if positions:
        lines.append(
            f"   {money(realized)} ya cobrado + {money(unrealized)} en apuestas que siguen abiertas"
        )
    lines.append("")

    if closed:
        pnl_24h = sum(p.get("realized_pnl") or 0 for p in closed)
        lines.append(
            f"Ayer terminaron {len(closed)} apuestas: {wins} ganadas, {len(closed) - wins} perdidas ({money(pnl_24h)})"
        )
    else:
        lines.append("Ayer no terminó ninguna apuesta")
    lines.append(f"Abiertas ahora: {len(open_now)} (${exposure:.2f} apostados)")

    usage = api(base, "/api/analyzer/usage").get("last_7_days", {})
    lines.append(f"Gasto en Claude (7 días): ${usage.get('cost_usd', 0.0):.2f}")

    # Only speak up about the machinery when something is wrong.
    problems = []
    if api(base, "/api/news/source/status").get("snapshot", {}).get("state") != "connected":
        problems.append("no está recibiendo noticias")
    if api(base, "/api/market/source/status").get("snapshot", {}).get("state") != "running":
        problems.append("no está actualizando mercados")
    errors = sum(
        api(base, f"/api/{name}/log").get("counters", {}).get("error", 0)
        for name in ("embedding", "analyzer", "entry")
    )
    if errors:
        problems.append(f"{errors} errores al analizar noticias")
    if problems:
        lines += ["", "⚠️ Revisar: " + "; ".join(problems)]
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=15) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {body.get('description')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the report instead of sending it"
    )
    args = parser.parse_args()

    load_env()
    base = os.environ.get("OPENPOLY_API_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        text = build_report(base, time.time())
    except (urllib.error.URLError, OSError) as exc:
        text = f"⚠️ <b>OpenPoly</b>: el backend no responde en {html.escape(base)}\n({html.escape(str(exc))})"

    if args.dry_run:
        print(text)
        return 0

    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set (env or .env)", file=sys.stderr)
        return 1
    try:
        send_telegram(token, chat_id, text)
    except urllib.error.HTTPError as exc:
        # The token is part of the URL; report only the status, never the URL.
        print(
            f"Telegram API error: HTTP {exc.code} {exc.read().decode(errors='replace')}",
            file=sys.stderr,
        )
        return 1
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M} report sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())

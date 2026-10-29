# shadow-picks

Objective: persist the analyzer picks that the min_confidence filter discards, and report what they would have returned, so the filter can be judged in days rather than months.
Branch: feat/shadow-picks · Delivery: single-pr · Runner: `uv run pytest` + ruff / format / mypy.

## Specs

User request (L1), in context: "Persistir cada elección descartada en una tabla nueva, con el lado, el ask y la ventaja (edge) en ese momento … Agregar un script que mida qué hubiera pasado con cada una." User reply: "si, implementalo".

- S1. Every analyzer `below min_confidence` skip that carries a pick (market_id, p_model, confidence) is persisted in a new `shadow_pick` table (migration 6, idempotent). The row also records the side the active entry would pick, that side's token, its depth-guarded ask and bid, and the edge, all taken from the cached books at decision time (no extra fetch). Recording never blocks or breaks the pipeline: it runs fire-and-forget, and errors are only logged.
- S2. The side rule mirrors entry v1's side choice (v0 `_read_quote`): the side with the larger edge, where edge = fair − ask, fair = p for YES and 1 − p for NO, and the ask is the level-1 ask (> 0). A side needs asks, bids and a depth-guarded bid (first bid level with ≥ 5 shares); ties go to YES. Later gates (spread, edge, portfolio, side_lock, staleness) are not applied. When no side can be quoted, the row is still stored with side/ask NULL. (Rewritten after T3: the earlier text both claimed v1 parity and specified a depth-guarded ask, which v1 does not use.)
- S3. A read-only report measures what each pick would have returned. Each stored pick is marked at +6h, +24h and +48h using the first persisted `order_book_snapshot` of its token at or after that horizon, at the depth-guarded bid, and also at the latest snapshot. The return is (bid − ask) / ask. Results are grouped by confidence, with count, mean return and win rate per horizon. Picks whose horizon has not elapsed are counted as `pending`; a pick with no quote, or no snapshot at or after the horizon, is counted as `no_data`. Snapshot retention (7 days) bounds the horizons.
- S4. The report is exposed as `GET /api/analytics/shadow` (read-only, no auth) and as a CLI script `scripts/shadow_report.py` that prints it for a DB path or URL.
- S5. As a comparison, the report includes the real closed positions grouped by `entry_confidence`: count, mean realized return and win rate.

## Tasks

- T1 [S1, S2] persistence: table, migration 6, store, orchestrator hook · route: inline (no writer reason) · status: done · commit: d6f40c2
- T2 [S3, S4, S5] report: analytics module, endpoint, script · route: inline · status: done · commit: d6f40c2
- T3 independent verifier (high risk item 1: migration) · status: done · verdict: S1, S3, S4, S5 PASS; S2 blocker B1 fixed in one correction batch

## Log

- L1 (2026-10-29) User asked whether there was enough data for a shadow report. There was not: 19 picks over 12 markets in a 200-entry in-memory ring, with no side or price. I proposed persisting the picks and adding a script; user: "si, implementalo".
- L2 T1+T2 in d6f40c2: 13 tests in tests/test_shadow_picks.py, RED first (import error, then endpoint override), GREEN. Gates: pytest 1366, ruff, format, mypy (89). One full-suite run showed 1 failure that did not reproduce in 9 reruns. T3 verifier launched.
- L3 T3 verdict: S1/S3/S4/S5 PASS (migration preserves other tables byte-for-byte, re-run no-op, fresh == migrated; recording never blocks or raises; endpoint off-loop, no auth). B1: the shadow side used a depth-guarded ask while v1 pays the level-1 ask and requires a depth-guarded bid. Correction: choose_shadow_side now mirrors v0 `_read_quote`; S2 rewritten; regression tests test_level1_ask_is_the_price_like_entry_v1 and test_side_without_a_depth_guarded_bid_is_not_quoted (RED on d6f40c2). A1: S3 wording aligned with pending/no_data. A2: the script exits cleanly on a missing file or an unmigrated DB. A3 (calibration limit includes open positions) and A4 (no side_lock/staleness) noted, unchanged.

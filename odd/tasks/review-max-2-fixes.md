# review-max-2-fixes

Objective: fix the findings of the second `/code-review max` on fix/audit-2026-10 (after simplify, commit 5ee5fa8).
Branch: fix/audit-2026-10 · Delivery: single-pr (ask-on-risk) · Runner: `uv run pytest` + ruff/mypy, frontend `npx --yes yarn@1.22.22 typecheck|lint|test`.

## Specs

User request (L1): "si corrigelos, usa el nuevo gentle-ai odd y agrega el archivo al commit"

Each spec is a review finding to fix. Acceptance for every spec: a regression test that fails before and passes after the fix; all gates green; v0 behavior unchanged unless the spec says otherwise.

- S0. `openpoly/sections/entry/_types.py` is included in the commit (done in 5ee5fa8).
- S1. Exit sells must not sweep down to a deep stink bid on a bid pull. Normal stops (stop_loss, trailing, take-profit) sell only against level 1 (floor = best bid). A deep sweep is allowed only for a confirmed hard stop, with floor = max(one tick, entry × (1 − hard_stop_loss_pct − 0.10)).
- S2. The thin hard-stop floor (and every live SELL limit) is never below the market tick (min 0.01); live never signs a sub-tick price.
- S3. The thin hard-stop ask confirmation requires a depth-qualified ask (first ask level with ≥ 5 shares), not a dust/probe level-1 ask.
- S4. The thin no-ask streak counts distinct book fetches (by fetch stamp), not exit ticks; a frozen snapshot cannot complete the streak.
- S5. Live `_resolve_fill` books an inferred/maker fill at the limit (or actual maker price) when the order rested; a raced remainder is not priced at the cross average. Proceeds are never overstated vs main.
- S6. `_close` respects `ExecResult.resting_order_id`: while a prior live SELL may still be resting, no new SELL is posted for that position.
- S7. The required-field check in the shared LLM client must not turn a v0/v1 abstain (selected_index 0 without p_yes/confidence) or a pick missing only `rationale` into an error; only a truncated (`stop_reason == "max_tokens"`) response that lacks the fields needed for its verdict is a structural failure.
- S8. The kill switch's consecutive-loss streak and drawdown curve use close order (closed_at), not id order.
- S9. A malformed news frame (ValueError, RecursionError, UnicodeDecodeError, any parse exception) is dropped and logged; it never kills the WS task.
- S10. `ensure_fresh` reports ages measured at return time (after the gather), so a side that aged past budget while a sibling refreshed is reported with its real age.
- S11. `_parse_model` handles gateway ids (`anthropic/` prefix, dotted versions `claude-opus-4.7`, `claude-3.5-sonnet`, `claude-haiku-4.5`); 5.x models whose forced tool_choice support is documented keep forced tool_choice (verify against the claude-api skill / docs; when unknown, keep auto + instruction).
- S12. The 404 "gone" mark is timestamped; a batch book received before the mark cannot clear it.
- S13. Close backoff is evaluated against the confirmed decision; a stale trigger flip does not wipe it, and a new lower floor in a moving market does not keep doubling the wait.
- S14. The held-book loop skips gone tokens (or backs off them) instead of refetching every 10s.
- S15. Lower findings: calibration bucket float rounding at 0.8/0.9; exit-log row when a held position's book is missing; canvas 409 vs v3 backend handled as conflict, not network error; news freshness canvas change reaches the running WS client; llm_usage DB write does not delay the entry decision; usage totals keep an index-friendly query; new tests do not patch the process-wide `time` module.

## Tasks

- T1 [S1–S6, S10, S12–S14, S15 exit/store parts] route: delegated (writer A, parallel units, disjoint surface) · status: done · commit: 2e5a7c9
- T2 [S7–S9, S11, S15 llm/news/calibration/canvas/usage parts] route: delegated (writer B, parallel units, disjoint surface) · status: done · commit: be9ccdb
- T3 independent verifier (high risk: live trading, item 6/4) · status: done · verdict: no blockers; S7 partial (advisory, same as baseline)

## Log

- L1 (2026-10-13) User: "si corrigelos, usa el nuevo gentle-ai odd y agrega el archivo al commit"
- L2 S0 done: simplify committed with `_types.py` as 5ee5fa8 (gates: pytest 1273, ruff, format, mypy, frontend 78).
- L3 Route: two parallel writers with disjoint edit surfaces (Writer trigger (a)); parent owns git and commits per unit; then one independent verifier.
- L4 T2 done (be9ccdb). RED→GREEN per spec; B's surface tests pass; mypy, ruff, frontend 79 green. Intentional changes: v0 kill-switch close order; forced tool_choice for opus 5 / sonnet 5 / fable 5 / haiku 5.5 (per claude-api skill docs); truncated-but-complete responses accepted. Follow-ups: the kill-switch 500-row slice is still id-chosen; add a public freshness setter on TradingNewsWSSource.
- L5 T1 done (2e5a7c9). RED at HEAD via git-archive extract, GREEN after. Parent updated test_entry_freshness_v1 (asserted pre-S10 ages). Seam check: pytest 1346 passed, ruff, format, mypy (88), frontend 79. Live changes: normal stops limit at level-1 bid; thin hard stop limit entry×0.60; SELL ≥ 0.01; SDK price errors → live_rejected (buy too); maker/raced fills at limit; resting-order guard. v0 exit mark now limited to top 3 levels within 0.05 of best bid. T3 verifier launched.
- L6 T3 verdict: S0–S6, S8–S15 PASS with probes RED at 5ee5fa8 / GREEN at HEAD; S7 PARTIAL. Advisories:
  - a truncated abstain still costs 2 calls (as at baseline);
  - a thin hard stop below entry×0.6 sells only level 1;
  - a normal stop over a dust top sells only the sliver;
  - on 0.001-tick markets the v0 mark can go thin;
  - a transient 404 hides a book for up to 5 min;
  - there are more refetches during backoff;
  - BUY maker fills are booked at the limit;
  - the in-memory resting registry is lost on restart.

  No blockers, so no correction batch.

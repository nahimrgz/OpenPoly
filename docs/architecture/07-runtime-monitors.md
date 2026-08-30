# Runtime monitors

Everything in `openpoly/runtime/` that runs on a timer rather than on an event.

The news pipeline is event-driven: a `NewsItem` arrives, the orchestrator walks
it through embedding → analyzer → entry, and a position may open. Nothing about
that shape can *close* a position — closing is driven by price, by clock, and by
on-chain reality, none of which produce an event on the news socket. So there
are three periodic sweeps, each answering a different question about an open
position:

| Monitor | Question | Default tick | Closes with reason |
|---|---|---|---|
| `ExitMonitor` | has it hit a threshold? | 120s | `take_profit` / `stop_loss` / `peak_drawdown` |
| `SettlementMonitor` | has its market resolved? | 300s | `settlement` |
| `ReconciliationMonitor` | does the wallet still hold it? | 300s | `reconciled` |

They are deliberately separate loops. A Gamma outage stalls settlement without
touching the take-profit path; a data-api outage stalls reconciliation without
touching either. One loop with three responsibilities would have coupled all
three failures together.

All three are module-level singletons that the FastAPI lifespan `configure()`s
with a `PortfolioStore` and `start()`s — construction touches no DB. See
[05-runtime-network-risk.md](05-runtime-network-risk.md) for the exit policy
these implement, and [02-strategy-sections.md](02-strategy-sections.md) for the
section contract the exit monitor calls into.

## The shared loop: `TickLoopMonitor`

`runtime/tick_loop.py` owns start / stop / the loop itself; each monitor
subclasses it and implements `_tick_once`. Three properties are load-bearing:

- **A tick error never kills the loop.** `_tick_once` runs inside
  `try/except Exception`, logged with the subclass's own module logger.
  `CancelledError` is re-raised — shutdown is not a tick failure.
- **The stop `Event` is recreated on every `start()`.** An `asyncio.Event` binds
  to the loop it is first awaited on, and these are process singletons started
  on a fresh loop by every test; a once-in-`__init__` event would wait forever
  on the second start.
- **The loop yields before sleeping.** `await asyncio.sleep(0)` runs even when
  the tick found nothing to do, because a tick with no await point starves every
  other task on the loop — the reconnect starvation described in
  [05-runtime-network-risk.md](05-runtime-network-risk.md#async-reconnect-starvation).

`stop()` cancels the loop task, then calls the `_after_stop()` hook, then flips
the state to `stopped`. Only the exit monitor overrides that hook.

## ExitMonitor

`runtime/exit_monitor.py`. Each tick reads every open position, marks it, runs
the `exit` section on it, and routes any resulting `CloseIntent` to
`executor.execute_sell`.

### Depth-guarded mark

The mark is **the first bid level carrying at least `min_mark_bid_size` shares**
(default 5.0) — and nothing else. A resting level-1 bid can be a single
minimum-size probe far from fair value, and marking there produced false
stop-outs.

There is deliberately **no mid fallback**. Both executors sell into the book's
raw level-1 bid, so a mid mark would evaluate take-profit and peak-drawdown
against a price the position can never realize — closing a "winner" into a dust
bid at a loss. When no level qualifies, the position is reported **blocked**,
held, and logged once per occurrence as `no_executable_bid`, so an unevaluable
position (its stop-loss cannot fire) is visible rather than silent.

### Trailing logic and the peak

The exit section's trailing lock needs a peak; the monitor is what tracks it.

- `self._peak[position_id]` is the monotone maximum of the depth-guarded mark
  across this process's lifetime, seeded at the position's first observed mark.
- `observe_book` is a **push hook** wired to the market-source book sampler.
  The exit tick runs every 120s but the sampler refreshes far more often;
  without the hook, a run-up that happens and reverses between two ticks is
  invisible and the lock trails a peak that never existed. Only tokens held by a
  position seen on the last sweep are tracked (`self._watch`), so this stays a
  dict lookup on a hot path. The limitation is real: there is no push/WS book
  feed, so "fresh" means the sampler's poll interval (60s by default), not every
  quote update.
- The peak dict is pruned to the currently-open set at the top of every tick.
  `_close` only drops the peaks of positions *this* monitor closed, and the
  settlement and reconciliation monitors close positions behind its back — every
  one of those used to leave an entry behind forever.
- On a **partial** fill the peak and the book subscription are kept: the
  remainder is still an open position with a trailing stop, and resetting them
  would re-seed the stop at the next tick's mark and throw away the run-up the
  position already had.

> **`bootstrap_peaks` exists but is not wired.** The method rebuilds each open
> position's peak from the `order_book_snapshot` table at startup (applying the
> same depth guard, so a recorded dust bid cannot seed an unreachable peak), but
> nothing calls it — the FastAPI lifespan goes straight from `configure()` to
> `start()`. **Peaks therefore reset on every restart**: after a restart a
> position's peak re-seeds at the first mark observed, so a trailing lock that
> had armed on an earlier run-up is disarmed until the price makes a new high.
> The stop-loss is unaffected. Wiring it is a one-line lifespan change plus the
> session factory; it is listed here rather than fixed silently because it
> changes exit behavior on restart.

### Dust skip

A remainder below one share (`execution.sizing.is_dust_qty`) is skipped **before**
evaluation. It cannot be sold at any price ≤ 1.0 without falling under the
venue's $1 minimum, so evaluating it produced a `CloseIntent` → a sell the
executor could only skip → an `error` row, every tick, for as long as the market
stayed unresolved (~720 rows/day into a 200-entry ring). The row stays open until
settlement closes it at the resolution price. Logged once per position
(`dust_remainder`), and **not** counted as blocked: nothing is wrong with the
book, there is simply nothing to do.

### The closing-registry claim protocol

Three loops can close the same position. They used to be serialized by the event
loop because `execute_sell` ran inline; it no longer does — the live sell is
offloaded with `asyncio.to_thread` and hands the loop back for the seconds the
on-chain order takes. In that window another monitor can see a position the
wallet has already emptied and close it first, and the exit monitor then fails
to persist the real fill (`ValueError: position N is closed`) — losing the actual
exit price and realized PnL.

`runtime/closing_registry.py` is the fix, and it is deliberately small: a
process-local `set` of position ids, mutated only on the event-loop thread (the
sell runs in a worker, the registry calls around it do not), so no lock.

The protocol, in order:

1. The section returns a `CloseIntent`.
2. The monitor **re-reads the position's status** (`_still_open`). The sweep's
   open list was read at the top of the tick and every sell since handed the
   loop back; selling from that stale snapshot means an `execute_sell` against
   an already-closed row — a spurious `error` on paper, a real on-chain sell
   that can never be persisted on live.
3. `mark_closing(position_id)`. There is **no `await` between the check and the
   claim**, so nothing can close it in between. `_still_open` is synchronous by
   design for exactly this reason.
4. The sell runs as its own task, awaited under `asyncio.shield`.
5. `clear_closing` in a `finally` — a failed sell must never leave a position
   permanently unreconcilable.

The settlement and reconciliation monitors skip any claimed id for that tick.
They are periodic sweeps, so skipping costs nothing: they reconsider on the next
tick, by which time the sell has landed or been abandoned. Both manual-close
routes (`POST /api/positions/{id}/close`, `POST /api/positions/close-all`) take
the same claim, and neither sells a position the monitor already holds. They
report it differently because they answer different questions: the single-close
route refuses the whole request with `409 exit_in_flight`, while close-all is a
bulk operation that must not abort on one position — it returns `200` and marks
that position in `details` with `ok: false, skip_reason: "exit_in_flight"`
(counted under `skipped`).

### In-flight drain on shutdown

`execute_sell` runs in a worker thread and **cannot be cancelled** — it completes
on-chain and in the DB regardless. So the sell plus its bookkeeping live in
their own task, and `_after_stop()` waits up to
`INFLIGHT_DRAIN_TIMEOUT_SECONDS` (30s, covering the live executor's own retry
budget) for it. Cancelling the tick loop without draining would strand the
`exit_log` entry and the peak cleanup for a close that already happened. The
drain runs on every `stop()`, including one where the loop was never started.

### Tick telemetry

Within-threshold holds write **no** `exit_log` entry: at one row per position
per tick they evicted the rare `ok` / `error` closes from the ring. Liveness is
carried instead by `last_tick_at` / `open_positions` / `blocked`, surfaced via
`GET /api/exit/log` so the canvas badge can show "the monitor is working"
without the flood. `no_executable_bid` and `dust_remainder` are logged once per
occurrence and re-armed when the position becomes markable again or closes.

### Hot-swap

`replace_exit_section` swaps the section instance under `_exit_lock` while the
monitor runs. An in-flight `run(...)` keeps the old instance alive through its
own reference; the next tick reads the attribute and gets the new one. Called by
`api/canvas_routes._apply_canvas_reload` after a canvas PUT changes the exit
config — same atomicity story as the orchestrator's `replace_section`.

## SettlementMonitor

`runtime/settlement_monitor.py`. When a market resolves, Gamma stamps
`outcomePrices` on it — but the resolved market drops out of the discovery
catalog (`/events` is filtered to `closed=false`), so the exit monitor sees no
order book and the position sits `open` forever.

Each tick groups open positions by `condition_id` and fetches exactly those
markets through `fetch_markets_by_condition_id`, which does **not** pass
`closed=false`. For a resolved market it calls `PortfolioStore.close_position`
directly at the 0/1 final price — no broker tx, no CLOB call.

Only clean resolutions are accepted: `outcomePrices` must be `{0.0, 1.0}` as a
set. A disputed market that settles split (`[0.5, 0.5]`) is skipped as
`ambiguous_outcome` and reconsidered next tick, because downstream PnL math on a
split would be fiction.

Every non-close outcome writes a `settlement_log` entry rather than passing
silently — `still_trading`, `no_outcome_prices`, `market_not_returned_by_gamma`,
`gamma_fetch_failed`, `exit_in_flight`. A settlement lag that is invisible looks
identical to no lag at all.

**CTF redemption** — turning winning tokens into pUSD on the DepositWallet — is
a separate on-chain action and is **out of scope**. The ledger closes; the
tokens are redeemed elsewhere.

## ReconciliationMonitor

`runtime/reconciliation_monitor.py`. The other two monitors assume the DB ledger
matches on-chain reality. It can diverge: a position exited on-chain (sold,
redeemed, transferred) without openPoly recording the close. The row then sits
`open` forever, the exit monitor fires into a void, and the UI shows fictional
exposure. Settlement cannot catch it — that only closes positions whose *market*
resolved, and this market is still trading.

Each tick asks an injected `holdings_fetcher` what the wallet actually holds.
Production wires `fetch_held_condition_sides`, which reads the Polymarket
data-api `/positions` indexer — authoritative, and it accounts for neg-risk
wrapping, which a raw `balanceOf` on a token id does not.

**Forward diff** — open in the ledger, absent on-chain → close as `reconciled`.
Three gates before it fires:

- `live_check`: production passes `exec_mode == "live"`. In paper mode the
  indexer knows nothing of paper positions, so an ungated sweep would close
  every one of them. Default `None` (always run) is for tests.
- `grace_seconds` (300 default): a fresh buy's indexer update lags, and
  reconciling it would close a position that was just opened.
- `is_closing`: the exit monitor is mid-sell and the wallet can already read
  flat while its fill is still being persisted.

Realized PnL is recorded as **0** (closed at `avg_entry_price`). The real exit
price is on-chain but cannot be reliably attributed back to a specific openPoly
position when the same market was traded more than once, so no number is
fabricated. The reconciled close stops the bleed; PnL truth is a separate,
manual concern.

**Reverse diff** — held on-chain, no open ledger position → log
`untracked_onchain_holding` and warn, **once** per `(condition_id, side)` per
process. It never auto-opens a position: the cost basis is unknown and a
synthetic position would corrupt entry dedup. A human decides.

**`min_size`** is what makes the reverse diff usable. `fetch_held_condition_sides`
counts a position as held only at `min_size` shares or more (default
`MIN_SELLABLE_QTY`, the venue's size precision). Below that is a residue no
order can clear — `record_sell` closes the ledger position when the remainder
falls under it — so reporting it as a holding raised `untracked_onchain_holding`
against a position that was closed correctly, which trains the operator to
ignore the warning.

## The executor dispatcher

`execution/dispatcher.py`. The monitors and the orchestrator all call
`executor.execute_buy` / `execute_sell` and get an `ExecResult`; none of them
knows which implementation filled. `ExecutorDispatcher` routes on
`runtime_state.exec_mode`, which is the single place mode-awareness lives.

- `paper` (default) → `PaperExecutor`, a level-1 fill model capped by that
  level's depth on both sides.
- `live` → the CLOB executor, pre-built by the lifespan whenever a wallet is
  configured, regardless of the current mode, so a UI flip is cheap.
- `live` with no live executor → `ExecResult.skip("live_not_ready")` plus a
  warning, so a paper-only deployment cannot be talked into a half-live state.

`get_collateral_balance_raw` is deliberately mode-**independent**: the wallet
balance is an on-chain fact, so the dashboard shows the same number in both
modes.

Both executors size through `execution/sizing.py` — one definition of the venue
rules (2 size decimals, one-share minimum, $1.10 minimum notional). Paper is the
simulation of live, so a paper fill that live would have rejected is a lie about
the strategy's realized behavior.

### Startup demotion to paper

`runtime.json` outlives the process, so `exec_mode: "live"` comes back on every
restart — including the restart where the API token went missing (unit file
edited, secret rotated away, container redeployed without it). The mode-switch
route refuses live without a usable token; a *restore* that skipped that check
would put real funds behind an open API precisely when nobody is watching.

`api/main._demote_restored_live_without_token` re-runs the check at startup and
forces paper. It **fails closed**: if persisting the demotion fails, the
in-memory mode is still forced to paper (the dispatcher routes on the in-memory
value) and the failure is logged `CRITICAL`. `runtime.json` still says live,
which only means the demotion re-runs next boot.

## Entry-side gates and the kill switch

Not a monitor, but the other half of the risk story: the brakes live in the
`entry` section (`sections/entry/edge_threshold_v0.py`), because the cheapest
place to stop a loss is before the buy. All are opt-in via canvas config, all
default to off, and **all are entry-only** — a tripped brake never closes
anything, and open positions keep running their normal exit logic (the exit
monitor and manual close both still work).

| Knob | Trips when |
|---|---|
| `heat_cap_usd` | Σ(qty × avg_entry_price) over open positions is at or above the cap |
| `same_market_cooldown_minutes` | a position on the same (market, side) opened or closed within the window |
| `same_market_lifetime_lockout` | any prior position exists on (market, side) — supersedes the cooldown |
| `kill_max_consecutive_losses` | the most recent N closed positions are all losses |
| `kill_daily_loss_usd` | Σ realized PnL over the last 24h is at or below `-limit` |
| `kill_max_drawdown_usd` | the cumulative realized-PnL curve has dropped this far from its peak |

The gates share one bounded read (`list_positions(limit=500)`), and the portfolio
is fetched **only when at least one gate is enabled** — so a default config
touches no DB at all, which the contract tests rely on. Order is cheapest-first:
heat cap → kill switches → per-market lockout, first trip wins.

`heat_cap_usd` does double duty. Besides gating entry on exposure already taken,
it bounds `size_edge_multiplier_max`: the extra size the edge multiplier grants
is trimmed to the headroom left over open exposure, and never below the base
`order_size_usd`. When the cap is on but the portfolio is unreadable the
multiplier is refused outright (`portfolio_unavailable`) rather than applied
unbounded — "the portfolio was unreadable" is exactly the moment not to take a
3× position on trust.

Edge-scaled sizing ships **off** (`size_edge_multiplier_max: 1.0`). Betting more
on a larger "edge" computed from an uncalibrated probability only loses faster,
so the knob is gated on `GET /api/analytics/calibration` first showing each
bucket's win rate near its own midpoint with n ≥ 100 behind it.

## Write-behind persistence

`db/writer.py`. The hot paths — the news WS callback, the market poll and
book-sample loops — must never block on a DB round-trip. They `enqueue`
synchronously into a bounded queue (5000 rows); one background task drains it in
batches (200) and persists each batch off the loop through an injected sink.

Overflow drops the **newest** row rather than blocking a producer, the same
discipline as the orchestrator's queue.

Four counters, all readable and all surfaced rather than swallowed — a dropped
row is a lost order-book or news sample and a failing sink is a persistence
outage, and neither used to leave a trace outside a counter nobody read:

| Counter | Meaning |
|---|---|
| `dropped` | rows refused because the queue was full |
| `written` | rows the sink accepted |
| `errors` | sink failures (a whole batch each) |
| `pending` | rows currently queued |

Both failure kinds report at WARNING with the **cumulative** count, rate-limited
to one message per kind per `WARN_INTERVAL_SECONDS` (60): a saturated queue must
not turn its own diagnosis into the flood.

`stop()` waits (bounded, `STOP_DRAIN_TIMEOUT_SECONDS` = 10s) for the batch
already in flight instead of cancelling out from under it. The worker thread
behind `asyncio.to_thread` runs to completion regardless, so cancelling only
threw away the bookkeeping for a batch that did get written — the same reasoning
as the exit monitor's in-flight drain.

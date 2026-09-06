# Strategy Changelog

How the trading strategy itself has evolved — entry/exit policy, risk
gates, and the reasoning behind each change. Engineering work (refactors,
infrastructure, UI) is deliberately out of scope here; this file answers
one question: *what does the system believe about trading, and when did
that belief change?*

Dates are US-style (MM/DD/YYYY).

---

## 09/05/2026 — An order that did not fill is cancelled, not left on the book

**Nothing rests behind the ledger's back.** The live executor posts a crossing
GTC limit at the level-1 price. When nothing crossed, it reported no match and
left the order resting at a price that was already stale, where it could fill
minutes later with no position row — the orphan the partial-fill cancel was
written for, one branch over. Every answered order that is short of a full
fill is now cancelled, with a bounded retry across Polymarket's matching-delay
window, and the cancel response is actually read: the venue answers 200 even
when it refuses. A refused cancel is checked against the order itself, and
only a full match or a cancelled status counts as "nothing rests" — `MATCHED`
alone does not, because a partial fill reports it too. A fill that raced the
cancel is booked at the limit price, the conservative bound. When the executor
cannot prove the order is gone it says so (`live_cancel_failed`,
`live_fill_unknown`) instead of claiming a clean miss, and it carries the order
id so callers can back off rather than post a second order on top of the
first.

**Trailing-stop peaks survive a restart.** The Phase 4 note below recorded
that `bootstrap_peaks` was not wired. The same-day code-review follow-up wired
it into the startup path, so a lock that armed on an earlier run-up is no
longer disarmed by a restart; the runtime docs now say so.

---

## 08/30/2026 — Phase 4: the canvas can now tell you the truth about the strategy

Phase 4 is mostly engineering — a test framework, a base class, docs, hooks —
and that is out of scope for this file. Three of its changes are not, because
they change what the operator is told about the strategy, and what a page in
another tab is allowed to do to it.

**The canvas' offline catalog was advertising a strategy that no longer
exists.** `frontend/src/sections/catalog.ts` carries a fallback copy of the
section catalog, used whenever the backend is unreachable, and
`defaultConfigForType` seeds a new node's config from it. It still described
`exit` at v0.1.0 as two knobs — `take_profit_pct` and `stop_loss_pct` — which
is the strategy as it stood before the trailing lock existed. An operator
reading that panel saw a system that takes profit at +20% and has no trailing
behaviour at all, when the shipped exit (v0.3.0) has take-profit **off**, arms
a trailing lock at +30% of cost basis, and floors the trail at two ticks or the
live spread. `entry` was three versions stale in the same way: no
`size_edge_multiplier_max`, no `heat_cap_usd`, none of the A4 kill switches.
Both entries are now mirrored field-for-field from the pydantic Configs,
defaults and descriptions included, at the versions actually running.

**A bulk close that half-worked said it had failed.** `POST
/api/positions/close-all` started reporting partials under their own counter in
Phase 3, but the mode-switch dialog still folded them into `skipped + errored`
— and because a partial carries neither a `skip_reason` nor an `error`, it
rendered as `1 failed (unknown)`. The operator pressing "close all" to be out
of the market was told the sell had not happened when it had, on the one screen
where that question is the entire point. Partials now read as *N partially
closed, remainder open*, with the residual share count, and only genuine skips
and errors count as failures.

**Any web page could close your book.** A body-less `POST` is a CORS *simple
request*: the browser issues it for real and only withholds the response. The
Host allowlist admits loopback by design, so
`fetch('http://127.0.0.1:8000/api/positions/close-all', {method:'POST'})` from
any page the operator happened to have open reached the route and sold
everything — with the API token unset (the loopback default), and equally with
it set, because a browser attaches no header it was not asked to. Never seeing
the response does not undo the sell. Every mutating request a browser labels
cross-origin is now refused **403 cross_origin_write** before it reaches a
route. `Sec-Fetch-Site: cross-site` is refused outright, without consulting the
`Origin`; `same-origin` and `none` settle it the other way. `same-site` — and
any value this code does not know — settles nothing and falls through to the
`Origin`, which must name the **same authority, host *and* port**, as the
request's own `Host`, or be listed in `OPENPOLY_ALLOWED_HOSTS`. Loopback gets
no port-free pass on that check: it is the one authority every local page
shares, so admitting it would let a dev server on `localhost:3000` drive the
backend on `localhost:8000`. `curl`, systemd timers and other non-browser
clients send neither header and are unaffected. The web UI, in turn, now sends the API
token: paste it once into **Keys → API token** (ASCII only — an HTTP header
cannot carry anything else).

Everything else in Phase 4 is engineering and leaves behaviour unchanged: a
vitest suite over the canvas store and template (de)serialization (blocking in
CI), a shared `TickLoopMonitor` base for the three runtime monitors, the
module-scope monkey-patched methods folded back into their classes, mypy and
pre-commit wired up, and
[docs/architecture/07-runtime-monitors.md](docs/architecture/07-runtime-monitors.md)
documenting the runtime — including, as of that day, that `bootstrap_peaks` was
**not wired**, so trailing-stop peaks reset on every restart. The same-day
code-review follow-up wired it; see the 09/05/2026 entry above.

---

## 08/30/2026 — Phase 3: what counts as a real outcome, and who is allowed to trade

Nothing here changes an entry or exit threshold. What changed is which numbers
the system is willing to *believe* about its own trades, and the conditions
under which it is willing to trade at all.

**A partial sell realizes a partial gain.** The exit monitor computed realized
P&L as `(fill_price − entry) × held.qty` — the whole position — on every filled
sell, including one that only cleared part of it. An IOC order fills against
whatever depth was resting, and `record_sell` reduces `qty` and leaves the row
open when the residual is still sellable, so a sell of 8 of 20 shares was
logged as if all 20 had gone. It now realizes `result.qty`, and — the more
consequential half — it stops treating the position as finished: the remainder
is still an open position with a trailing stop, so its peak and its book
subscription are kept. Dropping them re-seeded the stop at the next tick's mark
and threw away the run-up the position had already had. "Still open after the
sell" is the test, not `qty < held.qty`: a sell leaving a sub-0.01 residue
closes the row outright, and that is a completed exit.

**"Closed" has to mean flat.** The manual close routes reported `filled: true`
for a sell capped by bid depth — indistinguishable from a completed exit, with
15 of 25 shares still on the book and no way for the operator to know. The
single close now answers `partial` (and `remaining_qty` when it is), and
close-all counts partials under their own counter with `ok` meaning *flat*,
never merely "the order went through". Bulk close is the button pressed to be
out of the market; a summary that counted a half-sold position as closed was
answering a different question than the one being asked.

**A fabricated zero is not evidence.** The calibration report bucketed every
closed position with an `entry_p_model`, including ones closed as `reconciled`
— whose realized P&L is recorded as exactly 0 by construction, because the
position was exited outside the ledger and the real exit price cannot be
attributed back to it. Counting that zero scores a trade whose result is
unknown as a loss, so a run of reconciled closes reads as a miscalibrated model
rather than as missing data — and calibration is the gate that decides whether
`size_edge_multiplier_max` may ever rise above 1.0. Reconciled closes are now
excluded; `settlement`, `take_profit`, `stop_loss`, `peak_drawdown` and manual
closes all closed at a price that actually happened and still count.

**Sizing above the base order requires a readable portfolio.** `heat_cap_usd`
is the only thing bounding an edge-scaled order. When the portfolio could not
be read the open exposure was unknown, the cap bound nothing, and a 3x
multiplier sized straight past the ceiling the operator set precisely to stop
that. The multiplier is now refused in that case — base size, with
`size_multiplier_skipped: portfolio_unavailable` in the signals. "The portfolio
was unreadable" is exactly the moment not to take the larger position on trust.

**An unreadable balance is unknown, not zero.** The pre-order CTF balance read
returned 0 when the venue's balance field was present but unparseable, which
defeated the `ctf_balance_unavailable` guard added on 08/30: the caller saw a
successfully read baseline of 0 and posted the order. It now returns unknown
and the buy refuses, which is what that guard was for. In the other direction,
sub-0.01-share residues are no longer counted as on-chain holdings at all — the
reconciliation monitor's reverse diff was raising `untracked_onchain_holding`
against positions `record_sell` had correctly closed, which is the fastest way
to train an operator to ignore a warning that matters.

**Live trading now requires an authenticated API.** Every mutating route
(POST / PUT / DELETE / PATCH) is guarded by an optional shared secret
(`X-OpenPoly-Token`, from `OPENPOLY_API_TOKEN`); reads stay open. Leaving it
unset keeps loopback development working and logs one warning — but the switch
to live mode is **refused outright** (403 `api_token_required`). Loopback is
not an authorization boundary: every other process on the host can reach it,
and real funds behind an unauthenticated endpoint is not a state anyone should
reach by omission. A `Host` allowlist backs it up, refusing any name that is
not loopback or explicitly allowed (421), which is what turns the DNS-rebinding
path into a rejection rather than a mode switch. See
[docs/deploy](docs/deploy/README.md#securing-the-api).

**Order-book history is now pruned, and the window is a strategy parameter.**
`order_book_snapshot` grew without bound; it is kept for 7 days by default.
That number is not arbitrary: peak bootstrap rebuilds a position's trailing
stop from the snapshots taken since it opened, so the retention window has to
outlive the longest position the strategy will hold. Shorten it and a
long-held winner comes back from a restart with its peak reset to entry.

## 08/30/2026 — Execution integrity, and sizing that has to earn the right

Three beliefs changed about the gap between what the system *records* and what
actually happened at the venue, and a fourth about what has to be true before
the size of a bet is allowed to vary at all.

**A remainder that cannot be sold is still worth its resolution price.** A
partial sell can leave less than one share open, which is not a placeable order
at the venue — every later exit attempt skips. The remainder is not worthless,
though: the settlement monitor closes open rows at the resolution price, so
those tokens pay out 1.0 on the winning side. Closing them at 0.0 would book a
loss that never happened and orphan tokens still sitting in the wallet, so the
sell now skips (`dust_remainder`, warned once per position) and the row stays
open until settlement. The tradeoff is accepted deliberately: the remainder
keeps counting toward the open-position list and `heat_cap_usd` until the
market resolves, which is a bounded, honest cost — a fabricated realized loss
is not. Sizing was also the reason most of that dust existed: orders were
floored to *whole shares* and additionally required `qty × price` to land on
clean cents. The venue asks for neither. Its SDK allows two size decimals at
every tick size and rounds the order amount itself, so the cent rule was
inventing rejections — at a three-decimal price (0.999, 0.993: exactly the
tick regime a winner exits through) no whole-share quantity aligns, so a
perfectly sellable nine-share winner quantized to zero and was treated as
unsellable. Sizing now floors to two decimals and nothing else.

**Paper has to be a rehearsal of live, not a friendlier version of it.** The
two fill models had drifted apart: paper accepted orders down to $1.00 that
live rejects below $1.10, never quantized the size at all, and — worst — sold
the *entire* position into the level-1 bid regardless of that bid's depth,
reporting an exit price live could never have realized. Both executors now size
through one module (`execution/sizing.py`), and the paper sell caps at bid
depth and leaves the unsold remainder open, exactly as the live path does.
Paper P&L is now a lower-bound rehearsal rather than an optimistic one.

**An order that cannot be confirmed is not worth placing.** The venue SDK
offers no client-supplied order id, so the only way to tell "lost response" from
"real fill" is the wallet's CTF balance before and after. When that pre-order
read fails there is no recovery signal at all, and a fill that did land would
become an untracked on-chain position. The buy path now refuses to place the
order (`ctf_balance_unavailable`) rather than trade blind. The manual close and
close-all routes were the other hole: they sold positions the exit monitor
already had in flight (the row stays `open` for the seconds the on-chain order
takes), which is a second sell of tokens already gone. Both now consult and
hold the same in-flight claim as the monitors — 409 `exit_in_flight` for a
single close, skipped-and-reported for close-all.

**Sizing may scale with edge, but only once calibration says so.** New knob
`size_edge_multiplier_max`, defaulting to **1.0 — off**: at the default,
sizing is byte-for-byte what it was, `order_size_usd / held_price`, ignoring
edge entirely. Above 1.0 the notional becomes
`order_size_usd × clamp(edge / min_edge, 1.0, max)`. The reason it ships off is
that "edge" is `p_model − held_price`, and nothing so far has established that
`p_model` means what it says; betting more on a bigger number derived from an
uncalibrated probability just loses faster. So the evidence comes first: every
entry now freezes its `p_model`, `confidence` and `edge` onto the position row
(the analyzer log ring evicts a call long before the position it opened
closes), and `GET /api/analytics/calibration` buckets closed positions by the
model's probability for the side actually held, with each bucket's win rate and
mean realized return. The knob should be raised only when a bucket's win rate
sits near its own midpoint with n ≥ 100 behind it. `heat_cap_usd` still bounds
the result: the extra size a multiplier grants is trimmed to the headroom left
over open exposure — never below the base order, so the default path is
unchanged.

## 08/30/2026 — Exit policy v2: the trailing stop stops eating the trade

Live behavior exposed a defect in exit policy v1 (05/24): winners were being
closed on the *first downtick* of a move, at barely above entry. The
compounding causes, all now fixed:

**The trailing distance was measured in the wrong unit.** `peak_drawdown_pct`
was applied to the banked gain — retrace 12% *of (peak − entry)*. That
distance is tightest exactly when a move is youngest: on a $10 position that
had just armed, 12% of the gain worked out to less than one Polymarket tick
(0.01), so the very next quote closed the position. The rule now compares the
retrace against an absolute price distance:

    max(min_trail_ticks × tick_size, current_spread, peak_drawdown_pct × (peak − entry))

Never tighter than two ticks, never tighter than the book's own spread, and
widening as the move grows. New knobs: `min_trail_ticks` (default 2) and
`tick_size` (default 0.01).

**It armed far too early.** `peak_meaningful_floor_pct` was 1% of cost basis,
so on a small position the $1 USD floor did all the work and the lock engaged
after roughly a +10% move — a range where a retrace is quote noise, not
given-back profit. The floor is now 30% of cost basis: the trailing lock only
ever protects a gain worth protecting.

**Precedence made take-profit dead code.** With `peak_drawdown` evaluated
before `take_profit`, almost every winner large enough to reach +20% had
already retraced enough to close as a drawdown first — the ceiling nominally
existed but essentially never fired. Precedence is now **stop-loss →
take-profit → peak-drawdown**.

**Take-profit now ships off.** The new `take_profit_enabled` flag defaults to
`false`. This is forced by the two changes above: the trailing lock arms at
+30% of cost basis, so a +20% ceiling would close every winner *before* the
lock could ever engage and the whole trailing redesign would be inert. Under
the shipped defaults a position is therefore exited by the trailing lock once
it has run +30% or more, or by the stop-loss at -15%; `take_profit_pct` (still
0.20) is kept as an opt-in hard cap for anyone who wants one. The trade-off is
explicit and worth stating: **between entry and +30% there is no profit-taking
rule at all — a position in that band is protected only by the stop-loss**, so
a +25% gain can round-trip back to -15% without the section closing it.

**Marks now require depth.** The mark was the raw level-1 bid, whatever its
size. A single minimum-size resting order sitting away from fair value was
enough to print a loss the position never had and fire the stop. The monitor
now marks at the first bid level carrying at least `min_mark_bid_size` shares
(default 5 — above the venue's $1 minimum order at the prices traded here) and
at nothing else: there is no mid fallback, because both executors sell into the
book's raw level-1 bid, so a mid mark would evaluate take-profit and the
trailing lock against a price the position can never realize (0.40 bid / 0.72
ask marks at 0.56 and "takes profit" into a 0.40 fill). When no bid level
qualifies the position is held, counted as *blocked*, and logged once as
`no_executable_bid` so the gap is visible instead of silent. A stop-loss can no
longer fire off a bid nobody is standing behind, and a take-profit can no
longer fire off a price nobody is bidding.

**Peaks track the book, not the tick.** The peak was only sampled by the 120s
exit tick, so a run-up that happened and reversed between two ticks left no
trace and the stop trailed a peak that never existed. The order-book sampler
(60s) now pushes every observed book into the monitor's peak tracker. This is
still polling, not a live quote stream — the runtime has no push book feed —
so the limit is the sampler's interval, which is the smallest honest change
available today.

**Closing a position is now single-writer.** The exit monitor's `execute_sell`
moved onto a worker thread (`asyncio.to_thread`) so a seconds-long on-chain
sell no longer stalls the WS reconnects and market polls — but that hands the
event loop back mid-sell, and the settlement and reconciliation monitors could
then close the same position id first, leaving the real fill unpersistable. A
process-local in-flight registry (`runtime/closing_registry.py`) now holds the
id for the duration of the sell; the other two loops skip it and reconsider on
their next sweep. For the same reason `stop()` no longer just cancels the tick
loop: a sell already handed to a worker thread cannot be cancelled, so the sell
and its bookkeeping run as their own task, which shutdown drains (30s cap)
before reporting stopped.

On a synthetic path from 0.50 to 0.80 with single-tick noise, the old rules
gave the trade back at ~0.55; the shipped defaults hold through every dip and
exit at 0.76 on the trailing lock, keeping ~87% of the move.

## 06/01/2026 — The strategy canvas becomes the operating surface

The canvas page was promoted from a configuration sketchpad to the actual
control plane: a working **Run / Pause** for the pipeline, a readiness bar
that shows which sections block a start, and a calm **Paper | Live**
toggle. No change to trade logic — the change is that every strategy
parameter edit now happens where its effect is visible, and hot-reloads
into the running pipeline without a restart.

## 05/25/2026 — Kill switch: three independent brakes

Added a circuit-breaker layer in front of entry with three independently
configurable trips: `kill_max_consecutive_losses`, `kill_daily_loss_usd`,
and a drawdown brake. Rationale: at grain-scale stakes the realistic
worst case isn't one bad trade, it's a *bad afternoon* — a news regime
the model misreads repeatedly. The brakes are deliberately dumb counters,
not model-driven: when the system is wrong in a correlated way, the last
thing to trust is the same model's opinion about whether to keep going.

## 05/25/2026 — Settlement as a first-class exit

Resolved markets now settle positions automatically at the 0/1 outcome
price. Before this, a position whose market resolved just sat there.
Settlement joins the closed set of exit paths — the strategy's exit
philosophy is that a position can leave the book in exactly four ways:
**settlement, circuit-breaker, the strategy's own exit rules, or a manual
click**. There is intentionally no fifth "the system reconsidered the
thesis" path (see 05/24).

## 05/24/2026 — Live execution model: integer shares, resting limit orders

First live-capable execution policy: orders go out as limit orders at the
touch, quantized to **whole shares** so the notional always lands on
clean cents (the venue rejects sub-cent maker amounts), with a minimum
notional floor above the venue's $1 minimum. Paper and live share the
same code path through a dispatcher, so paper results stay an honest
rehearsal of live behavior.

## 05/24/2026 — Anti-churn gates on entry

Three new optional gates, all motivated by the same observation in paper
trading — the model loves re-entering markets it just lost in:

- `same_market_cooldown_minutes` — after a stop-loss, the market is
  off-limits for a window. Blocks the stop→re-enter→stop whipsaw loop.
- `same_market_lifetime_lockout` — optionally, one shot per market, ever.
- `heat_cap_usd` — a cap on total open exposure, so a burst of correlated
  news (one geopolitical narrative spawning five related markets) can't
  stack the whole book onto a single thesis.

## 05/24/2026 — Exit policy v1: three thresholds, strict precedence

The exit section settled on three rules evaluated every tick against the
held side's bid, with precedence **stop-loss → peak-drawdown →
take-profit**:

- `stop_loss_pct` (default −15%) — the absolute loss circuit, checked first.
- `peak_drawdown_pct` (default 12% retrace from the peak) — locks in
  gains, but only once the peak gain is *meaningful* (a floor in both USD
  and % of cost), so noise around entry can't trigger it.
- `take_profit_pct` (default +20%) — the absolute ceiling, checked last.

Two things were considered and **deliberately rejected**: a trailing-stop
variant with cost-adjusted peaks (more parameters than the data can
justify at this scale), and asking the LLM to reconsider open positions
("position review"). The latter is a philosophical line: the model gets
exactly one decision per thesis, at entry. Letting it re-litigate open
positions converts every losing trade into a conversation.

## 05/23/2026 — Price-move veto: don't buy news the market already priced

Entry gained an optional veto: if the market has already moved more than
`veto_move_threshold` (default 10%) within `veto_window_min` (default
60 min) of the news item, the trade is skipped. The edge model compares
model probability against the *current* price, but a price that already
jumped is evidence the news is stale or consensus — exactly the trades
where a freshness-based edge is an illusion.

## 05/22/2026 — Edge-threshold entry

The entry decision became a single inequality: trade only when the
analyzer's probability estimate diverges from the market price by at
least `min_edge` (default 5¢), with guards for `max_spread` (illiquid
books overstate edge) and `slippage_tolerance`. Fixed `order_size_usd`
per trade — position sizing is intentionally flat until there's enough
fill history to justify anything cleverer (Kelly-style sizing on a
20-trade sample is noise worship).

## 05/19–05/21/2026 — News → market matching, then an LLM with one job

The signal chain took its current shape:

- An **embedding filter** (MiniLM, cosine similarity) ranks the live
  market catalog against each news item and passes the `top_k` survivors
  above `similarity_threshold`. Cheap, local, and it means the expensive
  step only ever sees plausibly-related markets.
- An **LLM analyzer** reads the news plus the candidate markets and emits
  a probability (`p_yes`), a confidence grade, and a written rationale.
  A `min_confidence` gate drops low-conviction calls before they reach
  entry. The prompt is deliberately skeptical — stale news (>2h),
  ambiguous resolution criteria, or a direction mismatch all force a
  downgrade.

## Mid-May 2026 — Founding decision: sections, not strategies

openPoly didn't port its predecessor's strategy. Instead, the rules that
had accumulated inside a monolithic strategy were **atomized into typed,
swappable pipeline sections** (news source → embedding filter → analyzer
→ entry → exit), each with a declared config schema and its own
observability. The bet: at experimental scale, the ability to measure and
swap one rule at a time is worth more than any individual rule. Every
entry above is a consequence of that choice.

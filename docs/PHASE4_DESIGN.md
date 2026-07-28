# Phase 4 design: paper trading

No application code yet - this is the design, written up before touching
`app/paper/`, per the working agreement in CLAUDE.md. This phase matters more
than the others: a paper trader that quietly flatters itself (fills at the
signal price, never books a loss, computes a win rate off eleven trades and
calls it a result) produces false confidence, and false confidence is worse
than no paper trading at all - it's the thing that gets real money risked on
a strategy that was never actually tested. Every design choice below is
argued from that principle: realism over flattering numbers, and every
statistic refuses to answer when the sample is too small to mean anything.

## Flags and assumptions

1. **Resolution detection has no authoritative field to lean on today, and
   this needs verifying before implementation, not just noted here.**
   `GammaMarket` (`app/collectors/schemas.py`) and the `markets` table
   currently carry no `resolved`/`winningOutcome`/`payoutNumerators`-style
   field - only `closed`, `active`, `accepting_orders`, and price/liquidity
   metrics. `docs/API_REFERENCE.md` doesn't document one either. CLAUDE.md's
   working agreement is explicit: verify an endpoint's real response shape
   against `docs.polymarket.com` before coding against it, don't guess a
   field exists. Section 5 below documents a price-inference fallback rule,
   but that's what it is - a fallback for the case where Gamma genuinely
   doesn't expose settlement data, not a first choice. Before writing
   `app/paper/`, check whether Gamma's `/markets` response actually has a
   resolution field (CTF markets on Polymarket settle on-chain via
   `payoutNumerators`, and the Gamma API may surface that under a name
   `GammaMarket` doesn't currently parse). If it does, that's the primary
   rule and price-inference becomes the fallback for the rare market Gamma
   hasn't back-filled yet, not the whole rule.
2. **One decision per `(portfolio_id, signal_id)`, ever - no retries.** The
   spec says "for each ACTIVE signal the portfolio hasn't traded," which
   could mean either "no `paper_trades` row yet" (retry every cycle until
   something sticks) or "already decided, one way or another" (never
   revisit). Retrying forever would let a portfolio keep re-attempting a
   signal whose price drifted out of band, hoping it drifts back - that's
   chasing, not the disciplined behavior a portfolio simulation should
   model. Decision: the first cycle a signal clears a portfolio's entry
   filters, sizing and the fill model run exactly once and the outcome
   (`OPEN`, `MISSED`, or a sizing-skip) is final for that
   `(portfolio_id, signal_id)` pair - reinforcement of the signal later
   (more contributors, higher `weighted_score`) doesn't reopen the
   decision. This is also what makes "never double-enter" trivial to
   guarantee: existence of a row is the only check needed.
3. **A signal that never clears a portfolio's entry filters gets no row at
   all**, and is re-evaluated every cycle until it does, expires, or the
   portfolio otherwise stops considering it (see section 4a). Only a
   sizing/fill-level outcome - the signal passed the entry filters but the
   fill model rejected it, or sizing couldn't fit it - is worth a permanent
   record, because that's a real decision with a real reason attached. A
   signal that never even cleared "is this good enough for this portfolio"
   isn't a decision, it's just "not yet interesting," and recording a
   `MISSED` row for every stricter-portfolio non-match would flood
   `paper_trades` with rows that carry no information.
4. **Entry timing reference is the signal's own `created_at`, not any
   individual contributing event's `detected_at`.** The signal is the thing
   a portfolio reacts to, and multiple contributors can have different
   `detected_at` values within the same group - there's no single "the"
   event to delay from. Reinforcement doesn't move `created_at` (Phase 3,
   flag 7), so a heavily-reinforced signal's entry timing is still anchored
   to when it first appeared, which matches the "we're reacting to
   consensus forming" framing rather than "we're reacting to the latest
   top-up."
5. **Take-profit and stop-loss are plain percentage moves in price, not
   distance-to-boundary.** The spec names the two exit conditions but not
   their formula. Given `entry_price` and `current_price` both live in
   `[0, 1]`, the simplest and most standard reading is `current_price >=
   entry_price * (1 + TAKE_PROFIT_PCT)` and `current_price <= entry_price *
   (1 - STOP_LOSS_PCT)`. An alternative (distance to 0 or 1, since a
   position entered at 0.90 can't move up 50% in price-space) was
   considered and rejected for now - it's more correct but more parameters
   to explain and tune, and the plain-percentage version is what "take
   profit / stop loss" means everywhere else. Worth revisiting if baseline
   portfolios end up entering most positions above ~0.7, where the
   asymmetry starts to matter.
6. **Exposure is cost basis of open positions, checked and updated
   sequentially within a cycle, not a start-of-cycle snapshot.** Signals are
   processed within one portfolio's entry phase in `weighted_score`
   descending order (strongest consensus gets first claim on limited
   capital), and each sizing decision reads the portfolio's bankroll and
   open exposure *as they stand after every earlier fill in this same
   cycle* - not a value frozen at cycle start. This is what makes "size down
   to fit, or skip" behave sensibly when several signals compete for the
   same limited headroom in one cycle, and it's a real reading of
   "current exposure," not an invented one, but it's spelled out here
   because the spec doesn't say which of the two orderings governs.
7. **Ambiguous-resolution markets can sit open indefinitely, with no
   auto-alerting beyond the existing Logs page.** Section 5's fallback rule
   explicitly leaves a trade open and logs a warning rather than guess. The
   spec doesn't ask for anything beyond that, but it's worth naming the
   consequence: a portfolio's bankroll can end up with capital permanently
   marked-to-market-but-uncollectable if a market never resolves cleanly.
   No new alerting mechanism is proposed here - the operations console
   already has a Logs page - but this is a known, accepted gap, not an
   oversight.

None of the above are objections to the spec - they're the calls this design
makes where the spec left room, written down so they're visible rather than
buried in code, same convention as `docs/PHASE3_DESIGN.md`.

## 1. Multi-portfolio core

This is the design's central idea, not a configuration nicety bolted onto a
single trading loop: **every portfolio sees the identical signal stream and
trades it under its own rules**, so that improving the strategy is an
empirical, comparable exercise instead of a matter of opinion. Add a
challenger portfolio with a new parameter set, let it run alongside the
existing ones against the same signals for enough trades, and either it
outperforms on real numbers or it doesn't - there's no "I think the new
rule would help" without the data to back it.

`paper_portfolios` holds one row per named configuration: `name`,
`params` (JSONB - the portfolio's own copy of every tunable in section 10,
overriding the global default per key it sets), `starting_bankroll`,
`current_bankroll`, `created_at`, `is_active`. Three rows are seeded at
creation, matching the spec exactly:

- **`baseline`** - every parameter at its global default.
- **`strict`** - entry filters raised above the signal generator's own
  thresholds (higher `min_weighted_score`, higher `min_liquidity_usd`),
  betting that stricter selection beats the raw signal stream.
- **`conservative`** - smaller position sizing (`FIXED_FRACTION` at a lower
  percentage) and tighter exits (lower `TAKE_PROFIT_PCT`/`STOP_LOSS_PCT`),
  betting that protecting capital beats maximizing upside per trade.

`is_active` lets a portfolio be retired (stop taking new entries) without
deleting its history - closed trades and metrics on a deactivated portfolio
remain queryable forever, which matters for exactly the "did the challenger
actually win" comparison this whole design exists to enable.

## 2. Realistic fill model (`app/paper/fills.py`)

Pure, no DB or network access - same principle as `app/consensus/engine.py`
and `app/collectors/diffing.py` - and the most heavily tested module in this
phase, because this is where a paper trader lies to itself if it's allowed
to. A paper trade never fills at the signal's recorded price. Given a signal
and the market/price state, `simulate_fill` computes:

**1. Start from the ask, not the mid or the bid.** Buying costs the ask
price - using the mid or (worse) the bid would silently manufacture edge
that doesn't exist. The market's `best_ask` (from the `markets` table, or a
later snapshot per step 3 below) is the fill model's starting point.

**2. Add a size-scaled slippage penalty.**
```
penalty = min(PAPER_SLIPPAGE_K * (order_notional / market_liquidity), PAPER_SLIPPAGE_MAX)
fill_price = ask + penalty
```
`order_notional` is the target dollar size from `app/paper/sizing.py` (see
section 3 - sizing runs *before* the fill model specifically so this
formula has a real notional to work with, not a placeholder). `market_
liquidity` is the market's `liquidity` column. Thin markets punish harder:
the same $1,000 order against $2,000 of liquidity moves the fill price far
more than against $200,000 of liquidity, exactly mirroring what happens
against a real orderbook. `PAPER_SLIPPAGE_MAX` caps the penalty so a
near-zero-liquidity market doesn't produce an absurd fill price instead of
correctly getting rejected by drift-check (step 4).

**3. Apply the penalty at a simulated delay.** A real trader reacting to a
whale's on-chain position change is never faster than
`PAPER_ENTRY_DELAY_SECONDS` (default below) behind it - there's detection
latency, decision latency, and execution latency all bundled into one
number for simplicity. Look up the earliest `prices` row for the asset at or
after `signal.created_at + PAPER_ENTRY_DELAY_SECONDS`; if one exists, its
price is what step 1 starts from instead of the entry-time ask. If no
snapshot exists that far out yet (the signal is very fresh, or the markets
collector hasn't run since), fall back to the entry-time ask *plus a larger
penalty* (`PAPER_NO_DELAYED_SNAPSHOT_PENALTY`, added on top of step 2's
slippage) - modeling the extra uncertainty of not actually knowing what the
market did in the intervening window, rather than pretending the entry-time
price was still available to trade against.

**4. Reject the fill if the price already moved too far.** Compare the
delayed price (from step 3) against the signal's own price
(`average_entry_price`) as a fractional move. If
`abs(delayed_price - signal_price) / signal_price > PAPER_MAX_ENTRY_PRICE_
DRIFT`, the opportunity is gone - the move already happened before we could
have traded it. Return `filled=False, reason="missed_price_drift"` rather
than force a fill at a price that no longer reflects the entry thesis. This
check runs *after* delay lookup but conceptually is "did we miss it," so a
missed fill is recorded as `MISSED`, not `OPEN` - the counterfactual matters
exactly as much as the trades that happened (spec section 4a).

**Returns:**
```python
@dataclass(frozen=True)
class FillResult:
    filled: bool
    fill_price: Decimal | None
    slippage_paid: Decimal | None   # penalty actually applied, for reporting
    reason: str                     # "filled" | "missed_price_drift" | "no_market_price"
```
`reason="no_market_price"` covers the case where the market has no ask at
all (e.g. `best_ask` is null) - fail closed, never fabricate a price.

Every constant named above (`PAPER_SLIPPAGE_K`, `PAPER_SLIPPAGE_MAX`,
`PAPER_ENTRY_DELAY_SECONDS`, `PAPER_NO_DELAYED_SNAPSHOT_PENALTY`,
`PAPER_MAX_ENTRY_PRICE_DRIFT`) is a named setting per section 10 - nothing
in this module is a bare literal.

## 3. Position sizing (`app/paper/sizing.py`)

Pure, same testing bar as the fill model. Given the portfolio's current
bankroll, its current open exposure, and the signal, `size_position`
returns a **target notional** (a dollar amount), which the fill model then
turns into an actual fill price (section 2) and the engine turns into a
share count (`shares = target_notional / fill_price`, computed after the
fill price is known - sizing can't know the exact share count in advance
because it doesn't know the fill price yet, and the fill model needs the
notional as an input to its slippage formula, so the ordering is fixed:
size first, fill second, shares last).

**FIXED_FRACTION:** `target_notional = current_bankroll * fixed_fraction_pct`.
A flat percentage of bankroll per trade, independent of how strong the
signal is - `conservative`'s whole edge is this number being small.

**CONFIDENCE_WEIGHTED:**
```
multiplier = clamp(weighted_score / confidence_reference_score,
                    confidence_min_multiplier, confidence_max_multiplier)
target_notional = current_bankroll * confidence_base_fraction_pct * multiplier
```
`confidence_reference_score` is the `weighted_score` that counts as "1x"
sizing - a separate tunable, not implicitly borrowed from
`consensus_min_weighted_score`, because a portfolio's own entry filter can
set its own `min_weighted_score` independently and coupling the two would
make the sizing curve silently shift whenever the filter threshold changes.
`weighted_score` is unbounded above (it's a sum of trader weights), so the
multiplier is clamped on both ends - a merely-adequate signal shouldn't get
a near-zero position, and one enormous outlier signal shouldn't 10x a
position either.

**Exposure limits, checked in this order, for every candidate trade:**
1. Cap `target_notional` at `max_position_notional_pct * current_bankroll`
   (no single trade, however sized by the rule above, exceeds a hard
   per-position cap).
2. Cap it again at whatever headroom remains under
   `max_total_exposure_pct * current_bankroll` minus the portfolio's
   current open cost basis (see flag 6 - this is read fresh, mid-cycle).
3. If the resulting `target_notional` is below
   `paper_min_position_notional_usd`, skip the trade entirely (a $4 dust
   position isn't a meaningful simulation of anything and just adds noise
   to the trade log) rather than open it.

Returns a `SizingResult(target_notional: Decimal | None, skipped_reason:
str | None)` - `None` notional with a reason when exposure or the dust
floor kills the trade before the fill model ever runs.

## 4. Trade lifecycle (`app/paper/engine.py`)

Orchestrates the DB and the two pure modules above - same split as
`app/signals/generator.py` orchestrating `app/consensus/engine.py`. One
`now = datetime.now(UTC)` captured once per cycle and reused for every
timestamp written that cycle, matching the existing `_run_cycle(now)`
pattern in the signal generator. Runs after the consensus job (section 9),
per active portfolio, in this order:

**a. ENTRY.** For each portfolio, load every `ACTIVE` signal that has no
existing `paper_trades` row for `(portfolio_id, signal_id)` (flag 2/3),
ordered by `weighted_score` descending (flag 6). For each:
1. Check the portfolio's entry filters (its own `min_traders`,
   `min_weighted_score`, `min_combined_value_usd`, `min_liquidity_usd`,
   `max_spread` - names deliberately mirroring the global consensus/signal
   settings, but read from the portfolio's `params` JSONB, falling back to
   the matching global default per key it doesn't set). A signal that fails
   here gets no row (flag 3) - it's simply not considered this cycle and
   will be re-checked next cycle.
2. Run `sizing.size_position`. If it returns no notional, insert a
   `MISSED`-status row with `exit_reason` set to the skip reason (exposure
   or dust floor) and move on - no fill model call needed, there's nothing
   to price.
3. Run `fills.simulate_fill` with the sized notional. On `filled=True`,
   insert an `OPEN` `paper_trades` row: `entry_price` = fill price,
   `signal_price` = the signal's `average_entry_price` (kept alongside the
   fill price specifically so slippage cost is queryable per-trade later),
   `size` = `target_notional / fill_price`, `slippage_paid` from the
   `FillResult`, `entry_at = now`. Debit `current_bankroll` by
   `size * entry_price`.
4. On `filled=False`, insert a `MISSED` row with the fill model's `reason`.
   No bankroll change - nothing was spent.

**b. MARK-TO-MARKET.** For every `OPEN` trade across every portfolio, look
up the latest `prices` row for its `asset`, set `current_price` and
`unrealized_pnl = (current_price - entry_price) * size`. Runs before exit
evaluation (below) so exit conditions see this cycle's freshest price, not
last cycle's.

**c. EXIT.** For every `OPEN` trade, check in this fixed order (first match
wins, matching the engine's own filter-order precedent from Phase 3):
1. **Market resolved** (section 5) - exit at the resolution value: `1.0` if
   the held outcome won, `0.0` if it lost. This is the one exit path that
   can produce a full loss, and it's deliberately first in the order so a
   resolved market never gets miscategorized as a stop-loss or expiry exit
   instead - the resolution is the ground truth the instant it's known.
2. **Take-profit** - `current_price >= entry_price * (1 + take_profit_pct)`.
3. **Stop-loss** - `current_price <= entry_price * (1 - stop_loss_pct)`.
4. **Signal expired** - `now - signal.created_at >
   exit_on_signal_expiry_hours` (a fixed clock off the *signal's* creation,
   independent of `signal_ttl_hours` - a paper trade's exit horizon is a
   portfolio-tunable choice, not necessarily the same number the signal
   generator uses for its own housekeeping).

On any match: set `exit_price`, `realized_pnl = (exit_price - entry_price) *
size`, `exit_reason`, `exit_at = now`, status `CLOSED`. Credit
`current_bankroll` by `size * exit_price`.

**Idempotency.** Re-running a cycle (after a crash, or a scheduler retry)
never double-enters or double-exits: entry only considers signals with no
existing row for that portfolio (a natural existence check, not a separate
lock), and exit only considers `status = OPEN` trades - a trade already
`CLOSED` by an earlier, interrupted run of the same cycle is simply skipped
on re-entry into the loop. No special idempotency machinery needed beyond
"check current state before acting," same as the rest of this codebase.

## 5. Resolution detection

**Primary rule (pending the verification in flag 1):** if Gamma's `/markets`
response exposes an actual settlement/payout field once checked against
current docs, that's the rule - a market is resolved when that field says
so, and the winning outcome comes from it directly, not inferred.

**Fallback rule, if no such field exists:** a market is resolved when its
`markets` row has `closed = true`. The winning outcome is whichever outcome
token's most recent `prices` snapshot (at or after the market's `end_date`,
or simply the latest snapshot on file if `end_date` has passed and `closed`
is true) is within `PAPER_RESOLUTION_PRICE_THRESHOLD` of `1.0`. On a
binary market this also implies the other outcome is near `0.0`, so only
one side needs to clear the threshold to resolve both a winning and a
losing paper trade on that market.

**Failure mode, documented rather than papered over:** if `closed = true`
but no outcome's latest price clears the threshold (a genuinely ambiguous
close - illiquid market, a snapshot gap right at resolution, or a market
that settled at a price that never fully converged to 0/1 before our last
snapshot), do not guess. Leave any `OPEN` paper trade on that market open,
and log a `WARNING` naming the condition_id and the prices actually seen.
This is the one place in the whole design where "we don't know" is the
correct, honest answer, and it's a real state, not an error to be silently
worked around (flag 7 documents the consequence: capital can sit stuck).

## 6. Metrics (`app/paper/metrics.py`)

Pure, fully tested - same bar as sections 2 and 3, because this is the
other place self-deception can creep in (a metric computed correctly but
presented without its confidence caveat is still a lie, just a subtler
one). Given one portfolio's closed (and open, for unrealized figures)
trades:

- **Total realized PnL** - sum of `realized_pnl` over `CLOSED` trades.
- **Unrealized PnL** - sum of `unrealized_pnl` over `OPEN` trades.
- **Current bankroll** - read directly off `paper_portfolios.current_
  bankroll` (already tracked incrementally by the engine, not recomputed
  here - one source of truth for the number that actually gates new
  entries).
- **ROI %** - `(current_bankroll + unrealized_pnl - starting_bankroll) /
  starting_bankroll`.
- **Win rate** - fraction of `CLOSED` trades with `realized_pnl > 0`.
- **Average win / average loss** - mean `realized_pnl` over winning /
  losing closed trades respectively.
- **Profit factor** - gross realized profit over gross realized loss
  (`sum(wins) / abs(sum(losses))`); `None` if there are no losing trades yet
  (undefined, not infinite - reported as `None` with a note, same as the
  small-sample case below, rather than a made-up sentinel like `inf`).
- **Max drawdown** - largest peak-to-trough decline on the equity curve
  (`current_bankroll + unrealized_pnl` at each `CLOSED`/mark event, in
  chronological order), as a percentage of the peak.
- **Sharpe** - mean of per-trade realized returns (`realized_pnl /
  (entry_price * size)`, i.e. return on the capital that trade actually
  committed) over their standard deviation, annualized by the portfolio's
  observed average trades-per-year rate.

**Every ratio-shaped statistic - win rate, profit factor, Sharpe - returns
`None` alongside an explicit `"insufficient sample (n<PAPER_MIN_TRADES_FOR_
STATS)"` note when the portfolio has fewer than `PAPER_MIN_TRADES_FOR_STATS`
closed trades**, rather than a number computed from a handful of trades that
happens to look decisive. Total PnL, current bankroll, and open-position
counts are always reported regardless of sample size - those aren't
statistics being asked to generalize, they're just current facts.

## 7. Statistical honesty

This has to be prose in the design doc, not just a threshold buried in
`metrics.py`, because it's the whole reason this phase exists in a form
more careful than "run it and eyeball the numbers."

A win rate estimated from `n` trades carries a standard error of roughly
`1/(2*sqrt(n))` on the underlying probability (the usual binomial-proportion
approximation) - concretely, at 25 trades that's on the order of ±20
percentage points. A portfolio that "wins 60% of the time" after 25 trades
is statistically indistinguishable from one that wins 40% of the time; the
headline number is mostly noise dressed up as a result. The same problem
applies, differently shaped, to profit factor and Sharpe on small samples -
a single large win or loss dominates both.

**Concrete rules this design commits to:**
- No promotion decision - "challenger portfolio B beats baseline A, switch
  to B" - is made below `PAPER_MIN_TRADES_FOR_STATS` (default 30) *closed*
  trades on the portfolio being evaluated. Open/unrealized trades don't
  count toward this - they haven't produced a real outcome yet.
- When a decision is warranted, prefer profit factor and max drawdown over
  win rate. A strategy can have a low win rate and be highly profitable
  (small frequent losses, rare large wins) or a high win rate and be a slow
  bleed (frequent small wins, rare catastrophic losses) - win rate alone
  answers neither "is this profitable" nor "how much can this lose me,"
  which are the two questions that actually matter for promotion.
- Any single portfolio's results before it reaches the minimum sample are
  treated as noise, full stop, in any human-facing report of them (the
  Phase 8 dashboard's presentation of this data, when it gets built, must
  carry the same caveat the `metrics.py` `None`-return already encodes).
- **The engine only surfaces metrics. It never auto-promotes, disables, or
  reallocates between portfolios.** Promotion is a human decision made by
  looking at real numbers over a real sample - automating it would just
  move the self-deception risk up one level, from "trusting a six-trade win
  rate" to "trusting an algorithm that trusts a six-trade win rate."

## 8. New tables

**`paper_portfolios`**: `id`, `name` (unique), `params` (JSONB - see
sections 1/3/4 for which keys it can override), `starting_bankroll`,
`current_bankroll`, `created_at`, `is_active`. `starting_bankroll` and
`current_bankroll` are `NUMERIC(24,6)` via the existing `Money` alias in
`app/db/base.py`, same as every other money column in this schema.

**`paper_trades`**: `id`, `portfolio_id` (FK to `paper_portfolios`),
`signal_id` (FK to `signals`), `condition_id`, `asset`, `outcome`, `status`
(`String(10)`: `OPEN` / `CLOSED` / `MISSED` - plain string, not a native
enum, same reasoning as `position_history.event_type` and `signals.status`
in earlier phases: a new status value later is an application change, not a
migration), `size`, `entry_price`, `signal_price`, `slippage_paid`,
`entry_at`, `current_price`, `unrealized_pnl`, `exit_price`,
`realized_pnl`, `exit_reason`, `exit_at`. All money/size/price columns
`NUMERIC(24,6)` via `Money`. Indexes: composite on `(portfolio_id, status)`
(the engine's own entry/mark/exit loops all filter by exactly this pair)
and on `signal_id` (duplicate-decision lookup per flag 2, and "which
portfolios traded this signal" for later analysis).

## 9. Scheduling

A `PeriodicJob` named `"paper"` added to the existing jobs list in
`app/main.py`, on `PAPER_INTERVAL_SECONDS` (default 120s), positioned
*after* `"consensus"` in that list so it always trades on signals from the
freshest completed consensus cycle rather than racing it. This runs inside
the same `collectors` service already deployed on Railway - paper trading
is data collection's natural next step in the same process, not a new
Railway service, same as how `consensus` itself was added to `app.main`
rather than split out. No dashboard/deployment changes are implied by this
section beyond the one new job entry.

## 10. Settings

All in `app/config/settings.py`, `paper_`-prefixed for grep-ability
alongside the existing `consensus_`/`signal_` groups, none hardcoded in
`app/paper/`. Portfolio `params` JSONB overrides any of the
portfolio-scoped ones (marked below) per key it sets; unset keys fall back
to these defaults.

| Setting | Default | Portfolio-overridable |
|---|---|---|
| `paper_interval_seconds` | 120 | no |
| `paper_entry_delay_seconds` | 30 | no |
| `paper_slippage_k` | 0.5 | no |
| `paper_slippage_max` | 0.15 | no |
| `paper_no_delayed_snapshot_penalty` | 0.05 | no |
| `paper_max_entry_price_drift` | 0.15 | yes |
| `paper_resolution_price_threshold` | 0.02 (i.e. within 0.02 of 0 or 1) | no |
| `paper_sizing_rule` | `FIXED_FRACTION` | yes |
| `paper_fixed_fraction_pct` | 0.02 | yes |
| `paper_confidence_base_fraction_pct` | 0.02 | yes |
| `paper_confidence_reference_score` | 1.0 | yes |
| `paper_confidence_min_multiplier` | 0.5 | yes |
| `paper_confidence_max_multiplier` | 2.0 | yes |
| `paper_max_position_notional_pct` | 0.10 | yes |
| `paper_max_total_exposure_pct` | 0.60 | yes |
| `paper_min_position_notional_usd` | 10 | yes |
| `paper_min_traders` (entry filter) | = `consensus_min_traders` | yes |
| `paper_min_weighted_score` (entry filter) | = `consensus_min_weighted_score` | yes |
| `paper_min_combined_value_usd` (entry filter) | = `consensus_min_combined_value_usd` | yes |
| `paper_min_liquidity_usd` (entry filter) | = `signal_min_liquidity_usd` | yes |
| `paper_max_spread` (entry filter) | = `signal_max_spread` | yes |
| `paper_take_profit_pct` | 0.30 | yes |
| `paper_stop_loss_pct` | 0.20 | yes |
| `paper_exit_on_signal_expiry_hours` | = `signal_ttl_hours` | yes |
| `paper_min_trades_for_stats` | 30 | no (a sample-size floor is a statement about honesty, not a strategy choice - every portfolio is held to the same bar) |

# Phase 3 design: consensus engine and signal generation

No application code yet - this is the design, written up before touching
`app/consensus/` or `app/signals/`, per the working agreement in CLAUDE.md
(pure logic gets designed and tested before it gets wired to the DB).

## Flags and assumptions

The spec is precise almost everywhere, but a few points were either silent
or created a real behavior change worth calling out explicitly instead of
quietly deciding and moving on.

1. **Tracked-wallet count will shrink, probably a lot.** The current
   placeholder (`is_tracked` = rank ≤ `tracked_wallets_limit` in *any*
   leaderboard period) is a union across periods - our own Phase 2 smoke
   test tracked 90 wallets off a limit of 50, because MONTH and ALL each
   contributed their own top-50 and the union was bigger. The new rule
   ("top `TRACKED_WALLETS_LIMIT` wallets by score") is a single ranking, so
   the tracked set becomes *at most* 50, not a union of two 50s. That's the
   intended, better behavior (score already blends both windows), but it's
   a visible drop in wallet count the first time this ships, not a bug.
2. **combined_entry_value and weighted_score deliberately dedupe
   differently.** `weighted_score`/`distinct_traders` count each wallet
   once (a trader who both opened and later added to the same position is
   still one vote of conviction). `combined_entry_value` sums every
   qualifying event's dollar amount, so that same wallet's second buy adds
   to the dollar total. This is intentional - one metric measures "how many
   independent smart traders agree," the other measures "how much capital
   actually moved" - but the spec doesn't say so explicitly, so it's
   flagged here in case a single dedup convention was actually intended for
   both.
3. **Average entry price is size-weighted, not a naive per-event mean.**
   The spec just says "average entry price." A plain arithmetic mean would
   let a 1-share test buy and a 10,000-share position count equally, which
   would be a misleading number next to combined_entry_value. Implementing
   it as `combined_entry_value / total acted size` (a volume-weighted
   average) instead - flagging since this wasn't spelled out.
4. **Missing market data fails its filter, it doesn't pass by default.**
   `end_date`, `liquidity`, `volume_24h`, `spread`, and price are all
   nullable columns. The spec doesn't say what a NULL does at each filter.
   Decision: any filter whose required value is missing is treated as
   failed (fail-closed), with its own rejection reason - a threshold
   compared against nothing should never silently pass.
5. **Consistency's "capture cycle" = distinct `captured_at` timestamps in
   `leaderboard_snapshots`, across every period/category together**, not
   per-period. The leaderboard collector already stamps one `captured_at`
   per whole sweep (all periods, same timestamp), so "did this wallet show
   up this cycle" naturally means "does it have a row at that timestamp,
   in any period." Flagging because "capture cycle" isn't defined in the
   spec and a per-period-per-category count was a plausible alternative
   reading.
6. **"Each wallet counted once at its maximum" collapses to "counted once"
   in this design**, because weight is looked up as the wallet's single
   *current* `trader_scores.score` at generation time, not a
   snapshot-at-event-time value. Since a wallet only has one weight at
   generation time, there's nothing to take a maximum over. The
   alternative reading - storing/looking up the wallet's score as it stood
   at each individual event's `detected_at`, then taking the max across a
   wallet's own events - was not implemented, because it requires joining
   against historical `trader_scores` rows per event rather than one join
   per wallet, for a difference that only matters if a wallet's score
   materially changed *within* the freshness window. Flagging in case that
   historical-weight interpretation was actually the intent.
7. **Signal expiry is a hard 72h wall-clock from `created_at`, even for a
   heavily reinforced signal.** Per spec, expiry is `created_at +
   SIGNAL_TTL_HOURS`, and reinforcement only touches metrics and
   `updated_at`, not `created_at` or `expires_at`. So a signal that keeps
   getting fresh corroborating trades right up to hour 72 still dies on
   schedule instead of having its life extended. Implementing it exactly as
   specified (expiry is immutable once set), but flagging since an
   actively-reinforced signal arguably deserves a fresh clock, and "expiry
   never moves" was an assumption, not a stated requirement.
8. **Rejections are not persisted anywhere.** The new-tables list
   (`trader_scores`, `signals`) has no table for rejected candidates. The
   "machine-readable rejection reason" per filter is used to build the
   funnel log line for that cycle and then discarded - there's no queryable
   history of "why did group X get rejected on day Y." That matches what
   was asked for, just noting it so it's a conscious choice, not a gap
   nobody noticed.

None of the above are objections - they're the calls this design makes
where the spec left room, written down so they're visible rather than
buried in code.

## 1. Trader scoring (`app/consensus/scoring.py`)

Pure functions, same shape as `app/collectors/diffing.py`: given
`leaderboard_snapshots` rows already loaded for one wallet, compute a score
in `[0, 1]`. No DB or network access in this module - a caller elsewhere
loads the rows and hands them in.

**Why log-scale for the pnl components:** trading pnl is heavy-tailed - a
handful of wallets can post 100x the pnl of an otherwise-excellent trader on
pure position size, not skill. A linear normalization would let those
outliers dominate consensus weight completely. `log10(pnl + 1) / 6` flattens
that: it takes $1,000 to move the score from 0 to ~0.5, and a wallet needs
roughly $1,000,000 in a window to saturate at 1.0. Above that, a $5M month
and a $50M month score identically - by design, since past that point more
dollars doesn't mean more predictive skill for our purposes. `max(pnl, 0)`
before the log floors any losing or breakeven window at a 0 component
instead of an undefined/negative log.

**Why a separate month and all-time component:** `all_time_component` is a
credibility floor (has this wallet actually been good, over a long
horizon), `month_component` is recency (are they *currently* performing).
A wallet can be a strong all-time performer skating on an old hot streak, or
a currently-hot wallet with no track record - both are worth downweighting
relative to a wallet strong on both axes.

**Why consistency_component exists at all:** raw pnl, even blended across
two windows, doesn't catch a wallet that appears on the leaderboard once
after a single lucky trade and then vanishes. Consistency is the fraction of
leaderboard capture cycles in the last `SCORING_LOOKBACK_DAYS` where the
wallet shows up at all (any period, any category), capped at 1.0. A wallet
seen in every cycle scores 1.0 here regardless of its pnl; a one-week wonder
scores low even with a huge headline pnl number, which pulls its blended
score down.

**Weights:** `score = w_month * month + w_all * all_time + w_cons *
consistency`, validated at settings-load time to sum to 1.0 (a silent
mis-sum would quietly under- or over-weight every wallet). Default
0.45/0.25/0.30 leans hardest on recent performance, treats all-time as a
secondary credibility check, and weights consistency close to month - a
score built entirely from month+all-time with no consistency term would
happily hand full weight to a wallet nobody's ever seen before.

Because each component is independently clamped to `[0, 1]` and the weights
sum to 1.0, the final score is a convex combination and lands in `[0, 1]`
automatically - no extra clamp needed on the output.

## 2. Tracking rule change

`app/collectors/leaderboard.py` currently sets `is_tracked` with a
placeholder: true for any wallet ranked ≤ `tracked_wallets_limit` in *any*
fetched period, union across periods. That comment ("phase 3 replaces
this") is what this section replaces.

New rule, run immediately after every leaderboard collection cycle, in the
same transaction as the existing tracked-set refresh:

1. Load every wallet with at least one `leaderboard_snapshots` row inside
   the last `SCORING_LOOKBACK_DAYS`.
2. Score each one via `app/consensus/scoring.py`, write one `trader_scores`
   row per wallet for this cycle's `captured_at`.
3. Set `is_tracked = true` for the top `TRACKED_WALLETS_LIMIT` wallets by
   score, `false` for everyone else - same "clear everyone, then re-mark"
   atomic pattern already used for the placeholder, just ranked by score
   instead of raw leaderboard rank.

Ordering matters: scoring must run and commit *before* `is_tracked` is
recomputed in the same cycle, since the new rule's ranking depends on the
scores it just computed. This mirrors the existing invariant that
leaderboard collection must run once before the first position sweep (see
`app/main.py`) - the position collector only sweeps wallets where
`is_tracked` is true, and that flag now depends on scores that don't exist
until this step has run.

## 3. Consensus engine (`app/consensus/engine.py`)

Pure and fully unit-tested, same principle as `diffing.py` - no DB, no
network, so every filter and every metric can be tested with plain
in-memory data.

**Grouping key is `(condition_id, asset)`, not just `condition_id`.** A
market has multiple outcome tokens (Yes/No, or more); a wallet buying "Yes"
and a wallet buying "No" on the same market are making opposite bets, not
agreeing with each other. Grouping by the specific asset keeps them in
separate candidate groups, so consensus can never be manufactured by
netting out contradictory positions on the same market. This is exactly the
kind of thing the CONTEXT note about "raw position overlap dominated by
worthless leftovers" is warning about - the asset-level grouping is what
prevents a second version of that problem here.

**Candidate input:** fresh, non-bootstrap `OPENED` events, plus `INCREASED`
events (using `size_after - size_before` as the acted amount) when
`CONSENSUS_INCLUDE_INCREASES` is on, both restricted to the last
`CONSENSUS_FRESHNESS_HOURS`. Each event arrives already carrying the
wallet's current weight (looked up by the caller in `app/signals/
generator.py` from that wallet's latest `trader_scores.score` - the engine
itself never touches the DB).

**Per-group metrics:**
- `distinct_traders`: count of unique wallets contributing to the group.
- `weighted_score`: sum of each *distinct* contributing wallet's weight
  (see flag 6 - a wallet with multiple qualifying events in the group still
  counts once, since its weight is one current value).
- `combined_entry_value`: sum, over every qualifying *event* (not deduped
  by wallet - see flag 2), of `acted_size * entry_price`, where
  `entry_price` is `avg_price` if present else `cur_price`.
- `average_entry_price`: `combined_entry_value / total acted size` (see
  flag 3 - size-weighted, not a naive mean).
- `latest_market_price`: the most recent `prices` row for the asset.

**Filters**, run in this fixed order, each yielding a machine-readable
rejection code the moment it fails (a group never gets evaluated past its
first failure):

| # | Filter | Rule | Rejection code |
|---|---|---|---|
| a | market_known | a `markets` row exists for condition_id | `market_unknown` |
| b | market_open | not closed, accepting orders, `end_date` ≥ now + `SIGNAL_MIN_HOURS_TO_END` (missing end_date fails) | `market_not_open` |
| c | liquidity | `liquidity` ≥ `SIGNAL_MIN_LIQUIDITY_USD` and `volume_24h` ≥ `SIGNAL_MIN_VOLUME_24H_USD` | `insufficient_liquidity` |
| d | price_band | latest price in `[SIGNAL_PRICE_MIN, SIGNAL_PRICE_MAX]` | `price_out_of_band` |
| e | spread | market spread ≤ `SIGNAL_MAX_SPREAD` | `spread_too_wide` |
| f | breadth | `distinct_traders` ≥ `CONSENSUS_MIN_TRADERS` | `insufficient_breadth` |
| g | quality | `weighted_score` ≥ `CONSENSUS_MIN_WEIGHTED_SCORE` | `insufficient_quality` |
| h | conviction | `combined_entry_value` ≥ `CONSENSUS_MIN_COMBINED_VALUE_USD` | `insufficient_conviction` |

The ordering isn't arbitrary: a-e ask "is this market even tradeable right
now" (properties of the market alone, independent of who's in the group),
f-h ask "is this specific group's signal actually convincing" (properties
of the traders). Checking market viability first means a bad market gets
rejected on a cheap check before any per-trader math runs, and it's the
natural order for a funnel log to read in.

A group that clears every filter becomes a `SignalDraft`, carrying every
metric above plus the contributor list (see section 4).

## 4. Signal generation (`app/signals/generator.py`)

The orchestration layer - this is where the DB reads, the engine call, and
the DB writes live. Roughly, one generation cycle:

1. **Expire first.** Before building anything new, sweep: any `ACTIVE`
   signal where `now >= expires_at` or its market has closed becomes
   `EXPIRED`. Running this first means a cycle's funnel numbers and
   duplicate-detection both see a clean, current set of active signals.
2. **Build candidate groups from the DB** (fresh, non-bootstrap
   `OPENED`/`INCREASED` `position_history` rows in the freshness window,
   joined to each wallet's current `trader_scores.score` for weight),
   group by `(condition_id, asset)`, hand the groups to
   `app/consensus/engine.py`.
3. **Log one funnel line per cycle**, counting survivors at each stage, in
   the same shape as the example in the spec:
   `funnel: 214 events -> 57 groups -> market 21 -> liquidity 14 -> price
   12 -> spread 11 -> breadth 9 -> quality 6 -> conviction 5 -> new 3,
   reinforced 2`
   This is the only place rejection reasons get used (see flag 8) - they
   drive this log line, they aren't stored per-group.
4. **Duplicate detection.** At most one `ACTIVE` signal may exist per
   `(condition_id, asset)`. If a `SignalDraft` matches an existing `ACTIVE`
   signal on that key, update the existing row's metrics, contributors, and
   `updated_at` (never its `created_at` or `expires_at` - see flag 7), and
   count it as "reinforced" in the funnel. Otherwise insert a new row and
   count it as "new."
5. Every signal row is self-contained: every metric from section 3, plus
   `contributors` as a JSONB list of `{address, username, weight,
   event_type, acted_size, entry_price, detected_at}` per contributing
   event. The rule this is enforcing (and the one going into CLAUDE.md) is
   that nobody should ever need to reconstruct "why did this signal fire"
   by re-querying `position_history` - the row has to answer that by
   itself.

`CONSENSUS_INTERVAL_SECONDS` (300s default) means this becomes a fourth
scheduled job in `app/main.py`'s scheduler alongside leaderboard/positions/
markets, once this phase's code actually gets written - noted here since
this design doc doesn't touch `app/main.py`, but the wiring is implied by
having an interval setting for it at all.

## 5. New tables

**`trader_scores`** - one row per wallet per scoring cycle (append-only,
same pattern as `leaderboard_snapshots`): `wallet_id` (FK), `captured_at`,
`month_component`, `all_time_component`, `consistency_component`, `score`,
composite index on `(wallet_id, captured_at)` for "give me this wallet's
score history" and "give me this wallet's latest score" queries.

**`signals`**: `condition_id`, `asset`, `outcome`, `title`, `event_slug`,
`side` (always `"BUY"` for now - stored as a real column rather than
assumed, so the schema doesn't need a migration the day a sell-side or
short-consensus concept shows up), `status` as `String(10)` (`ACTIVE` /
`EXPIRED`) rather than a native Postgres enum - same reasoning as
`position_history.event_type` in Phase 2: adding a status value later is an
application change, not a migration. Plus every metric column from section
3, `contributors` JSONB, `created_at`, `updated_at`, `expires_at`, and a
composite index on `(condition_id, asset, status)` - that's exactly the
lookup duplicate detection needs ("is there an ACTIVE signal for this
condition_id+asset").

All money/size/price columns on both tables are `NUMERIC(24,6)` via the
existing `Money` alias in `app/db/base.py` - including the score components,
which aren't money but are bounded `[0, 1]` decimals; reusing the one
existing precise-decimal type is simpler than introducing a second numeric
type for a handful of columns, and correctness doesn't suffer since 6
decimal places is more than enough resolution for a `[0, 1]` score.

## 6. Default thresholds

All settings-driven, all in `app/config/settings.py`, none hardcoded in
`app/consensus/` or `app/signals/` - a threshold written directly into
scoring or engine code is a bug per the new CLAUDE.md rule below.

| Setting | Default |
|---|---|
| `scoring_lookback_days` | 14 |
| score weights (month / all_time / consistency) | 0.45 / 0.25 / 0.30 |
| `consensus_interval_seconds` | 300 |
| `consensus_freshness_hours` | 48 |
| `consensus_include_increases` | true |
| `consensus_min_traders` | 3 |
| `consensus_min_weighted_score` | 1.0 |
| `consensus_min_combined_value_usd` | 500 |
| `signal_min_liquidity_usd` | 5000 |
| `signal_min_volume_24h_usd` | 1000 |
| `signal_price_min` / `signal_price_max` | 0.05 / 0.95 |
| `signal_max_spread` | 0.05 |
| `signal_min_hours_to_end` | 12 |
| `signal_ttl_hours` | 72 |

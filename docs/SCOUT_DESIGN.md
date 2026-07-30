# The Scout: standalone trader-discovery service — design

No application code yet - design only, per the working agreement. Every
prior phase (3-6) assumed a fixed, already-tracked wallet set and asked "is
this signal trustworthy." The Scout asks a different, prior question: which
wallets should be in that set at all, and for how long. It is a
predict-then-verify pipeline, not a scorer - a wallet's full history earns it
a hypothesis (CANDIDATE), and only live, out-of-sample forward performance
earns it trust (VALIDATED). Nothing here places, signs, or cancels an order,
same as every other phase - the Scout's entire output is a ranked read-only
list.

## Flags and assumptions

Written down here, visible, rather than buried in code - same convention
every prior phase design doc uses.

1. **"Standalone" means a separate deployable process, not a separate
   codebase or a ban on reusing this project's own math.** The prompt is
   explicit that the Scout doesn't depend on the paper-trading *engine*
   (`app/paper/engine.py`) - it never will, since it has no portfolios, no
   fills, no sizing. But it reads the same Postgres database this whole
   project already writes to, and re-deriving Wilson intervals, CLV, or the
   zombie guard a fourth time would be exactly the kind of duplication this
   project has consistently avoided (Workstream 4 reused
   `app/risk/calibration.py`'s `wilson_interval()`; Workstream 6/7 reused it
   again). Decision: the Scout lives in its own package
   (`app/scout/`) with its own entrypoint and its own Railway service -
   deployed independently, scheduled independently, and it never imports
   `app/paper/*` - but it freely imports the pure math already proven
   correct in `app/risk/calibration.py` (`wilson_interval`),
   `app/optimization/clv.py` (`clv_value`, `resolve_horizon_clv`,
   `resolve_resolution_clv`), and reads (never writes) the crowdedness table
   the same way the dashboard already does. Exactly the same relationship
   `dashboard` already has to `collectors` - two Railway services, one repo,
   one database, each with its own process and scheduler.
2. **Discovery is bounded by what the positions collector already polls,
   and that's worth stating plainly, not glossed over.** `Wallet.is_tracked`
   "gates which wallets collectors actually poll for positions" (see
   `app/db/models/wallets.py`) - `position_history` only exists for a wallet
   that was, at some point, in `TraderScore`'s top `tracked_wallets_limit`.
   A wallet that has never once been tracked has no position history in
   this database at all, and the Scout cannot screen what was never
   collected. This is not as narrow as "only wallets tracked right now" -
   `is_tracked` is recomputed every scoring cycle, so a wallet that fell out
   of today's top-N keeps its historical `position_history` rows and stays
   screenable - but it is narrower than "every Polymarket trader." The
   Scout's real population is *every wallet ever tracked, past or present*,
   not the full public trader universe. Widening that further (polling
   positions for a broader wallet set purely for discovery, independent of
   `TraderScore`) is a real, separate improvement to
   `app/collectors/positions.py` - out of scope here, named so it's never
   mistaken for solved.
3. **Stage 1 has two different time scopes, not one, and both are named
   explicitly.** "Full-history screening" governs total trade count, the
   Wilson win-rate bound, profit factor, and ROI - computed over a wallet's
   entire resolved-trade history, no window. The activity floor
   (`SCOUT_MIN_TRADES_PER_DAY` sustained over
   `SCOUT_ACTIVITY_LOOKBACK_DAYS`) is deliberately a *separate*, recent-only
   window - a wallet that traded heavily three years ago and has gone quiet
   should not pass on historical volume alone; activity has to be current.
   Weekly consistency (majority of profitable weeks) is computed over the
   same full history as the win-rate/profit-factor stats, not the 14-day
   activity window - a wallet needs many weeks of data for "majority of
   weeks" to mean anything, and the activity window alone is too short to
   supply them.
4. **Stage 1's win-rate bound is a plain (undecayed) Wilson interval over
   raw full-history counts - it deliberately does NOT reuse Workstream 3's
   time-decay machinery.** Workstream 3
   (`app/optimization/scoring_category.py`) decays each position's
   contribution by `0.5 ** (age_days / halflife_days)` because it's
   producing a single continuously-updated live score. The Scout's Stage 1
   is a discrete, full-history pass/fail screen by design ("full-history
   screening PLUS a forward validation window" - the forward window is
   where recency enters, deliberately kept separate from the historical
   gate). Reusing decay here would blur the two mechanisms the prompt keeps
   distinct. The zombie guard and `wilson_interval()` itself ARE reused
   (flag 1); the decay weighting is not.
5. **Win rate alone cannot be "edge" on a probability-priced market, which
   is exactly why the prompt requires profit factor and ROI alongside it,
   not instead of it.** A wallet buying only near-certain 0.95-priced
   outcomes could clear a high raw win-rate bound while still losing money
   overall on its rare misses - raw win rate says nothing about stake-
   weighted edge on its own. Stated explicitly here because it's the reason
   Stage 1 is an AND of three independent conditions (win-rate bound,
   profit factor, ROI), not any one of them alone.
6. **The zombie guard's reach extends from Workstream 3's win/loss
   classification to Stage 1's dollar-weighted stats too.** Workstream 3
   forces a stuck position (still `is_open`, market `end_date` more than
   `SCOUT_ZOMBIE_GRACE_DAYS` in the past) to count as a **loss** in the
   win/loss tally. Profit factor and ROI are dollar-weighted, so a forced
   loss needs a dollar figure too: the zombie's last-observed (mark-to-
   market) `cash_pnl` and `initial_value` feed the loss side of profit
   factor and the denominator of ROI, same as a genuinely closed position
   would - the same reasoning (never let a hidden loser disappear from a
   stat) applied to every stat that could otherwise hide one, not just the
   win rate.
7. **Forward CLV reuses Workstream 2's formula, not its entry-price
   convention.** `clv_value()`/`resolve_horizon_clv()`/
   `resolve_resolution_clv()` are pure and directly reusable. But
   `signal_clv.entry_price` is a *simulated, delayed* entry (this project's
   own hypothetical fill, per `clv_entry_delay_seconds`) - the Scout isn't
   simulating a trade of its own, it's grading a wallet's actual skill on
   its own real trade. Forward CLV's entry price is the wallet's own
   `PositionHistory.avg_price` at its real `OPENED` event, no delay
   simulation. Same formula, genuinely different (and simpler) entry-price
   source.
8. **A new, fully independent `scout_*` settings namespace - not a reuse of
   `clv_horizon_hours`/`crowd_penalty_weight`/etc.** The underlying
   functions are shared (flag 1); the *tunables* are not. The Scout's
   forward-tracking cadence (`SCOUT_VALIDATION_DAYS`, weeks-to-months scale)
   operates on a completely different timescale than the paper engine's
   per-signal CLV horizon (hours), and coupling their settings would make
   one system's tuning silently move the other's. Every Scout parameter
   gets its own `scout_`-prefixed setting, defaulting to a sensible value
   independent of Phase 6's equivalents even where they happen to match
   today.
9. **The prompt names one table (`trader_pipeline`); the confirmation- and
   decay-window counting it asks for needs two more, and that's called out
   explicitly rather than silently added.** "Sustains positive forward CLV
   across `SCOUT_VALIDATION_CONFIRMATIONS` separate windows" and "drops...
   for `SCOUT_DECAY_WINDOWS` consecutive windows" both require remembering
   the pass/fail *history* of past windows, not just a wallet's current
   stage - `trader_pipeline.metrics` (current-state JSONB) can't hold a
   growing history of window outcomes without becoming its own ad hoc
   history table. Decision: `trader_pipeline` stays current-state (one row
   per wallet, exactly as specified); a new `scout_validation_windows`
   table (append-only, one row per completed forward-tracking window per
   wallet) is what `SCOUT_VALIDATION_CONFIRMATIONS`/`SCOUT_DECAY_WINDOWS`
   actually count against; a new `scout_forward_trades` table (append-only,
   one row per forward trade, the direct analogue of `signal_clv` but
   wallet-scoped instead of signal-scoped) is what a window's aggregate CLV
   is computed FROM. Named and justified here so the schema isn't a
   surprise later.
10. **Promotion requires a confidence-interval bound; decay responds to the
    point estimate.** Getting INTO `WATCHLIST`/`VALIDATED` requires the
    forward-CLV confidence interval's LOWER bound to clear zero - hard to
    fake, exactly as the prompt specifies. Falling OUT (`VALIDATED` ->
    `DECAYING`) is deliberately more responsive: it triggers on the rolling
    window's mean CLV alone crossing `SCOUT_DECAY_THRESHOLD`, not a CI
    bound. This asymmetry is intentional, not an oversight - `DECAYING` is
    cheap and auto-reversible (a wallet that recovers returns to
    `VALIDATED`, per the prompt), so the cost of a false demotion is low,
    while the cost of a false promotion (copying a wallet that was never
    really skilled) is exactly what the CI-bound requirement exists to
    prevent. Conservative to earn trust, quick to lose it provisionally.
11. **`REJECTED` is a status, not a life sentence.** The daily Stage 1 run
    re-screens every wallet with `position_history`, including previously
    `REJECTED` ones (but never touches a wallet currently mid-pipeline in
    `CANDIDATE`/`WATCHLIST`/`VALIDATED`/`DECAYING` - those are governed by
    their own stage's logic, not overwritten by the next daily screen). A
    `REJECTED` wallet that starts clearing the Stage 1 bar again becomes a
    fresh `CANDIDATE`, forward-tracked from that moment - no memory of the
    earlier rejection held against it, same "auto-recoverable" spirit as
    `DECAYING`.
12. **Crowdedness is read, not rebuilt - it already exists.** Phase 6
    Workstream 7 shipped `app/optimization/crowdedness.py` and the
    `wallet_crowdedness` table this same project-session. The prompt's
    "if not yet built, use this proxy" branch is documented here for
    completeness but not implemented: the Scout reads
    `WalletCrowdedness.crowdedness` directly, read-only, the same soft-
    penalty combination `hidden_alpha_score()` already established
    (subtract, clamp, never a hard cutoff) - see the Crowdedness section
    below.
13. **The DB view is the Prompt-S.4 dashboard page's future data source,
    not a dashboard deliverable itself.** This design produces
    `scout_copy_list` as a plain SQL view; wiring an actual Scout page into
    `app/dashboard/` is explicitly deferred to a later prompt, per the
    task. Naming this now so the view's shape is designed with that future
    consumer in mind, not as an afterthought.

None of the above are objections to the spec - they're the calls this
design makes where the prompt left room, or where it named one mechanism
(a single table, a proxy crowdedness metric) that a already-further-along
codebase makes unnecessary or insufficient on its own, written down so
they're visible rather than buried in code, same convention every prior
phase design doc in this project follows.

## Scope discipline

Read-only, same as every other phase - the Scout never places, signs, or
cancels an order, and never touches `app/paper/*`. It introduces no new
external API calls: every input (`position_history`, `positions`,
`markets`, `prices`, `leaderboard_snapshots`, `wallet_clusters`,
`wallet_crowdedness`) is already collected by the existing collectors: the
Scout is a pure downstream consumer of data this project already has,
verified against the real schema throughout this document, not a new
integration surface. It is graded on one thing: does the copy list it
produces actually outperform the raw leaderboard, going forward - see
"Rollout and success criteria."

## Grounding

Sample-size statistics need a floor before they mean anything: roughly 30
observations before a Wilson interval is usable at all, 100+ before it's
genuinely reliable. A wallet with 8 resolved trades cannot be
statistically distinguished from a lucky coin-flipper no matter how
sophisticated the formula. This is why the design is two gates, not one:
a full-history screen (so the *historical* sample is large enough to say
anything) plus a forward validation window (so *out-of-sample* performance,
not a backfit, is what earns trust) - and why an activity floor
(`SCOUT_MIN_TRADES_PER_DAY` sustained over `SCOUT_ACTIVITY_LOOKBACK_DAYS`)
exists at all: a forward window of fixed calendar length
(`SCOUT_VALIDATION_DAYS`) only accumulates a usable forward sample if the
wallet trades often enough during it. A wallet that trades twice a month
would need years, not weeks, to forward-validate - correctly excluded by
the activity floor rather than left to validate on a handful of forward
trades that mean nothing.

## Pipeline overview

```
 ALL WALLETS WITH POSITION HISTORY (daily Stage 1 screen)
        |
        | passes activity + total-trades + Wilson win-rate + profit-factor +
        | ROI + weekly-consistency (zombie-guarded)              fails -> REJECTED
        v
    CANDIDATE  ---- forward tracking begins ---->  (Stage 2, continuous)
        |
        | window 1 passes (CI lower bound > 0, >= SCOUT_MIN_FORWARD_TRADES)
        v
    WATCHLIST  ---- forward tracking continues ---->
        |
        | SCOUT_VALIDATION_CONFIRMATIONS total consecutive passing windows
        v
    VALIDATED  <----------------------.   (Stage 3, continuous)
        |                             |
        | rolling CLV < SCOUT_DECAY_THRESHOLD    a DECAYING wallet whose
        | for SCOUT_DECAY_WINDOWS consecutive     rolling CLV recovers
        | windows                                 above threshold again
        v                             |
    DECAYING  --------------------------
        |
        | stays cold (no recovery) past a further SCOUT_DECAY_WINDOWS
        v
    REJECTED  (re-screenable from Stage 1 on a future daily run)
```

`trader_pipeline` (`wallet_id` PK, `stage`, `entered_stage_at`, `metrics`
JSONB, `updated_at`) holds exactly one current row per wallet - "why did
this wallet pass or fail" is always answerable from its own row, same
"explainable from its own row" convention this project holds every other
signal/score to.

## Stage 1: historical screen

Runs daily (`SCOUT_SCREEN_INTERVAL_HOURS`, default 24) over every wallet
with at least one `position_history` row (flag 2's population).

**Raw population**, per wallet, mirroring Workstream 3's exact source
(flag 6 extends its reach, not its definition): closed `Position` rows
(`is_open = false`, `cash_pnl` and `closed_at` populated) plus "zombie"
positions (`is_open = true`, `market.end_date` more than
`SCOUT_ZOMBIE_GRACE_DAYS` in the past), forced into the loss side of every
stat computed below, always using each position's actual `cash_pnl`/
`initial_value` (Polymarket's own reported figures - independent evidence,
not re-derived from the price series, same reasoning Workstream 3 gives for
using `Position.cash_pnl` over the leaderboard's numbers).

**Gates, all required (an AND, not an OR - flag 5):**

1. **Activity.** Resolved-trade count in the trailing
   `SCOUT_ACTIVITY_LOOKBACK_DAYS` (default 14), divided by the window
   length, must be `>= SCOUT_MIN_TRADES_PER_DAY` (default 5) - a dormant or
   low-frequency wallet is rejected here regardless of historical
   profitability (flag 3: this window is independent of the full-history
   stats below).
2. **Sample size.** Total resolved trades (full history) `>=
   SCOUT_MIN_TOTAL_TRADES` (default 50) - below this, none of the
   statistics below mean anything (see Grounding).
3. **Win-rate edge, luck-controlled.** `wilson_interval(wins, n,
   z=1.96)`'s LOWER bound, over full-history (wins, n) with the zombie
   guard applied, must exceed `SCOUT_MIN_WILSON_WINRATE` (default 0.52) -
   reusing `app/risk/calibration.py`'s `wilson_interval()` directly (flag
   1), undecayed (flag 4). An 8/10 wallet ranks below an 800/1000 wallet
   even at the identical 80% raw rate, exactly the Workstream 3 precedent.
4. **Profit factor and ROI.** `profit_factor = sum(cash_pnl for winning
   qualifying positions) / abs(sum(cash_pnl for losing qualifying
   positions))`; `roi = sum(cash_pnl) / sum(initial_value)` across the same
   zombie-guarded population. No minimum values are specified in the
   prompt beyond "computed" - both are stored in `metrics` and surfaced on
   the dashboard (Prompt S.4) as supporting evidence for a CANDIDATE's
   ranking, not as an additional pass/fail gate on top of the four gates
   above; over-gating on correlated stats risks rejecting real edge for no
   statistical reason.
5. **Weekly consistency.** Bucket qualifying positions by the ISO calendar
   week of `closed_at` (or the zombie's forced-loss timestamp - the moment
   it was judged a loss, same convention Workstream 3 uses for its decay
   clock). A week with zero qualifying positions doesn't count toward
   either side of the fraction (flag 3: this isn't a penalty for a slow
   week, only for a losing one). `profitable_week_fraction =
   (weeks with positive summed cash_pnl) / (weeks with >= 1 qualifying
   position)` must exceed `SCOUT_MIN_PROFITABLE_WEEK_FRACTION` (default
   0.5, i.e. a strict majority - "not one lucky spike").

A wallet clearing all five becomes `CANDIDATE` (`entered_stage_at = now`,
starting Stage 2's forward-tracking clock from this exact instant - a
trade opened one second before becoming `CANDIDATE` is history, not
forward evidence). A wallet failing any gate is `REJECTED`, with the
specific failing gate(s) recorded in `metrics` (flag 11: re-screened daily
either way).

## Stage 2: forward tracking (verification)

Runs continuously (not on the daily cadence - `SCOUT_FORWARD_TRACKING_
INTERVAL_SECONDS`, default 3600, mirroring Workstream 2's CLV job cadence)
for every `CANDIDATE` and `WATCHLIST` wallet.

**Collection.** From the instant a wallet enters `CANDIDATE`, every new
`OPENED` `PositionHistory` event it generates is recorded into
`scout_forward_trades` (`wallet_id`, `condition_id`, `asset`, `entry_price`
- the wallet's own real `avg_price`, flag 7 - `entry_at`, `price_at_
horizon` nullable, `clv_horizon` nullable, `price_at_resolution` nullable,
`clv_resolution` nullable, `computed_at`). `price_at_horizon`/`clv_horizon`
fill in once `SCOUT_CLV_HORIZON_HOURS` (default 24) has elapsed, using the
nearest `PriceSnapshot` at/after that instant - the identical `clv_value()`
call Workstream 2 uses (flag 7), just fed a different entry price.
`price_at_resolution`/`clv_resolution` fill in once the market closes and
resolves unambiguously (reusing `detect_resolution`, never guessed).

**Windows.** A window is one `SCOUT_VALIDATION_DAYS`-long (default 14)
period, evaluated back-to-back (window 2 starts the instant window 1
ends, no gap and no overlap). At the end of each window, average forward
CLV (using `clv_horizon` for trades old enough to have it, falling back
within the window to whatever's available) and its 95% CI are computed
over every `scout_forward_trades` row whose `entry_at` falls inside that
window. If the window's trade count is below `SCOUT_MIN_FORWARD_TRADES`
(default 40), the window doesn't close yet - tracking simply continues
into the next `SCOUT_VALIDATION_DAYS` slice until enough forward trades
have accumulated (an activity-floor wallet, per Stage 1's own gate, should
clear this in one window at default settings, but never judged early
regardless). A closed window's outcome (`window_started_at`,
`window_ended_at`, `forward_trade_count`, `avg_forward_clv`, `ci_low`,
`ci_high`, `passed = ci_low > 0`) is appended to `scout_validation_windows`
- append-only, one row per completed window per wallet (flag 9).

**Promotion.**
- `CANDIDATE` -> `WATCHLIST` the moment its FIRST window closes with
  `passed = true`.
- `WATCHLIST` -> `VALIDATED` once `SCOUT_VALIDATION_CONFIRMATIONS` (default
  2) TOTAL consecutive passing windows have accumulated for this wallet
  (the window that earned `WATCHLIST` counts as confirmation 1 - at the
  default of 2, exactly one more consecutive passing window is needed).
  Worked example at the defaults: window 1 passes -> `WATCHLIST`; window 2
  passes -> `VALIDATED` (2 consecutive passes, confirmed twice, "right,
  repeatedly, out of sample"); if window 2 instead fails, the streak
  breaks and the wallet is `REJECTED` (its history looked good, the edge
  didn't hold up forward - flag 5's whole point).
- Any window failing while `CANDIDATE` or `WATCHLIST` (not yet
  `VALIDATED`) - `passed = false` - moves the wallet straight to
  `REJECTED`. No partial credit: the prompt is explicit that a flat/
  negative forward CLV means history looked good but didn't materialise,
  not "try again."

## Stage 3: decay monitoring

Runs continuously, same cadence as Stage 2, for every `VALIDATED` wallet -
forward tracking never stops for a wallet once `VALIDATED`; the exact same
`scout_forward_trades`/window mechanism keeps running, it just now feeds
decay detection instead of promotion.

A `VALIDATED` wallet whose most recently closed window has
`avg_forward_clv < SCOUT_DECAY_THRESHOLD` (default `0` - the rolling CLV
turning non-positive at all, a point-estimate trigger, not a CI bound -
flag 10) for `SCOUT_DECAY_WINDOWS` (default 2) consecutive windows moves to
`DECAYING`, logged with the window data that triggered it. A `DECAYING`
wallet is excluded from the copy list's default view (see Output) but
keeps forward-tracking exactly like a `VALIDATED` one:
- a window closing with `avg_forward_clv >= SCOUT_DECAY_THRESHOLD` returns
  it to `VALIDATED` immediately (flag 10: cheap, auto-reversible).
- `SCOUT_DECAY_WINDOWS` further consecutive sub-threshold windows while
  `DECAYING` (i.e. decay confirmed, not just a single bad window twice)
  moves it to `REJECTED` - still re-screenable from Stage 1 later (flag
  11), but off the copy list until it re-earns `CANDIDATE` and re-validates
  from scratch.

## Crowdedness

Read directly from `wallet_crowdedness` (flag 12) - already built, already
scored daily. The Scout never recomputes it and never uses the prompt's
fallback proxy (public leaderboard rank + follower-reaction), since both of
those are already exactly what `follower_reaction`/`leaderboard_fame`
measure inside the real `crowdedness` score. The goal, stated plainly: a
copy list of HIDDEN validated skill, not FAMOUS validated skill - a wallet
everyone already watches has less room left to be profitably copied, even
if its edge is real.

Combination mirrors `hidden_alpha_score()` exactly (soft penalty, clamped,
never a hard cutoff - a very-skilled, very-crowded wallet still ranks
respectably, never zeroed): `scout_rank_score = clamp(f(wilson_low,
avg_forward_clv) - SCOUT_CROWD_PENALTY_WEIGHT (default 0.3) * crowdedness,
0, upper_bound)`, where `f` combines the historical Wilson lower bound and
current forward CLV into one ranking number (documented, not hidden, in
the copy-list view's own SQL - see Output). A wallet absent from
`wallet_crowdedness` (never computed yet) defaults to 0 crowdedness - no
penalty without evidence, same convention `hidden_alpha_weighted_score()`
already established.

## Output

**`scout_copy_list`**, a plain SQL view (created via a raw-SQL Alembic
migration, same as every other schema change in this project) joining:
`trader_pipeline` (`stage = 'VALIDATED'`), the wallet's latest
`scout_validation_windows` row (forward CLV + CI + how many consecutive
confirmations it's carrying), `trader_pipeline.metrics` (the stored
Wilson-bounded historical edge, profit factor, ROI, activity rate), and
`wallet_crowdedness.crowdedness` (defaulting to 0 via a `LEFT JOIN` where
absent) - ranked by `scout_rank_score` descending. `DECAYING` wallets are
visible in the underlying `trader_pipeline` table (nothing is hidden - the
"why is a wallet no longer on the list" question stays answerable) but
excluded from `scout_copy_list` itself by definition (`stage = 'VALIDATED'`
only) - a decaying trader is, by design, not currently on the copy list.

The Prompt-S.4 dashboard page queries this view directly - out of scope
for this design (flag 13), but the view's column shape (wallet, Wilson-
bounded historical edge with its sample size, forward CLV with its CI and
window count, time since `VALIDATED`, activity rate, crowdedness,
`scout_rank_score`) is exactly what that page will render, designed with
that consumer in mind now rather than reshaped later.

**Success criterion, stated explicitly so this can fail honestly:** the
`scout_copy_list`, tracked forward via the same CLV machinery that
validates it, should show higher average CLV than the raw Polymarket
leaderboard's own top-N traders over the same forward period. This is not
assumed - it's the exact thing continued forward-CLV tracking exists to
prove or disprove, the same champion/challenger discipline every prior
phase in this project applies to its own claims.

## Statistical honesty

- Every stat this service produces carries its sample size and, where
  applicable, its confidence interval alongside it - never a bare number.
- Nothing is trusted below its stated minimum sample
  (`SCOUT_MIN_TOTAL_TRADES` historically, `SCOUT_MIN_FORWARD_TRADES` per
  forward window) - a window short of the floor simply keeps accumulating
  rather than reporting a premature verdict.
- Forward CLV, not reported PnL, is the arbiter at every promotion/decay
  decision - PnL is gameable (a wallet can look profitable by luck, by
  hiding losers as zombies, or by cherry-picking near-certain bets); CLV
  measures whether the market moved the wallet's way after it acted,
  independent of whether any one bet happened to resolve favourably yet.
- The vocabulary is deliberately not interchangeable: a `CANDIDATE` is
  "historically promising, unverified." A `WATCHLIST` wallet is "promising,
  one out-of-sample window in." Only `VALIDATED` means "verified,
  repeatedly, out of sample" - and the dashboard (Prompt S.4) is expected
  to show the distinction as plainly as this document does, never
  collapsing "promising" and "validated" into one undifferentiated "good
  wallet" label.

## Cross-cutting

**Everything here is offline/scheduled** - Stage 1 daily
(`SCOUT_SCREEN_INTERVAL_HOURS`), Stages 2-3 on a shared continuous cadence
(`SCOUT_FORWARD_TRACKING_INTERVAL_SECONDS`) - run by the Scout's own
scheduler loop (`app/scout/main.py`, the same `PeriodicJob`/`run_jobs`
shape `app/main.py` already uses), inside its own Railway service, reading
the shared Postgres. No hot path, no order execution, nothing reacting to
a single price tick - same scope discipline every prior phase holds to.

**New tables:**
- `trader_pipeline` (`wallet_id` FK PK, `stage` String, `entered_stage_at`,
  `metrics` JSONB, `updated_at`) - current-state, one row per wallet
  (flag 9).
- `scout_forward_trades` (`id` PK, `wallet_id` FK, `condition_id`, `asset`,
  `entry_price`, `entry_at`, `price_at_horizon` nullable, `clv_horizon`
  nullable, `price_at_resolution` nullable, `clv_resolution` nullable,
  `computed_at`) - append-only, the wallet-scoped analogue of `signal_clv`
  (flag 7/9).
- `scout_validation_windows` (`id` PK, `wallet_id` FK, `window_started_at`,
  `window_ended_at`, `forward_trade_count`, `avg_forward_clv`, `ci_low`,
  `ci_high`, `passed` Boolean) - append-only, one row per completed
  forward-tracking window per wallet (flag 9).
- `scout_copy_list` - a SQL view, not a table (see Output).

**Settings (all `scout_`-prefixed, independent of every other phase's
settings - flag 8; none hardcoded in `app/scout/`):**

| Setting | Default | Stage |
|---|---|---|
| `scout_screen_interval_hours` | 24 | 1 |
| `scout_min_trades_per_day` | 5 | 1 |
| `scout_activity_lookback_days` | 14 | 1 |
| `scout_min_total_trades` | 50 | 1 |
| `scout_min_wilson_winrate` | 0.52 | 1 |
| `scout_min_profitable_week_fraction` | 0.5 | 1 |
| `scout_zombie_grace_days` | 14 | 1 (mirrors `ranking_zombie_grace_days`' default, independently tunable) |
| `scout_forward_tracking_interval_seconds` | 3600 | 2/3 |
| `scout_clv_horizon_hours` | 24 | 2/3 (mirrors `clv_horizon_hours`' default, independently tunable) |
| `scout_validation_days` | 14 | 2 |
| `scout_min_forward_trades` | 40 | 2 |
| `scout_validation_confirmations` | 2 | 2 |
| `scout_decay_threshold` | 0 | 3 |
| `scout_decay_windows` | 2 | 3 |
| `scout_crowd_penalty_weight` | 0.3 | crowdedness (mirrors `crowd_penalty_weight`'s default, independently tunable) |

## Rollout and success criteria

Stage 1 ships first and stands alone as a useful daily report even before
Stage 2 exists (a list of historically-strong, luck-controlled candidates
is already more than the raw leaderboard offers). Stage 2 depends on
Stage 1 (nothing to forward-track without a `CANDIDATE`) and on Workstream
2's CLV formula. Stage 3 depends on Stage 2's window mechanism existing
first. Crowdedness integration depends on Workstream 7 - already shipped,
so it lands with Stage 1/2 rather than as a deferred follow-up.

- **Stage 1:** candidate count vs. total screened-wallet count, and the
  distribution of *why* wallets fail (which gate, most often) - a near-zero
  candidate rate is a real, useful answer (either genuinely few wallets
  clear this bar, or a threshold is miscalibrated - both worth knowing, not
  assumed to be a bug).
- **Stage 2:** `CANDIDATE` -> `WATCHLIST` -> `VALIDATED` conversion rates,
  and the split between "never passed window 1" vs. "passed once, failed
  the confirmation" - the direct evidence for whether Stage 1's historical
  screen is actually predictive of forward performance, or just fitting
  history.
- **Stage 3:** how often a `VALIDATED` wallet decays, and how often a
  `DECAYING` one recovers vs. gets fully rejected - the direct measure of
  how long a validated edge tends to last before this system needs to
  refresh its own copy list.
- **Overall (the stated success criterion):** `scout_copy_list`'s forward
  CLV vs. the raw leaderboard's top-N forward CLV, tracked continuously
  once both Stage 2's mechanism and enough calendar time exist to compare
  them honestly - the answer this whole design exists to produce, not to
  assume.

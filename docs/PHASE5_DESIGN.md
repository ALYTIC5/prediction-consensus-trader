# Phase 5 design: risk management and dynamic position sizing

No application code yet - this is the design, written up before touching
`app/risk/`, per the working agreement in CLAUDE.md. Phase 4 gave every
portfolio the same two placeholder sizers (`FIXED_FRACTION`,
`CONFIDENCE_WEIGHTED`) with a couple of caps folded directly into
`size_position()`. That was correct for standing paper trading up at all,
but it means risk control today is scattered - two caps live inside the
sizer, nothing polices market or event concentration, and nothing can pull
the plug on a portfolio that is bleeding out. This phase pulls every risk
decision into one auditable layer that every sizer sits behind, and adds a
second, more principled sizer that only turns on once there is enough
resolved-trade history to trust it. The two ideas this phase is built on,
stated once here so every section below can just apply them: **sizing
survives or kills a strategy faster than edge does** (a strategy with a
real edge and reckless sizing goes broke; a strategy with a small edge and
disciplined sizing compounds), and **never full Kelly** (full Kelly on a
sequence of even-money bets carries roughly an f-chance of drawing down to
an f-fraction of starting bankroll before recovering, and betting 2x the
full-Kelly fraction has zero expected long-run growth despite every
individual bet still having positive expected value - the fraction is not
a suggestion, it is the difference between growth and ruin).

## Flags and assumptions

1. **"RiskManager first, then the sizer" is only half the picture - it
   actually runs twice, once before and once after.** Read literally, the
   prompt's ordering can't work: several of RiskManager's own rules
   (`max_position_size`, and `max_total_exposure`'s resize-to-fit) need a
   candidate dollar size to evaluate against, and no size exists until a
   sizer has proposed one. But the other rules
   (`daily_stop_loss`, `min_bankroll_halt`, `emergency_stop`,
   `max_open_positions`, and the "is there any room left at all" half of
   `max_market_exposure`/`max_correlated_exposure`) need no size at all -
   they're pure preconditions, and checking them before running a sizer
   (especially Stage 3's calibration lookup) avoids wasted work on a
   portfolio that's halted anyway. Decision: `RiskManager.pre_check()` runs
   first as a deny-only gate (no size involved); if it passes, the
   selected sizer proposes a raw target notional; `RiskManager.apply()`
   then takes that proposal and returns the final allow/resize/deny against
   the size-dependent rules. Both calls go through the same `RiskManager`
   and the same rule table - it's one component, described as running
   "first" and "after sizing" rather than only "first," because that's what
   the individual rules actually require.
2. **Precedence across rules: deny beats resize beats allow; a deny
   short-circuits, but resize doesn't.** The consensus engine's own
   `evaluate_group` rejects on the first filter a candidate fails
   (`app/consensus/engine.py`) - RiskManager's deny-capable rules follow
   the same first-failure-wins pattern, in the order listed in the prompt
   (`max_open_positions` and the halt-style rules before the exposure caps,
   since there's no point computing an exposure resize for a portfolio
   that's halted). But resize is different: `max_position_size` might allow
   $50 while `max_total_exposure`'s remaining headroom only allows $30 -
   stopping at the first resize-capable rule and taking $50 would silently
   ignore the tighter one. So every resize-capable rule that applies is
   evaluated, and the *minimum* of their proposed sizes is the final
   resize. If any deny-capable rule fires anywhere in this pass, deny wins
   outright regardless of what any resize would have allowed.
3. **`daily_stop_loss`'s "day-start bankroll" is derived, not stored.**
   There's no daily bankroll snapshot table today, and adding one purely to
   answer "what was bankroll at UTC midnight" is more state than the
   question needs. Instead: `day_start_bankroll = current_bankroll -
   sum(realized_pnl of trades exited today) + sum(cost basis of trades
   entered today, still open)` - reversing today's exits (which credited
   bankroll) and today's entries (which debited it) reconstructs the
   midnight value from `paper_trades` rows alone, filtered on
   `entry_at`/`exit_at` falling in the current UTC day. This is exact as
   long as the engine ran continuously through the day; a portfolio only
   marked-to-market right at a process restart could misjudge the boundary
   by one cycle, which is the same class of accepted gap Phase 4 already
   lives with for ambiguous resolutions (section 7 of that design) rather
   than a new problem.
4. **Kelly's `c` (the price used in `f = (p_hat - c) / (1 - c)`) is
   already the slippage-adjusted fill price - there is no separate fee to
   fold in yet.** The prompt says "fees reduce net odds," which is correct
   professional-consensus guidance, but this codebase has never confirmed a
   Polymarket trading fee schedule against `docs.polymarket.com` (CLAUDE.md
   is explicit: verify before coding against an assumed field, don't
   guess), and `app/paper/fills.py` models slippage only. Today, `c` is
   `paper_trades.entry_price` as already computed by `compute_fill` -
   slippage-adjusted, fee un-adjusted because there is no confirmed fee to
   subtract. If a real fee schedule is confirmed later, a
   `risk_kelly_fee_pct` setting should be added and subtracted from the
   edge at that point - not invented here.
5. **Calibration will spend a long time falling back to Stage 2, and
   that's correct, not a bug.** Paper trading has produced on the order of
   dozens of resolved trades total across all ten portfolios as of this
   writing, not the hundreds-per-bucket that `CALIBRATION_MIN_SAMPLES_PER_
   BUCKET` implies is needed to trust a `p_hat`. Cross a trader-count band,
   a score band, and a price band against each other and the bucket count
   multiplies fast - most buckets will sit at zero or one resolved trades
   for a long time. A `kelly`-sizer portfolio will therefore spend most of
   its early life quietly sized by Stage 2 under the hood, and that's
   exactly what "gated behind data" means; it isn't a sign Stage 3 is
   broken.
6. **Recency: a rolling window, not a decay-weighted average.** The prompt
   flags regime change (old resolutions going stale) as a real failure
   mode and suggests either weighting recent trades more or using a
   rolling window. Decision: a rolling window
   (`CALIBRATION_WINDOW_DAYS`, default 90) rather than a decay function -
   "these are the N trades that counted, resolved in the last 90 days" is
   a sentence anyone can audit by re-running the same query; "each trade
   counts `exp(-age/tau)` as much" is not, and this project's own
   conventions favor a number you can explain from a single row over a
   smoother that isn't. `CALIBRATION_WINDOW_DAYS` is itself a setting, so
   this can be revisited once there's enough history to tell whether 90
   days throws away too much or too little.
7. **Bucket dimensions need their own bands, same as Stage 2's score
   bands - and the bucket count needs naming so the sparse-bucket problem
   in flag 5 isn't abstract.** `distinct_traders` and entry price are both
   continuous-ish inputs same as `weighted_score`; each needs its own
   settings-driven band edges (`CALIBRATION_TRADER_BANDS`,
   `CALIBRATION_PRICE_BANDS`, alongside Stage 2's existing
   `TIERED_SCORE_BANDS` shape) rather than inventing a second banding
   scheme. Three dimensions with even a modest 3-4 bands each is 27-64
   buckets before a single trade has resolved into any of them - naming
   this here is what makes flag 5's "expect a long fallback period" a
   concrete, checkable claim instead of a hand-wave.
8. **`event_slug` needs to live on `paper_trades` directly, not just be
   reachable through a join.** `max_correlated_exposure` groups open
   positions by event (`signals.event_slug`, already populated at signal
   creation from the market row - see `app/signals/generator.py`'s
   `_build_groups`), but `paper_trades` today only denormalizes
   `condition_id`/`asset`/`outcome` from its signal, not `event_slug`.
   Every other cross-market grouping this design needs
   (`max_market_exposure` on `condition_id`, `max_correlated_exposure` on
   `event_slug`) should be answerable from `paper_trades` alone, matching
   CLAUDE.md's "every signal must be explainable from its own row"
   convention already applied to the columns that are there - so
   `event_slug` is added to `paper_trades` in this design (section 8),
   extending the prompt's own column list by one.
9. **`emergency_stop` is a boolean in the existing adjustable-settings
   registry, not a new mechanism.** The prompt already calls it "a runtime
   override key," which is precisely what `app/config/adjustable.py` /
   the Tuning page already is - a `paper_emergency_stop` entry added to
   `ADJUSTABLE` gets a live toggle, an audit trail (`RuntimeOverride`
   already logs every change), and a dashboard control for free, with zero
   new plumbing.
10. **`risk_events` logs deny/resize/halt, not every routine allow.**
    CLAUDE.md's own convention - "every rejected candidate is accounted
    for in the funnel log with a reason" - already describes exactly this
    shape for `paper_trades.rejection_reason`. Logging every `ALLOW`
    decision too would add one `risk_events` row per trade attempt forever
    for information that's already implicit (a trade with no matching
    `risk_events` row this cycle was allowed). The table exists to explain
    the exceptions, not restate the routine case.
11. **Stage 1's `max_position_size`/`max_total_exposure` replace, not
    duplicate, the two caps `size_position()` already enforces inline
    today.** `app/paper/sizing.py`'s current `size_position()` already caps
    at `max_position_notional_pct` and checks exposure headroom against
    `max_total_exposure_pct` - correct for a world with one sizer, wrong
    once there are three (`FIXED`/`TIERED`/`KELLY`), because those two caps
    would otherwise need reimplementing identically in every sizer. This
    design moves both out of the sizer and into `RiskManager.apply()`, so
    every sizer's job shrinks to "propose a raw target," and every
    guardrail applies uniformly regardless of which sizer produced the
    proposal. `paper_max_position_notional_pct` and
    `paper_max_total_exposure_pct` are renamed to the `risk_`-prefixed
    settings in section 9 to reflect that they're no longer sizer-owned.

None of the above are objections to the spec - they're the calls this
design makes where the spec left room, written down so they're visible
rather than buried in code, same convention as `docs/PHASE4_DESIGN.md`.

## 1. `RiskManager` (`app/risk/manager.py`)

A pure component - no DB session, no network - that takes plain values in
and returns a plain decision out, same shape as `app/paper/engine.py`'s
existing `passes_entry_filters`/`check_exit`. Two entry points, per flag 1:

```
RiskManager.pre_check(context: RiskContext) -> RiskDecision
RiskManager.apply(context: RiskContext, proposed_notional: Decimal) -> RiskDecision
```

`RiskContext` bundles everything a rule might need: current bankroll,
starting bankroll, today's realized PnL so far, open positions (for
exposure/market/correlated totals), the candidate's `condition_id` and
`event_slug`, and the per-portfolio rule configuration (see section 6).

`RiskDecision` is `(action: ALLOW | RESIZE | DENY, resized_notional:
Decimal | None, rule: str | None, reason: str, detail: dict)` -
`rule`/`reason` are machine-readable (`StrEnum` values, same convention as
`RejectionStage`/`ExitReason` in `app/paper/engine.py`), `detail` carries
the numbers that made the decision (e.g. `{"cap_pct": "0.10",
"bankroll": "100.00", "requested": "12.00", "allowed": "10.00"}`) so a
`risk_events` row (section 8) is self-explanatory without re-deriving the
math later.

Every rule below is independently toggleable
(`risk_<rule>_enabled: bool`) and every threshold is portfolio-overridable
through the same `params` JSONB mechanism Phase 4 already uses (`_get_param`
in `app/paper/engine.py`), falling back to the global setting when a
portfolio doesn't override it.

## 2. Stage 1 guardrail rules

Deny-capable, evaluated in `pre_check` (no candidate size needed):

- **`max_open_positions`**: `len(open_trades) >= RISK_MAX_OPEN_POSITIONS`
  denies outright. A hard concurrency cap independent of dollar exposure -
  ten $1 positions and two $500 positions can both be "under budget" by
  dollars while one is clearly harder to actually track and reason about.
- **`daily_stop_loss`**: if today's realized PnL (trades exited today, UTC)
  is `<= -RISK_DAILY_STOP_LOSS_PCT * day_start_bankroll` (flag 3's derived
  value), deny all new entries until the next UTC day. Existing open
  positions are unaffected - they still mark-to-market and exit normally
  through `_mark_to_market`/`_run_exits`; this only blocks new entries.
- **`min_bankroll_halt`**: if `current_bankroll < RISK_MIN_BANKROLL_PCT *
  starting_bankroll`, deny all new entries indefinitely (not just for the
  day) and log at `CRITICAL` - the paper "circuit breaker." Recovery is
  manual: an operator re-activates the portfolio's entries by adjusting
  the halt threshold or accepting the portfolio has failed, not an
  automatic reset.
- **`emergency_stop`**: if the `paper_emergency_stop` adjustable override
  is `true`, deny all new entries across every portfolio immediately (flag
  9). Checked first among the deny-capable rules, since it's the one an
  operator reaches for when something's actively wrong and every other
  check is irrelevant next to "stop everything now."

Resize-capable, evaluated in `apply` (candidate size required), all
following flag 2's "evaluate every applicable one, take the minimum"
rule:

- **`max_position_size`**: no single trade's notional may exceed
  `RISK_MAX_POSITION_PCT * current_bankroll`, full stop - this cap wins
  even over what Stage 2/3's sizer recommends, per the prompt's explicit
  instruction that the hard ceiling always overrides the sizing model.
- **`max_total_exposure`**: sum of open position notionals plus the
  candidate must not exceed `RISK_MAX_EXPOSURE_PCT * current_bankroll`; a
  breaching candidate is resized down to the remaining headroom, or denied
  if headroom is already zero.
- **`max_market_exposure`**: sum of open notionals already in this
  `condition_id`, plus the candidate, capped at
  `RISK_MAX_MARKET_EXPOSURE_PCT * current_bankroll` - stops many
  correlated signals in the same single market (e.g. reinforcement pushing
  a portfolio to keep adding to the same position) from concentrating past
  what the per-position cap alone would allow.
- **`max_correlated_exposure`**: same idea, grouped by `event_slug`
  instead of `condition_id` (flag 8) - five different outcomes of the same
  event ("who wins the 2026 World Cup") are five correlated bets on
  essentially one belief, not five diversified ones, and this rule caps
  the combined exposure across all of them at
  `RISK_MAX_CORRELATED_EXPOSURE_PCT * current_bankroll`.

## 3. Stage 2: tiered confidence sizing (`app/risk/sizing_tiered.py`)

Pure function, no DB/network, same shape as today's `size_position`. Maps
a signal's `weighted_score` to a bankroll fraction via a configurable list
of `(min_score, max_score, fraction_pct)` bands
(`RISK_TIERED_SCORE_BANDS`), defaulting to:

| Score band | Fraction |
|---|---|
| 1.0 - 1.5 | 0.5% |
| 1.5 - 2.0 | 1.0% |
| 2.0 - 3.0 | 2.0% |
| > 3.0 | 3.0% |

`raw_notional = current_bankroll * fraction_for(weighted_score)`. That's
the entire function - no exposure/position caps here, those moved to
`RiskManager.apply()` per flag 11. Documented in the module docstring, not
just here, as an honest heuristic bridge: it scales with consensus
strength, which is strictly better than a flat fraction, but it is not
edge-optimal and doesn't claim to be - it's what a `TIERED` or a
calibration-starved `KELLY` portfolio uses until Stage 3 has enough
resolved history to trust its own numbers instead.

## 4. Stage 3a: consensus-to-probability calibration (`app/risk/calibration.py`)

The one rule this whole stage is built around: **we do not invent a
win probability, we measure one.** A `p_hat` that isn't measured from this
project's own resolved trades is a guess wearing a Kelly formula's
clothing, and would be worse than the honest heuristic in Stage 2.

**Bucketing.** Every resolved (`CLOSED`) paper trade across all
portfolios (not just one - more shared history means less sparse buckets,
and calibration is a property of the signal-generation process, not of any
one portfolio's own rules) is assigned a bucket key from three banded
features, each with settings-driven edges (flag 7):

- `distinct_traders` banded by `CALIBRATION_TRADER_BANDS` (default e.g.
  `[2,3), [3,5), [5,∞)`)
- `weighted_score` banded by `CALIBRATION_SCORE_BANDS` (mirrors Stage 2's
  band shape, independently configurable)
- `average_entry_price` banded by `CALIBRATION_PRICE_BANDS` (default e.g.
  `[0,0.3), [0.3,0.7), [0.7,1)`)

**Empirical hit rate.** For each bucket, `p_hat = (count of resolved
trades in this bucket whose held outcome won) / (count of resolved trades
in this bucket)`, restricted to trades resolved within the last
`CALIBRATION_WINDOW_DAYS` (flag 6), with a confidence interval (Wilson
score interval - well-behaved at the small sample sizes this will see for
a long time, unlike a naive normal-approximation interval). A bucket with
fewer than `CALIBRATION_MIN_SAMPLES_PER_BUCKET` qualifying trades is
**not trusted**: `calibrate(features) -> p_hat | None`, and `None` means
"Stage 3 has nothing to say for this signal" - the caller (section 6)
falls back to Stage 2 for that one signal, not for the whole portfolio.

**Named failure modes** (the prompt asks for these explicitly, not just a
happy-path description):

- *Sparse buckets* - the default, for a long time, per flag 5. Handled by
  the `None`-means-fall-back contract above, not by lowering the
  min-samples threshold to force an answer.
- *Regime change* - old resolutions may no longer describe current market
  behavior (e.g. Polymarket's user base, or this project's own tracked
  wallet list, shifts). Handled by the rolling window (flag 6), which
  ages out stale trades rather than weighting them down forever.
- *Survivorship in the signal set* - the resolved-trade population this
  calibrates against is exactly the set of signals that passed every
  upstream filter (consensus thresholds, entry filters, the fill model).
  A bucket's `p_hat` describes "signals of this shape that were good
  enough to become a paper trade," not "signals of this shape in
  general" - if upstream thresholds change, old buckets describe a
  population that no longer exists, which is really the same failure mode
  as regime change wearing a different name, and the same rolling window
  mitigates it the same way.

## 5. Stage 3b: fractional Kelly sizing (`app/risk/sizing_kelly.py`)

Pure function. Given `p_hat` (from calibration) and `c` (the candidate's
expected fill price - flag 4 on what "adjusted for fees" means today):

```
edge = p_hat - c
f_kelly = edge / (1 - c)
```

the standard binary-market Kelly fraction for a bet that pays $1 if the
held outcome wins, bought at price `c` per share. If `f_kelly <= 0`, this
is not a trade - the model is saying there's no edge (or negative edge)
at this price, and the signal is skipped (recorded, per section 8, as a
sizing-stage skip with `edge` stored so the "why" is on the row itself,
same spirit as `SizingSkipReason.BELOW_MIN_NOTIONAL` today).

If `f_kelly > 0`: `raw_notional = current_bankroll * f_kelly *
RISK_KELLY_FRACTION` (default **0.25** - quarter Kelly, because `p_hat` is
a measured estimate with real uncertainty, not a known probability;
`RISK_KELLY_FRACTION` must never be configured above 0.5 - enforced as a
`Settings` validator, not just a docstring warning, same pattern
`Settings._validate_paper_sizing_rule` already uses for `paper_sizing_rule`
today). This is deliberately the only sizer of the three that can refuse
to trade at all rather than just proposing a small size - a heuristic
sizer (Stage 2) always has *some* fraction to propose; a calibrated one is
allowed to say "no edge here," which is the entire point of measuring
`p_hat` instead of assuming one.

The resulting `raw_notional` still goes through `RiskManager.apply()` like
every other sizer's output (flag 11) - Kelly's own math has no concept of
"but what about this market's total exposure," and isn't meant to; the
hard caps are a separate, always-on layer that wins regardless of what any
sizing model - Kelly included - recommends.

## 6. Sizer selection and portfolio wiring

A portfolio param, `paper_sizer` (`FIXED` | `TIERED` | `KELLY`), picks
which of sections 3/5/the existing `FIXED_FRACTION`/`CONFIDENCE_WEIGHTED`
logic produces the raw notional for that portfolio's trades - `FIXED`
keeps today's Phase 4 behavior unchanged (both existing sizing rules stay
available for it, since this phase adds new sizers rather than removing
old ones), `TIERED` always uses section 3, `KELLY` uses section 5 with an
automatic per-signal fallback to section 3 whenever `calibrate()` returns
`None` for that signal's bucket (never a hard failure, and never a fall
back to `FIXED` - the point of `TIERED` existing at all is to be `KELLY`'s
safety net).

A fourth paper portfolio, `kelly`, is seeded (alongside the existing
`baseline`/`strict`/`conservative` and the strategy-experimentation set
from `app/paper/strategies.py`) with `paper_sizer=KELLY` and otherwise
`baseline`'s entry filters, so it competes head-to-head against
`baseline` on the identical signal stream, differing in exactly one
dimension - this is how the project finds out empirically whether
calibrated sizing actually beats fixed/tiered sizing on its own numbers,
rather than assuming a more sophisticated model must be better.

## 7. Integration into the paper engine's entry step

`app/paper/engine.py`'s `_run_entries` gains two calls around the existing
sizing step, per flag 1:

1. After `passes_entry_filters` passes and before sizing:
   `RiskManager.pre_check(context)`. A deny here records a `MISSED` trade
   exactly like today's entry-filter/sizing rejections, with
   `rejection_reason=RejectionStage.RISK` and the specific rule name as
   `exit_reason`, plus a `risk_events` row (section 8).
2. After the selected sizer (section 6) proposes `raw_notional`:
   `RiskManager.apply(context, raw_notional)`. `DENY` records `MISSED` the
   same way; `RESIZE` replaces `raw_notional` with the resized amount
   before it reaches `compute_fill` (unchanged); `ALLOW` proceeds exactly
   as today, no `risk_events` row (flag 10).

`compute_fill` and everything after it in `_run_entries` is unchanged -
this phase only inserts decisions *before* the fill model runs, never
changes what the fill model itself does.

## 8. New tables and columns

**`risk_events`**: `id`, `portfolio_id` (FK to `paper_portfolios`),
`signal_id` (FK to `signals`, nullable - a `pre_check` denial may have no
candidate size or even a specific signal yet, e.g. `emergency_stop`
denying before any signal-level evaluation), `rule` (`String`, the
`StrEnum` rule name), `decision` (`String(10)`: `RESIZE` / `DENY`; `ALLOW`
is never stored, per flag 10), `reason` (`String`, human-readable summary
matching `detail`), `detail` (`JSONB`, the numbers behind the decision -
see section 1), `occurred_at`. Indexed on `(portfolio_id, occurred_at)`
for the dashboard's per-portfolio risk-event log.

**`paper_trades`** gains: `sizer_used` (`String(10)`: `FIXED` / `TIERED` /
`KELLY` - which sizer actually produced this trade's size, since a
`KELLY` portfolio's individual trades may each have fallen back to
`TIERED`), `kelly_fraction` (`Money`, nullable - the post-`RISK_KELLY_
FRACTION`, pre-cap fraction actually used, null unless `sizer_used=
KELLY`), `p_hat` (`Money`, nullable - the calibrated probability used,
null unless `sizer_used=KELLY` and a bucket had enough samples),
`edge` (`Money`, nullable - `p_hat - c` at decision time, null under the
same condition as `p_hat`; stored even on a Kelly-rejected (skipped)
candidate so "there was no edge here" is answerable from the row without
recomputing it), and `event_slug` (`String(300)`, flag 8 - denormalized
from the signal at entry time, the same way `condition_id`/`asset`/
`outcome` already are).

**Calibration is served from a query, not stored as its own table.**
`p_hat`, sample count, and the Wilson interval per bucket are cheap to
recompute from `paper_trades` on request (bucket boundaries are settings
that can change; a materialized `calibration_buckets` table would need
invalidating every time they do) - the dashboard's calibration view
(a `get_calibration_buckets()` query, `app/dashboard/queries.py`) computes
them live. If this ever becomes a real query-latency problem, a
materialized table can be added later as a pure performance optimization
without changing what it measures - not needed to ship this phase.

All money/percentage columns `NUMERIC(24,6)` via the existing `Money`
alias, same as every other money column in this schema.

## 9. Settings

All in `app/config/settings.py`, `risk_`-prefixed (the two caps this phase
absorbs from `paper_` per flag 11 move prefix along with ownership), none
hardcoded in `app/risk/`. Portfolio `params` JSONB overrides any
portfolio-scoped one, same mechanism as every existing paper-trading
setting.

| Setting | Default | Portfolio-overridable |
|---|---|---|
| `risk_max_position_pct` (was `paper_max_position_notional_pct`) | 0.10 | yes |
| `risk_max_exposure_pct` (was `paper_max_total_exposure_pct`) | 0.60 | yes |
| `risk_max_market_exposure_pct` | 0.20 | yes |
| `risk_max_correlated_exposure_pct` | 0.30 | yes |
| `risk_daily_stop_loss_pct` | 0.10 | yes |
| `risk_max_open_positions` | 10 | yes |
| `risk_min_bankroll_pct` | 0.20 | yes |
| `risk_max_position_size_enabled` | true | yes |
| `risk_max_total_exposure_enabled` | true | yes |
| `risk_max_market_exposure_enabled` | true | yes |
| `risk_max_correlated_exposure_enabled` | true | yes |
| `risk_daily_stop_loss_enabled` | true | yes |
| `risk_max_open_positions_enabled` | true | yes |
| `risk_min_bankroll_halt_enabled` | true | yes |
| `paper_emergency_stop` | false | no (global kill switch, adjustable-only) |
| `risk_tiered_score_bands` | see section 3 table | yes |
| `risk_kelly_fraction` | 0.25 | yes (validated: never > 0.5) |
| `calibration_min_samples_per_bucket` | 30 | no |
| `calibration_window_days` | 90 | no |
| `calibration_trader_bands` | `[2,3), [3,5), [5,inf)` | no |
| `calibration_score_bands` | mirrors `risk_tiered_score_bands` shape | no |
| `calibration_price_bands` | `[0,0.3), [0.3,0.7), [0.7,1)` | no |

## 10. Rollout

Stage 1 (guardrails) needs no probability estimate and no historical data
- it's built and active immediately, wired into every existing portfolio
on deploy (default thresholds are deliberately generous - e.g.
`risk_max_open_positions=10`, comfortably above what any current portfolio
holds today - so turning Stage 1 on doesn't itself change behavior for
portfolios that were already operating within these bounds; it only starts
mattering once a portfolio actually approaches a limit). Stage 2 replaces
the informal reasoning behind `CONFIDENCE_WEIGHTED` with settings-driven
bands and is available to any portfolio immediately by setting
`paper_sizer=TIERED`. Stage 3 (calibration + Kelly) is only meaningful
once resolved-trade volume exists, which is why the `kelly` portfolio is
seeded now, run alongside the others, and left to accumulate history -
Stage 3's calibration will report `None` for nearly everything at first
(flag 5), which is the expected, honest starting state, not a rollout
blocker.

# Phase 6 design: signal-quality optimization

No application code yet - design only, per the working agreement. Phases
1-5 built the pipeline that turns tracked-wallet activity into consensus
signals, paper-traded those signals with realistic fills, and layered a
centralised risk manager plus staged sizing (fixed, tiered, calibration-
gated Kelly) on top. Every one of those phases assumed the *signal itself*
was trustworthy - N wallets agreeing meant N independent opinions, a
wallet's score reflected real skill, and "the market moved our way after we
signalled" was never actually measured. This phase stops assuming that and
starts checking it: independence verification (are five agreeing wallets
really five, or one operator wearing five hats?), an honest fast-feedback
edge metric (closing line value) that doesn't wait for markets to resolve,
statistically sound wallet ranking (small samples and never-closed losers
currently distort the score), rigorous calibration with a proper scoring
rule (Brier), and a bounded, measured form of adaptive weighting. Nothing
here changes what a paper portfolio does with a signal once it has one -
this phase is entirely about making the signal worth trusting.

## Scope discipline

This system is read-only paper trading and stays that way. Nothing in this
phase optimises execution latency, and nothing here introduces streaming or
HFT infrastructure - no Kafka, no ClickHouse, no direct RPC/mempool access,
no Rust. Those tools solve for real-order execution speed against a live
book; this project never places an order and, per CLAUDE.md, legally
cannot in the operator's jurisdiction. Every workstream below is graded on
**signal quality** - measured against paper portfolios' realised results
(PnL, CLV, calibration) - never on how fast a signal reaches a decision.
Where a design choice could be read as implying sub-second processing or
order routing, it's called out explicitly as **out of scope** at that
point, not left ambiguous. Concretely: clustering runs daily, CLV/
calibration/bandit updates run on a schedule (hourly-to-daily), and nothing
in this phase adds a code path that reacts to a single price tick.

## Flags and assumptions

1. **Cluster identity must be stable across reruns, and the algorithm's own
   output label isn't - so `cluster_id` is a content hash, not Louvain's
   raw integer.** Community-detection libraries (Louvain, label
   propagation) label communities arbitrarily each run - "cluster 3" today
   and "cluster 3" tomorrow have no guaranteed relationship, even if
   membership hasn't changed. That's silently fatal for Workstream 5:
   `cluster_bandit_state` accumulates reward history keyed by cluster id,
   and if that key drifts for no real reason, a cluster's earned reputation
   evaporates on every recompute. Decision: `cluster_id` is
   `sha256(sorted member wallet addresses)[:16]`, not the raw community-
   detection label. An unchanged cluster gets the same id every run (bandit
   state persists correctly); a cluster whose actual membership changes
   gets a new id - which is *correct* bandit semantics, since a changed
   membership is a genuinely different independent-voice profile and
   shouldn't inherit an unrelated reward history.
2. **`distinct_clusters` is a new column computed at signal-creation time
   from clusters as they exist then - never retroactively recomputed onto
   old signals.** Clustering runs daily and drifts; a signal's
   `distinct_traders` was computed under today's plain wallet-count
   definition at the instant it fired. Rewriting history to "what would
   this signal's cluster count have been" isn't answerable in general
   (wallets may not have existed in any cluster snapshot at that instant),
   and CLAUDE.md's "every signal must be explainable from its own row"
   convention already points at the answer: `distinct_clusters` (nullable)
   is populated going forward only, same point-in-time-snapshot philosophy
   every other column on `signals` already follows. `distinct_traders`
   stays on the row unchanged, for audit and backward compatibility.
   Anything that consumes cluster-aware consensus (calibration bucketing,
   the adaptive challenger) simply filters to signals where
   `distinct_clusters is not null` - pre-cutover signals are naturally
   excluded, not miscounted.
3. **Cold start: a wallet with no cluster assignment yet counts as its own
   singleton cluster.** Before the first daily clustering run - or for a
   wallet added to tracking after the last one - there's no evidence it's
   colluding with anyone. Treating it as independent (today's exact
   behaviour) is the correct default, not a workaround: this raises the
   bar for provably-correlated wallets, it doesn't lower it for everyone
   else while the graph catches up.
4. **Community detection needs one new dependency, not two.** The prompt
   allows "networkx / python-louvain." Current `networkx` (>=2.8) ships
   Louvain natively as `networkx.algorithms.community.louvain_communities`
   - the separate `python-louvain` package is no longer necessary. Decision:
   add `networkx` only. (Confirmed neither is currently a dependency -
   `uv.lock` has zero matches for either name.)
5. **Co-trading edges are built from `OPENED` events only, not
   `INCREASED`.** The prompt's own wording is "when they opened the same
   outcome" - an add-to-an-existing-position event says a wallet is
   managing a bet it already placed, not independently discovering it, so
   it's a weaker (and noisier) independence signal than a fresh open.
   Restricting to `OPENED` also keeps the graph's edge count bounded by
   genuinely new decisions, not every top-up.
6. **CLV's entry price is signal-level and portfolio-agnostic - it is not
   any one paper portfolio's actual (slippage-adjusted, per-portfolio-
   delay) fill price.** `signal_clv` has one row per signal, not one per
   (signal, portfolio) - but different portfolios can configure different
   `paper_entry_delay_seconds`, so "our simulated entry" isn't a single
   number across portfolios. Decision: CLV uses its own fixed simulated
   delay (`clv_entry_delay_seconds`, decoupled from any portfolio's own
   setting) and looks up the nearest `PriceSnapshot` at or after
   `signal.created_at + clv_entry_delay_seconds`, falling back to
   `signal.average_entry_price` if no snapshot exists yet - mirroring
   the fallback app/paper/fills.py already uses for a missing delayed
   snapshot, for the same reason (an honest "we don't have a better number
   yet" answer, not a stall).
7. **Per-market-category CLV aggregation is deferred - no category field
   is collected today, and one must not be invented.** `Market` has no
   `category`/`tag` column, and CLAUDE.md requires verifying a field
   against docs.polymarket.com before building against it - out of scope
   for a no-code design doc. If Gamma's markets/events endpoints expose a
   category or tag field (unconfirmed here), a follow-up adds a `category`
   column during collection and this aggregation slots in with no schema
   change to `signal_clv` itself. Until then, CLV aggregates by cluster and
   weighted-score band only, both of which already exist.
8. **The scorer has no win-rate component today - Workstream 3 adds one, it
   doesn't "replace" one.** `TraderScore` is built entirely from
   Polymarket's own reported leaderboard PnL (`month_component`/
   `all_time_component`, log-normalized) plus a leaderboard-appearance
   consistency component - there is no locally-computed win rate anywhere
   in scoring today, and nothing to "replace." This phase adds a fourth
   component, `win_rate_component`, computed locally from closed `Position`
   rows (see Workstream 3), with its own weight (`score_weight_win_rate`)
   folded into the existing sum-to-1.0 validator.
9. **The zombie guard forces a stuck position into the loss column - it
   doesn't apply a vague multiplier discount.** "Discount the win rate
   accordingly" is implemented as a specific mechanism: a position still
   open long past its market's `end_date` (past `ranking_zombie_grace_days`)
   is counted as a **loss** in the win/loss tally feeding
   `win_rate_component`, in addition to genuinely closed positions. A vague
   multiplicative discount can be tuned into meaninglessness; forcing the
   exploit's proceeds into the denominator as a realized loss directly
   neutralizes the "never admit a loser" trick the research warns about.
10. **Time decay is implemented as decay-weighted counts fed straight into
    the Wilson formula's `n` - not a true effective-sample-size
    correction.** A rigorous treatment would compute an effective sample
    size from the decay weights' first and second moments; this project's
    established preference (docs/PHASE5_DESIGN.md flag 6) is an auditable
    number over a smoother-but-opaque one. Decision: each closed/zombie
    position contributes `0.5 ** (age_days / ranking_halflife_days)` to
    both its bucket's numerator (if a win) and denominator; those weighted
    sums feed `wilson_interval()` directly as `wins`/`n`. Simple, and "why
    is this wallet's score X" is answerable from one formula, not two.
11. **"Require MIN_RESOLVED_TRADES before trusted, below that shrink" is one
    continuous mechanism, not a hard gate plus a separate shrink step.** A
    hard cliff at exactly `ranking_min_resolved_trades` would jump a
    wallet's score discontinuously the moment its Nth trade resolves.
    Decision: Empirical-Bayes shrinkage
    (`shrunk_p_hat = (wins + k * population_mean) / (n + k)`, with
    `k = ranking_prior_strength` acting as a virtual sample size at the
    population mean) runs unconditionally and produces the same "not
    trusted yet" effect smoothly - at `n=0` a wallet's rate is exactly the
    population mean; every real observation moves it a little further from
    that prior. `ranking_min_resolved_trades` still exists, but only as a
    display-only "trusted" threshold (e.g. a dashboard tag), never a code
    branch in the score itself.
12. **The Wilson lower bound is computed on the already-shrunk rate, not
    the raw one.** Shrinkage and the Wilson bound solve different problems
    (a biased point estimate vs. honest uncertainty around whatever point
    estimate you have) and compose cleanly: shrink first, then take the
    lower bound of the shrunk `(p_hat, n)` pair. Reusing
    `app/risk/calibration.py`'s existing `wilson_interval()` here, not a
    second implementation.
13. **The bandit's per-cycle multiplier uses the Beta posterior *mean*, not
    a literal Thompson-sampled draw.** Textbook Thompson sampling draws a
    random sample from the posterior each time an arm is pulled, for
    sequential explore/exploit action selection. This isn't sequential arm-
    pulling - every consensus cycle weighs every cluster simultaneously,
    and the same signal stream is read identically by every non-challenger
    portfolio, so a randomized multiplier would make results non-
    reproducible run to run for no benefit. Decision:
    `adaptive_multiplier = clamp(2 * alpha / (alpha + beta), MIN, MAX)` -
    the posterior mean, deterministic given the current `(alpha, beta)`,
    centred at 1.0 (neutral) when the posterior mean CLV-hit-rate is
    exactly 0.5. "Thompson-style" describes the Beta-Bernoulli conjugate
    update, not literal per-cycle sampling.
14. **The adaptive challenger recomputes its own effective weighted_score
    at entry-filter time - it does not fork signal generation.** Phase 4's
    entire comparison methodology rests on every portfolio seeing the
    *identical* signal stream (docs/PHASE4_DESIGN.md section 1); a second,
    adaptively-reweighted consensus pipeline would break that guarantee for
    every portfolio, not just the challenger. Decision: one new portfolio-
    level toggle (`paper_use_adaptive_weighting`). When set, the entry
    filter recomputes `effective_weighted_score` from the signal's own
    `contributors` list - each contributor's wallet looked up against the
    current `wallet_clusters`/`cluster_bandit_state`, weight multiplied by
    that cluster's live `adaptive_multiplier` - and compares *that* against
    `paper_min_weighted_score`, instead of the signal's stored (static)
    `weighted_score`. The signal itself, and every other portfolio's view
    of it, is untouched.
15. **Numbering note.** `docs/PHASE8_DESIGN.md` (the operations console)
    already exists, so there's a gap before it in the doc sequence. Named
    this `PHASE6_DESIGN.md` anyway, per explicit instruction - nothing in
    Phase 8's scope (console pages) overlaps these five workstreams.
16. **(Workstream 7) "Same asset" already encodes "same direction/outcome" -
    there is no separate direction filter to add.** The crowdedness prompt's
    mitigation for coincidental co-trading asks for matching a reaction in
    "the SAME direction/outcome... within a tight window." In this schema
    `PositionHistory.asset` already IS the per-outcome CLOB token id - a
    market's Yes and No outcomes are different assets entirely. Two wallets
    whose `OPENED` events share an asset are, by construction, on the same
    side of the same market. Decision: follower-matching keys on asset
    alone; "same asset" and "same direction" are the same condition here,
    not two.
17. **(Workstream 7) `follower_reaction`'s "sample" is every `OPENED` event
    in a settings-bounded lookback window, not a literal random subsample.**
    A randomized draw would make "why is this wallet's crowdedness X"
    unanswerable without separately recording which rows happened to be
    picked - the same auditability preference flags 6 and 10 already
    established for this project (a bounded, reproducible window over a
    smoother-but-opaque mechanism).
18. **(Workstream 7) Follower-counting is a new, simpler primitive, not a
    reuse of `build_cotrading_edges`.** Both start from the same `OPENED`,
    non-bootstrap, tracked-wallet event stream (flag 5), but they answer
    different questions: co-trading counts *repeated* co-occurrence across
    many markets to justify drawing a collusion edge (a threshold,
    `cotrade_min_shared_markets`); `follower_reaction` counts *one wallet's
    average* followers per its own open, and specifically needs candidates
    OUTSIDE the origin's cluster rather than needing them assigned to a
    shared one. Forcing one function to serve both would bend one use case
    around the other's semantics; a second small pure function, built the
    same way, is simpler than a shared abstraction here.
19. **(Workstream 7) `market_obviousness`'s liquidity/volume/spread reference
    points are fixed settings, not a percentile rank against whichever
    markets happen to be open today.** A population-relative rank would make
    one wallet's score depend on which OTHER markets happened to be open
    that day - not reproducible, and not explainable from the wallet's own
    row alone. Fixed, operator-tunable reference points (same convention as
    every other dollar/spread threshold in this project) keep the score
    comparable day to day.
20. **(Workstream 7) `wallet_crowdedness` is a current-state table (delete +
    reinsert each daily run), not an append-only history like
    `trader_scores`/`trader_category_scores`.** Nothing in the prompt asks
    for a crowdedness trend chart, only a live per-wallet number the
    dashboard shows alongside skill and hidden-alpha "right now" - the same
    shape and reasoning `wallet_clusters` and `cluster_bandit_state` already
    use (Workstreams 1 and 5), recomputed and replaced each run rather than
    trended.
21. **(Workstream 7) `hidden_alpha` is a runtime combination, never a stored
    column.** `skill_score` is category-specific as of Workstream 6 and
    varies per (wallet, category); `crowdedness` is a slower-moving,
    wallet-level number recomputed daily. Storing a precomputed
    "hidden_alpha" would mean one stale-on-arrival number per (wallet,
    category) pair, going stale the moment either input next recomputes on
    its own independent schedule. Decision: `hidden_alpha_score()` /
    `hidden_alpha_weighted_score()` are pure functions, combined fresh at
    the point of use from the signal's own contributor weights (already
    category-specific, per Workstream 6) and a live `wallet_crowdedness`
    lookup - the exact "recompute at entry-filter time from the signal's own
    contributors" pattern Workstream 5's adaptive multiplier already
    established (flag 14).
22. **(Workstream 7) This lands as a challenger portfolio's entry filter
    only - it does not touch sizing, and it does not change the default
    consensus engine.** The prompt's own framing names two different scopes
    in tension: "the consensus engine and the paper portfolios' sizing use
    hidden_alpha... [but] run this as a CHALLENGER first." Flagging this
    disagreement explicitly rather than silently picking one: resolved the
    same way Workstream 5 resolved the identical tension (flag 14) - one new
    portfolio (`hidden_alpha`), one new toggle
    (`paper_use_hidden_alpha_weighting`), recomputing only the entry
    filter's `weighted_score` check, never the signal itself, never any
    other portfolio's view of it, and never Stage 2/3 sizing (which is keyed
    off the signal's own persisted `weighted_score`/cluster-count/price
    bands, not a live per-portfolio recompute - reworking that is a bigger
    change than one challenger portfolio warrants before there's any
    evidence penalizing crowdedness helps). If `hidden_alpha` beats
    `baseline`, expanding it into sizing or into the default consensus
    weighting is a follow-up made from evidence, not assumed here.

None of the above are objections to the spec - they're the calls this
design makes where the prompt left room or where the current codebase
didn't match an assumption (flag 8 in particular), written down so they're
visible rather than buried in code, same convention as every prior phase
design doc.

## Workstream 1: independence / Sybil verification

**Why this is highest priority.** Every filter downstream - breadth
(`consensus_min_traders`), quality (`consensus_min_weighted_score`),
conviction (`consensus_min_combined_value_usd`) - counts distinct wallets
as distinct opinions. If one operator runs five wallets that all open the
same bet within minutes of each other, today's engine sees five
independent votes and a healthy weighted score; it's actually one voice,
amplified five times. No amount of downstream sizing or risk management
fixes a consensus signal that was never really consensus. This has to be
fixed at the source.

**The co-trading graph.** Nodes are tracked wallets (`Wallet.is_tracked`).
An edge between wallet A and wallet B gets one point of weight every time
they both have an `OPENED` `PositionHistory` event on the same
`(condition_id, asset)` within `cotrade_window_minutes` of each other
(flag 5: `OPENED` only). An edge only *exists* once that count reaches
`cotrade_min_shared_markets` distinct `condition_id`s - a pair that
happened to pile into one popular market together isn't evidence of
collusion; a pair that keeps doing it across many different markets, in a
tight window, is exactly the coincidental-vs-suspicious line the research
warns about. The graph is rebuilt from scratch each run (not incrementally
updated) - `PositionHistory` is small enough at this project's volumes
that a full rebuild is simpler and self-correcting.

**Community detection.** `networkx.algorithms.community.louvain_communities`
(flag 4) partitions the graph into communities; each connected component
with only one member is its own singleton cluster (flag 3 covers wallets
absent from the graph entirely - never having co-traded with anyone).

**Storage.** `wallet_clusters` (`wallet_id` FK unique, `cluster_id`
String - the content hash from flag 1, `cluster_size` int,
`computed_at`) - one row per wallet, upserted in place each run, not an
append-only history (the "current assignment" is all consensus needs;
`computed_at` is enough to tell how fresh it is). Cluster-level metadata
(size, member list) is derivable from a `GROUP BY cluster_id` over this
same table - no second table needed for that.

**Consensus impact - the core fix.** `app/consensus/engine.py`'s
`_weighted_score()` today dedupes `ContributorEvent`s by wallet address,
keeping each wallet's max weight, then sums those (`distinct_traders =
len(...)`, `weighted_score = sum(...)`). This changes to dedupe by
*cluster* instead: every contributing wallet is mapped to its
`wallet_clusters` row (or treated as a singleton per flag 3), grouped by
`cluster_id`, and **the max weight within each cluster** is kept - not the
sum of all wallets in that cluster, per the prompt's explicit instruction.
`distinct_traders` is renamed in spirit to `distinct_clusters` (new column,
flag 2) and counts clusters; `weighted_score` sums one value per cluster.
Worked example: five wallets in the same cluster, weights 0.9/0.7/0.6/0.5/
0.4, one wallet outside it at weight 0.8 - today's engine sees
`distinct_traders=6`, `weighted_score=3.9`; this workstream sees
`distinct_clusters=2`, `weighted_score=0.9+0.8=1.7`. The five-wallet cluster
contributes exactly as much as its single strongest voice, never more.

**Failure modes, named explicitly (the prompt asks for these, not a happy
path):**
- *Coincidental co-trading.* Two genuinely independent wallets who both
  follow, say, election markets will occasionally open the same outcome
  close together. The multi-market-AND-tight-window requirement
  (`cotrade_min_shared_markets` distinct `condition_id`s, not one) is the
  mitigation, not a guarantee - a popular-enough pair of markets could still
  produce a false edge over enough time. `cotrade_window_minutes` should be
  tight (minutes, not hours) precisely to keep this rare.
- *Regime change.* A cluster computed today reflects trading behaviour up
  to today; a wallet that stops colluding (or starts) shows up differently
  next run. Daily recomputation (flag 1's stable-id design) is what lets
  this correct itself instead of ossifying a stale judgment.
- *A sophisticated operator can defeat any single heuristic.* Wider time
  gaps, varying position sizes, deliberately trading different markets per
  wallet - all defeat this specific graph. This raises the bar for
  Sybil consensus; it does not, and cannot, close the door entirely. That's
  stated here so it's never mistaken for a solved problem later.

## Workstream 2: closing line value

**Why CLV first, before PnL.** A signal produces a CLV data point the
moment `clv_horizon_hours` elapses - no market has to resolve. At this
project's current trade volumes (dozens of resolved trades total, per
docs/PHASE5_DESIGN.md flag 5), CLV will have an order of magnitude more
data than paper PnL for a long time, and it answers a narrower, cleaner
question: did the market move our way after we signalled, on average.
Positive average CLV is direct evidence the signal precedes real price
discovery, independent of whether any given bet's market has settled yet.

**What gets recorded, per signal (`signal_clv` table):** `signal_id` (FK,
unique - one row per signal), `entry_price` (flag 6's simulated-delay
lookup, falling back to `average_entry_price`), `price_at_horizon`
(nullable - the nearest `PriceSnapshot` at/after `created_at +
clv_horizon_hours`, populated once that time has passed), `clv_horizon`
(`price_at_horizon - entry_price`, computed once available; always
positive-favourable-means-positive since `Signal.side` is always `"BUY"`
today), `price_at_resolution` and `clv_resolution` (same shape, populated
once the market actually resolves - reusing Phase 4's own
`detect_resolution`/`ResolutionOutcome` convention:
1.0 for WON, 0.0 for LOST, left null on AMBIGUOUS rather than guessing),
`computed_at` (last-updated timestamp, since this row is filled in over
two or three separate passes as time and resolution actually happen, not
all at once).

**Aggregation.** Overall average, per cluster (`wallet_clusters` via
`signal.contributors`), per `weighted_score` band (reusing the same
band-list-setting shape Stage 2 tiered sizing already established) - per
flag 7, *not* per market category yet.

## Workstream 3: statistically sound wallet ranking

**Current state (flag 8): there is no win rate here today.** `TraderScore`
= `score_weight_month * month_component + score_weight_all_time *
all_time_component + score_weight_consistency * consistency_component`,
where the PnL components are Polymarket's own self-reported leaderboard
PnL (log-normalized), and consistency is leaderboard-appearance frequency.
This workstream adds a fourth, locally-computed component.

**The raw population.** Every wallet's closed `Position` rows
(`is_open = false`, has `closed_at`) are wins if `cash_pnl > 0` (using the
Position table's own PnL, not the leaderboard's - so this component is
independent evidence, not a re-statement of `month_component`/
`all_time_component`). Every `Position` still `is_open = true` whose
market's `end_date` is more than `ranking_zombie_grace_days` in the past is
folded in too, forced to count as a **loss** (flag 9) - the position was
never honestly resolved, but pretending it doesn't exist is exactly the
exploit the research warns about.

**Time decay (flag 10).** Each qualifying position (closed or zombie)
contributes `0.5 ** (age_days / ranking_halflife_days)` to its win/loss
tally, where `age_days` is measured from `closed_at` (or, for a zombie
position, from `market.end_date + ranking_zombie_grace_days` - the moment
it was judged a loss, not whenever it happened to first open). Summing
those weighted contributions gives a decayed `wins` and decayed `n` per
wallet.

**Shrinkage, then the Wilson bound (flags 11-12).**
`shrunk_p_hat = (wins + ranking_prior_strength * population_mean) /
(n + ranking_prior_strength)`, where `population_mean` is the decayed
win rate averaged across every tracked wallet with at least one
qualifying position (recomputed at the same cadence the scorer already
runs, no new schedule). `win_rate_component = wilson_interval(shrunk_p_hat
treated as wins/n pair, n, z=1.96)[0]` (the lower bound only - the prompt's
explicit ask: 8/10 must rank below 800/1000 even though both have the same
raw 80% rate, because the lower bound on 8/10 is far wider and lower).
`ranking_min_resolved_trades` is a display-only "trusted" threshold (flag
11), not a branch here.

**Score formula, updated:** `score = score_weight_month * month_component +
score_weight_all_time * all_time_component + score_weight_consistency *
consistency_component + score_weight_win_rate * win_rate_component`, four
weights now validated to sum to 1.0 (extending
`Settings._validate_score_weights`). Proposed default split: month 0.35,
all-time 0.20, consistency 0.20, win-rate 0.25 - giving the new, most
directly-verifiable-by-us signal real weight without letting it dominate
the Polymarket-reported components entirely on day one.

**New `TraderScore` columns** (mirroring the existing per-component-column
shape, not a separate table): `win_rate_component` (Money), plus two
audit fields so a score is explainable from its own row -
`resolved_trade_count` (the decayed `n` that fed it) and `zombie_count`
(how many of those were forced-loss zombies, not genuine resolutions).

## Workstream 4: calibration + Brier score

**Formalising Phase 5's bridge.** `app/risk/calibration.py` already
buckets resolved paper trades by `(distinct_traders band, weighted_score
band, entry-price band)` and computes a Wilson-bounded empirical hit rate
per bucket (docs/PHASE5_DESIGN.md section 4). This workstream changes the
first dimension to cluster count (flag 2: only signals with
`distinct_clusters is not null` are calibration-eligible going forward) -
renaming `CalibrationConfig.trader_bands` to `cluster_bands` and
`Settings.calibration_trader_bands` to `calibration_cluster_bands`
accordingly, everything else in `app/risk/calibration.py`'s bucketing/
Wilson-interval machinery is unchanged and reused as-is.

**Brier score - the piece that was missing.** For every signal that
resolved `market_resolved` (Phase 4/5's strict definition, reused for
consistency), compute `(p - outcome)^2` where `outcome` is 1.0/0.0 (WON/
LOST) and `p` is compared against **two** reference probabilities, not one:
1. `entry_price` itself - the market's own implied probability at signal
   time. This is the baseline: how good is the raw market price alone.
2. `get_p_hat()`'s calibrated bucket probability, when that signal's bucket
   clears `calibration_min_samples_per_bucket` - i.e., is *our*
   calibration better than just reading the market price.

Tracking both, and their difference, over time and per bucket is the
direct answer to "is calibration improving as Workstreams 1-3 land": if
Phase 5's calibrated `p_hat` doesn't beat the raw market price on Brier,
the calibration isn't adding anything yet, and that's worth knowing exactly
as much as if it does.

**The dependency, stated explicitly.** Kelly sizing (docs/PHASE5_DESIGN.md
section 5) is only as good as `p_hat`. A `p_hat` with a bad Brier score is
a confidently-wrong probability, and Kelly will size aggressively against
it - worse than the honest Stage 2 heuristic it's supposed to improve on.
This workstream's Brier tracking is Phase 5's own sizing quality gate,
not a separate concern.

## Workstream 5: adaptive whale selection

**Clusters, not wallets, are the arm (post-Workstream-1).** Reward per
cluster is binarized `clv_horizon > 0` for every signal that cluster
contributed to (not paper PnL - see the "why CLV first" reasoning in
Workstream 2; PnL requires resolution and this project has few resolved
trades so far). Each observation updates a Beta posterior: `alpha += 1` on
a positive-CLV signal, `beta += 1` otherwise.

**The multiplier (flag 13).** `adaptive_multiplier = clamp(2 * alpha /
(alpha + beta), adaptive_weight_min, adaptive_weight_max)` - a posterior
mean-derived, deterministic number, not a per-cycle random draw. Below
`adaptive_min_signals` observations for a cluster, the multiplier is fixed
at 1.0 (neutral - equivalent to the base score, unadjusted) rather than
computed from a near-empty posterior that would otherwise swing wildly on
one or two early data points.

**Storage.** `cluster_bandit_state` (`cluster_id` - the same stable hash
from flag 1, `alpha`, `beta`, `observations` int, `adaptive_multiplier`
Money - cached, recomputed each update rather than derived on every read,
`updated_at`).

**Bounded, and measured, not assumed (flag 14).** A new `adaptive`
paper portfolio, otherwise identical to `baseline` (same entry filters,
same fixed sizer), with one new portfolio param,
`paper_use_adaptive_weighting=true`. Its entry filter recomputes
`effective_weighted_score` from the signal's own `contributors` at
evaluation time - reading current `wallet_clusters`/`cluster_bandit_state`
for each contributor's cluster and multiplying that cluster's max weight
by its live `adaptive_multiplier` - and filters on that instead of the
signal's stored `weighted_score`. Every other portfolio, `adaptive`
included, still sees and paper-trades the exact same signal stream; only
which signals *pass its own entry filter*, and at what apparent score,
differs. Running `adaptive` head-to-head against `baseline` on identical
signals is how this project finds out whether adaptive weighting helps,
exactly the same champion/challenger methodology `kelly` already uses
against `baseline` for sizing (docs/PHASE5_DESIGN.md section 6).

## Workstream 7: on-chain crowdedness penalty

**Why this depends on Workstreams 1 and 2, and lands after both.**
`follower_reaction` (below) needs `wallet_clusters` to tell a genuine
follower from the wallet's own syndicate - without it, a five-wallet
cluster copying its own leader within seconds would inflate that leader's
crowdedness score on nothing but its own collusion. The wider point CLV
established - measure the thing directly, don't wait on PnL resolution
(Workstream 2's own reasoning) - applies here too: crowdedness is measured
from the same on-chain activity this project already collects, on the same
kind of rolling/aggregated schedule as everything else in this phase, not
from a new data source.

**Scope discipline, restated for this workstream specifically.** The
research blueprint this prompt is drawn from describes a social-popularity
signal (follower counts, mention volume, discovery velocity on platforms
like Twitter/Nansen). That is explicitly out of scope: no reliable
address-to-social-mention feed exists, and adding a scraper or a
third-party analytics dependency would violate this project's read-only,
public-API-only posture (CLAUDE.md). Every component below is derived
exclusively from `position_history`, `leaderboard_snapshots`, and
`markets` - data this project already collects for other reasons.

**Three components, each normalised to [0, 1], each with a named failure
mode:**

- **`follower_reaction`.** For every `OPENED`, non-bootstrap event a wallet
  made in the last `crowd_reaction_lookback_days` (flag 17), count how many
  OTHER tracked wallets - in a DIFFERENT cluster (flag 16: matching on
  `asset` alone already means "same outcome," so no separate direction
  check is needed) - opened the identical asset within
  `crowd_reaction_window_minutes` afterward. Average that follower count
  across the wallet's own sampled opens, then saturate to [0, 1] via
  `min(average / crowd_reaction_saturation, 1)` - a simple clamped-linear
  saturation, not an exponential one, matching this project's stated
  preference for an auditable number over a smoother-but-opaque one (flag
  10's precedent). High = this wallet's moves are widely and quickly
  copied by genuinely independent voices. Failure mode: a popular-enough
  market could still produce a coincidental reaction between two
  independent wallets who both just happen to like it; the cross-cluster
  requirement and the tight window (minutes, not hours) bound this the same
  way `cotrade_window_minutes` bounds the same risk in Workstream 1, but
  neither guarantees it away entirely.
- **`leaderboard_fame`.** From `leaderboard_snapshots` within the existing
  `scoring_lookback_days` window (reused, not a new setting): the median
  rank across every snapshot the wallet appeared in (median, not best - one
  lucky top-1 snapshot shouldn't alone read as "famous"), turned into
  `rank_score = max(0, 1 - (median_rank - 1) / (leaderboard_top_n - 1))`
  (rank 1 scores 1.0, a rank at the tracked ceiling scores ~0), multiplied
  by `appearance_fraction` (appearances / possible appearances in the
  window, capped at 1.0 - the same formula `app/consensus/scoring.py`'s
  private `_consistency()` already uses, reimplemented locally rather than
  importing a private, underscore-prefixed name across a module boundary).
  Multiplicative, not averaged: a wallet seen once at rank 1 and absent
  every other snapshot should not score as famous as one that holds rank 1
  consistently. High = discovered and tracked by everyone watching the
  public leaderboard, the literal definition of "not hidden." Failure mode:
  a wallet that just started being tracked has zero snapshots and scores
  0 fame by construction - correct (it hasn't had the chance to be famous
  yet), but worth stating so a new wallet's crowdedness score isn't
  misread as "definitely uncrowded" rather than "not yet measured."
- **`market_obviousness`.** The average, across the distinct markets behind
  this wallet's currently-open positions, of a per-market efficiency score:
  `(liquidity_score + volume_score + spread_score) / 3`, where
  `liquidity_score = min(liquidity / crowd_liquidity_reference_usd, 1)`,
  `volume_score = min(volume_24h / crowd_volume_reference_usd, 1)`, and
  `spread_score = 1 - min(spread / crowd_spread_reference, 1)` (tight
  spread -> high obviousness, per the blueprint's microstructure finding
  that efficient markets leak alpha faster). A market missing any of these
  three fields (not yet synced) contributes 0 for that field - the same
  "not yet measured, treated as least-informative" default `market_obviousness`
  and `leaderboard_fame` both use, rather than skipping the field or the
  whole market. High = this wallet's edge, if any, lives in the markets
  everyone already watches. Failure mode: a wallet with genuinely no open
  positions right now scores 0 - not "provably in thin markets," just
  "nothing to measure this cycle," same caveat as `leaderboard_fame`'s.

**Crowdedness.** `crowdedness = clamp(crowd_weight_reaction *
follower_reaction + crowd_weight_fame * leaderboard_fame +
crowd_weight_obvious * market_obviousness, 0, 1)`, weights defaulting to
0.5 / 0.3 / 0.2 (reaction weighted highest - it is the most direct evidence
of actual copying, the other two are proxies for *likelihood* of being
found) and validated to sum to 1.0, same `_validate_score_weights`-style
check this project already applies to the Workstream 3 score weights.

**Storage.** `wallet_crowdedness` (`wallet_id` FK unique,
`follower_reaction`, `leaderboard_fame`, `market_obviousness`,
`crowdedness` - all `Money` - `computed_at`) - one row per wallet, deleted
and reinserted each daily run (flag 20), same shape as `wallet_clusters`
and `cluster_bandit_state`.

**Hidden-alpha score.** `hidden_alpha = clamp(skill_score -
crowd_penalty_weight * crowdedness, 0, 1)` - a SOFT penalty (subtraction,
not a multiplicative or hard cutoff), per the blueprint's explicit
instruction that a genuinely skilled wallet should never be discarded just
for having some following. `skill_score` is the Workstream-3 machinery's
output - per-category where the caller has one (Workstream 6's
`trader_category_scores`, already the weight every `ContributorEvent`
carries as of that workstream), the wallet's raw score otherwise. Computed
fresh at the point of use, never stored (flag 21) - `hidden_alpha_score()`
for a single (skill, crowdedness) pair, and
`hidden_alpha_weighted_score(contributors, cluster_of, crowdedness_of,
crowd_penalty_weight)` for a whole signal's contributor list, mirroring
`app/optimization/bandit.py`'s `effective_weighted_score()` shape exactly:
apply the per-wallet adjustment first (there, multiply by the cluster's
bandit multiplier; here, subtract the wallet's own crowdedness penalty),
THEN take the max within each cluster and sum across clusters - one vote
per independent cluster, same as everywhere else in this phase.

**Consensus/sizing impact - challenger only (flag 22).** A new paper
portfolio, `hidden_alpha`, otherwise identical to `baseline`, with one new
portfolio param, `paper_use_hidden_alpha_weighting=true`. Its entry filter
recomputes the `weighted_score` it checks via
`hidden_alpha_weighted_score()` from the signal's own stored contributors
and a live `wallet_crowdedness` lookup, instead of the signal's stored
`weighted_score` - the exact same "recompute at entry-filter time, never
fork signal generation" pattern `adaptive` already established (flag 14).
Sizing is untouched for every portfolio, `hidden_alpha` included. Running
`hidden_alpha` head-to-head against `baseline` (and against `adaptive`,
independently) on the identical signal stream is how this project finds
out whether penalizing crowdedness actually improves results, rather than
assuming a skilled-but-crowded wallet's edge is really gone.

**Failure modes, named explicitly:**
- *A skilled wallet gets penalized for followers it never asked for.* The
  soft-penalty subtraction (not a hard cutoff) is the direct mitigation -
  `crowd_penalty_weight` defaults well below 1.0 specifically so a
  very-high-skill crowded wallet still scores respectably, never zeroed out
  on crowdedness alone.
- *A brand-new wallet scores as "uncrowded" purely from having no history
  yet*, not from genuinely trading in obscurity - `leaderboard_fame` and
  `market_obviousness` both default toward 0 (uncrowded) when there is
  nothing yet to measure, which reads the same as "provably hidden" even
  though it means "not yet observed." This is the same cold-start
  asymmetry flag 3 already accepts for clustering (independent-until-proven
  otherwise); here it cuts the opposite way (uncrowded-until-proven-
  otherwise) but for the identical reason - a thin prior shouldn't be
  penalized while the evidence catches up.
- *A sophisticated operator could still defeat `follower_reaction`* by
  varying position sizes, delaying entries beyond the reaction window, or
  routing "copies" through wallets deliberately never clustered together in
  the first place - the same limit flag/Workstream 1's own third failure
  mode names for co-trading detection generally. Stated here for the same
  reason: never mistaken for a solved problem later.

## Cross-cutting

**Everything here is offline/scheduled, none of it a hot path** (scope
discipline, restated): daily cluster recomputation
(`cluster_recompute_interval_hours`, default 24), and a separate periodic
job for CLV/calibration/bandit-state updates (hourly is plenty - these are
read-mostly aggregations over a small table, not anything latency-
sensitive). Both are new `PeriodicJob` entries in `app/main.py`'s existing
scheduler, same shape as every collector/consensus/paper job already there.

**New tables:**
- `wallet_clusters` (`wallet_id` FK unique, `cluster_id` String,
  `cluster_size` int, `computed_at`).
- `signal_clv` (`signal_id` FK unique, `entry_price`, `price_at_horizon`
  nullable, `price_at_resolution` nullable, `clv_horizon` nullable,
  `clv_resolution` nullable, `computed_at`).
- `cluster_bandit_state` (`cluster_id` String PK, `alpha`, `beta`,
  `observations` int, `adaptive_multiplier`, `updated_at`).
- `trader_category_scores` (`wallet_id` FK, `category` String,
  `score`, `resolved_trades`, `computed_at`) - Workstream 6.
- `wallet_crowdedness` (`wallet_id` FK unique, `follower_reaction`,
  `leaderboard_fame`, `market_obviousness`, `crowdedness`, `computed_at`) -
  Workstream 7.

**New/changed columns:**
- `markets.category` (String, not nullable, default "Other") - Workstream
  6, normalised from Gamma's per-market `tags` array.
- `signals.distinct_clusters` (int, nullable - flag 2).
- `trader_scores.win_rate_component`, `.resolved_trade_count` (int),
  `.zombie_count` (int).

All money/probability columns `NUMERIC(24,6)` via the existing `Money`
alias, same as every other numeric column in this schema. `cluster_id`
is a string hash, not numeric - never used in arithmetic, only equality/
grouping.

**Settings (all `phase6`-adjacent groups, `_`-prefixed by topic, following
the existing per-phase-banner convention - none hardcoded in application
code):**

| Setting | Default | Notes |
|---|---|---|
| `cotrade_window_minutes` | 15 | Workstream 1 |
| `cotrade_min_shared_markets` | 3 | Workstream 1 |
| `cluster_recompute_interval_hours` | 24 | Workstream 1 |
| `clv_horizon_hours` | 24 | Workstream 2 |
| `clv_entry_delay_seconds` | 30 | Workstream 2, decoupled from `paper_entry_delay_seconds` (flag 6) |
| `ranking_min_resolved_trades` | 10 | Workstream 3, display-only (flag 11) |
| `ranking_halflife_days` | 30 | Workstream 3 |
| `ranking_prior_strength` | 20 | Workstream 3, virtual sample size |
| `ranking_zombie_grace_days` | 14 | Workstream 3 |
| `score_weight_month` | 0.35 | Workstream 3, was 0.45 |
| `score_weight_all_time` | 0.20 | Workstream 3, was 0.25 |
| `score_weight_consistency` | 0.20 | Workstream 3, was 0.30 |
| `score_weight_win_rate` | 0.25 | Workstream 3, new; four weights now sum to 1.0 |
| `calibration_cluster_bands` | mirrors old `calibration_trader_bands` | Workstream 4, renamed |
| `adaptive_weight_min` | 0.5 | Workstream 5 |
| `adaptive_weight_max` | 2.0 | Workstream 5 |
| `adaptive_min_signals` | 30 | Workstream 5 |
| `ranking_halflife_days` / `ranking_prior_strength` / `ranking_zombie_grace_days` (reused) | - | Workstream 6, per-category shrinkage |
| `crowd_reaction_window_minutes` | 15 | Workstream 7 |
| `crowd_reaction_lookback_days` | 14 | Workstream 7 |
| `crowd_reaction_saturation` | 5 | Workstream 7, avg cross-cluster followers that saturates to 1.0 |
| `crowd_liquidity_reference_usd` | 50000 | Workstream 7 |
| `crowd_volume_reference_usd` | 20000 | Workstream 7 |
| `crowd_spread_reference` | 0.10 | Workstream 7 |
| `crowd_weight_reaction` | 0.5 | Workstream 7, sums to 1.0 with the two below |
| `crowd_weight_fame` | 0.3 | Workstream 7 |
| `crowd_weight_obvious` | 0.2 | Workstream 7 |
| `crowd_penalty_weight` | 0.3 | Workstream 7, soft penalty on `hidden_alpha` |
| `crowdedness_recompute_interval_hours` | 24 | Workstream 7 |

## Rollout and success criteria

Prioritised exactly in the order given, because each later workstream
either consumes an earlier one's output or is far less meaningful without
it: Workstream 4's cluster-banded calibration needs Workstream 1's
`distinct_clusters`; Workstream 5's cluster-level bandit needs Workstream
1's stable cluster ids; Workstream 3 (wallet ranking) and Workstream 2
(CLV) are independent of each other and of Workstream 1, and can land in
parallel once 1 is in place.

Every workstream ties to one measurable, stated up front so this phase can
fail honestly instead of being declared a win by assumption:
- **Workstream 1:** cluster count vs. raw tracked-wallet count - if
  clustering finds near-zero correlated groups, that's a real (and useful)
  answer, not a sign the workstream is broken.
- **Workstream 2:** average CLV, overall and per cluster/score-band -
  positive and stable is the health signal this project has wanted since
  Phase 4, available now without waiting on resolutions.
- **Workstream 3:** the wallet-ranking distribution before/after - does
  the shrinkage-plus-Wilson-bound component actually reorder wallets
  relative to raw win rate the way the 8/10-vs-800/1000 example demands.
- **Workstream 4:** Brier trend over time, and specifically whether
  calibrated `p_hat` beats raw `entry_price` on Brier - the direct
  evidence for or against Phase 5's Kelly sizer being worth trusting yet.
- **Workstream 5:** `adaptive` vs. `baseline` paper portfolio results,
  head-to-head, same signal stream - if `adaptive` doesn't beat `baseline`
  after `adaptive_min_signals` worth of data has accumulated across enough
  clusters, that's this workstream failing honestly, and the multiplier
  bounds (flag 13, `adaptive_weight_min`/`max`) exist precisely so that
  failure stays cheap and reversible rather than compounding.
- **Workstream 6:** the per-category score spread for wallets with history
  in more than one category - specialists (high in one category, low/
  neutral elsewhere) should visibly separate from genuine generalists
  (similar scores everywhere), the direct evidence the GMX "ROI-per-asset"
  lesson actually applies to this project's wallets, not just in theory.
- **Workstream 7:** `hidden_alpha` vs. `baseline` (and vs. `adaptive`,
  independently) paper portfolio results, head-to-head, same signal stream
  - the direct test of whether penalizing measured crowdedness improves
  results, exactly the same champion/challenger standard every other
  workstream in this phase is held to. A crowdedness score that never
  separates skilled-but-crowded from skilled-and-hidden wallets (flag 19-20's
  cold-start caveats aside) would be this workstream failing honestly, not
  a reason to force the penalty into the default consensus weighting anyway.

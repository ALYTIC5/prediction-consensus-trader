# Consensus V2: whale-implied probability, not trader count

## What changes and why

The current consensus engine (app/consensus/engine.py, docs/PHASE3_DESIGN.md)
answers "how many independent, skilled wallets just opened the same side of
this market" - a count, thresholded (BREADTH stage: `distinct_traders >=
min_traders`). It never asks what those wallets are collectively *implying*
about the market's true probability, and it never compares that implied
probability against the market's own price except indirectly, through
Stage 3's calibration buckets (which measure "signals like this win X% of
the time," not "this specific market is mispriced by X").

V2 asks a different, sharper question: **given every tracked wallet's
current position, weighted by skill, what probability do they collectively
imply - and does that diverge from the market's own price by more than
noise?** That divergence is a testable edge (paired Brier score against
market price), independent of P&L, and it's exactly the p_hat Kelly sizing
has been synthesizing indirectly through calibration buckets all along -
this makes it direct.

This is additive, not a replacement: a new portfolio (`consensus_prob`)
runs alongside every existing count-based portfolio, on its own signal
stream, so the two approaches can be compared on identical footing rather
than one silently subsuming the other. Nothing about the existing
consensus engine, its Signal rows, or the count-based portfolios changes.

## 1. Whale-consensus probability

For each market with at least one qualifying position:

```
raw = Σ(score_i × position_usd_i × sign_i) / Σ(score_i × |position_usd_i|)   # in [-1, 1]
P_consensus = (raw + 1) / 2                                                  # rescaled to [0, 1]
```

The requested formula's ratio is bounded to `[-1, 1]` by construction
(`|Σ x_i·sign_i| <= Σ|x_i|` always) - a net directional consensus
("unanimously YES" = +1, "unanimously NO" = -1, "evenly split" = 0), not
itself a probability. The standard `(raw + 1) / 2` rescale is applied so
`P_consensus` lands in `[0, 1]` and is directly comparable to `P_market`
for the divergence signal and paired Brier score, both of which require
the same scale.

**Aggregation unit is the cluster, not the wallet.** i indexes clusters
(app/optimization/clustering.py's Louvain wallet_clusters - the exact same
Sybil-defense grouping the count-based engine already uses via
`_weighted_score()`'s cluster-collapsed distinct_traders), not raw wallet
rows. Within a cluster, `position_usd` is the SUM of every member wallet's
position_usd in that market (a coordinated ring's split position is one
position, not several), and `score` is the MAX of member wallets' scores
(the cluster's best evidence of skill, not diluted by including weaker
co-conspirators - consistent with the count-based engine treating a
cluster as one voice). A wallet with no cluster row (never clustered, or a
true singleton) is its own cluster of one, same convention
app/optimization/clustering.py already establishes.

- `position_usd_i` = `Σ(size × cur_price)` across every OPEN Position row
  the cluster holds in this market, on whichever outcome token(s) it holds.
- `sign_i` = +1 if the cluster's position is in the outcome being scored,
  -1 if in the complementary outcome. **Scoped to binary markets only in
  this pass** - see "Scope: binary markets only" below for why.
- `score_i` = the cluster's max member TraderCategoryScore for this
  market's category (app/optimization/scoring_category.py) - reusing the
  category-aware skill score Workstream 3 already computes, not a new
  scoring mechanism, and not the flat global TraderScore (a wallet sharp
  on Sports shouldn't get equal say on a Politics market's probability).

Result is clamped to `[0, 1]` (a pathological weight distribution could
otherwise push it outside the valid range) and is `None` when the
denominator is zero (no qualifying positions at all - "no opinion," never
a fabricated 0.5).

### Dominant-position cap

A single large position can otherwise swing P_consensus on its own,
which is the opposite of "consensus." Each cluster's `score_i ×
|position_usd_i|` weight is capped at
`consensus_prob_max_cluster_weight_fraction` (default 0.35) of the total
weight sum, computed iteratively (capping one cluster changes the total,
which can push a previously-uncapped cluster over the new total) until no
cluster exceeds the cap or only one cluster remains. Documented failure
mode: with very few contributing clusters (2-3), the cap can still leave
a lot of concentration - CONFIDENCE (below) is what actually protects
downstream consumers from over-trusting a thin, concentrated read, not
the cap alone.

### Confidence

```
CONFIDENCE = f(n_clusters, agreement, total_weight, score_dispersion)
```

Four components, each in `[0, 1]`, combined by multiplication (a
confidence claim this project makes should require EVERY component to be
decent, not just the average of a strong one and a weak one masking each
other):

- **n_clusters**: `min(1, n_clusters / consensus_prob_confidence_n_clusters_target)`
  (default target 5) - more independent voices, more confidence, capped at 1.
- **agreement**: `1 - weighted_std(sign_i, weight=score_i × |position_usd_i|)`
  - clusters split on direction (some long YES, some long NO) lowers
  confidence sharply; unanimous direction with real disagreement only on
  size does not.
- **total_weight**: `min(1, Σ|position_usd_i| / consensus_prob_confidence_notional_target)`
  (default target $5,000) - a consensus built from a few hundred dollars
  of aggregate position is not the same claim as one built from tens of
  thousands.
- **score_dispersion**: `mean(score_i)` weighted by position_usd - a
  consensus built from consistently high-skill clusters is worth more
  than the same divergence built from low-score clusters that happened to
  clear the score threshold.

### Scope: binary markets only

The `sign_i = +1/-1` formulation is a two-outcome concept - "the
probability of THIS outcome" only cleanly decomposes into a signed sum
when there are exactly two complementary outcomes. A native N-outcome
market (N>2 outcome tokens under one condition_id) does not have a single
well-defined "sign" the way a YES/NO market does. V2 computes P_consensus
per condition_id **only for markets with exactly 2 outcomes**; N-outcome
markets are skipped entirely in this pass, not approximated. Extending to
N-outcome markets (treating each outcome as its own binary "does this
outcome win" sub-question, independently) is a reasonable future
extension but changes the normalization story (N independent binary
P_consensus values don't automatically sum to 1 the way a real N-outcome
distribution must) and is out of scope here.

### Failure modes

- **Stale positions**: `Position.cur_price` is only as fresh as the last
  markets-collector sync (docs/API_REFERENCE.md's archival-gap fix already
  addresses the worst case - a resolved-but-unmarked market - but ordinary
  staleness between syncs still means position_usd can lag the live book
  by up to `markets_interval_seconds`).
- **Thin universe**: this project tracks `tracked_wallets_limit` wallets
  (50 by default) total, not per market - for any single market, the
  number of tracked wallets holding a position in it at all may be very
  small, well below what count-based consensus already requires
  (`min_traders`, default 3). P_consensus can be computed from 1-2
  clusters for most markets; CONFIDENCE is what's supposed to catch this,
  but it's worth stating plainly: **this signal will likely be sparser,
  not denser, than the count-based one**, precisely because it requires
  live position size, not just any historical trading activity.
- **Cluster reclassification lag**: wallet_clusters is only as fresh as
  the last clustering job run (cluster_recompute_interval_hours) - a
  wallet whose true cluster membership just changed is scored under its
  stale cluster assignment until the next run, same limitation the
  count-based engine already lives with.

## 2. Divergence signal and the consensus_prob signal pipeline

**This cannot be bolted onto app/consensus/engine.py's existing pipeline.**
That engine is event-driven: it evaluates fresh OPENED/INCREASED
position_history events within a rolling window, grouped per market, and
produces a Signal exactly once per (market, window) via an 8-stage
first-failure-wins filter chain keyed on discrete counts
(`distinct_traders >= min_traders`, etc.). P_consensus is a **state**
computation (current aggregate position across all open positions,
regardless of when they were opened) with a **continuous** entry
condition (divergence past a threshold), not an event count. Forcing it
through the existing pipeline would mean either faking event counts to
satisfy BREADTH, or gutting the pipeline's whole shape for every other
portfolio. See "Flagged for confirmation" below - this is the single
biggest structural decision in this doc.

New module `app/consensus_v2/probability.py` (pure) + `app/consensus_v2/
scan.py` (DB orchestration, its own periodic job) parallels app/consensus/
engine.py + app/signals/generator.py's split, but produces a **new**
signal type rather than reusing `signals.Signal`:

`signal_prob` table - one row per (condition_id, scan cycle) a divergence
clears the filter:

| column | meaning |
|---|---|
| `condition_id` | the market |
| `p_consensus` | this cycle's whale-implied probability |
| `p_market` | market price at signal time (see "What counts as P_market" below) |
| `divergence` | `p_consensus - p_market` |
| `confidence` | this cycle's CONFIDENCE |
| `n_clusters` | contributing cluster count (nominal, for display) |
| `liquidity`, `spread` | carried at signal time, same filters as today |
| `created_at` | |

Entry filters, evaluated in order (same first-failure-wins shape as the
existing engine, same MARKET_KNOWN/MARKET_OPEN/LIQUIDITY/SPREAD stages
verbatim - only BREADTH/QUALITY/CONVICTION are replaced):

1. MARKET_KNOWN / MARKET_OPEN / LIQUIDITY / SPREAD - unchanged, reused
   from app/consensus/engine.py's own filter functions directly (no
   duplication).
2. FRESHNESS - the market's price/book snapshot must be within
   `consensus_prob_max_snapshot_age_seconds` (default 300s) of scan time -
   the divergence version of the existing engine's own freshness handling.
3. DIVERGENCE - `abs(divergence) >= consensus_prob_min_divergence`
   (default 0.05 - five points of implied probability).
4. CONFIDENCE - `confidence >= consensus_prob_min_confidence` (default
   0.4).

### What counts as P_market

`mid = (best_bid + best_ask) / 2` from the latest order_books snapshot,
falling back to `Market.last_trade_price` when no book snapshot exists
yet. Not `best_ask`: the ask side is the price a TAKER pays, already
skewed away from the market's true implied probability by half the
spread - mid is the standard, spread-neutral "the market's belief" used
for calibration comparisons throughout this design (the paired Brier
score in particular would be biased if compared against a systematically
high or low reference).

### The consensus_prob portfolio

A new PaperPortfolio row (`consensus_prob`), seeded like any other
strategy (scripts/seed_portfolios.py), entering on `signal_prob` rows the
same way today's portfolios enter on `signals` rows: book-walk fill
(app/paper/fills.py), fee model (app/paper/fees.py), sized by
`consensus_prob_fixed_fraction_pct` initially (Kelly is gated - see
section 3), held to the same take-profit/stop-loss/signal-expiry exit
rules every other portfolio uses. It runs alongside every existing
portfolio, never instead of them - the comparison page
(app/dashboard/queries.py's `get_comparison_data`) already handles an
arbitrary portfolio list, so `consensus_prob` appears there automatically
with everything else's ROI/win-rate/Sharpe once it has trades.

## 3. Kelly wiring, gated

Kelly's `size_kelly_position` currently reads `p_hat` from
`get_p_hat()` - an EMPIRICALLY measured hit rate for signals in the
same [cluster_count, score, price] bucket (app/risk/calibration.py),
never a per-market theoretical estimate. P_consensus is the latter: a
direct probability claim for THIS market, not "signals shaped like this
one win X% of the time historically." **This is a real swap of what
p_hat means for Kelly, not just a new candidate input alongside the old
one** - see "Flagged for confirmation" below.

Gate (checked once per Kelly sizing decision, cheap - reads a small
cached aggregate, never recomputes the full paired-Brier pass inline):
P_consensus is trusted as Kelly's p_hat for a `consensus_prob` signal
only when ALL of:

- `paired_brier.effective_n >= consensus_prob_kelly_min_effective_n`
  (default 30 - distinct event clusters, app/optimization/
  event_clustering.py, not nominal resolved-market count)
- `paired_brier.mean_difference > 0` (consensus beat market on average)
  and `paired_brier.t_statistic >= consensus_prob_kelly_min_t_stat`
  (default 1.96 - the same z-value this project already treats as "95%
  confidence" everywhere else, Wilson intervals included)

Until that gate passes, `consensus_prob`'s own signals fall back to
TIERED sizing, exactly like every other portfolio does today when Kelly's
existing calibration gate isn't cleared - no new failure mode, same
"prove it before trusting it" shape docs/PHASE5_DESIGN.md already
established for Stage 3.

## 4. Paired Brier score - the honest edge metric

For every `signal_prob` row whose market has since resolved
unambiguously (docs/PHASE4_DESIGN.md's `market_resolved` semantics,
reused verbatim - a market_resolved exit is a real settlement, not a
take-profit/stop-loss exit that only tells you the price moved):

```
brier_consensus = (p_consensus - outcome)^2
brier_market    = (p_market    - outcome)^2
paired_diff     = brier_market - brier_consensus   # positive = we beat the market
```

Stored per-row on `signal_prob` (`brier_consensus`, `brier_market`,
`paired_diff`, filled in once, at resolution - same "computed once time/
resolution actually happen" convention as `SignalCLV`).

**Aggregation uses event-clustered standard errors**
(app/optimization/event_clustering.py, from U.3), not the naive
per-market standard error: resolved markets sharing an event cluster
(same election, same tournament) are correlated observations of the
same underlying uncertainty, and treating them as independent overstates
significance exactly the way U.3's own effective-n work exists to catch.
Concretely: group paired_diff by event_cluster_id, compute each cluster's
mean, then the standard error of the CLUSTER MEANS (not of the raw
per-market values) - the standard "cluster-robust" approach, and the
natural generalization of "effective n = cluster count" to a continuous
statistic rather than a sample-size count.

```
stderr      = std(cluster_means) / sqrt(effective_n)   # standard error of the mean
t_statistic = mean(cluster_means) / stderr
```

Reported: `mean_difference`, `t_statistic`, `effective_n` (cluster count)
alongside `nominal_n` (resolved market count) - never one without the
other, same "n=X (effective n=Y)" convention U.3 already established
everywhere else in this project.

**This is the actual verdict**, independent of the portfolio's own P&L:
if `mean_difference <= 0` or `t_statistic` isn't significant, the whales
this project tracks don't beat the market on this measure, regardless of
what `consensus_prob`'s bankroll shows - a portfolio can look profitable
on a small, lucky, correlated sample exactly like U.3's whole premise
warns against.

## 5. Calibration curve

Ten deciles by `p_consensus` (0-10%, 10-20%, ..., 90-100%), each showing
predicted probability (bucket mean p_consensus) vs. realized frequency
(fraction that actually resolved YES), plotted alongside the market's own
calibration curve (same deciles, bucketed by `p_market` instead). Closer
to the 45-degree diagonal than the market's own curve is the visual form
of a positive paired Brier result - shown on the dashboard, not asserted
in prose. Buckets below `calibration_min_samples_per_bucket` (reused from
Stage 3, docs/PHASE5_DESIGN.md) are shown but flagged thin, same
suppress-not-fabricate rule as every other bucketed statistic in this
project.

## Dashboard

New `/consensus-v2` page: live P_consensus/divergence/confidence per
market, the consensus_prob portfolio's metrics (reusing
compute_portfolio_metrics, same as every other portfolio), the paired
Brier result (mean difference, t-statistic, nominal vs. effective n), and
the calibration curve comparison.

## Flagged for confirmation before implementation

Three decisions above are substantial enough that this doc's own
instruction ("flag disagreement before implementing") applies to them
specifically - implementing before confirming risks building the wrong
shape:

1. **New parallel pipeline, not an extension of app/consensus/engine.py.**
   The existing engine is fundamentally event-driven and count-thresholded;
   P_consensus is state-driven and continuous. I'm proposing a new
   `signal_prob` table and a new scan job rather than forcing this through
   the existing `Signal`/8-stage-filter shape. This is the biggest
   structural choice in this doc and the one most worth a second look.
2. **Kelly's p_hat source actually SWAPS, not adds.** "Wire it as an input
   to Kelly" reads most naturally as P_consensus REPLACING the empirical
   calibration-bucket p_hat for `consensus_prob` signals specifically
   (once gated) - a genuine change in what kind of probability estimate
   Kelly reasons over, not an additional signal blended in. If a
   blend/ensemble was intended instead, that's a different (and more
   complex) design.
3. **Score source for score_i is TraderCategoryScore (category-aware),
   not the flat global TraderScore.** The request's formula doesn't
   specify which score; I chose category-aware for consistency with how
   the count-based engine already weights contributors, but the global
   score is simpler and was the more literal historical "trader score"
   before Workstream 3 existed.

Everything else in this doc (the dominant-position cap mechanism, binary-
markets-only scope, mid-price as P_market, the specific default
thresholds) is a normal implementation decision, not a fork in the
strategy - noted for the record, not gating.

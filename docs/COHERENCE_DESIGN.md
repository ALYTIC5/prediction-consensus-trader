# Coherence Arbitrage Detector

## Why this exists

Every other signal in this project depends on watching profitable wallets:
consensus, calibration, Scout. That's a whale-tracking edge — real, but
correlated with whatever those wallets are doing, and gone if they stop
trading or get crowded out.

Coherence arbitrage needs no wallet signal at all. Polymarket's own pricing
must obey basic logical constraints: a YES token and a NO token on the same
binary market are complementary outcomes, so their prices should sum to
$1.00 (minus the market's edge). A set of mutually exclusive outcomes on one
event should sum to $1.00 across the set. A market conditioned on another
market winning can never be priced higher than the market it depends on.
When market pricing violates one of these constraints, buying the
mispriced side is a **provable** profit at resolution, not a
probabilistic bet — the payoff isn't "usually right," it's guaranteed by
arithmetic (for the two scan types actually traded — see the auto-trading
section below for why NOT every scan type gets this guarantee).

This runs **alongside** the consensus strategy, not instead of it. It's a
second, structurally independent edge source, so its own paper portfolio
answers a real question: after real fills and real fees, is measurable
mispricing actually there often enough, and long enough, to trade?

## Non-negotiable: fills and fees first

A gross spread computed from `best_ask`/`best_bid` alone is not an
opportunity. Two costs eat it before it's real:

1. **Fill cost.** Buying a leg at any real size walks the book
   (app/paper/fills.py's `walk_the_book`) past the best price. A spread
   that looks like $0.02 at the top of book can vanish entirely once
   size any bigger than a few dollars is actually filled.
2. **Fees.** Every leg is a taker fill and pays Polymarket's taker fee
   (app/paper/fees.py's `compute_taker_fee`, verified against
   docs.polymarket.com/trading/fees — see docs/API_REFERENCE.md). A 7%
   crypto-category fee on each of two legs is 14 cents of the dollar
   before any edge is realized.

Every violation this module reports carries **both** a `gross_spread`
(top-of-book, for context) and a `net_profit` (after walking the book for
the actually-fillable size on every leg, and after fees on every leg).
Only `net_profit` decides whether something is a real opportunity. A
violation whose net_profit is None or ≤ 0 is logged for the historical
record but never captured.

## Scan types

### 1. YES + NO sum (single binary market)

Every binary market has exactly two outcome tokens. Their prices must sum
to $1.00 at resolution (exactly one pays $1, the other $0). Two directions:

- **`best_ask_YES + best_ask_NO < 1.00 − buffer`**: buying one share of
  each guarantees a $1.00 payout at resolution for less than $1.00 spent.
  **Actionable** — this is what the coherence portfolio trades.
- **`best_bid_YES + best_bid_NO > 1.00 + buffer`**: whoever already holds
  both legs could sell for more than $1.00 combined. **Not actionable**
  by this project: capturing it requires selling (shorting) a position we
  don't already hold, and this codebase has no shorting infrastructure
  anywhere (paper trading is long-only throughout, matching the project's
  own READ-ONLY / no-live-orders posture). Detected and logged for
  completeness and honesty about how often it happens, never captured.

### 2. Multi-outcome consistency

Generalizes scan 1 to any set of outcomes that are mutually exclusive and
collectively exhaustive — exactly one member of the set resolves YES.
Two shapes exist on Polymarket and both are checked with the same pure
function (`detector.py`'s `detect_outcome_set_violation`, parameterized by
whatever leg set is passed in):

- **One market's own outcome tokens** — a single condition_id with N>2
  outcomes (e.g. "Who wins the tournament") — scan 1 is the N=2 case of
  this, so there's no separate code path for it.
- **An event's sibling binary markets** — many Polymarket elections are
  modeled as N separate binary markets ("Will Candidate X win?") sharing
  one `event_slug`, not one N-outcome market. Grouped by `event_slug`
  (the same grouping app/optimization/event_clustering.py uses for
  independence discounting — reused here for market identification, not
  for effective-n) using each market's own "Yes" leg as that candidate's
  representative in the set.

`Σ(best_ask_i) < 1.00 − buffer` across the group ⇒ buying one share of
every leg guarantees exactly one $1.00 payout for less than $1.00 total
spent. Actionable, same as scan 1's ask direction.

### 3. Nested/conditional logic — detection only, never auto-traded

The constraint: winning a prerequisite event is necessary for winning the
dependent event (a classic case: winning a party's presidential primary is
necessary for winning that party's nomination to compete in, and possibly
win, the general election), so `P(wins general) ≤ P(wins primary)` must
hold. A violation is `ask_general > ask_primary + buffer` (using best_ask
as the tradeable probability proxy on both sides — not a true midpoint,
which biases this toward the price actually payable, a deliberately
conservative choice).

**Pair identification, and why it's genuinely risky:** within one
`event_slug` group, a market is heuristically classified as the
"prerequisite" leg if its question text contains "primary" or
"nomination" (case-insensitive), and every other market in the same group
is treated as a candidate "dependent" leg it constrains. This is a crude
keyword heuristic, not semantic understanding of the question — it will
misfire on any event whose sibling markets don't happen to use those exact
words, and it can't verify the two markets are actually about the same
underlying entity (candidate, team) beyond sharing an event_slug. An
incorrectly identified pair produces a **false arb**: a "violation" that
isn't a real logical constraint at all, just two unrelated numbers that
happen to compare a certain way.

Because of this, scan 3 output is written to `coherence_opportunities`
with `type=NESTED_LOGIC` and `net_profit` computed for reference, but
**the coherence portfolio never auto-trades it.** It exists for a human
to review on the dashboard and decide, market by market, whether the pair
is real. This mirrors the same caution CLAUDE.md and this project's
existing fill-model already apply elsewhere: prefer NO_LIQUIDITY over a
fantasy price, prefer no trade over a wrong one.

### 4. Cross-platform (Kalshi) — not implemented this pass

Explicitly optional ("only if reachable") in the request, and skipped:
this project has no Kalshi API client, no Kalshi docs verification, and no
existing convention for a second exchange's data anywhere in the codebase.
Building one is a meaningfully sized new integration (new client, new
credential/config surface, new resolution-criteria mapping) rather than an
extension of what's already here, and cross-platform resolution criteria
differing subtly between platforms is exactly the kind of judgment call
CLAUDE.md's working agreement says to bring back to the operator rather
than silently build around. If this becomes a priority, it should verified
against Kalshi's own docs first, exactly like every Polymarket endpoint in
this codebase was, and even then treated as **candidate for manual review,
never auto-traded** — resolution-criteria drift between platforms means a
"guaranteed" cross-platform profit can simply not be guaranteed at all.

## Data model

`coherence_opportunities` — one row per persisting violation, not one row
per scan cycle:

| column | meaning |
|---|---|
| `id` | PK |
| `opportunity_key` | stable identity across cycles: `type` + sorted leg condition_ids, hashed. A scan that finds the same violation again UPDATES this row's `last_seen_at` rather than inserting a new one - this is what makes `duration` meaningful. |
| `type` | `YES_NO_SUM_ASK` / `YES_NO_SUM_BID` / `MULTI_OUTCOME_ASK` / `MULTI_OUTCOME_BID` / `NESTED_LOGIC` |
| `legs` | JSONB list of `{condition_id, asset, outcome, side}` - every leg involved |
| `gross_spread` | top-of-book spread at most recent detection, before fills/fees |
| `size` | shares walked per leg at most recent detection (the binding constraint - the smallest fillable size across all legs) |
| `net_profit` | after book-walking every leg for `size` and applying fees - None if not fillable at any size (NO_LIQUIDITY on some leg) |
| `required_capital` | total cost to enter every leg at the walked size |
| `detected_at` | first cycle this opportunity (by `opportunity_key`) was seen |
| `last_seen_at` | most recent cycle it was still present |
| `resolved_at` | first cycle it was no longer detected (None while still open) - `resolved_at - detected_at` is how long it persisted |
| `captured` | whether the coherence portfolio actually entered it (only ever True for `YES_NO_SUM_ASK`/`MULTI_OUTCOME_ASK`) |

`coherence_fills` — one row per leg actually entered, not reusing
`paper_trades`: every `paper_trades` row requires a `signal_id` (NOT
NULL, joined by calibration/Brier as an INNER JOIN elsewhere in this
project), and a coherence leg has no underlying Signal at all - making
that column nullable to accommodate a structurally different strategy
would weaken a real invariant every other consumer of that table relies
on. Instead `coherence_fills` is a small, dedicated table carrying only
what a leg needs (condition_id, asset, entry_price, size, fee_paid,
exit_price, realized_pnl, status), linked to its `coherence_opportunities`
row by `opportunity_id` and to the "coherence" `paper_portfolios` row (kept
`is_active=false` so the normal signal-driven engine cycle already skips
it via its existing active-portfolio filter - no change needed there) by
`portfolio_id` for bankroll bookkeeping. app/paper/metrics.py's pure
`compute_portfolio_metrics`/`TradeData` are still reused as-is (they take
plain values, not an ORM row) - only the DB loader that builds `TradeData`
from `coherence_fills` instead of `paper_trades` is new, so win rate / ROI
/ Sharpe come for free from the exact same tested formulas every other
portfolio uses.

## Why holding to resolution is the exit, not a rule choice

For `YES_NO_SUM_ASK` and `MULTI_OUTCOME_ASK`, the coherence portfolio's
legs are simply held to `market_resolved` (app/paper/engine.py's
ExitReason) like any other trade - never take-profit/stop-loss/expiry.
This isn't a configuration choice, it's the only exit that actually
realizes the guarantee: exactly one leg resolves to $1.00 and every other
leg resolves to $0.00 (or, for a multi-outcome set spanning more than one
market, the analogous split), and the sum of those payouts is exactly
$1.00 by construction of the constraint being exploited. Selling a leg
early (even at a profit) breaks that guarantee and turns a provable
arbitrage back into a directional bet on which leg wins - exactly the
kind of thing this strategy exists to avoid depending on. Redemption at
resolution is also fee-free (see app/paper/fees.py's docstring), so the
only fees the trade ever pays are the entry fills - never a second round
on exit.

## Scan cadence and settings

Runs as its own periodic job (`app/coherence/scan.py`'s
`run_coherence_scan_job`), every `coherence_scan_interval_seconds`
(default 180s - frequent enough to catch a spread before it's arbed away
by someone else, infrequent enough to stay well inside this project's own
conservative-rate-limit convention). Settings:

- `coherence_scan_interval_seconds` (180)
- `coherence_min_edge` - minimum gross spread before a raw candidate is
  even worth book-walking (0.01 - a cent; smaller than that is almost
  certainly fee-negative before any fill cost is even considered)
- `coherence_max_book_depth_fraction` - same reasoning and same default
  (0.20) as `paper_max_book_depth_fraction`: beyond this fraction of
  visible depth, walking the book further would move the market enough
  that the price no longer describes what's actually payable
- `coherence_fee_rate_default` - same fallback role as
  `paper_fee_rate_default` for a leg with no live fee_rates snapshot yet
- `coherence_max_position_notional` - hard cap on capital committed to
  any single captured opportunity, so a mispriced/illiquid market can't
  swallow the whole portfolio on one bet

Unlike app/collectors/orderbook.py (which only snapshots books for
markets this project already holds a position or signal in - it exists to
price OUR fills, not to discover new ones), the coherence scanner needs
books for markets we may hold nothing in at all, since finding a new
mispricing regardless of prior exposure is the entire point. It fetches
its own candidate books live from the CLOB (`PolymarketClient.get_book`/
`get_fee_rate`, the exact same calls orderbook.py/fee_rates.py make) and
uses them immediately for that cycle's detection - it does not read from
or write to the `order_books`/`fee_rates` tables, since it needs the book
at scan time, not a snapshot that might be stale by the time it's read
back.

Candidates are the `coherence_max_markets_per_cycle` (default 50 - start
conservative, same reasoning as every other brand-new CLOB call volume in
this project) highest-liquidity not-yet-closed markets. At 2 outcome
tokens and 2 calls (book + fee rate) per market, that's ~200 requests per
scan cycle - the same order of magnitude already established as safe for
orderbook.py/fee_rates.py's own 200-token caps. Ranking by liquidity is a
deliberate choice: a mispriced illiquid market is real but tiny (the
depth cap makes it barely fillable anyway), while a mispriced *liquid*
market is both rarer and more interesting - it's a bigger, more
capturable edge, and it's also the market most likely to be re-priced
correctly soon by someone else, which is exactly the frequency/duration
question this feature exists to answer.

## Dashboard

`/coherence` page:

- **Live opportunities** - every currently-open row (`resolved_at IS NULL`),
  type, legs, gross spread, net profit, age since `detected_at`, captured
  or not.
- **Historical frequency** - opportunities detected per day, by type.
- **Duration distribution** - histogram of `resolved_at - detected_at`
  for closed opportunities, by type. This is the number that answers
  "is this a real edge": violations persisting for minutes are
  structurally different from ones that vanish in one scan cycle.
- **Net capture P&L** - the coherence portfolio's own metrics
  (app/paper/metrics.py's `compute_portfolio_metrics`, the same function
  every other portfolio uses), plus a capture-rate figure: opportunities
  captured vs. opportunities detected-but-vanished-before-fill.

## Known limitations (read before trusting this strategy's numbers)

- Scan-cycle granularity: an opportunity that appears and disappears
  entirely between two scans is never seen at all. The scan interval is
  itself a lower bound on how fast a real opportunity would need to
  vanish before this system could act on it - a violation that persists
  for less than `coherence_scan_interval_seconds` may exist and never
  appear in `coherence_opportunities` regardless of how real it was.
- The multi-outcome event grouping trusts `event_slug` and, transitively,
  Polymarket's own editorial choice of which markets belong to one event -
  the same trust (and the same failure mode) app/optimization/
  event_clustering.py documents for its own clustering.
- Scan 3's pairing heuristic is explicitly weak (see above) - its output
  is for manual review, and should never be read as "these many nested-
  logic arbs exist" without a human checking each pair.
- A captured opportunity can still lose the race between detection and
  fill: the book this project walks against is the last snapshot
  app/collectors/orderbook.py took, not a live quote at the instant of
  entry. `captured=True` rows whose actual paper-trade fill differs
  materially from the detected `net_profit` are the honest signal for how
  much of this edge is real vs. a snapshot-staleness artifact - this is
  exactly what the dashboard's capture-rate figure is for.

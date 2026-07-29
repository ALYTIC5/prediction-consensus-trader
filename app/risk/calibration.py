"""Stage 3a: consensus-to-probability calibration.

See docs/PHASE5_DESIGN.md section 4, formalised by docs/PHASE6_DESIGN.md
workstream 4. The rule this whole stage is built around: we do not invent a
win probability, we measure one, from this project's own resolved paper
trades. Split into a pure core (bucketing, Wilson interval, the bucket
lookup) and a thin DB loader (load_resolved_trade_records) - the same split
app/paper/sizing.py and app/paper/engine.py use everywhere else, so the
statistics themselves are unit-testable without a database.

Bucketing is by INDEPENDENT-CLUSTER count, not raw wallet count, as of
Phase 6 workstream 1: Signal.distinct_traders was redefined in place at
signal-creation time to count clusters (see app/consensus/engine.py's
_weighted_score() docstring) - this module just reads that same column,
renamed here to cluster_count for clarity, with no separate migration
needed. Resolved trades from BEFORE that redefinition shipped still carry
raw wallet counts in the same column; there is no way to distinguish them
retroactively (docs/PHASE6_DESIGN.md flag 2), but the rolling window below
already ages them out of every bucket on its own - no special-case code
needed, the same mechanism that already handles regime change handles this
cutover for free.

Because Kelly sizing (Stage 3b, app/risk/sizing_kelly.py) reads p_hat
straight from get_p_hat() below, every improvement this bucketing gets -
cluster-based independence included - flows directly into Kelly's sizing
decisions with no separate wiring; see resolve_calibration_config() in
app/paper/engine.py for where get_p_hat()'s inputs are actually assembled.

"Resolved" means CLOSED with exit_reason == market_resolved specifically -
not any CLOSED trade. A trade closed by take_profit/stop_loss/signal_expiry
tells you the price moved favorably or unfavorably enough to trigger an
exit rule, not whether the held outcome actually won or lost at
settlement - counting those as "wins"/"losses" would measure the exit
rules, not the signal's true hit rate. This is stricter than a literal
reading of "every resolved (CLOSED) paper trade" would suggest, and is why
buckets stay sparse for a long time (docs/PHASE5_DESIGN.md flag 5) - it's
the honest cost of measuring the thing p_hat actually claims to measure.

Recency: a rolling window, not a decay-weighted average (flag 6, unchanged
here) - only trades whose exit_at falls within the last
CALIBRATION_WINDOW_DAYS days count toward a bucket's p_hat at all; every
trade inside the window counts with equal weight, and one outside it counts
for nothing. This is what "weight recent trades more" means concretely in
this implementation: a hard cutoff, not a smooth decay, because "these are
the N trades that counted, resolved in the last 90 days" is a query anyone
can audit by re-running it - "each trade counts exp(-age/tau) as much" is
not, and this project's conventions favor the auditable version.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PaperTrade, PaperTradeStatus, Signal

# Mirrors app.paper.engine.ExitReason.MARKET_RESOLVED's value - not imported
# directly to avoid a circular import (engine.py imports this module for
# the KELLY sizer path).
_MARKET_RESOLVED_EXIT_REASON = "market_resolved"


@dataclass(frozen=True)
class ResolvedTradeRecord:
    """One resolved paper trade's calibration-relevant features - plain
    values, not an ORM row, so compute_bucket_stats stays DB-free.
    """

    cluster_count: int
    weighted_score: Decimal
    average_entry_price: Decimal
    won: bool


@dataclass(frozen=True)
class SignalFeatures:
    """The subset of a signal's fields a bucket lookup needs."""

    cluster_count: int
    weighted_score: Decimal
    average_entry_price: Decimal


@dataclass(frozen=True)
class CalibrationConfig:
    """Every calibration tunable - see docs/PHASE5_DESIGN.md section 9. Not
    portfolio-overridable (flag 9's table) - calibration is a property of
    the shared signal-generation process, not a per-portfolio choice.
    """

    cluster_bands: tuple[tuple[Decimal, Decimal], ...]
    score_bands: tuple[tuple[Decimal, Decimal], ...]
    price_bands: tuple[tuple[Decimal, Decimal], ...]
    min_samples_per_bucket: int
    window_days: int
    # 95% two-sided Wilson interval - not a Settings field, since nothing in
    # this task asks the confidence level itself to be operator-tunable.
    confidence_z: Decimal = Decimal("1.96")


@dataclass(frozen=True)
class BucketStats:
    n: int
    wins: int
    p_hat: Decimal
    ci_low: Decimal
    ci_high: Decimal


@dataclass(frozen=True)
class CalibrationResult:
    p_hat: Decimal
    n: int
    ci_low: Decimal
    ci_high: Decimal


BucketKey = tuple[int, int, int]


def _band_index(value: Decimal, bands: tuple[tuple[Decimal, Decimal], ...]) -> int | None:
    """Half-open [min, max) bands, same convention as
    app/risk/sizing_tiered.py's fraction_for(): the index of the band value
    falls into, the last band's index if value is at/above its max
    (bands are configured with an effectively unbounded top edge), or None
    if value is below the lowest band's min.
    """
    if value < bands[0][0]:
        return None
    for i, (lo, hi) in enumerate(bands):
        if lo <= value < hi:
            return i
    return len(bands) - 1


def bucket_key(
    cluster_count: int,
    weighted_score: Decimal,
    average_entry_price: Decimal,
    config: CalibrationConfig,
) -> BucketKey | None:
    """Public (unlike _band_index) - app/optimization/brier.py also needs
    this to group Brier scores by the same buckets calibration itself uses.
    """
    cluster_idx = _band_index(Decimal(cluster_count), config.cluster_bands)
    score_idx = _band_index(weighted_score, config.score_bands)
    price_idx = _band_index(average_entry_price, config.price_bands)
    if cluster_idx is None or score_idx is None or price_idx is None:
        return None
    return (cluster_idx, score_idx, price_idx)


def wilson_interval(wins: int, n: int, z: Decimal) -> tuple[Decimal, Decimal]:
    """Wilson score interval for a binomial proportion - well-behaved at
    the small sample sizes this project will see for a long time, unlike a
    naive normal-approximation interval (docs/PHASE5_DESIGN.md section 4).
    """
    if n == 0:
        return (Decimal("0"), Decimal("1"))
    n_d = Decimal(n)
    p_hat = Decimal(wins) / n_d
    z2 = z * z
    denom = 1 + z2 / n_d
    center = (p_hat + z2 / (2 * n_d)) / denom
    variance_term = p_hat * (1 - p_hat) / n_d + z2 / (4 * n_d * n_d)
    margin = (z * variance_term.sqrt()) / denom
    return (max(Decimal("0"), center - margin), min(Decimal("1"), center + margin))


def compute_bucket_stats(
    records: list[ResolvedTradeRecord], config: CalibrationConfig
) -> dict[BucketKey, BucketStats]:
    """Groups records by bucket and computes each bucket's empirical hit
    rate + Wilson interval. A record whose features fall below the lowest
    band on any dimension is dropped (docs/PHASE5_DESIGN.md flag 7) - there
    is no "below the lowest band" bucket for calibration the way Stage 2's
    sizer has a floor-or-skip choice; it's simply not bucketable.
    """
    grouped: dict[BucketKey, list[bool]] = {}
    for record in records:
        key = bucket_key(
            record.cluster_count, record.weighted_score, record.average_entry_price, config
        )
        if key is None:
            continue
        grouped.setdefault(key, []).append(record.won)

    stats: dict[BucketKey, BucketStats] = {}
    for key, outcomes in grouped.items():
        n = len(outcomes)
        wins = sum(outcomes)
        p_hat = Decimal(wins) / Decimal(n)
        ci_low, ci_high = wilson_interval(wins, n, config.confidence_z)
        stats[key] = BucketStats(n=n, wins=wins, p_hat=p_hat, ci_low=ci_low, ci_high=ci_high)
    return stats


def get_p_hat(
    bucket_stats: dict[BucketKey, BucketStats], features: SignalFeatures, config: CalibrationConfig
) -> CalibrationResult | None:
    """The one public lookup callers need: a signal's calibrated p_hat, or
    None meaning "Stage 3 has nothing to say for this signal" - not enough
    resolved trades in its bucket (or its bucket has none at all) to trust
    a rate. None is the caller's (app/paper/engine.py's) signal to fall back
    to Stage 2 tiered sizing for this one signal, not the whole portfolio.
    """
    key = bucket_key(
        features.cluster_count, features.weighted_score, features.average_entry_price, config
    )
    if key is None:
        return None
    stats = bucket_stats.get(key)
    if stats is None or stats.n < config.min_samples_per_bucket:
        return None
    return CalibrationResult(
        p_hat=stats.p_hat, n=stats.n, ci_low=stats.ci_low, ci_high=stats.ci_high
    )


def load_resolved_trade_records(
    session: Session, window_days: int, now: datetime
) -> list[ResolvedTradeRecord]:
    """Every resolved paper trade across all portfolios (not just one - more
    shared history means less sparse buckets, and calibration is a property
    of the shared signal-generation process, not any one portfolio's own
    rules - docs/PHASE5_DESIGN.md section 4), within the rolling recency
    window.
    """
    window_start = now - timedelta(days=window_days)
    rows = session.execute(
        select(
            Signal.distinct_traders,
            Signal.weighted_score,
            Signal.average_entry_price,
            PaperTrade.exit_price,
        )
        .join(Signal, PaperTrade.signal_id == Signal.id)
        .where(
            PaperTrade.status == PaperTradeStatus.CLOSED,
            PaperTrade.exit_reason == _MARKET_RESOLVED_EXIT_REASON,
            PaperTrade.exit_at >= window_start,
        )
    ).all()
    return [
        ResolvedTradeRecord(
            # Signal.distinct_traders IS the cluster count for any signal
            # created after the Phase 6 workstream 1 cutover - see the
            # module docstring for why older, pre-cutover rows need no
            # special handling here (the rolling window ages them out).
            cluster_count=row.distinct_traders,
            weighted_score=row.weighted_score,
            average_entry_price=row.average_entry_price,
            won=(row.exit_price == Decimal("1.0")),
        )
        for row in rows
    ]

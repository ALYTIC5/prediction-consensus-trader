"""Brier score - docs/PHASE6_DESIGN.md workstream 4.

For every signal that resolved market_resolved, compares an implied
probability against the actual 0/1 outcome via the standard Brier score
(lower is better - 0 is a perfect prediction, 1 the worst possible one).
Two reference probabilities, evaluated separately, not one:

- brier_raw: entry_price itself against the outcome - the market's own
  implied probability at signal time. The baseline: how good is the raw
  market price alone. Stored on signal_clv, filled in once per signal
  (entry_price and the outcome never change once resolved).
- brier_calibrated (aggregate_brier_calibrated_overall below): get_p_hat()'s
  CURRENT calibrated bucket probability against the same outcome, computed
  fresh on every call, never stored - a bucket's p_hat changes as more
  trades resolve, so freezing this into a column would silently go stale.
  This is the direct evidence for whether Phase 5's calibration (as
  formalised by this workstream) is worth trusting: if calibrated doesn't
  beat raw on average Brier, calibration isn't adding anything yet, and
  that's exactly as important to know as the reverse.

Evaluated against every resolved signal in signal_clv - not just the
paper-trade population app/risk/calibration.py's own load_resolved_trade_records
measures p_hat from (a downstream, entry-filtered subset of all signals,
since only signals some portfolio actually sized and filled become paper
trades). Testing against the broader signal population is a more honest
check of whether calibration generalises, not just a restatement of the
population it was fit on.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Signal, SignalCLV
from app.db.session import db_session
from app.risk.calibration import (
    BucketKey,
    CalibrationConfig,
    SignalFeatures,
    bucket_key,
    compute_bucket_stats,
    get_p_hat,
    load_resolved_trade_records,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrierUpdateSummary:
    backfilled: int


@dataclass(frozen=True)
class BrierAggregate:
    label: str
    n: int
    avg_brier: Decimal | None


def brier_score(probability: Decimal, outcome: Decimal) -> Decimal:
    """The standard Brier score: (probability - outcome)^2. Lower is
    better - 0 is a perfect prediction, 1 the worst possible one.
    """
    return (probability - outcome) ** 2


def run_brier_updates() -> BrierUpdateSummary:
    """Backfills brier_raw for every resolved signal_clv row that doesn't
    have one yet - entry_price and price_at_resolution (the outcome) are
    both already fixed by the time a row reaches this state, so this is a
    one-time fill per row, safe to run as often as needed (a no-op once
    caught up).
    """
    with db_session() as session:
        rows = (
            session.execute(
                select(SignalCLV).where(
                    SignalCLV.price_at_resolution.isnot(None), SignalCLV.brier_raw.is_(None)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.brier_raw = brier_score(row.entry_price, row.price_at_resolution)

    summary = BrierUpdateSummary(backfilled=len(rows))
    logger.info("brier: backfilled=%d", summary.backfilled)
    return summary


async def run_brier_job() -> None:
    """Scheduler entrypoint - same convention as every other Phase 5/6
    periodic job. No settings needed: backfilling brier_raw is unconditional
    (docs/PHASE6_DESIGN.md workstream 4 asks for no new tunables here).
    """
    await asyncio.to_thread(run_brier_updates)


def _bucket_label(key: BucketKey, config: CalibrationConfig) -> str:
    def band_str(idx: int, bands: tuple[tuple[Decimal, Decimal], ...]) -> str:
        lo, hi = bands[idx]
        return f"{lo}+" if idx == len(bands) - 1 else f"{lo}-{hi}"

    cluster_idx, score_idx, price_idx = key
    return (
        f"clusters {band_str(cluster_idx, config.cluster_bands)}, "
        f"score {band_str(score_idx, config.score_bands)}, "
        f"price {band_str(price_idx, config.price_bands)}"
    )


def aggregate_brier_raw_overall(session: Session) -> BrierAggregate:
    values = (
        session.execute(select(SignalCLV.brier_raw).where(SignalCLV.brier_raw.isnot(None)))
        .scalars()
        .all()
    )
    if not values:
        return BrierAggregate(label="overall (raw)", n=0, avg_brier=None)
    return BrierAggregate(
        label="overall (raw)",
        n=len(values),
        avg_brier=sum(values, Decimal(0)) / Decimal(len(values)),
    )


def aggregate_brier_raw_by_bucket(
    session: Session, calibration_config: CalibrationConfig
) -> list[BrierAggregate]:
    rows = session.execute(
        select(
            Signal.distinct_traders,
            Signal.weighted_score,
            Signal.average_entry_price,
            SignalCLV.brier_raw,
        )
        .join(SignalCLV, SignalCLV.signal_id == Signal.id)
        .where(SignalCLV.brier_raw.isnot(None))
    ).all()

    values_by_bucket: dict[BucketKey, list[Decimal]] = {}
    for cluster_count, weighted_score, entry_price, brier in rows:
        key = bucket_key(cluster_count, weighted_score, entry_price, calibration_config)
        if key is None:
            continue
        values_by_bucket.setdefault(key, []).append(brier)

    return [
        BrierAggregate(
            label=_bucket_label(key, calibration_config),
            n=len(values),
            avg_brier=sum(values, Decimal(0)) / Decimal(len(values)),
        )
        for key, values in values_by_bucket.items()
    ]


def aggregate_brier_calibrated_overall(
    session: Session, calibration_config: CalibrationConfig, now: datetime | None = None
) -> BrierAggregate:
    """See the module docstring - computed fresh, never stored. Only counts
    resolved signals whose bucket currently clears calibration_min_samples_
    per_bucket (get_p_hat returns None otherwise, same "not trusted" gate
    Kelly itself respects); a resolved signal outside every trusted bucket
    contributes nothing here rather than being scored against a guess.
    """
    now = now or datetime.now(UTC)
    records = load_resolved_trade_records(session, calibration_config.window_days, now)
    bucket_stats = compute_bucket_stats(records, calibration_config)

    rows = session.execute(
        select(
            Signal.distinct_traders,
            Signal.weighted_score,
            Signal.average_entry_price,
            SignalCLV.price_at_resolution,
        )
        .join(SignalCLV, SignalCLV.signal_id == Signal.id)
        .where(SignalCLV.price_at_resolution.isnot(None))
    ).all()

    values: list[Decimal] = []
    for cluster_count, weighted_score, entry_price, outcome in rows:
        result = get_p_hat(
            bucket_stats,
            SignalFeatures(
                cluster_count=cluster_count,
                weighted_score=weighted_score,
                average_entry_price=entry_price,
            ),
            calibration_config,
        )
        if result is None:
            continue
        values.append(brier_score(result.p_hat, outcome))

    if not values:
        return BrierAggregate(label="overall (calibrated)", n=0, avg_brier=None)
    return BrierAggregate(
        label="overall (calibrated)",
        n=len(values),
        avg_brier=sum(values, Decimal(0)) / Decimal(len(values)),
    )


def aggregate_brier_raw_trend(session: Session, bucket_days: int = 7) -> list[BrierAggregate]:
    """Average raw Brier score per bucket_days-wide window, oldest first -
    the trend line docs/PHASE6_DESIGN.md workstream 4 wants to track
    ("is calibration improving as other workstreams land"). Windowed by
    computed_at (last-updated) since signal_clv has no separate
    resolved_at column - the closest available proxy for "around when this
    resolution became known."
    """
    rows = session.execute(
        select(SignalCLV.computed_at, SignalCLV.brier_raw).where(SignalCLV.brier_raw.isnot(None))
    ).all()
    if not rows:
        return []

    epoch = min(row.computed_at for row in rows)
    buckets: dict[int, list[Decimal]] = {}
    for computed_at, brier in rows:
        idx = (computed_at - epoch).days // bucket_days
        buckets.setdefault(idx, []).append(brier)

    return [
        BrierAggregate(
            label=f"window {idx}",
            n=len(values),
            avg_brier=sum(values, Decimal(0)) / Decimal(len(values)),
        )
        for idx, values in sorted(buckets.items())
    ]

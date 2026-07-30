"""Closing line value (CLV) - docs/PHASE6_DESIGN.md workstream 2.

For every signal, the favourable price movement between our simulated
post-delay entry and a later reference point (T+CLV_HORIZON_HOURS, and at
resolution once the market has closed) - the fastest honest edge signal
available, since it doesn't require any market to resolve. Split the same
way every other orchestration module in this project is: pure math
(clv_value, _band_index) separate from the DB-touching update/aggregation
functions, so the formula itself is trivially unit-testable.

Signal.side is always "BUY" today, so a higher later price is unambiguously
favourable - clv_value is a plain subtraction, never a percentage or a
signed-by-side calculation. If a short/sell-side consensus concept is ever
added, this function is exactly where that would need to branch.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import Market, PriceSnapshot, Signal, SignalCLV, Wallet, WalletCluster
from app.db.session import db_session
from app.optimization.brier import run_brier_updates
from app.optimization.crowdedness import crowdedness_by_address
from app.paper.resolution import ResolutionOutcome, detect_resolution

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalClvConfig:
    entry_delay_seconds: int
    horizon_hours: int
    resolution_price_threshold: Decimal


@dataclass(frozen=True)
class ClvUpdateSummary:
    created: int
    horizon_filled: int
    resolution_filled: int


@dataclass(frozen=True)
class ClvAggregate:
    label: str
    n: int
    avg_clv: Decimal | None


def clv_value(entry_price: Decimal, reference_price: Decimal) -> Decimal:
    """Favourable price movement since entry - reference_price higher than
    entry_price is positive (good), lower is negative (bad). See the module
    docstring for why this is a plain subtraction, not side-aware.
    """
    return reference_price - entry_price


def resolve_horizon_clv(entry_price: Decimal, horizon_price: Decimal | None) -> Decimal | None:
    """Pure decision: None means "not enough price history yet" - either
    the horizon hasn't arrived, or it has but no snapshot exists there yet.
    Never an error either way - the caller (_fill_horizon) retries next run.
    """
    if horizon_price is None:
        return None
    return clv_value(entry_price, horizon_price)


@dataclass(frozen=True)
class ResolutionClv:
    price_at_resolution: Decimal
    clv_resolution: Decimal


def resolve_resolution_clv(
    entry_price: Decimal, resolution: ResolutionOutcome
) -> ResolutionClv | None:
    """Pure decision: None means not resolved yet, or resolved ambiguously -
    never guessed (same convention app/paper/engine.py's detect_resolution
    already established for paper trading).
    """
    if resolution == ResolutionOutcome.WON:
        price = Decimal("1.0")
    elif resolution == ResolutionOutcome.LOST:
        price = Decimal("0.0")
    else:
        return None
    return ResolutionClv(price_at_resolution=price, clv_resolution=clv_value(entry_price, price))


def _band_index(value: Decimal, bands: list[tuple[Decimal, Decimal]]) -> int | None:
    """Half-open [min, max) bands, same convention as app/risk/sizing_tiered.py's
    fraction_for() and app/risk/calibration.py's _band_index(): the index of
    the band value falls into, the last band's index if value is at/above
    its max, or None if value is below the lowest band's min.
    """
    if value < bands[0][0]:
        return None
    for i, (lo, hi) in enumerate(bands):
        if lo <= value < hi:
            return i
    return len(bands) - 1


def _nearest_price_at_or_after(
    session: Session, condition_id: str, asset: str, at: datetime
) -> Decimal | None:
    return session.execute(
        select(PriceSnapshot.price)
        .where(
            PriceSnapshot.condition_id == condition_id,
            PriceSnapshot.asset == asset,
            PriceSnapshot.captured_at >= at,
        )
        .order_by(PriceSnapshot.captured_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_price(session: Session, condition_id: str, asset: str) -> Decimal | None:
    return session.execute(
        select(PriceSnapshot.price)
        .where(PriceSnapshot.condition_id == condition_id, PriceSnapshot.asset == asset)
        .order_by(PriceSnapshot.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _resolve_entry_price(session: Session, signal: Signal, config: SignalClvConfig) -> Decimal:
    """The simulated-delay lookup (docs/PHASE6_DESIGN.md flag 6) - a
    snapshot at/after created_at + entry_delay_seconds, falling back to
    average_entry_price if none exists yet (mirroring app/paper/fills.py's
    own fallback for a missing delayed snapshot, same reasoning: an honest
    "we don't have a better number yet" answer, not a stall). Computed once,
    at row-creation time, and never revisited - same as that fallback.
    """
    delay_at = signal.created_at + timedelta(seconds=config.entry_delay_seconds)
    snapshot_price = _nearest_price_at_or_after(
        session, signal.condition_id, signal.asset, delay_at
    )
    if snapshot_price is not None:
        return snapshot_price
    return signal.average_entry_price


def _fill_horizon(
    session: Session, row: SignalCLV, signal: Signal, config: SignalClvConfig, now: datetime
) -> bool:
    """Returns True if price_at_horizon/clv_horizon were just filled in.
    False (never an exception) means either the horizon hasn't been reached
    yet, or it has but no price snapshot exists there yet - both are honest
    "nothing to report yet" states, retried on the next run.
    """
    if row.price_at_horizon is not None:
        return False
    horizon_at = signal.created_at + timedelta(hours=config.horizon_hours)
    if now < horizon_at:
        return False
    price = _nearest_price_at_or_after(session, signal.condition_id, signal.asset, horizon_at)
    clv = resolve_horizon_clv(row.entry_price, price)
    if clv is None:
        return False
    row.price_at_horizon = price
    row.clv_horizon = clv
    return True


def _fill_resolution(
    session: Session, row: SignalCLV, signal: Signal, market: Market | None, config: SignalClvConfig
) -> bool:
    """Same shape as _fill_horizon - False means not resolved yet, or
    resolved ambiguously (never guessed, reusing app/paper/engine.py's own
    detect_resolution convention for consistency with paper trading).
    """
    if row.price_at_resolution is not None:
        return False
    if market is None or not market.closed:
        return False
    latest = _latest_price(session, signal.condition_id, signal.asset)
    resolution = detect_resolution(market.closed, latest, config.resolution_price_threshold)
    result = resolve_resolution_clv(row.entry_price, resolution)
    if result is None:
        return False
    row.price_at_resolution = result.price_at_resolution
    row.clv_resolution = result.clv_resolution
    return True


def run_clv_updates(settings: Settings) -> ClvUpdateSummary:
    """The full update pass: create a signal_clv row for any signal that
    doesn't have one yet (entry_price always populated), then try to fill
    in price_at_horizon/price_at_resolution for every row still missing
    either one. Safe to run as often as needed - every step is a no-op
    when there's nothing new to do.
    """
    config = SignalClvConfig(
        entry_delay_seconds=settings.clv_entry_delay_seconds,
        horizon_hours=settings.clv_horizon_hours,
        resolution_price_threshold=settings.paper_resolution_price_threshold,
    )
    now = datetime.now(UTC)
    created = horizon_filled = resolution_filled = 0

    with db_session() as session:
        existing_signal_ids = set(session.execute(select(SignalCLV.signal_id)).scalars().all())
        query = select(Signal)
        if existing_signal_ids:
            query = query.where(Signal.id.notin_(existing_signal_ids))
        new_signals = session.execute(query).scalars().all()
        for signal in new_signals:
            entry_price = _resolve_entry_price(session, signal, config)
            session.add(SignalCLV(signal_id=signal.id, entry_price=entry_price, computed_at=now))
            created += 1

        pending = (
            session.execute(
                select(SignalCLV).where(
                    or_(
                        SignalCLV.price_at_horizon.is_(None),
                        SignalCLV.price_at_resolution.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if pending:
            signal_ids = {row.signal_id for row in pending}
            signals_by_id = {
                s.id: s
                for s in session.execute(select(Signal).where(Signal.id.in_(signal_ids))).scalars()
            }
            condition_ids = {s.condition_id for s in signals_by_id.values()}
            markets_by_condition = {
                m.condition_id: m
                for m in session.execute(
                    select(Market).where(Market.condition_id.in_(condition_ids))
                ).scalars()
            }

            for row in pending:
                signal = signals_by_id.get(row.signal_id)
                if signal is None:
                    continue
                if _fill_horizon(session, row, signal, config, now):
                    horizon_filled += 1
                    row.computed_at = now
                market = markets_by_condition.get(signal.condition_id)
                if _fill_resolution(session, row, signal, market, config):
                    resolution_filled += 1
                    row.computed_at = now

    summary = ClvUpdateSummary(
        created=created, horizon_filled=horizon_filled, resolution_filled=resolution_filled
    )
    logger.info(
        "clv: created=%d horizon_filled=%d resolution_filled=%d",
        summary.created,
        summary.horizon_filled,
        summary.resolution_filled,
    )
    return summary


async def run_clv_job() -> None:
    """Scheduler entrypoint - same convention as every other Phase 5/6
    periodic job (fetch effective settings fresh, run the blocking DB work
    in a thread). Also runs the Brier backfill (docs/PHASE6_DESIGN.md
    workstream 4) right after - brier_raw only has something new to fill
    once this same pass's resolution fill has run, so there's no value in
    a separate schedule for it.
    """
    settings = get_effective_settings()
    await asyncio.to_thread(run_clv_updates, settings)
    await asyncio.to_thread(run_brier_updates)


# --- Aggregation - overall, per cluster, per weighted_score band ---


def _clv_column(kind: str):
    if kind not in ("horizon", "resolution"):
        raise ValueError(f"kind must be 'horizon' or 'resolution', got {kind!r}")
    return SignalCLV.clv_horizon if kind == "horizon" else SignalCLV.clv_resolution


def aggregate_clv_overall(session: Session, kind: str = "horizon") -> ClvAggregate:
    column = _clv_column(kind)
    values = session.execute(select(column).where(column.isnot(None))).scalars().all()
    if not values:
        return ClvAggregate(label="overall", n=0, avg_clv=None)
    return ClvAggregate(
        label="overall", n=len(values), avg_clv=sum(values, Decimal(0)) / Decimal(len(values))
    )


def address_to_cluster_map(session: Session) -> dict[str, str]:
    """Public (unlike most of this module's helpers) - app/optimization/
    bandit.py also needs this to attribute a signal's CLV to every cluster
    among its contributors.
    """
    rows = session.execute(
        select(Wallet.address, WalletCluster.cluster_id).join(
            WalletCluster, WalletCluster.wallet_id == Wallet.id
        )
    ).all()
    return dict(rows)


def aggregate_clv_by_cluster(session: Session, kind: str = "horizon") -> list[ClvAggregate]:
    """One data point per (signal, cluster among its contributors) - a
    signal co-signed by two independent clusters counts toward both. A
    contributor address absent from wallet_clusters is its own singleton
    (docs/PHASE6_DESIGN.md flag 3), same fallback app/consensus/engine.py's
    _weighted_score() uses.
    """
    column = _clv_column(kind)
    rows = session.execute(
        select(Signal.contributors, column)
        .join(SignalCLV, SignalCLV.signal_id == Signal.id)
        .where(column.isnot(None))
    ).all()
    cluster_of = address_to_cluster_map(session)

    values_by_cluster: dict[str, list[Decimal]] = {}
    for contributors, value in rows:
        clusters_seen = {cluster_of.get(c["address"], c["address"]) for c in contributors}
        for cluster_id in clusters_seen:
            values_by_cluster.setdefault(cluster_id, []).append(value)

    return [
        ClvAggregate(
            label=cluster_id, n=len(values), avg_clv=sum(values, Decimal(0)) / Decimal(len(values))
        )
        for cluster_id, values in values_by_cluster.items()
    ]


def aggregate_clv_by_score_band(
    session: Session, bands: list[tuple[Decimal, Decimal]], kind: str = "horizon"
) -> list[ClvAggregate]:
    column = _clv_column(kind)
    rows = session.execute(
        select(Signal.weighted_score, column)
        .join(SignalCLV, SignalCLV.signal_id == Signal.id)
        .where(column.isnot(None))
    ).all()

    values_by_band: dict[int, list[Decimal]] = {}
    for weighted_score, value in rows:
        idx = _band_index(weighted_score, bands)
        if idx is None:
            continue
        values_by_band.setdefault(idx, []).append(value)

    results = []
    for idx in sorted(values_by_band):
        lo, hi = bands[idx]
        label = f"{lo}-{hi}" if idx != len(bands) - 1 else f"{lo}+"
        values = values_by_band[idx]
        results.append(
            ClvAggregate(
                label=label, n=len(values), avg_clv=sum(values, Decimal(0)) / Decimal(len(values))
            )
        )
    return results


def aggregate_clv_trend(
    session: Session, kind: str = "horizon", bucket_days: int = 7
) -> list[ClvAggregate]:
    """Average CLV per bucket_days-wide window, oldest first - same
    windowing convention as app/optimization/brier.py's
    aggregate_brier_raw_trend (computed_at is the closest proxy signal_clv
    has to "when this became known," there being no separate resolved_at
    column).
    """
    column = _clv_column(kind)
    rows = session.execute(select(SignalCLV.computed_at, column).where(column.isnot(None))).all()
    if not rows:
        return []

    epoch = min(row.computed_at for row in rows)
    buckets: dict[int, list[Decimal]] = {}
    for computed_at, value in rows:
        idx = (computed_at - epoch).days // bucket_days
        buckets.setdefault(idx, []).append(value)

    return [
        ClvAggregate(
            label=f"window {idx}",
            n=len(values),
            avg_clv=sum(values, Decimal(0)) / Decimal(len(values)),
        )
        for idx, values in sorted(buckets.items())
    ]


_CROWDEDNESS_TIER_LABELS = ("low", "medium", "high")


def average_contributor_crowdedness(
    contributors: list[dict], crowdedness_of: dict[str, Decimal]
) -> Decimal:
    """The average crowdedness across a signal's contributing wallets -
    pure, so the bucketing this feeds (aggregate_clv_by_crowdedness_tier)
    stays testable without a database. A contributor absent from
    crowdedness_of (never computed yet, or not a tracked wallet) defaults
    to 0 crowdedness - same "no penalty without evidence" convention
    hidden_alpha_weighted_score already uses, not a reason to drop the
    signal from the comparison. Public: contributors is never empty by the
    time a signal exists (evaluate_group requires at least
    consensus_min_traders), but callers should still guard the empty case
    rather than divide by zero.
    """
    values = [crowdedness_of.get(c["address"], Decimal(0)) for c in contributors]
    return sum(values, Decimal(0)) / Decimal(len(values))


def aggregate_clv_by_crowdedness_tier(
    session: Session, bands: list[tuple[Decimal, Decimal]], kind: str = "horizon"
) -> list[ClvAggregate]:
    """Buckets signals by the AVERAGE crowdedness of their contributing
    wallets into low/medium/high tiers - docs/PHASE6_DESIGN.md workstream 7's
    forward validation. This is the prospective test the workstream design
    replaces the blueprint's impossible years-of-history backtest with: the
    blueprint's central prediction is that LOW-crowdedness signals should
    show BETTER CLV than HIGH-crowdedness ones - the direct, honest test of
    whether the whole crowdedness idea earns its place.
    """
    column = _clv_column(kind)
    rows = session.execute(
        select(Signal.contributors, column)
        .join(SignalCLV, SignalCLV.signal_id == Signal.id)
        .where(column.isnot(None))
    ).all()
    crowdedness_of = crowdedness_by_address(session)

    values_by_band: dict[int, list[Decimal]] = {}
    for contributors, value in rows:
        if not contributors:
            continue
        avg_crowdedness = average_contributor_crowdedness(contributors, crowdedness_of)
        idx = _band_index(avg_crowdedness, bands)
        if idx is None:
            continue
        values_by_band.setdefault(idx, []).append(value)

    results = []
    for idx in sorted(values_by_band):
        label = (
            _CROWDEDNESS_TIER_LABELS[idx] if idx < len(_CROWDEDNESS_TIER_LABELS) else f"tier{idx}"
        )
        values = values_by_band[idx]
        results.append(
            ClvAggregate(
                label=label, n=len(values), avg_clv=sum(values, Decimal(0)) / Decimal(len(values))
            )
        )
    return results


def aggregate_clv_by_category(session: Session, kind: str = "horizon") -> list[ClvAggregate]:
    """Deferred - docs/PHASE6_DESIGN.md flag 7. `Market` has no category/tag
    column, and none is confirmed in Gamma's documented `/markets` response
    (checked against docs/API_REFERENCE.md and app/collectors/schemas.py's
    GammaMarket field list - neither shows one). Raises rather than
    silently returning an empty result, so "not implemented" can never be
    misread as "no signals in any category."
    """
    raise NotImplementedError(
        "Per-market-category CLV aggregation is deferred: Market has no "
        "category/tag column, and none is confirmed in Gamma's documented "
        "/markets response (docs/PHASE6_DESIGN.md flag 7). Add a category "
        "column during collection first, if/when Gamma is confirmed to "
        "expose one."
    )

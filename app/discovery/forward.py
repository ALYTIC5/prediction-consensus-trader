"""Niche Stage 2: forward tracking for NicheTraderPipeline CANDIDATE/
WATCHLIST (wallet, niche) pairs - mirrors app/scout/forward.py's shape
exactly (same clv_value()/resolve_horizon_clv()/resolve_resolution_clv()
pure formulas, same window-closing mechanism, same CI-lower-bound
promotion criterion), with one addition: every OPENED PositionHistory event
is filtered to markets that classify to the SAME niche as the pipeline
row, via discovery_market_niches - "swisstony's MMA picks" is the unit
being verified, never swisstony's positions in any other market.

A niche-candidate only accumulates PositionHistory at all once
Wallet.niche_tracked flips True (app/discovery/promotion.py), which is
what makes app/collectors/positions.py start polling it - forward-tracking
here is therefore necessarily silent (recorded=0) for a wallet in its very
first cycle after promotion, until the next positions sweep runs.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.db.models import (
    DiscoveryMarketNiche,
    Market,
    NicheForwardTrade,
    NicheTraderPipeline,
    NicheTraderPipelineStage,
    NicheValidationWindow,
    PositionHistory,
    PriceSnapshot,
)
from app.db.session import db_session
from app.optimization.clv import resolve_horizon_clv, resolve_resolution_clv
from app.paper.resolution import detect_resolution
from app.scout.forward import (
    ForwardTradeRecord,
    WindowVerdict,
    clv_window_stats,
    evaluate_window,
    window_ready_to_close,
)

logger = logging.getLogger(__name__)

_CONFIDENCE_Z = Decimal("1.96")


@dataclass(frozen=True)
class DiscoveryForwardConfig:
    clv_horizon_hours: int
    validation_days: int
    min_forward_trades: int
    validation_confirmations: int
    resolution_price_threshold: Decimal
    clv_fill_max_per_cycle: int
    confidence_z: Decimal = _CONFIDENCE_Z

    @classmethod
    def from_settings(cls, settings: Settings) -> "DiscoveryForwardConfig":
        return cls(
            clv_horizon_hours=settings.discovery_clv_horizon_hours,
            validation_days=settings.discovery_validation_days,
            min_forward_trades=settings.discovery_min_forward_trades,
            validation_confirmations=settings.discovery_validation_confirmations,
            clv_fill_max_per_cycle=settings.discovery_clv_fill_max_per_cycle,
            resolution_price_threshold=settings.paper_resolution_price_threshold,
        )


def _entry_price(row: PositionHistory) -> Decimal:
    if row.avg_price and row.avg_price > 0:
        return row.avg_price
    return row.cur_price


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


def candidate_since(pipeline_row: NicheTraderPipeline) -> datetime | None:
    raw = (pipeline_row.metrics or {}).get("candidate_since")
    return datetime.fromisoformat(raw) if raw else None


def current_window_start(
    session: Session, wallet_id: int, niche: str, pipeline_row: NicheTraderPipeline
) -> datetime | None:
    last_window_end = session.execute(
        select(NicheValidationWindow.window_ended_at)
        .where(NicheValidationWindow.wallet_id == wallet_id, NicheValidationWindow.niche == niche)
        .order_by(NicheValidationWindow.window_ended_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last_window_end is not None:
        return last_window_end
    return candidate_since(pipeline_row)


def _record_new_forward_trades(session: Session, now: datetime) -> int:
    """Every OPENED, non-bootstrap event for a CANDIDATE/WATCHLIST
    (wallet, niche) pair, whose market's niche matches that pair's niche,
    not already recorded - dedup by position_history_id, floored by
    candidate_since.
    """
    tracked = (
        session.execute(
            select(NicheTraderPipeline).where(
                NicheTraderPipeline.stage.in_(
                    [NicheTraderPipelineStage.CANDIDATE, NicheTraderPipelineStage.WATCHLIST]
                )
            )
        )
        .scalars()
        .all()
    )
    if not tracked:
        return 0

    wallet_ids = list({row.wallet_id for row in tracked})
    since_by_key = {(row.wallet_id, row.niche): candidate_since(row) for row in tracked}
    niche_by_key = {(row.wallet_id, row.niche) for row in tracked}

    already_tracked_ids = set(
        session.execute(select(NicheForwardTrade.position_history_id)).scalars()
    )

    candidates = session.execute(
        select(PositionHistory, DiscoveryMarketNiche.niche)
        .join(
            DiscoveryMarketNiche,
            DiscoveryMarketNiche.condition_id == PositionHistory.condition_id,
        )
        .where(
            PositionHistory.wallet_id.in_(wallet_ids),
            PositionHistory.event_type == "OPENED",
            PositionHistory.is_bootstrap.is_(False),
        )
    ).all()

    created = 0
    for row, niche in candidates:
        key = (row.wallet_id, niche)
        if key not in niche_by_key or row.id in already_tracked_ids:
            continue
        since = since_by_key.get(key)
        if since is None or row.detected_at < since:
            continue
        session.add(
            NicheForwardTrade(
                wallet_id=row.wallet_id,
                niche=niche,
                position_history_id=row.id,
                condition_id=row.condition_id,
                asset=row.asset,
                entry_price=_entry_price(row),
                entry_at=row.detected_at,
                computed_at=now,
            )
        )
        created += 1
    return created


def _fill_forward_clv(
    session: Session, config: DiscoveryForwardConfig, now: datetime
) -> tuple[int, int]:
    horizon_filled = resolution_filled = 0
    horizon_cutoff = now - timedelta(hours=config.clv_horizon_hours)
    horizon_pending = (
        session.execute(
            select(NicheForwardTrade)
            .where(
                NicheForwardTrade.price_at_horizon.is_(None),
                NicheForwardTrade.entry_at <= horizon_cutoff,
            )
            .order_by(NicheForwardTrade.entry_at.asc())
            .limit(config.clv_fill_max_per_cycle)
        )
        .scalars()
        .all()
    )
    for row in horizon_pending:
        horizon_at = row.entry_at + timedelta(hours=config.clv_horizon_hours)
        price = _nearest_price_at_or_after(session, row.condition_id, row.asset, horizon_at)
        clv = resolve_horizon_clv(row.entry_price, price)
        if clv is not None:
            row.price_at_horizon = price
            row.clv_horizon = clv
            row.computed_at = now
            horizon_filled += 1

    resolution_pending = (
        session.execute(
            select(NicheForwardTrade)
            .join(Market, Market.condition_id == NicheForwardTrade.condition_id)
            .where(NicheForwardTrade.price_at_resolution.is_(None), Market.closed.is_(True))
            .limit(config.clv_fill_max_per_cycle)
        )
        .scalars()
        .all()
    )
    for row in resolution_pending:
        latest = _latest_price(session, row.condition_id, row.asset)
        resolution = detect_resolution(True, latest, config.resolution_price_threshold)
        result = resolve_resolution_clv(row.entry_price, resolution)
        if result is not None:
            row.price_at_resolution = result.price_at_resolution
            row.clv_resolution = result.clv_resolution
            row.computed_at = now
            resolution_filled += 1

    return horizon_filled, resolution_filled


def close_ready_windows(
    session: Session,
    config: DiscoveryForwardConfig,
    now: datetime,
    pipeline_by_key: dict[tuple[int, str], NicheTraderPipeline],
) -> list[NicheValidationWindow]:
    closed: list[NicheValidationWindow] = []
    for (wallet_id, niche), pipeline_row in pipeline_by_key.items():
        window_start = current_window_start(session, wallet_id, niche, pipeline_row)
        if window_start is None:
            continue

        rows = session.execute(
            select(
                NicheForwardTrade.condition_id,
                Market.event_cluster_id,
                NicheForwardTrade.clv_horizon,
            )
            .outerjoin(Market, Market.condition_id == NicheForwardTrade.condition_id)
            .where(
                NicheForwardTrade.wallet_id == wallet_id,
                NicheForwardTrade.niche == niche,
                NicheForwardTrade.entry_at >= window_start,
                NicheForwardTrade.entry_at < now,
                NicheForwardTrade.clv_horizon.is_not(None),
            )
        ).all()
        records = [
            ForwardTradeRecord(
                condition_id=row.condition_id,
                event_cluster_id=row.event_cluster_id,
                clv=row.clv_horizon,
            )
            for row in rows
        ]
        stats = clv_window_stats(records, config.confidence_z)

        if not window_ready_to_close(
            window_start, now, config.validation_days, stats.effective_n, config.min_forward_trades
        ):
            continue

        verdict = evaluate_window(stats)
        row = NicheValidationWindow(
            wallet_id=wallet_id,
            niche=niche,
            window_started_at=window_start,
            window_ended_at=now,
            forward_trade_count=stats.n,
            effective_forward_trade_count=stats.effective_n,
            avg_forward_clv=stats.avg_clv,
            ci_low=stats.ci_low,
            ci_high=stats.ci_high,
            passed=(verdict == WindowVerdict.PASS),
        )
        session.add(row)
        closed.append(row)
    return closed


def _transition(
    session: Session,
    pipeline_row: NicheTraderPipeline,
    new_stage: str,
    now: datetime,
    **extra_metrics: object,
) -> None:
    pipeline_row.stage = new_stage
    pipeline_row.entered_stage_at = now
    pipeline_row.updated_at = now
    pipeline_row.metrics = {**(pipeline_row.metrics or {}), **extra_metrics}


def _count_passing_windows(session: Session, wallet_id: int, niche: str) -> int:
    return len(
        list(
            session.execute(
                select(NicheValidationWindow.id).where(
                    NicheValidationWindow.wallet_id == wallet_id,
                    NicheValidationWindow.niche == niche,
                    NicheValidationWindow.passed.is_(True),
                )
            ).scalars()
        )
    )


@dataclass(frozen=True)
class DiscoveryForwardSummary:
    recorded: int
    horizon_filled: int
    resolution_filled: int
    promoted: int
    rejected: int


def run_discovery_forward_tracking(settings: Settings) -> DiscoveryForwardSummary:
    config = DiscoveryForwardConfig.from_settings(settings)
    now = datetime.now(UTC)

    with db_session() as session:
        recorded = _record_new_forward_trades(session, now)
        horizon_filled, resolution_filled = _fill_forward_clv(session, config, now)

        tracked = (
            session.execute(
                select(NicheTraderPipeline).where(
                    NicheTraderPipeline.stage.in_(
                        [NicheTraderPipelineStage.CANDIDATE, NicheTraderPipelineStage.WATCHLIST]
                    )
                )
            )
            .scalars()
            .all()
        )
        pipeline_by_key = {(row.wallet_id, row.niche): row for row in tracked}
        closed_windows = close_ready_windows(session, config, now, pipeline_by_key)

        promoted = rejected = 0
        for window in closed_windows:
            pipeline_row = pipeline_by_key[(window.wallet_id, window.niche)]
            window_metrics = {
                "last_window_avg_clv": str(window.avg_forward_clv),
                "last_window_ci_low": str(window.ci_low),
                "last_window_ci_high": str(window.ci_high),
                "last_window_n": window.forward_trade_count,
            }

            if not window.passed:
                _transition(
                    session, pipeline_row, NicheTraderPipelineStage.REJECTED, now, **window_metrics
                )
                rejected += 1
                logger.info(
                    "discovery: wallet %d niche=%s -> REJECTED avg_clv=%.4f n=%d",
                    window.wallet_id,
                    window.niche,
                    window.avg_forward_clv,
                    window.forward_trade_count,
                )
                continue

            if pipeline_row.stage == NicheTraderPipelineStage.CANDIDATE:
                _transition(
                    session, pipeline_row, NicheTraderPipelineStage.WATCHLIST, now, **window_metrics
                )
                promoted += 1
                logger.info(
                    "discovery: wallet %d niche=%s CANDIDATE -> WATCHLIST avg_clv=%.4f",
                    window.wallet_id,
                    window.niche,
                    window.avg_forward_clv,
                )
            else:
                confirmations = _count_passing_windows(session, window.wallet_id, window.niche)
                if confirmations >= config.validation_confirmations:
                    _transition(
                        session,
                        pipeline_row,
                        NicheTraderPipelineStage.VALIDATED,
                        now,
                        **window_metrics,
                    )
                    promoted += 1
                    logger.info(
                        "discovery: wallet %d niche=%s WATCHLIST -> VALIDATED "
                        "(%d/%d confirmations)",
                        window.wallet_id,
                        window.niche,
                        confirmations,
                        config.validation_confirmations,
                    )
                else:
                    pipeline_row.metrics = {**(pipeline_row.metrics or {}), **window_metrics}
                    pipeline_row.updated_at = now

    summary = DiscoveryForwardSummary(
        recorded=recorded,
        horizon_filled=horizon_filled,
        resolution_filled=resolution_filled,
        promoted=promoted,
        rejected=rejected,
    )
    logger.info(
        "discovery: forward_tracking recorded=%d horizon_filled=%d resolution_filled=%d "
        "promoted=%d rejected=%d",
        summary.recorded,
        summary.horizon_filled,
        summary.resolution_filled,
        summary.promoted,
        summary.rejected,
    )
    return summary

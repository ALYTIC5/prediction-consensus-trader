"""Paper trading orchestration: DB in, the pure fill/sizing models, DB out.

See docs/PHASE4_DESIGN.md sections 4 and 5. Mirrors app/signals/generator.py's
split - this module does the loading, DB writes, and logging; app/paper/
fills.py and app/paper/sizing.py do the pure evaluation. The decision-point
functions below (passes_entry_filters, check_exit, detect_resolution,
select_undecided_signals) are pure too, kept in this file because they're
the orchestration's own decision logic, not reusable evaluation like the
fill/sizing modules - but they take plain values, not a live session, so
they're fully unit-testable without a database.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import (
    Market,
    PaperPortfolio,
    PaperTrade,
    PaperTradeStatus,
    PriceSnapshot,
    RiskEvent,
    Signal,
    SignalStatus,
)
from app.db.session import db_session
from app.optimization.bandit import effective_weighted_score, load_multipliers
from app.optimization.clv import address_to_cluster_map
from app.paper.fills import FillConfig, FillRequest, MarketSnapshot, compute_fill
from app.paper.resolution import ResolutionOutcome, detect_resolution
from app.paper.sizing import SizingConfig, SizingRequest, size_position
from app.risk.calibration import (
    BucketStats,
    CalibrationConfig,
    SignalFeatures,
    compute_bucket_stats,
    get_p_hat,
    load_resolved_trade_records,
)
from app.risk.manager import RiskManager
from app.risk.rules import (
    OpenPosition,
    PortfolioState,
    ProposedTrade,
    RiskAction,
    RiskDecision,
    RiskRuleConfig,
)
from app.risk.sizing_kelly import KellySizingConfig, KellySizingRequest, size_kelly_position
from app.risk.sizing_tiered import TieredSizingConfig, TieredSizingRequest, size_tiered_position

logger = logging.getLogger(__name__)


class ExitReason(StrEnum):
    """Why an OPEN trade was closed - see docs/PHASE4_DESIGN.md section 4c
    for the fixed evaluation order (first match wins).
    """

    MARKET_RESOLVED = "market_resolved"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    SIGNAL_EXPIRED = "signal_expired"


class RejectionStage(StrEnum):
    """Stage at which a paper trade candidate was rejected, stored in
    paper_trades.rejection_reason so the dashboard can group rejections
    by stage in per-portfolio signal funnels.
    """

    ENTRY_FILTER = "ENTRY_FILTER"
    SIZING = "SIZING"
    FILL = "FILL"
    RISK = "RISK"


# --- Pure decision points - no session, no I/O, fully unit-testable ---


@dataclass(frozen=True)
class EntryFilters:
    min_traders: int
    min_weighted_score: Decimal
    min_combined_value_usd: Decimal
    min_liquidity_usd: Decimal
    max_spread: Decimal


@dataclass(frozen=True)
class SignalMetrics:
    """The subset of a signals row entry filters actually look at."""

    distinct_traders: int
    weighted_score: Decimal
    combined_entry_value: Decimal


@dataclass(frozen=True)
class MarketMetrics:
    """The subset of a markets row entry filters and resolution actually
    look at - liquidity/spread re-checked at evaluation time, not frozen at
    signal-creation time (they may have drifted since).
    """

    liquidity: Decimal | None
    spread: Decimal | None
    closed: bool


@dataclass(frozen=True)
class ExitConfig:
    take_profit_pct: Decimal
    stop_loss_pct: Decimal
    exit_on_signal_expiry_hours: int


def passes_entry_filters(
    signal: SignalMetrics, market: MarketMetrics, filters: EntryFilters
) -> tuple[bool, str | None]:
    """Returns (passed, reason). When passed is False, reason is the name of
    the filter that rejected the signal so it can be stored in the MISSED
    row's exit_reason. market.closed isn't checked here - that's a separate
    concern handled by the caller before this is even called.
    """
    if signal.distinct_traders < filters.min_traders:
        return False, "BELOW_MIN_TRADERS"
    if signal.weighted_score < filters.min_weighted_score:
        return False, "BELOW_MIN_SCORE"
    if signal.combined_entry_value < filters.min_combined_value_usd:
        return False, "BELOW_MIN_VALUE"
    if market.liquidity is None or market.liquidity < filters.min_liquidity_usd:
        return False, "BELOW_MIN_LIQUIDITY"
    if market.spread is None or market.spread > filters.max_spread:
        return False, "ABOVE_MAX_SPREAD"
    return True, None


def check_exit(
    entry_price: Decimal,
    current_price: Decimal | None,
    resolution: ResolutionOutcome,
    signal_created_at: datetime,
    now: datetime,
    config: ExitConfig,
) -> ExitReason | None:
    """First matching condition wins, in this fixed order - resolution
    first (it's ground truth the instant it's known, and must never be
    miscategorized as a take-profit/stop-loss/expiry exit instead), then
    take-profit, then stop-loss, then signal expiry. None means stay open.
    """
    if resolution in (ResolutionOutcome.WON, ResolutionOutcome.LOST):
        return ExitReason.MARKET_RESOLVED

    if current_price is not None:
        if current_price >= entry_price * (1 + config.take_profit_pct):
            return ExitReason.TAKE_PROFIT
        if current_price <= entry_price * (1 - config.stop_loss_pct):
            return ExitReason.STOP_LOSS

    if now - signal_created_at > timedelta(hours=config.exit_on_signal_expiry_hours):
        return ExitReason.SIGNAL_EXPIRED

    return None


def select_undecided_signals(
    candidate_signal_ids: list[int], already_decided_signal_ids: set[int]
) -> list[int]:
    """The mechanism that makes entry idempotent (docs/PHASE4_DESIGN.md
    flag 2): exactly one decision per (portfolio, signal) ever. A signal
    with an existing paper_trades row - filled, missed, or skipped - is
    never reconsidered, no matter how many more cycles run.
    """
    return [sid for sid in candidate_signal_ids if sid not in already_decided_signal_ids]


def _get_param(params: dict, key: str, default: object) -> object:
    """A portfolio's params JSONB stores overrides as JSON-native types
    (numbers end up as strings where the default is a Decimal, since JSON
    has no Decimal type - see scripts/seed_portfolios.py) - coerce to match
    the default's type rather than trust the JSON type directly.
    """
    if key not in params:
        return default
    raw = params[key]
    if isinstance(default, Decimal):
        return Decimal(str(raw))
    if isinstance(default, bool):
        return bool(raw)
    if isinstance(default, int):
        return int(raw)
    return raw


@dataclass(frozen=True)
class PortfolioConfig:
    entry_filters: EntryFilters
    sizer: str
    sizing: SizingConfig
    tiered_sizing: TieredSizingConfig
    kelly_sizing: KellySizingConfig
    fill: FillConfig
    exits: ExitConfig
    risk: RiskRuleConfig
    resolution_price_threshold: Decimal
    # docs/PHASE6_DESIGN.md workstream 5 / flag 14 - "adaptive" is the only
    # seeded portfolio that sets this True.
    use_adaptive_weighting: bool


def resolve_kelly_sizing_config(params: dict, settings: Settings) -> KellySizingConfig:
    """docs/PHASE5_DESIGN.md section 5/9. max_position_pct mirrors
    RiskManager's own cap, same reasoning as resolve_tiered_sizing_config.
    """
    g = _get_param
    return KellySizingConfig(
        kelly_fraction=g(params, "risk_kelly_fraction", settings.risk_kelly_fraction),
        fee_pct=g(params, "risk_kelly_fee_pct", settings.risk_kelly_fee_pct),
        max_position_pct=g(params, "risk_max_position_pct", settings.risk_max_position_pct),
    )


def resolve_calibration_config(settings: Settings) -> CalibrationConfig:
    """Never portfolio-overridable (docs/PHASE5_DESIGN.md section 9's
    table) - calibration is a property of the shared signal-generation
    process across all portfolios' resolved trades, not a per-portfolio
    choice, so this reads straight off settings with no params argument.
    """
    return CalibrationConfig(
        cluster_bands=tuple(map(tuple, settings.calibration_cluster_bands)),
        score_bands=tuple(map(tuple, settings.calibration_score_bands)),
        price_bands=tuple(map(tuple, settings.calibration_price_bands)),
        min_samples_per_bucket=settings.calibration_min_samples_per_bucket,
        window_days=settings.calibration_window_days,
    )


def resolve_tiered_sizing_config(params: dict, settings: Settings) -> TieredSizingConfig:
    """docs/PHASE5_DESIGN.md section 3/9 - bands and the floor-vs-skip
    toggle are portfolio-overridable; max_position_pct mirrors RiskManager's
    own cap (section 9's risk_max_position_pct, flag 11) so this sizer's
    output is never misleading on its own.
    """
    g = _get_param
    return TieredSizingConfig(
        bands=tuple(
            tuple(band)
            for band in g(params, "risk_tiered_score_bands", settings.risk_tiered_score_bands)
        ),
        max_position_pct=g(params, "risk_max_position_pct", settings.risk_max_position_pct),
        floor_below_lowest_band=g(
            params,
            "risk_tiered_floor_below_lowest_band",
            settings.risk_tiered_floor_below_lowest_band,
        ),
    )


def resolve_risk_config(params: dict, settings: Settings) -> RiskRuleConfig:
    """Every Stage 1 risk threshold/toggle a portfolio's params dict can
    override, resolved against the global defaults for whatever it doesn't
    set - see docs/PHASE5_DESIGN.md section 9. emergency_stop_active is
    deliberately read straight off settings, never portfolio-overridable
    (flag 9) - settings here is expected to already be effective settings
    (get_effective_settings()), so this picks up a live dashboard toggle
    with no redeploy.
    """
    g = _get_param
    return RiskRuleConfig(
        max_position_pct=g(params, "risk_max_position_pct", settings.risk_max_position_pct),
        max_position_size_enabled=g(
            params, "risk_max_position_size_enabled", settings.risk_max_position_size_enabled
        ),
        max_exposure_pct=g(params, "risk_max_exposure_pct", settings.risk_max_exposure_pct),
        max_total_exposure_enabled=g(
            params, "risk_max_total_exposure_enabled", settings.risk_max_total_exposure_enabled
        ),
        max_market_exposure_pct=g(
            params, "risk_max_market_exposure_pct", settings.risk_max_market_exposure_pct
        ),
        max_market_exposure_enabled=g(
            params, "risk_max_market_exposure_enabled", settings.risk_max_market_exposure_enabled
        ),
        max_correlated_exposure_pct=g(
            params, "risk_max_correlated_exposure_pct", settings.risk_max_correlated_exposure_pct
        ),
        max_correlated_exposure_enabled=g(
            params,
            "risk_max_correlated_exposure_enabled",
            settings.risk_max_correlated_exposure_enabled,
        ),
        daily_stop_loss_pct=g(
            params, "risk_daily_stop_loss_pct", settings.risk_daily_stop_loss_pct
        ),
        daily_stop_loss_enabled=g(
            params, "risk_daily_stop_loss_enabled", settings.risk_daily_stop_loss_enabled
        ),
        max_open_positions=g(params, "risk_max_open_positions", settings.risk_max_open_positions),
        max_open_positions_enabled=g(
            params, "risk_max_open_positions_enabled", settings.risk_max_open_positions_enabled
        ),
        min_bankroll_pct=g(params, "risk_min_bankroll_pct", settings.risk_min_bankroll_pct),
        min_bankroll_halt_enabled=g(
            params, "risk_min_bankroll_halt_enabled", settings.risk_min_bankroll_halt_enabled
        ),
        emergency_stop_active=settings.paper_emergency_stop,
    )


def resolve_portfolio_config(params: dict, settings: Settings) -> PortfolioConfig:
    """Every paper_* setting a portfolio's params dict can override,
    resolved against the global defaults for whatever it doesn't set - see
    docs/PHASE4_DESIGN.md section 10 for which fields are portfolio-
    overridable. resolution_price_threshold is deliberately not
    overridable (same section) - it's read straight off settings.
    """
    g = _get_param
    entry_filters = EntryFilters(
        min_traders=g(params, "paper_min_traders", settings.paper_min_traders),
        min_weighted_score=g(params, "paper_min_weighted_score", settings.paper_min_weighted_score),
        min_combined_value_usd=g(
            params, "paper_min_combined_value_usd", settings.paper_min_combined_value_usd
        ),
        min_liquidity_usd=g(params, "paper_min_liquidity_usd", settings.paper_min_liquidity_usd),
        max_spread=g(params, "paper_max_spread", settings.paper_max_spread),
    )
    sizing = SizingConfig(
        sizing_rule=g(params, "paper_sizing_rule", settings.paper_sizing_rule),
        fixed_fraction_pct=g(params, "paper_fixed_fraction_pct", settings.paper_fixed_fraction_pct),
        confidence_base_fraction_pct=g(
            params,
            "paper_confidence_base_fraction_pct",
            settings.paper_confidence_base_fraction_pct,
        ),
        confidence_reference_score=g(
            params, "paper_confidence_reference_score", settings.paper_confidence_reference_score
        ),
        confidence_min_multiplier=g(
            params, "paper_confidence_min_multiplier", settings.paper_confidence_min_multiplier
        ),
        confidence_max_multiplier=g(
            params, "paper_confidence_max_multiplier", settings.paper_confidence_max_multiplier
        ),
        max_position_notional_pct=g(
            params, "paper_max_position_notional_pct", settings.paper_max_position_notional_pct
        ),
        max_total_exposure_pct=g(
            params, "paper_max_total_exposure_pct", settings.paper_max_total_exposure_pct
        ),
        min_position_notional_usd=g(
            params, "paper_min_position_notional_usd", settings.paper_min_position_notional_usd
        ),
    )
    fill = FillConfig(
        entry_delay_seconds=g(
            params, "paper_entry_delay_seconds", settings.paper_entry_delay_seconds
        ),
        slippage_k=g(params, "paper_slippage_k", settings.paper_slippage_k),
        slippage_max=g(params, "paper_slippage_max", settings.paper_slippage_max),
        no_delayed_snapshot_penalty=g(
            params, "paper_no_delayed_snapshot_penalty", settings.paper_no_delayed_snapshot_penalty
        ),
        max_entry_price_drift=g(
            params, "paper_max_entry_price_drift", settings.paper_max_entry_price_drift
        ),
    )
    exits = ExitConfig(
        take_profit_pct=g(params, "paper_take_profit_pct", settings.paper_take_profit_pct),
        stop_loss_pct=g(params, "paper_stop_loss_pct", settings.paper_stop_loss_pct),
        exit_on_signal_expiry_hours=g(
            params, "paper_exit_on_signal_expiry_hours", settings.paper_exit_on_signal_expiry_hours
        ),
    )
    risk = resolve_risk_config(params, settings)
    tiered_sizing = resolve_tiered_sizing_config(params, settings)
    kelly_sizing = resolve_kelly_sizing_config(params, settings)
    return PortfolioConfig(
        entry_filters=entry_filters,
        sizer=g(params, "paper_sizer", settings.risk_default_sizer),
        sizing=sizing,
        kelly_sizing=kelly_sizing,
        tiered_sizing=tiered_sizing,
        fill=fill,
        exits=exits,
        risk=risk,
        resolution_price_threshold=settings.paper_resolution_price_threshold,
        use_adaptive_weighting=g(
            params, "paper_use_adaptive_weighting", settings.paper_use_adaptive_weighting
        ),
    )


# --- Orchestration - DB reads/writes live here ---


@dataclass(frozen=True)
class _CycleLog:
    """Log lines built inside the worker thread, emitted by the async
    caller afterwards - same split as app/signals/generator.py's
    _CycleResult, so logging never happens off the event loop's thread.
    """

    lines: list[str]


def _latest_price(session: Session, condition_id: str, asset: str) -> Decimal | None:
    return session.execute(
        select(PriceSnapshot.price)
        .where(PriceSnapshot.condition_id == condition_id, PriceSnapshot.asset == asset)
        .order_by(PriceSnapshot.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _load_snapshots(
    session: Session, market: Market, signal: Signal, fill_config: FillConfig
) -> list[MarketSnapshot]:
    """The entry-time snapshot's captured_at is labeled as signal.created_at
    even though its ask/liquidity data is actually the latest known market
    read - there's no historized liquidity series to do better (see
    app/paper/fills.py's MarketSnapshot docstring). This guarantees it's
    treated as compute_fill's "current" snapshot (the earliest one), since
    any delayed candidate is by construction captured after
    signal.created_at + entry_delay_seconds.
    """
    entry_snapshot = MarketSnapshot(
        captured_at=signal.created_at,
        ask=market.best_ask,
        bid=market.best_bid,
        liquidity=market.liquidity,
        price=market.last_trade_price,
    )
    delay_threshold = signal.created_at + timedelta(seconds=fill_config.entry_delay_seconds)
    delayed_row = session.execute(
        select(PriceSnapshot)
        .where(
            PriceSnapshot.condition_id == signal.condition_id,
            PriceSnapshot.asset == signal.asset,
            PriceSnapshot.captured_at >= delay_threshold,
        )
        .order_by(PriceSnapshot.captured_at.asc())
        .limit(1)
    ).scalar_one_or_none()

    snapshots = [entry_snapshot]
    if delayed_row is not None:
        snapshots.append(
            MarketSnapshot(
                captured_at=delayed_row.captured_at,
                ask=None,
                bid=None,
                liquidity=None,
                price=delayed_row.price,
            )
        )
    return snapshots


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, tzinfo=UTC)


def _compute_daily_pnl_state(
    session: Session, portfolio: PaperPortfolio, now: datetime
) -> tuple[Decimal, Decimal]:
    """Returns (realized_pnl_today, day_start_bankroll), both derived from
    paper_trades rows rather than stored (docs/PHASE5_DESIGN.md flag 3):
    reversing today's exits (which credited bankroll) and today's still-open
    entries (which debited it) reconstructs the midnight value.
    """
    day_start = _day_start(now)

    exited_today = (
        session.execute(
            select(PaperTrade.realized_pnl).where(
                PaperTrade.portfolio_id == portfolio.id,
                PaperTrade.status == PaperTradeStatus.CLOSED,
                PaperTrade.exit_at >= day_start,
            )
        )
        .scalars()
        .all()
    )
    realized_pnl_today = sum((pnl for pnl in exited_today if pnl is not None), Decimal("0"))

    entered_today_open = session.execute(
        select(PaperTrade.entry_price, PaperTrade.size).where(
            PaperTrade.portfolio_id == portfolio.id,
            PaperTrade.status == PaperTradeStatus.OPEN,
            PaperTrade.entry_at >= day_start,
        )
    ).all()
    entered_today_cost = sum(
        (row.entry_price * row.size for row in entered_today_open), Decimal("0")
    )

    day_start_bankroll = portfolio.current_bankroll - realized_pnl_today + entered_today_cost
    return realized_pnl_today, day_start_bankroll


def _record_risk_event(
    session: Session, portfolio: PaperPortfolio, signal: Signal, decision: RiskDecision
) -> None:
    """Persists a risk_events row for a non-ALLOW decision - the pure
    RiskManager/rule functions never touch a session (docs/PHASE5_DESIGN.md
    section 8 / flag 10: only deny/resize is logged, never a routine allow).
    """
    session.add(
        RiskEvent(
            portfolio_id=portfolio.id,
            signal_id=signal.id,
            rule=decision.rule,
            decision=decision.action,
            reason=decision.reason,
            detail=decision.detail,
        )
    )


def _record_risk_denial(
    session: Session,
    portfolio: PaperPortfolio,
    signal: Signal,
    decision: RiskDecision,
    now: datetime,
) -> None:
    _record_risk_event(session, portfolio, signal, decision)
    session.add(
        PaperTrade(
            portfolio_id=portfolio.id,
            signal_id=signal.id,
            condition_id=signal.condition_id,
            asset=signal.asset,
            outcome=signal.outcome,
            status=PaperTradeStatus.MISSED,
            signal_price=signal.average_entry_price,
            exit_reason=decision.rule,
            rejection_reason=RejectionStage.RISK,
            exit_at=now,
        )
    )


def _run_entries(
    session: Session,
    portfolio: PaperPortfolio,
    config: PortfolioConfig,
    now: datetime,
    bucket_stats: dict[tuple[int, int, int], BucketStats],
    calibration_config: CalibrationConfig,
    cluster_of: dict[str, str],
    multiplier_of: dict[str, Decimal],
) -> list[str]:
    lines: list[str] = []

    candidates = (
        session.execute(
            select(Signal)
            .where(Signal.status == SignalStatus.ACTIVE)
            .order_by(Signal.weighted_score.desc())
        )
        .scalars()
        .all()
    )
    already_decided = set(
        session.execute(select(PaperTrade.signal_id).where(PaperTrade.portfolio_id == portfolio.id))
        .scalars()
        .all()
    )
    undecided_ids = set(select_undecided_signals([s.id for s in candidates], already_decided))
    undecided = [s for s in candidates if s.id in undecided_ids]

    open_rows = session.execute(
        select(
            PaperTrade.condition_id, PaperTrade.event_slug, PaperTrade.entry_price, PaperTrade.size
        ).where(PaperTrade.portfolio_id == portfolio.id, PaperTrade.status == PaperTradeStatus.OPEN)
    ).all()
    open_positions = [
        OpenPosition(
            condition_id=row.condition_id,
            event_slug=row.event_slug,
            notional=row.entry_price * row.size,
        )
        for row in open_rows
    ]
    exposure = sum((p.notional for p in open_positions), Decimal("0"))
    open_count = len(open_positions)

    realized_pnl_today, day_start_bankroll = _compute_daily_pnl_state(session, portfolio, now)
    risk_manager = RiskManager(config.risk)

    def current_state() -> PortfolioState:
        return PortfolioState(
            current_bankroll=portfolio.current_bankroll,
            starting_bankroll=portfolio.starting_bankroll,
            day_start_bankroll=day_start_bankroll,
            realized_pnl_today=realized_pnl_today,
            open_positions=tuple(open_positions),
            open_count=open_count,
        )

    for signal in undecided:
        market = session.execute(
            select(Market).where(Market.condition_id == signal.condition_id)
        ).scalar_one_or_none()

        if market is None or market.closed:
            if market is None:
                session.add(
                    PaperTrade(
                        portfolio_id=portfolio.id,
                        signal_id=signal.id,
                        condition_id=signal.condition_id,
                        asset=signal.asset,
                        outcome=signal.outcome,
                        status=PaperTradeStatus.MISSED,
                        signal_price=signal.average_entry_price,
                        exit_reason="MARKET_NOT_FOUND",
                        rejection_reason=RejectionStage.ENTRY_FILTER,
                        exit_at=now,
                    )
                )
            continue

        # docs/PHASE6_DESIGN.md workstream 5 / flag 14 - the "adaptive"
        # portfolio's entry filter recomputes weighted_score from this
        # signal's own contributors, scaling each cluster's weight by its
        # live bandit multiplier, instead of using the signal's stored
        # (static) weighted_score. Every other portfolio, and the signal
        # row itself, is completely untouched - this never forks signal
        # generation, it only changes what THIS portfolio's own filter
        # compares against.
        entry_weighted_score = (
            effective_weighted_score(signal.contributors, cluster_of, multiplier_of)
            if config.use_adaptive_weighting
            else signal.weighted_score
        )

        passed, filter_reason = passes_entry_filters(
            SignalMetrics(
                distinct_traders=signal.distinct_traders,
                weighted_score=entry_weighted_score,
                combined_entry_value=signal.combined_entry_value,
            ),
            MarketMetrics(
                liquidity=market.liquidity,
                spread=market.spread,
                closed=market.closed,
            ),
            config.entry_filters,
        )
        if not passed:
            session.add(
                PaperTrade(
                    portfolio_id=portfolio.id,
                    signal_id=signal.id,
                    condition_id=signal.condition_id,
                    asset=signal.asset,
                    outcome=signal.outcome,
                    status=PaperTradeStatus.MISSED,
                    signal_price=signal.average_entry_price,
                    exit_reason=filter_reason,
                    rejection_reason=RejectionStage.ENTRY_FILTER,
                    exit_at=now,
                )
            )
            lines.append(
                f"paper[{portfolio.name}]: missed signal {signal.id} entry_filter={filter_reason}"
            )
            continue

        pre_check_trade = ProposedTrade(
            condition_id=signal.condition_id, event_slug=signal.event_slug
        )
        pre_check_decision = risk_manager.pre_check(current_state(), pre_check_trade)
        if pre_check_decision.action != RiskAction.ALLOW:
            _record_risk_denial(session, portfolio, signal, pre_check_decision, now)
            lines.append(
                f"paper[{portfolio.name}]: missed signal {signal.id} risk={pre_check_decision.rule}"
            )
            continue

        # paper_sizer picks which sizer produces the raw notional (docs/
        # PHASE5_DESIGN.md section 6): TIERED uses Stage 2's band mapping;
        # KELLY uses Stage 3's calibrated fraction when its signal's bucket
        # has enough resolved history, and falls back to TIERED per-signal
        # otherwise (never a hard failure - that's the entire point of
        # TIERED existing as KELLY's safety net); anything else (including
        # "FIXED") uses the existing sizer, matching sizing.py's own
        # fail-safe-to-FIXED convention.
        kelly_fraction_value: Decimal | None = None
        p_hat_value: Decimal | None = None
        edge_value: Decimal | None = None

        if config.sizer == "KELLY":
            # signal.distinct_traders IS the independent-cluster count as of
            # Phase 6 workstream 1 (see app/consensus/engine.py's
            # _weighted_score()) - Kelly's p_hat lookup is cluster-aware for
            # free, with no change needed here beyond this field rename
            # (docs/PHASE6_DESIGN.md workstream 4's "confirm Kelly reads the
            # improved buckets").
            calibration_result = get_p_hat(
                bucket_stats,
                SignalFeatures(
                    cluster_count=signal.distinct_traders,
                    weighted_score=signal.weighted_score,
                    average_entry_price=signal.average_entry_price,
                ),
                calibration_config,
            )
            if calibration_result is None:
                tiered_result = size_tiered_position(
                    TieredSizingRequest(
                        current_bankroll=portfolio.current_bankroll,
                        weighted_score=signal.weighted_score,
                    ),
                    config.tiered_sizing,
                )
                target_notional = tiered_result.target_notional
                skipped_reason = tiered_result.skipped_reason
                sizer_used = "TIERED_FALLBACK"
            else:
                kelly_result = size_kelly_position(
                    KellySizingRequest(
                        current_bankroll=portfolio.current_bankroll,
                        p_hat=calibration_result.p_hat,
                        fill_price=signal.average_entry_price,
                    ),
                    config.kelly_sizing,
                )
                target_notional = kelly_result.target_notional
                skipped_reason = kelly_result.skipped_reason
                sizer_used = "KELLY"
                kelly_fraction_value = kelly_result.kelly_fraction_used
                p_hat_value = calibration_result.p_hat
                edge_value = kelly_result.edge
        elif config.sizer == "TIERED":
            tiered_result = size_tiered_position(
                TieredSizingRequest(
                    current_bankroll=portfolio.current_bankroll,
                    weighted_score=signal.weighted_score,
                ),
                config.tiered_sizing,
            )
            target_notional = tiered_result.target_notional
            skipped_reason = tiered_result.skipped_reason
            sizer_used = "TIERED"
        else:
            sizing_result = size_position(
                SizingRequest(
                    current_bankroll=portfolio.current_bankroll,
                    current_exposure=exposure,
                    weighted_score=signal.weighted_score,
                ),
                config.sizing,
            )
            target_notional = sizing_result.target_notional
            skipped_reason = sizing_result.skipped_reason
            sizer_used = "FIXED"

        if target_notional is None:
            session.add(
                PaperTrade(
                    portfolio_id=portfolio.id,
                    signal_id=signal.id,
                    condition_id=signal.condition_id,
                    asset=signal.asset,
                    outcome=signal.outcome,
                    status=PaperTradeStatus.MISSED,
                    signal_price=signal.average_entry_price,
                    exit_reason=skipped_reason,
                    rejection_reason=RejectionStage.SIZING,
                    exit_at=now,
                )
            )
            lines.append(
                f"paper[{portfolio.name}]: missed signal {signal.id} reason={skipped_reason}"
            )
            continue

        apply_trade = ProposedTrade(
            condition_id=signal.condition_id, event_slug=signal.event_slug, notional=target_notional
        )
        apply_decision = risk_manager.apply(current_state(), apply_trade)
        if apply_decision.action != RiskAction.ALLOW:
            _record_risk_event(session, portfolio, signal, apply_decision)
            if apply_decision.action == RiskAction.DENY:
                session.add(
                    PaperTrade(
                        portfolio_id=portfolio.id,
                        signal_id=signal.id,
                        condition_id=signal.condition_id,
                        asset=signal.asset,
                        outcome=signal.outcome,
                        status=PaperTradeStatus.MISSED,
                        signal_price=signal.average_entry_price,
                        exit_reason=apply_decision.rule,
                        rejection_reason=RejectionStage.RISK,
                        exit_at=now,
                    )
                )
                lines.append(
                    f"paper[{portfolio.name}]: missed signal {signal.id} risk={apply_decision.rule}"
                )
                continue
            target_notional = apply_decision.resized_notional
            lines.append(
                f"paper[{portfolio.name}]: resized signal {signal.id} risk={apply_decision.rule} "
                f"notional={target_notional:.4f}"
            )

        snapshots = _load_snapshots(session, market, signal, config.fill)
        fill_result = compute_fill(
            FillRequest(
                signal_price=signal.average_entry_price,
                order_notional=target_notional,
                detected_at=signal.created_at,
            ),
            snapshots,
            config.fill,
            now,
        )

        if not fill_result.filled:
            session.add(
                PaperTrade(
                    portfolio_id=portfolio.id,
                    signal_id=signal.id,
                    condition_id=signal.condition_id,
                    asset=signal.asset,
                    outcome=signal.outcome,
                    status=PaperTradeStatus.MISSED,
                    signal_price=signal.average_entry_price,
                    exit_reason=fill_result.reason,
                    rejection_reason=RejectionStage.FILL,
                    exit_at=now,
                )
            )
            lines.append(
                f"paper[{portfolio.name}]: missed signal {signal.id} reason={fill_result.reason}"
            )
            continue

        size = target_notional / fill_result.fill_price
        session.add(
            PaperTrade(
                portfolio_id=portfolio.id,
                signal_id=signal.id,
                condition_id=signal.condition_id,
                asset=signal.asset,
                outcome=signal.outcome,
                event_slug=signal.event_slug,
                status=PaperTradeStatus.OPEN,
                signal_price=signal.average_entry_price,
                entry_price=fill_result.fill_price,
                size=size,
                slippage_paid=fill_result.slippage_paid,
                sizer_used=sizer_used,
                kelly_fraction=kelly_fraction_value,
                p_hat=p_hat_value,
                edge=edge_value,
                entry_at=now,
            )
        )
        cost = size * fill_result.fill_price
        portfolio.current_bankroll -= cost
        exposure += cost
        open_positions.append(
            OpenPosition(
                condition_id=signal.condition_id, event_slug=signal.event_slug, notional=cost
            )
        )
        open_count += 1
        lines.append(
            f"paper[{portfolio.name}]: entered signal {signal.id} {signal.asset} "
            f"size={size:.4f} price={fill_result.fill_price:.4f} "
            f"slippage={fill_result.slippage_paid:.4f}"
        )

    return lines


def _mark_to_market(session: Session, portfolio: PaperPortfolio) -> list[PaperTrade]:
    open_trades = (
        session.execute(
            select(PaperTrade).where(
                PaperTrade.portfolio_id == portfolio.id, PaperTrade.status == PaperTradeStatus.OPEN
            )
        )
        .scalars()
        .all()
    )
    for trade in open_trades:
        latest = _latest_price(session, trade.condition_id, trade.asset)
        if latest is not None:
            trade.current_price = latest
            trade.unrealized_pnl = (latest - trade.entry_price) * trade.size
    return open_trades


def _run_exits(
    session: Session,
    portfolio: PaperPortfolio,
    open_trades: list[PaperTrade],
    config: PortfolioConfig,
    now: datetime,
) -> list[str]:
    lines: list[str] = []

    for trade in open_trades:
        market = session.execute(
            select(Market).where(Market.condition_id == trade.condition_id)
        ).scalar_one_or_none()
        closed = market.closed if market is not None else False
        resolution_price = (
            _latest_price(session, trade.condition_id, trade.asset) if closed else None
        )
        resolution = detect_resolution(closed, resolution_price, config.resolution_price_threshold)

        if resolution == ResolutionOutcome.AMBIGUOUS:
            logger.warning(
                "paper[%s]: market %s closed but resolution ambiguous (latest price %s) - "
                "leaving trade %d open",
                portfolio.name,
                trade.condition_id,
                resolution_price,
                trade.id,
            )
            continue

        signal = session.get(Signal, trade.signal_id)
        exit_reason = check_exit(
            entry_price=trade.entry_price,
            current_price=trade.current_price,
            resolution=resolution,
            signal_created_at=signal.created_at,
            now=now,
            config=config.exits,
        )
        if exit_reason is None:
            continue

        if exit_reason == ExitReason.MARKET_RESOLVED:
            exit_price = Decimal("1.0") if resolution == ResolutionOutcome.WON else Decimal("0.0")
        elif trade.current_price is not None:
            exit_price = trade.current_price
        else:
            exit_price = trade.entry_price

        trade.exit_price = exit_price
        trade.realized_pnl = (exit_price - trade.entry_price) * trade.size
        trade.exit_reason = exit_reason
        trade.exit_at = now
        trade.status = PaperTradeStatus.CLOSED
        portfolio.current_bankroll += trade.size * exit_price

        lines.append(
            f"paper[{portfolio.name}]: exited trade {trade.id} reason={exit_reason} "
            f"exit_price={exit_price:.4f} pnl={trade.realized_pnl:.4f}"
        )

    return lines


def _run_portfolio_cycle(
    session: Session,
    portfolio: PaperPortfolio,
    settings: Settings,
    now: datetime,
    bucket_stats: dict[tuple[int, int, int], BucketStats],
    calibration_config: CalibrationConfig,
    cluster_of: dict[str, str],
    multiplier_of: dict[str, Decimal],
) -> _CycleLog:
    config = resolve_portfolio_config(portfolio.params, settings)

    entry_lines = _run_entries(
        session, portfolio, config, now, bucket_stats, calibration_config, cluster_of, multiplier_of
    )
    open_trades = _mark_to_market(session, portfolio)
    exit_lines = _run_exits(session, portfolio, open_trades, config, now)

    open_count = session.execute(
        select(PaperTrade.id).where(
            PaperTrade.portfolio_id == portfolio.id, PaperTrade.status == PaperTradeStatus.OPEN
        )
    ).all()
    summary = (
        f"paper[{portfolio.name}]: entries={sum(1 for line in entry_lines if 'entered' in line)} "
        f"exits={len(exit_lines)} bankroll={portfolio.current_bankroll:.2f} open={len(open_count)}"
    )
    return _CycleLog(lines=[*entry_lines, *exit_lines, summary])


def _run_cycle_sync(now: datetime) -> list[_CycleLog]:
    # Fetched fresh every cycle, same as app/signals/generator.py's
    # _run_cycle - so paper_emergency_stop/risk_default_sizer dashboard
    # overrides (docs/PHASE5_DESIGN.md flag 9) take effect on the very next
    # cycle, no restart required.
    settings = get_effective_settings()
    calibration_config = resolve_calibration_config(settings)
    with db_session() as session:
        # Computed once per cycle, shared by every KELLY portfolio - it's a
        # property of the shared signal-generation process across all
        # portfolios' resolved trades, not any one portfolio's own params
        # (docs/PHASE5_DESIGN.md section 4).
        records = load_resolved_trade_records(session, calibration_config.window_days, now)
        bucket_stats = compute_bucket_stats(records, calibration_config)

        # Also computed once per cycle, shared by every portfolio - only
        # "adaptive" actually reads multiplier_of, but both maps are cheap
        # to build and every portfolio's cluster_of lookup would otherwise
        # need to be conditional (docs/PHASE6_DESIGN.md workstream 5).
        cluster_of = address_to_cluster_map(session)
        multiplier_of = load_multipliers(session)

        portfolios = (
            session.execute(select(PaperPortfolio).where(PaperPortfolio.is_active.is_(True)))
            .scalars()
            .all()
        )
        return [
            _run_portfolio_cycle(
                session,
                portfolio,
                settings,
                now,
                bucket_stats,
                calibration_config,
                cluster_of,
                multiplier_of,
            )
            for portfolio in portfolios
        ]


async def run_cycle() -> None:
    """Run one full paper-trading cycle: entry, mark-to-market, exit, per
    active portfolio. Runs after the consensus job so it always trades on
    the freshest completed signal-generation cycle (docs/PHASE4_DESIGN.md
    section 9).
    """
    now = datetime.now(UTC)
    results = await asyncio.to_thread(_run_cycle_sync, now)
    for result in results:
        for line in result.lines:
            logger.info(line)

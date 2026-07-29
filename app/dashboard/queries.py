"""Query functions for the dashboard - routes never contain raw SQL.

Almost everything here is a plain, synchronous, blocking read. The one
exception is apply_override()/reset_override() - the Tuning page's settings
overrides are the console's only mutating endpoints (see
docs/PHASE8_DESIGN.md), and they live here too so ALL dashboard DB access,
read or write, stays in this one module. Dashboard routes are defined with
`def`, not `async def`, so FastAPI runs them in its threadpool automatically
- the same "keep blocking DB work off the event loop" rule the collectors
satisfy explicitly with asyncio.to_thread, just via FastAPI's own mechanism
for sync route handlers instead.
"""

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import redis
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.config.adjustable import ADJUSTABLE, AdjustableField, parse_and_validate
from app.config.effective import get_effective_settings
from app.config.settings import Settings, get_settings
from app.db.models import (
    ConsensusRun,
    LeaderboardSnapshot,
    Market,
    MarketHistory,
    OverrideAudit,
    PaperPortfolio,
    PaperTrade,
    Position,
    PositionHistory,
    PriceSnapshot,
    RiskEvent,
    RuntimeOverride,
    Signal,
    SignalStatus,
    TraderScore,
    Wallet,
)
from app.db.session import db_session
from app.paper.engine import resolve_calibration_config, resolve_risk_config
from app.paper.metrics import PortfolioMetrics, TradeData, compute_portfolio_metrics
from app.risk.calibration import compute_bucket_stats, load_resolved_trade_records
from app.risk.rules import RiskRule

logger = logging.getLogger(__name__)

# See docs/PHASE8_DESIGN.md flag 8: fresh = within 1x the collector's own
# interval, stale = within 3x, dead beyond that or never written at all.
# A dashboard-only display rule, not a Settings field - it doesn't affect
# collector behavior, so it isn't part of the tunable registry.
_FRESH_MULTIPLIER = 1
_STALE_MULTIPLIER = 3


@dataclass(frozen=True)
class Heartbeat:
    """One collector's last-write status, for the Overview heartbeat row."""

    name: str
    last_write: datetime | None
    interval_seconds: int
    status: str  # "fresh" | "stale" | "dead"


@dataclass(frozen=True)
class OverviewCounts:
    """The Overview page's count strip."""

    tracked_wallets: int
    open_positions: int
    fresh_events_24h: int
    active_signals: int


@dataclass(frozen=True)
class TapeEvent:
    """One position_history row, shaped for the Overview tape / Events feed."""

    detected_at: datetime
    event_type: str
    address: str
    username: str | None
    title: str
    outcome: str
    size_after: Decimal


def check_db_health() -> bool:
    """A trivial round trip - proves the DB is actually reachable right now,
    not just that it was configured. Broad except is deliberate: any
    failure mode here means "unhealthy," never a 500 out of /healthz.
    """
    try:
        with db_session() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("healthz: database check failed", exc_info=True)
        return False


def check_redis_health() -> bool:
    """Same reasoning as check_db_health - broad except is intentional here."""
    try:
        client = redis.from_url(get_settings().redis_url)
        return bool(client.ping())
    except Exception:
        logger.warning("healthz: redis check failed", exc_info=True)
        return False


def _heartbeat_status(last_write: datetime | None, interval_seconds: int, now: datetime) -> str:
    """fresh: age <= 1x interval. stale: <= 3x. dead: beyond that, or never written."""
    if last_write is None:
        return "dead"
    age_seconds = (now - last_write).total_seconds()
    if age_seconds <= interval_seconds * _FRESH_MULTIPLIER:
        return "fresh"
    if age_seconds <= interval_seconds * _STALE_MULTIPLIER:
        return "stale"
    return "dead"


def get_collector_heartbeats(settings: Settings) -> list[Heartbeat]:
    """Each collector's latest write, compared against its configured interval.

    Derived from each collector's own output table - no separate heartbeat
    table needed (leaderboard_snapshots, position_history, market_history,
    consensus_runs already carry the timestamps).
    """
    now = datetime.now(UTC)

    with db_session() as session:
        leaderboard_latest = session.execute(
            select(func.max(LeaderboardSnapshot.captured_at))
        ).scalar_one()
        positions_latest = session.execute(
            select(func.max(PositionHistory.detected_at))
        ).scalar_one()
        markets_latest = session.execute(select(func.max(MarketHistory.captured_at))).scalar_one()
        consensus_latest = session.execute(select(func.max(ConsensusRun.executed_at))).scalar_one()

    rows = (
        ("leaderboard", leaderboard_latest, settings.leaderboard_interval_seconds),
        ("positions", positions_latest, settings.positions_interval_seconds),
        ("markets", markets_latest, settings.markets_interval_seconds),
        ("consensus", consensus_latest, settings.consensus_interval_seconds),
    )
    return [
        Heartbeat(
            name=name,
            last_write=last_write,
            interval_seconds=interval_seconds,
            status=_heartbeat_status(last_write, interval_seconds, now),
        )
        for name, last_write, interval_seconds in rows
    ]


def get_overview_counts() -> OverviewCounts:
    """Tracked wallets, open positions, fresh (non-bootstrap) events in the
    last 24h, and currently active signals.
    """
    fresh_since = datetime.now(UTC) - timedelta(hours=24)

    with db_session() as session:
        tracked_wallets = session.execute(
            select(func.count()).select_from(Wallet).where(Wallet.is_tracked.is_(True))
        ).scalar_one()
        open_positions = session.execute(
            select(func.count()).select_from(Position).where(Position.is_open.is_(True))
        ).scalar_one()
        fresh_events_24h = session.execute(
            select(func.count())
            .select_from(PositionHistory)
            .where(
                PositionHistory.is_bootstrap.is_(False),
                PositionHistory.detected_at >= fresh_since,
            )
        ).scalar_one()
        active_signals = session.execute(
            select(func.count()).select_from(Signal).where(Signal.status == SignalStatus.ACTIVE)
        ).scalar_one()

    return OverviewCounts(
        tracked_wallets=tracked_wallets,
        open_positions=open_positions,
        fresh_events_24h=fresh_events_24h,
        active_signals=active_signals,
    )


def get_latest_consensus_run() -> ConsensusRun | None:
    """The most recent generator cycle - backs the funnel cascade component."""
    with db_session() as session:
        return (
            session.execute(select(ConsensusRun).order_by(ConsensusRun.executed_at.desc()).limit(1))
            .scalars()
            .first()
        )


def get_recent_tape_events(limit: int = 15) -> list[TapeEvent]:
    """Most recent non-bootstrap position_history events, newest first."""
    with db_session() as session:
        rows = session.execute(
            select(PositionHistory, Wallet.address, Wallet.username)
            .join(Wallet, Wallet.id == PositionHistory.wallet_id)
            .where(PositionHistory.is_bootstrap.is_(False))
            .order_by(PositionHistory.detected_at.desc())
            .limit(limit)
        ).all()

    return [
        TapeEvent(
            detected_at=history.detected_at,
            event_type=history.event_type,
            address=address,
            username=username,
            title=history.title,
            outcome=history.outcome,
            size_after=history.size_after,
        )
        for history, address, username in rows
    ]


@dataclass(frozen=True)
class ActiveSignalRow:
    """One ACTIVE signal, plus its market's current liquidity (not stored on
    the signal row itself, joined for display in the summary table only -
    the per-row drill-down stays strictly to what's stored on the signal).
    """

    signal: Signal
    liquidity: Decimal | None


@dataclass(frozen=True)
class FunnelHistoryPoint:
    """One consensus_runs row, decomposed into per-stage rejection counts
    for the funnel-over-time stacked bar chart. Each *_rejected value is
    how many groups died at that exact stage; they sum to `groups` together
    with `accepted`.
    """

    executed_at: datetime
    market_rejected: int
    liquidity_rejected: int
    price_rejected: int
    spread_rejected: int
    breadth_rejected: int
    quality_rejected: int
    conviction_rejected: int
    accepted: int


def get_active_signals() -> list[ActiveSignalRow]:
    """ACTIVE signals, highest conviction first - same ordering as
    scripts/show_signals.py.
    """
    with db_session() as session:
        signals = (
            session.execute(
                select(Signal)
                .where(Signal.status == SignalStatus.ACTIVE)
                .order_by(Signal.weighted_score.desc(), Signal.combined_entry_value.desc())
            )
            .scalars()
            .all()
        )

        condition_ids = {signal.condition_id for signal in signals}
        markets: dict[str, Market] = {}
        if condition_ids:
            markets = {
                market.condition_id: market
                for market in session.execute(
                    select(Market).where(Market.condition_id.in_(condition_ids))
                ).scalars()
            }

    return [
        ActiveSignalRow(
            signal=signal,
            liquidity=(
                markets[signal.condition_id].liquidity if signal.condition_id in markets else None
            ),
        )
        for signal in signals
    ]


def get_signal_by_id(signal_id: int) -> Signal | None:
    """A single signal row, for the drill-down fragment - deliberately no
    joins here. The drill-down's whole point is proving a signal is
    explainable from its own row alone (see CLAUDE.md Code conventions).
    """
    with db_session() as session:
        return session.get(Signal, signal_id)


def get_recent_signal_history(limit: int = 20) -> list[tuple[Signal, str]]:
    """Signals that recently expired, or ACTIVE signals recently reinforced
    (touched again after creation) - a change feed under the ACTIVE table.
    There's no separate reinforcement-log table, so "reinforced" is derived:
    an ACTIVE signal whose updated_at has moved past its created_at.
    """
    with db_session() as session:
        rows = (
            session.execute(
                select(Signal)
                .where(
                    or_(
                        Signal.status == SignalStatus.EXPIRED,
                        and_(
                            Signal.status == SignalStatus.ACTIVE,
                            Signal.updated_at > Signal.created_at,
                        ),
                    )
                )
                .order_by(Signal.updated_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

    return [
        (signal, "expired" if signal.status == SignalStatus.EXPIRED else "reinforced")
        for signal in rows
    ]


def get_funnel_history(hours: int = 48) -> list[FunnelHistoryPoint]:
    """consensus_runs rows for the last `hours`, chronological, decomposed
    into per-stage rejection counts for the stacked bar chart.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)

    with db_session() as session:
        runs = (
            session.execute(
                select(ConsensusRun)
                .where(ConsensusRun.executed_at >= since)
                .order_by(ConsensusRun.executed_at.asc())
            )
            .scalars()
            .all()
        )

    return [
        FunnelHistoryPoint(
            executed_at=run.executed_at,
            market_rejected=run.groups - run.market,
            liquidity_rejected=run.market - run.liquidity,
            price_rejected=run.liquidity - run.price,
            spread_rejected=run.price - run.spread,
            breadth_rejected=run.spread - run.breadth,
            quality_rejected=run.breadth - run.quality,
            conviction_rejected=run.quality - run.conviction,
            accepted=run.new_signals + run.reinforced,
        )
        for run in runs
    ]


# --- Traders page ---


@dataclass(frozen=True)
class TraderRow:
    """One wallet's latest score, for the Traders table."""

    wallet_id: int
    address: str
    username: str | None
    is_tracked: bool
    month_component: Decimal
    all_time_component: Decimal
    consistency_component: Decimal
    score: Decimal


@dataclass(frozen=True)
class WalletDetail:
    """A single wallet's open positions and recent event history."""

    wallet_id: int
    address: str
    username: str | None
    is_tracked: bool
    open_positions: list[Position]
    recent_events: list[PositionHistory]


def get_trader_scores() -> list[TraderRow]:
    """Each wallet's most recent score, highest score first."""
    with db_session() as session:
        ranked = select(
            TraderScore.wallet_id,
            TraderScore.month_component,
            TraderScore.all_time_component,
            TraderScore.consistency_component,
            TraderScore.score,
            func.row_number()
            .over(partition_by=TraderScore.wallet_id, order_by=TraderScore.captured_at.desc())
            .label("rn"),
        ).subquery()
        rows = session.execute(
            select(
                ranked.c.wallet_id,
                ranked.c.month_component,
                ranked.c.all_time_component,
                ranked.c.consistency_component,
                ranked.c.score,
                Wallet.address,
                Wallet.username,
                Wallet.is_tracked,
            )
            .join(Wallet, Wallet.id == ranked.c.wallet_id)
            .where(ranked.c.rn == 1)
            .order_by(ranked.c.score.desc())
        ).all()

    return [
        TraderRow(
            wallet_id=row.wallet_id,
            address=row.address,
            username=row.username,
            is_tracked=row.is_tracked,
            month_component=row.month_component,
            all_time_component=row.all_time_component,
            consistency_component=row.consistency_component,
            score=row.score,
        )
        for row in rows
    ]


def get_wallet_detail(wallet_id: int, event_limit: int = 50) -> WalletDetail | None:
    """One wallet's open positions and its last `event_limit` events.

    Unlike the Overview tape / Events feed, bootstrap events are NOT
    excluded here - this is a complete history of one wallet, and its
    bootstrap sweep (its portfolio the moment we started watching it) is
    legitimate context, not noise. Template marks them distinctly instead
    of hiding them.
    """
    with db_session() as session:
        wallet = session.get(Wallet, wallet_id)
        if wallet is None:
            return None

        open_positions = (
            session.execute(
                select(Position)
                .where(Position.wallet_id == wallet_id, Position.is_open.is_(True))
                .order_by(Position.current_value.desc())
            )
            .scalars()
            .all()
        )

        recent_events = (
            session.execute(
                select(PositionHistory)
                .where(PositionHistory.wallet_id == wallet_id)
                .order_by(PositionHistory.detected_at.desc())
                .limit(event_limit)
            )
            .scalars()
            .all()
        )

    return WalletDetail(
        wallet_id=wallet.id,
        address=wallet.address,
        username=wallet.username,
        is_tracked=wallet.is_tracked,
        open_positions=list(open_positions),
        recent_events=list(recent_events),
    )


# --- Markets page ---


@dataclass(frozen=True)
class MarketOutcomePrice:
    outcome: str
    asset: str
    price: Decimal | None


@dataclass(frozen=True)
class MarketRow:
    """One market, for the Markets table."""

    id: int
    condition_id: str
    question: str
    outcome_prices: list[MarketOutcomePrice]
    liquidity: Decimal | None
    volume_24h: Decimal | None
    spread: Decimal | None
    end_date: datetime | None
    closed: bool


@dataclass(frozen=True)
class PricePoint:
    captured_at: datetime
    price: Decimal


@dataclass(frozen=True)
class MarketDetail:
    market: Market
    outcome_prices: list[MarketOutcomePrice]
    price_series: dict[str, list[PricePoint]]


_SPARKLINE_MAX_POINTS = 200
_SPARKLINE_RECENT_FULL_RES = 50


def _latest_prices_for_assets(session: Session, assets: set[str]) -> dict[str, Decimal]:
    """Each asset's most recent prices row. Same window-function shape as
    app/signals/generator.py's _latest_prices_by_asset - duplicated rather
    than imported, since that module is a different layer (signal
    generation, not dashboard reads) and this query is small enough that
    sharing it isn't worth a cross-layer dependency.
    """
    if not assets:
        return {}
    ranked = (
        select(
            PriceSnapshot.asset,
            PriceSnapshot.price,
            func.row_number()
            .over(partition_by=PriceSnapshot.asset, order_by=PriceSnapshot.captured_at.desc())
            .label("rn"),
        )
        .where(PriceSnapshot.asset.in_(assets))
        .subquery()
    )
    rows = session.execute(select(ranked.c.asset, ranked.c.price).where(ranked.c.rn == 1)).all()
    return dict(rows)


def _build_outcome_prices(
    market: Market, prices_by_asset: dict[str, Decimal]
) -> list[MarketOutcomePrice]:
    outcomes = market.outcomes or []
    assets = market.clob_token_ids or []
    return [
        MarketOutcomePrice(outcome=outcome, asset=asset, price=prices_by_asset.get(asset))
        for outcome, asset in zip(outcomes, assets, strict=False)
    ]


def get_markets(status: str = "open") -> list[MarketRow]:
    """status: "open" (default), "closed", or "all"."""
    with db_session() as session:
        query = select(Market)
        if status == "open":
            query = query.where(Market.closed.is_(False))
        elif status == "closed":
            query = query.where(Market.closed.is_(True))
        markets = (
            session.execute(query.order_by(Market.volume_24h.desc().nulls_last())).scalars().all()
        )

        all_assets = {asset for market in markets for asset in (market.clob_token_ids or [])}
        prices_by_asset = _latest_prices_for_assets(session, all_assets)

    return [
        MarketRow(
            id=market.id,
            condition_id=market.condition_id,
            question=market.question,
            outcome_prices=_build_outcome_prices(market, prices_by_asset),
            liquidity=market.liquidity,
            volume_24h=market.volume_24h,
            spread=market.spread,
            end_date=market.end_date,
            closed=market.closed,
        )
        for market in markets
    ]


def _decimate_keeping_recent_full_res(
    points: list[PricePoint], max_points: int, recent_count: int
) -> list[PricePoint]:
    """Keep the most recent `recent_count` points untouched; thin the older
    remainder down to fit the rest of the budget - recent activity stays
    fully detailed, older history becomes a coarser trend line.
    """
    if len(points) <= max_points:
        return points

    recent = points[-recent_count:]
    older = points[:-recent_count]
    budget = max_points - len(recent)
    if budget <= 0 or not older:
        return recent
    step = max(1, len(older) // budget)
    return older[::step] + recent


def get_market_detail(market_id: int) -> MarketDetail | None:
    """One market's outcome prices plus a decimated price series per
    outcome, for the drill-down's Chart.js sparklines.
    """
    with db_session() as session:
        market = session.get(Market, market_id)
        if market is None:
            return None

        assets = market.clob_token_ids or []
        series: dict[str, list[PricePoint]] = {}
        for asset in assets:
            rows = session.execute(
                select(PriceSnapshot.captured_at, PriceSnapshot.price)
                .where(PriceSnapshot.asset == asset)
                .order_by(PriceSnapshot.captured_at.asc())
            ).all()
            points = [PricePoint(captured_at=row.captured_at, price=row.price) for row in rows]
            series[asset] = _decimate_keeping_recent_full_res(
                points, _SPARKLINE_MAX_POINTS, _SPARKLINE_RECENT_FULL_RES
            )

        prices_by_asset = _latest_prices_for_assets(session, set(assets))

    return MarketDetail(
        market=market,
        outcome_prices=_build_outcome_prices(market, prices_by_asset),
        price_series=series,
    )


# --- Events page ---


@dataclass(frozen=True)
class EventRow:
    """One position_history row, for the Events feed - a superset of
    TapeEvent (adds id, for the flash-on-new-row diffing, and is_bootstrap,
    since Events (unlike the tape) lets bootstrap rows be shown at all).
    """

    id: int
    detected_at: datetime
    event_type: str
    is_bootstrap: bool
    address: str
    username: str | None
    title: str
    outcome: str
    size_after: Decimal


_EVENTS_LIMIT = 200


def get_events(event_type: str | None = None, include_bootstrap: bool = False) -> list[EventRow]:
    """Newest-first position_history feed, capped at 200 rows, filterable
    by event type and whether bootstrap rows are included (default: not).
    """
    with db_session() as session:
        query = (
            select(PositionHistory, Wallet.address, Wallet.username)
            .join(Wallet, Wallet.id == PositionHistory.wallet_id)
            .order_by(PositionHistory.detected_at.desc())
            .limit(_EVENTS_LIMIT)
        )
        if not include_bootstrap:
            query = query.where(PositionHistory.is_bootstrap.is_(False))
        if event_type:
            query = query.where(PositionHistory.event_type == event_type)
        rows = session.execute(query).all()

    return [
        EventRow(
            id=history.id,
            detected_at=history.detected_at,
            event_type=history.event_type,
            is_bootstrap=history.is_bootstrap,
            address=address,
            username=username,
            title=history.title,
            outcome=history.outcome,
            size_after=history.size_after,
        )
        for history, address, username in rows
    ]


# --- Tuning page ---


@dataclass(frozen=True)
class TuningRow:
    """One ADJUSTABLE registry entry, joined with its live DB/effective state."""

    key: str
    description: str
    default: str
    current_override: str | None
    effective_value: str
    min: str | None
    max: str | None
    restart_required: bool
    type_name: str


@dataclass(frozen=True)
class OverrideApplyResult:
    ok: bool
    value: object | None
    error: str | None


def get_runtime_overrides() -> dict[str, str]:
    with db_session() as session:
        rows = session.execute(select(RuntimeOverride)).scalars().all()
    return {row.key: row.value for row in rows}


def _build_tuning_row(
    key: str,
    field: AdjustableField,
    base: Settings,
    effective: Settings,
    overrides: dict[str, str],
) -> TuningRow:
    return TuningRow(
        key=key,
        description=field.description,
        default=str(getattr(base, key)),
        current_override=overrides.get(key),
        effective_value=str(getattr(effective, key)),
        min=str(field.min) if field.min is not None else None,
        max=str(field.max) if field.max is not None else None,
        restart_required=field.restart_required,
        type_name=field.type.__name__,
    )


def get_tuning_rows() -> list[TuningRow]:
    """Every ADJUSTABLE entry, in registry order, with its current DB
    override (if any) and effective value.
    """
    base = get_settings()
    effective = get_effective_settings()
    overrides = get_runtime_overrides()
    return [
        _build_tuning_row(key, field, base, effective, overrides)
        for key, field in ADJUSTABLE.items()
    ]


def get_tuning_row(key: str) -> TuningRow | None:
    """A single ADJUSTABLE entry - used to re-render one row fragment after
    an apply/reset POST, without rebuilding the whole table.
    """
    field = ADJUSTABLE.get(key)
    if field is None:
        return None
    base = get_settings()
    effective = get_effective_settings()
    overrides = get_runtime_overrides()
    return _build_tuning_row(key, field, base, effective, overrides)


def apply_override(key: str, raw_value: str) -> OverrideApplyResult:
    """Validate, then upsert runtime_overrides and write an audit row - the
    Tuning page's only mutating write path. Validation happens BEFORE any
    DB write, so an invalid POST touches nothing.
    """
    try:
        parsed = parse_and_validate(key, raw_value)
    except ValueError as exc:
        return OverrideApplyResult(ok=False, value=None, error=str(exc))

    with db_session() as session:
        existing = (
            session.execute(select(RuntimeOverride).where(RuntimeOverride.key == key))
            .scalars()
            .first()
        )
        old_value = existing.value if existing is not None else None

        if existing is not None:
            existing.value = raw_value
        else:
            session.add(RuntimeOverride(key=key, value=raw_value))

        session.add(OverrideAudit(key=key, old_value=old_value, new_value=raw_value))

    return OverrideApplyResult(ok=True, value=parsed, error=None)


def reset_override(key: str) -> None:
    """Delete the runtime_overrides row (falling back to the base Settings
    value) and still write an audit entry - resets show up in the trail too.
    """
    with db_session() as session:
        existing = (
            session.execute(select(RuntimeOverride).where(RuntimeOverride.key == key))
            .scalars()
            .first()
        )
        if existing is None:
            return
        old_value = existing.value
        session.delete(existing)
        session.add(OverrideAudit(key=key, old_value=old_value, new_value=None))


def get_override_audit(limit: int = 50) -> list[OverrideAudit]:
    with db_session() as session:
        return (
            session.execute(
                select(OverrideAudit).order_by(OverrideAudit.changed_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )


# --- Logs page ---

_LOG_TAIL_LINES = 200


def _log_file_path() -> Path:
    """Settings.log_dir is read fresh, not cached at import time, so a
    LOG_DIR change takes effect on the next request with no restart.
    """
    return Path(get_settings().log_dir) / "polybot.log"


def _log_line_class(line: str) -> str:
    if " | ERROR | " in line or " | CRITICAL | " in line:
        return "text-alert"
    if " | WARNING | " in line:
        return "text-amber"
    return ""


def get_log_tail(lines: int = _LOG_TAIL_LINES) -> list[tuple[str, str]]:
    """Last `lines` lines of polybot.log, oldest first (reads top to
    bottom like a terminal), each paired with a CSS class for its level.
    [] if the file doesn't exist yet - it's only created on first write
    (see delay=True in app/utils/logging.py).
    """
    log_file = _log_file_path()
    if not log_file.exists():
        return []
    with log_file.open(encoding="utf-8", errors="replace") as f:
        raw_lines = [line.rstrip("\n") for line in deque(f, maxlen=lines)]
    return [(line, _log_line_class(line)) for line in raw_lines]


# --- Paper trading page ---


@dataclass(frozen=True)
class PaperPortfolioRow:
    id: int
    name: str
    starting_bankroll: Decimal
    current_bankroll: Decimal
    is_active: bool
    params: dict


@dataclass(frozen=True)
class PaperTradeRow:
    id: int
    signal_id: int
    condition_id: str
    asset: str
    outcome: str
    status: str
    size: Decimal | None
    entry_price: Decimal | None
    signal_price: Decimal
    slippage_paid: Decimal | None
    entry_at: datetime | None
    current_price: Decimal | None
    unrealized_pnl: Decimal | None
    exit_price: Decimal | None
    realized_pnl: Decimal | None
    exit_reason: str | None
    exit_at: datetime | None


@dataclass(frozen=True)
class EquityPoint:
    exit_at: datetime
    equity: Decimal


@dataclass(frozen=True)
class PaperComparisonRow:
    portfolio_id: int
    name: str
    roi_pct: Decimal | None
    win_rate: Decimal | None
    profit_factor: Decimal | None
    max_drawdown_pct: Decimal | None
    closed_count: int
    open_count: int
    total_realized_pnl: Decimal | None
    avg_return: Decimal | None
    sharpe: Decimal | None
    insufficient: bool


@dataclass(frozen=True)
class PaperOverviewStats:
    total_strategies: int
    active_trades: int
    total_capital: Decimal
    total_realized_pnl: Decimal
    total_unrealized_pnl: Decimal
    avg_win_rate: Decimal | None


@dataclass(frozen=True)
class PortfolioSignalFunnel:
    entry_filter_rejected: int
    sizing_rejected: int
    fill_rejected: int
    entered: int
    closed: int
    total_evaluated: int


@dataclass(frozen=True)
class TradeStatistics:
    avg_hold_duration_hours: float | None
    biggest_winner: Decimal | None
    biggest_loser: Decimal | None
    max_win_streak: int
    max_lose_streak: int


def list_portfolios() -> list[PaperPortfolioRow]:
    with db_session() as session:
        rows = (
            session.execute(select(PaperPortfolio).order_by(PaperPortfolio.id.asc()))
            .scalars()
            .all()
        )
    return [
        PaperPortfolioRow(
            id=p.id,
            name=p.name,
            starting_bankroll=p.starting_bankroll,
            current_bankroll=p.current_bankroll,
            is_active=p.is_active,
            params=p.params,
        )
        for p in rows
    ]


def _trades_for_portfolio(
    session: Session, portfolio_id: int, status_filter: str | None = None
) -> list[PaperTrade]:
    query = select(PaperTrade).where(PaperTrade.portfolio_id == portfolio_id)
    if status_filter and status_filter != "all":
        query = query.where(PaperTrade.status == status_filter)
    return session.execute(query.order_by(PaperTrade.id.desc())).scalars().all()


def portfolio_metrics_from_db(
    portfolio_id: int, status_filter: str | None = None
) -> PortfolioMetrics | None:
    """Load one portfolio's trades from DB and compute full metrics."""
    with db_session() as session:
        portfolio = session.get(PaperPortfolio, portfolio_id)
        if portfolio is None:
            return None
        trades = _trades_for_portfolio(session, portfolio_id, status_filter)

    trade_data = [
        TradeData(
            status=t.status,
            entry_price=t.entry_price,
            size=t.size,
            realized_pnl=t.realized_pnl,
            unrealized_pnl=t.unrealized_pnl,
            exit_at=t.exit_at,
        )
        for t in trades
    ]
    return compute_portfolio_metrics(
        trades=trade_data,
        starting_bankroll=portfolio.starting_bankroll,
        current_bankroll=portfolio.current_bankroll,
        min_trades_for_stats=get_settings().paper_min_trades_for_stats,
    )


def list_portfolio_trades(portfolio_id: int, status_filter: str = "all") -> list[PaperTradeRow]:
    with db_session() as session:
        trades = _trades_for_portfolio(session, portfolio_id, status_filter)

    return [
        PaperTradeRow(
            id=t.id,
            signal_id=t.signal_id,
            condition_id=t.condition_id,
            asset=t.asset,
            outcome=t.outcome,
            status=t.status,
            size=t.size,
            entry_price=t.entry_price,
            signal_price=t.signal_price,
            slippage_paid=t.slippage_paid,
            entry_at=t.entry_at,
            current_price=t.current_price,
            unrealized_pnl=t.unrealized_pnl,
            exit_price=t.exit_price,
            realized_pnl=t.realized_pnl,
            exit_reason=t.exit_reason,
            exit_at=t.exit_at,
        )
        for t in trades
    ]


def equity_curve_points(portfolio_id: int) -> list[EquityPoint]:
    """Equity curve built from closed trades in exit order. Each point is
    the cumulative equity (starting_bankroll + running realized PnL) at the
    moment of exit. Returns [] when there are no closed trades.
    """
    with db_session() as session:
        portfolio = session.get(PaperPortfolio, portfolio_id)
        if portfolio is None:
            return []
        closed = (
            session.execute(
                select(PaperTrade)
                .where(
                    PaperTrade.portfolio_id == portfolio_id,
                    PaperTrade.status == "CLOSED",
                    PaperTrade.exit_at.isnot(None),
                    PaperTrade.realized_pnl.isnot(None),
                )
                .order_by(PaperTrade.exit_at.asc())
            )
            .scalars()
            .all()
        )

    equity = portfolio.starting_bankroll
    points: list[EquityPoint] = []
    for trade in closed:
        equity += trade.realized_pnl
        points.append(EquityPoint(exit_at=trade.exit_at, equity=equity))
    return points


def get_comparison_data() -> list[PaperComparisonRow]:
    portfolios = list_portfolios()
    comparison: list[PaperComparisonRow] = []
    for p in portfolios:
        metrics = portfolio_metrics_from_db(p.id)
        if metrics is None:
            continue
        closed = metrics.closed_count
        avg_return = (metrics.total_realized_pnl / Decimal(str(closed))) if closed > 0 else None
        comparison.append(
            PaperComparisonRow(
                portfolio_id=p.id,
                name=p.name,
                roi_pct=metrics.roi_pct,
                win_rate=metrics.win_rate,
                profit_factor=metrics.profit_factor,
                max_drawdown_pct=metrics.max_drawdown_pct,
                closed_count=metrics.closed_count,
                open_count=metrics.open_count,
                total_realized_pnl=metrics.total_realized_pnl,
                avg_return=avg_return,
                sharpe=metrics.sharpe,
                insufficient=metrics.insufficient_sample_note is not None,
            )
        )
    return comparison


def get_missed_count(portfolio_id: int) -> int:
    with db_session() as session:
        return session.execute(
            select(func.count())
            .select_from(PaperTrade)
            .where(PaperTrade.portfolio_id == portfolio_id, PaperTrade.status == "MISSED")
        ).scalar_one()


def overview_stats() -> PaperOverviewStats:
    """Aggregate across all active portfolios - total strategies, active
    trades, total capital, total realized/unrealized PnL, and average win
    rate (weighted by closed trade count).
    """
    portfolios = list_portfolios()
    active_trades = 0
    total_capital = Decimal("0")
    total_realized = Decimal("0")
    total_unrealized = Decimal("0")
    weighted_win_rate = Decimal("0")
    total_closed = 0

    for p in portfolios:
        metrics = portfolio_metrics_from_db(p.id)
        if metrics is None:
            continue
        active_trades += metrics.open_count
        total_capital += metrics.current_bankroll
        total_realized += metrics.total_realized_pnl
        total_unrealized += metrics.unrealized_pnl
        if metrics.win_rate is not None and metrics.closed_count > 0:
            weighted_win_rate += metrics.win_rate * Decimal(str(metrics.closed_count))
            total_closed += metrics.closed_count

    avg_win_rate = (weighted_win_rate / Decimal(str(total_closed))) if total_closed > 0 else None

    return PaperOverviewStats(
        total_strategies=len(portfolios),
        active_trades=active_trades,
        total_capital=total_capital,
        total_realized_pnl=total_realized,
        total_unrealized_pnl=total_unrealized,
        avg_win_rate=avg_win_rate,
    )


def portfolio_signal_funnel(portfolio_id: int) -> PortfolioSignalFunnel:
    """Per-portfolio breakdown of signal decisions by rejection stage."""
    with db_session() as session:
        all_rows = session.execute(
            select(PaperTrade.status, PaperTrade.rejection_reason).where(
                PaperTrade.portfolio_id == portfolio_id
            )
        ).all()

    entry_filter = 0
    sizing = 0
    fill = 0
    entered = 0
    closed = 0

    for status, rejection_reason in all_rows:
        if status == "CLOSED":
            closed += 1
        elif status == "OPEN":
            entered += 1
        elif status == "MISSED":
            if rejection_reason == "ENTRY_FILTER":
                entry_filter += 1
            elif rejection_reason == "SIZING":
                sizing += 1
            elif rejection_reason == "FILL":
                fill += 1

    total = entry_filter + sizing + fill + entered + closed
    return PortfolioSignalFunnel(
        entry_filter_rejected=entry_filter,
        sizing_rejected=sizing,
        fill_rejected=fill,
        entered=entered,
        closed=closed,
        total_evaluated=total,
    )


def today_return(portfolio_id: int) -> Decimal:
    """Realized PnL from trades exited since midnight UTC today."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    with db_session() as session:
        result = session.execute(
            select(func.coalesce(func.sum(PaperTrade.realized_pnl), Decimal("0"))).where(
                PaperTrade.portfolio_id == portfolio_id,
                PaperTrade.status == "CLOSED",
                PaperTrade.exit_at >= today_start,
                PaperTrade.realized_pnl.isnot(None),
            )
        ).scalar_one()
    return result


def trade_statistics(portfolio_id: int) -> TradeStatistics:
    """Additional trade-level statistics not covered by PortfolioMetrics."""
    with db_session() as session:
        trades = (
            session.execute(
                select(PaperTrade).where(
                    PaperTrade.portfolio_id == portfolio_id,
                    PaperTrade.status == "CLOSED",
                    PaperTrade.entry_at.isnot(None),
                    PaperTrade.exit_at.isnot(None),
                    PaperTrade.realized_pnl.isnot(None),
                )
            )
            .scalars()
            .all()
        )

    if not trades:
        return TradeStatistics(
            avg_hold_duration_hours=None,
            biggest_winner=None,
            biggest_loser=None,
            max_win_streak=0,
            max_lose_streak=0,
        )

    durations = [
        (t.exit_at - t.entry_at).total_seconds() / 3600
        for t in trades
        if t.entry_at is not None and t.exit_at is not None
    ]
    avg_duration = (sum(durations) / len(durations)) if durations else None

    pnls = [t.realized_pnl for t in trades if t.realized_pnl is not None]
    biggest_winner = max(pnls) if pnls else None
    biggest_loser = min(pnls) if pnls else None

    # Win/loss streaks
    max_win = max_lose = current_win = current_lose = 0
    for t in sorted(trades, key=lambda x: x.exit_at or datetime.min.replace(tzinfo=UTC)):
        if t.realized_pnl is not None and t.realized_pnl > 0:
            current_win += 1
            current_lose = 0
            max_win = max(max_win, current_win)
        else:
            current_lose += 1
            current_win = 0
            max_lose = max(max_lose, current_lose)

    return TradeStatistics(
        avg_hold_duration_hours=avg_duration,
        biggest_winner=biggest_winner,
        biggest_loser=biggest_loser,
        max_win_streak=max_win,
        max_lose_streak=max_lose,
    )


# --- Risk page ---

# Bar color thresholds, shared by every cap-style bar below - amber once a
# portfolio is within 20% of a limit, alert once it's breached it. A
# dashboard-only display rule, not a Settings field (docs/PHASE5_DESIGN.md
# already owns the actual enforcement thresholds; this only decides when a
# bar changes color).
_BAR_AMBER_THRESHOLD = Decimal("0.8")

RISK_RULE_NAMES: list[str] = [r.value for r in RiskRule]


@dataclass(frozen=True)
class RiskBar:
    """One thin bar on the Risk page - value/cap already resolved to a
    display string and a clamped [0, 100] percent so the template does no
    arithmetic of its own, same split as every other query in this module.
    """

    label: str
    value_label: str
    pct: float
    color: str  # "phosphor" | "amber" | "alert"


@dataclass(frozen=True)
class RiskGuardrailState:
    portfolio_id: int
    name: str
    min_bankroll_halted: bool
    daily_stopped: bool
    bars: list[RiskBar]


@dataclass(frozen=True)
class RiskEventRow:
    id: int
    portfolio_name: str
    rule: str
    decision: str
    reason: str
    occurred_at: datetime


@dataclass(frozen=True)
class CalibrationBucketRow:
    trader_band: str
    score_band: str
    price_band: str
    n: int
    wins: int
    p_hat: Decimal
    ci_low: Decimal
    ci_high: Decimal
    trusted: bool


@dataclass(frozen=True)
class SizerBreakdownRow:
    portfolio_id: int
    portfolio_name: str
    counts: dict[str, int]


def get_emergency_stop_active() -> bool:
    return get_effective_settings().paper_emergency_stop


def _bar_color(ratio: Decimal) -> str:
    if ratio >= 1:
        return "alert"
    if ratio >= _BAR_AMBER_THRESHOLD:
        return "amber"
    return "phosphor"


def _clamped_pct(ratio: Decimal) -> float:
    return float(min(max(ratio, Decimal("0")), Decimal("1")) * 100)


def _cap_bar(label: str, value: Decimal, cap: Decimal) -> RiskBar:
    ratio = (value / cap) if cap > 0 else Decimal("0")
    return RiskBar(
        label=label,
        value_label=f"${value:,.2f} / ${cap:,.2f} cap",
        pct=_clamped_pct(ratio),
        color=_bar_color(ratio),
    )


def _floor_bar(label: str, value: Decimal, floor: Decimal, ceiling: Decimal) -> RiskBar:
    """Inverted from a cap bar - breach means falling below the floor, not
    rising above it, so the bar fills with headroom-above-the-floor and
    danger increases as that headroom shrinks toward zero.
    """
    span = ceiling - floor
    headroom_ratio = ((value - floor) / span) if span > 0 else Decimal("1")
    if value <= floor:
        color = "alert"
    elif headroom_ratio <= Decimal("0.2"):
        color = "amber"
    else:
        color = "phosphor"
    return RiskBar(
        label=label,
        value_label=f"${value:,.2f} (floor ${floor:,.2f})",
        pct=_clamped_pct(headroom_ratio),
        color=color,
    )


def _daily_pnl_bar(today_pnl: Decimal, threshold: Decimal) -> RiskBar:
    if today_pnl >= 0:
        return RiskBar(
            label="Daily PnL", value_label=f"+${today_pnl:,.2f} today", pct=0.0, color="phosphor"
        )
    loss = -today_pnl
    ratio = (loss / threshold) if threshold > 0 else Decimal("0")
    return RiskBar(
        label="Daily PnL",
        value_label=f"-${loss:,.2f} / -${threshold:,.2f} stop",
        pct=_clamped_pct(ratio),
        color=_bar_color(ratio),
    )


def _open_count_bar(open_count: int, max_open: int) -> RiskBar:
    ratio = (Decimal(open_count) / Decimal(max_open)) if max_open > 0 else Decimal("0")
    return RiskBar(
        label="Open positions",
        value_label=f"{open_count} / {max_open}",
        pct=_clamped_pct(ratio),
        color=_bar_color(ratio),
    )


def _daily_pnl_state(
    session: Session, portfolio: PaperPortfolio, today_start: datetime
) -> tuple[Decimal, Decimal]:
    """Returns (today_realized_pnl, day_start_bankroll) - the same
    reconstruction app/paper/engine.py's RiskManager wiring uses internally
    (docs/PHASE5_DESIGN.md flag 3), duplicated here for read-only display
    since the engine's version is a private, DB-coupled helper rather than
    a shared pure function.
    """
    today_pnl = session.execute(
        select(func.coalesce(func.sum(PaperTrade.realized_pnl), Decimal("0"))).where(
            PaperTrade.portfolio_id == portfolio.id,
            PaperTrade.status == "CLOSED",
            PaperTrade.exit_at >= today_start,
            PaperTrade.realized_pnl.isnot(None),
        )
    ).scalar_one()
    entered_today = session.execute(
        select(PaperTrade.entry_price, PaperTrade.size).where(
            PaperTrade.portfolio_id == portfolio.id,
            PaperTrade.status == "OPEN",
            PaperTrade.entry_at >= today_start,
        )
    ).all()
    entered_today_cost = sum((row.entry_price * row.size for row in entered_today), Decimal("0"))
    day_start_bankroll = portfolio.current_bankroll - today_pnl + entered_today_cost
    return today_pnl, day_start_bankroll


def get_risk_guardrail_states() -> list[RiskGuardrailState]:
    """Current risk state for every active portfolio - bankroll vs its halt
    floor, today's PnL vs the daily stop, exposure vs every cap, open count
    vs the max. Every cap is resolved through resolve_risk_config so this
    always matches what RiskManager actually enforces, portfolio overrides
    included - never a display-only copy of the global defaults.
    """
    settings = get_effective_settings()
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    states: list[RiskGuardrailState] = []

    with db_session() as session:
        portfolios = (
            session.execute(
                select(PaperPortfolio)
                .where(PaperPortfolio.is_active.is_(True))
                .order_by(PaperPortfolio.id.asc())
            )
            .scalars()
            .all()
        )

        for p in portfolios:
            risk_config = resolve_risk_config(p.params, settings)

            open_rows = session.execute(
                select(
                    PaperTrade.condition_id,
                    PaperTrade.event_slug,
                    PaperTrade.entry_price,
                    PaperTrade.size,
                ).where(PaperTrade.portfolio_id == p.id, PaperTrade.status == "OPEN")
            ).all()

            total_exposure = Decimal("0")
            by_market: dict[str, Decimal] = {}
            by_event: dict[str, Decimal] = {}
            for row in open_rows:
                notional = row.entry_price * row.size
                total_exposure += notional
                by_market[row.condition_id] = (
                    by_market.get(row.condition_id, Decimal("0")) + notional
                )
                if row.event_slug:
                    by_event[row.event_slug] = by_event.get(row.event_slug, Decimal("0")) + notional

            peak_market_id, peak_market_notional = (
                max(by_market.items(), key=lambda kv: kv[1]) if by_market else (None, Decimal("0"))
            )
            peak_event_slug, peak_event_notional = (
                max(by_event.items(), key=lambda kv: kv[1]) if by_event else (None, Decimal("0"))
            )

            today_pnl, day_start_bankroll = _daily_pnl_state(session, p, today_start)
            daily_stop_threshold = risk_config.daily_stop_loss_pct * day_start_bankroll
            bankroll_floor = risk_config.min_bankroll_pct * p.starting_bankroll

            bars = [
                _floor_bar("Bankroll", p.current_bankroll, bankroll_floor, p.starting_bankroll),
                _daily_pnl_bar(today_pnl, daily_stop_threshold),
                _cap_bar(
                    "Total exposure",
                    total_exposure,
                    risk_config.max_exposure_pct * p.current_bankroll,
                ),
                _cap_bar(
                    "Market peak" + (f" ({peak_market_id[:12]}…)" if peak_market_id else ""),
                    peak_market_notional,
                    risk_config.max_market_exposure_pct * p.current_bankroll,
                ),
                _cap_bar(
                    "Correlated peak" + (f" ({peak_event_slug[:20]}…)" if peak_event_slug else ""),
                    peak_event_notional,
                    risk_config.max_correlated_exposure_pct * p.current_bankroll,
                ),
                _open_count_bar(len(open_rows), risk_config.max_open_positions),
            ]

            states.append(
                RiskGuardrailState(
                    portfolio_id=p.id,
                    name=p.name,
                    min_bankroll_halted=p.current_bankroll < bankroll_floor,
                    daily_stopped=today_pnl <= -daily_stop_threshold,
                    bars=bars,
                )
            )

    return states


def get_risk_events(
    portfolio_id: int | None = None, rule: str | None = None, limit: int = 100
) -> list[RiskEventRow]:
    """The risk_events log, most recent first - every deny/resize
    RiskManager has issued, filterable by portfolio and rule.
    """
    with db_session() as session:
        query = select(RiskEvent, PaperPortfolio.name).join(
            PaperPortfolio, RiskEvent.portfolio_id == PaperPortfolio.id
        )
        if portfolio_id:
            query = query.where(RiskEvent.portfolio_id == portfolio_id)
        if rule:
            query = query.where(RiskEvent.rule == rule)
        rows = session.execute(query.order_by(RiskEvent.occurred_at.desc()).limit(limit)).all()

    return [
        RiskEventRow(
            id=event.id,
            portfolio_name=name,
            rule=event.rule,
            decision=event.decision,
            reason=event.reason,
            occurred_at=event.occurred_at,
        )
        for event, name in rows
    ]


def _band_label(index: int, bands: tuple[tuple[Decimal, Decimal], ...]) -> str:
    lo, hi = bands[index]
    if index == len(bands) - 1:
        return f"{lo}+"
    return f"{lo}-{hi}"


def get_calibration_buckets() -> list[CalibrationBucketRow]:
    """Every calibration bucket with at least one resolved trade in the
    rolling window, with its empirical hit rate, Wilson interval, and
    whether it clears CALIBRATION_MIN_SAMPLES_PER_BUCKET - i.e. exactly what
    a KELLY portfolio's next signal would see from get_p_hat().
    """
    settings = get_effective_settings()
    config = resolve_calibration_config(settings)
    now = datetime.now(UTC)

    with db_session() as session:
        records = load_resolved_trade_records(session, config.window_days, now)

    stats = compute_bucket_stats(records, config)
    rows = [
        CalibrationBucketRow(
            trader_band=_band_label(trader_idx, config.trader_bands),
            score_band=_band_label(score_idx, config.score_bands),
            price_band=_band_label(price_idx, config.price_bands),
            n=bucket.n,
            wins=bucket.wins,
            p_hat=bucket.p_hat,
            ci_low=bucket.ci_low,
            ci_high=bucket.ci_high,
            trusted=bucket.n >= config.min_samples_per_bucket,
        )
        for (trader_idx, score_idx, price_idx), bucket in stats.items()
    ]
    return sorted(rows, key=lambda r: (r.trader_band, r.score_band, r.price_band))


def get_sizer_used_breakdown() -> list[SizerBreakdownRow]:
    """Per-portfolio counts of which sizer actually produced each filled
    trade's size - how often KELLY fired vs fell back to TIERED_FALLBACK.
    """
    with db_session() as session:
        portfolios = (
            session.execute(
                select(PaperPortfolio)
                .where(PaperPortfolio.is_active.is_(True))
                .order_by(PaperPortfolio.id.asc())
            )
            .scalars()
            .all()
        )
        rows = session.execute(
            select(PaperTrade.portfolio_id, PaperTrade.sizer_used, func.count())
            .where(PaperTrade.sizer_used.isnot(None))
            .group_by(PaperTrade.portfolio_id, PaperTrade.sizer_used)
        ).all()

    by_portfolio: dict[int, dict[str, int]] = {}
    for portfolio_id, sizer_used, count in rows:
        by_portfolio.setdefault(portfolio_id, {})[sizer_used] = count

    return [
        SizerBreakdownRow(
            portfolio_id=p.id, portfolio_name=p.name, counts=by_portfolio.get(p.id, {})
        )
        for p in portfolios
    ]

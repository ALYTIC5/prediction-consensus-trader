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
    Position,
    PositionHistory,
    PriceSnapshot,
    RuntimeOverride,
    Signal,
    SignalStatus,
    TraderScore,
    Wallet,
)
from app.db.session import db_session

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

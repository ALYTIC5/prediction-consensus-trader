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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.db.models import (
    Market,
    PaperPortfolio,
    PaperTrade,
    PaperTradeStatus,
    PriceSnapshot,
    Signal,
    SignalStatus,
)
from app.db.session import db_session
from app.paper.fills import FillConfig, FillRequest, MarketSnapshot, compute_fill
from app.paper.sizing import SizingConfig, SizingRequest, size_position

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


class ResolutionOutcome(StrEnum):
    """See docs/PHASE4_DESIGN.md section 5 - the fallback price-inference
    rule and its documented failure mode.
    """

    NOT_RESOLVED = "NOT_RESOLVED"
    WON = "WON"
    LOST = "LOST"
    AMBIGUOUS = "AMBIGUOUS"


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


def detect_resolution(
    closed: bool, latest_price: Decimal | None, threshold: Decimal
) -> ResolutionOutcome:
    """Fallback resolution rule (docs/PHASE4_DESIGN.md section 5, pending
    the doc's own flag 1 - verify whether Gamma exposes an authoritative
    settlement field before this becomes the primary rule instead of a
    fallback). AMBIGUOUS is a real, honest answer, not an error - the
    caller leaves the trade open and logs a warning rather than guess.
    """
    if not closed:
        return ResolutionOutcome.NOT_RESOLVED
    if latest_price is None:
        return ResolutionOutcome.AMBIGUOUS
    if latest_price >= 1 - threshold:
        return ResolutionOutcome.WON
    if latest_price <= threshold:
        return ResolutionOutcome.LOST
    return ResolutionOutcome.AMBIGUOUS


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
    sizing: SizingConfig
    fill: FillConfig
    exits: ExitConfig
    resolution_price_threshold: Decimal


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
    return PortfolioConfig(
        entry_filters=entry_filters,
        sizing=sizing,
        fill=fill,
        exits=exits,
        resolution_price_threshold=settings.paper_resolution_price_threshold,
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


def _run_entries(
    session: Session, portfolio: PaperPortfolio, config: PortfolioConfig, now: datetime
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

    current_exposure = session.execute(
        select(PaperTrade.entry_price, PaperTrade.size).where(
            PaperTrade.portfolio_id == portfolio.id, PaperTrade.status == PaperTradeStatus.OPEN
        )
    ).all()
    exposure = sum((row.entry_price * row.size for row in current_exposure), Decimal("0"))

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

        passed, filter_reason = passes_entry_filters(
            SignalMetrics(
                distinct_traders=signal.distinct_traders,
                weighted_score=signal.weighted_score,
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

        sizing_result = size_position(
            SizingRequest(
                current_bankroll=portfolio.current_bankroll,
                current_exposure=exposure,
                weighted_score=signal.weighted_score,
            ),
            config.sizing,
        )
        if sizing_result.target_notional is None:
            session.add(
                PaperTrade(
                    portfolio_id=portfolio.id,
                    signal_id=signal.id,
                    condition_id=signal.condition_id,
                    asset=signal.asset,
                    outcome=signal.outcome,
                    status=PaperTradeStatus.MISSED,
                    signal_price=signal.average_entry_price,
                    exit_reason=sizing_result.skipped_reason,
                    rejection_reason=RejectionStage.SIZING,
                    exit_at=now,
                )
            )
            lines.append(
                f"paper[{portfolio.name}]: missed signal {signal.id} "
                f"reason={sizing_result.skipped_reason}"
            )
            continue

        snapshots = _load_snapshots(session, market, signal, config.fill)
        fill_result = compute_fill(
            FillRequest(
                signal_price=signal.average_entry_price,
                order_notional=sizing_result.target_notional,
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

        size = sizing_result.target_notional / fill_result.fill_price
        session.add(
            PaperTrade(
                portfolio_id=portfolio.id,
                signal_id=signal.id,
                condition_id=signal.condition_id,
                asset=signal.asset,
                outcome=signal.outcome,
                status=PaperTradeStatus.OPEN,
                signal_price=signal.average_entry_price,
                entry_price=fill_result.fill_price,
                size=size,
                slippage_paid=fill_result.slippage_paid,
                entry_at=now,
            )
        )
        cost = size * fill_result.fill_price
        portfolio.current_bankroll -= cost
        exposure += cost
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
    session: Session, portfolio: PaperPortfolio, settings: Settings, now: datetime
) -> _CycleLog:
    config = resolve_portfolio_config(portfolio.params, settings)

    entry_lines = _run_entries(session, portfolio, config, now)
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


def _run_cycle_sync(settings: Settings, now: datetime) -> list[_CycleLog]:
    with db_session() as session:
        portfolios = (
            session.execute(select(PaperPortfolio).where(PaperPortfolio.is_active.is_(True)))
            .scalars()
            .all()
        )
        return [_run_portfolio_cycle(session, portfolio, settings, now) for portfolio in portfolios]


async def run_cycle(settings: Settings) -> None:
    """Run one full paper-trading cycle: entry, mark-to-market, exit, per
    active portfolio. Runs after the consensus job so it always trades on
    the freshest completed signal-generation cycle (docs/PHASE4_DESIGN.md
    section 9).
    """
    now = datetime.now(UTC)
    results = await asyncio.to_thread(_run_cycle_sync, settings, now)
    for result in results:
        for line in result.lines:
            logger.info(line)

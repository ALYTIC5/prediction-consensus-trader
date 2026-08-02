"""Stage 1: historical screen - see docs/SCOUT_DESIGN.md.

Pure stats core (Wilson lower bound - reused, not reimplemented, from
app/risk/calibration.py - profit factor, ROI, activity rate, weekly-
consistency fraction, zombie detection) separate from a thin DB loader/
writer, same split every pure-core module in this project uses.

Population and zombie guard mirror app/optimization/scoring_category.py's
loader shape (docs/SCOUT_DESIGN.md flag 6): closed Position rows plus
positions still open long past their market's end_date, forced into the
loss side of every stat computed here - not just the win rate the way
Workstream 3 originally scoped it, but profit factor and ROI too (flag 6
extends the zombie guard's reach, not its definition).

Deliberately undecayed (flag 4): this is a discrete full-history pass/fail
screen, not a continuously-decaying score - recency enters through the
separate activity floor and the (not yet implemented) forward-tracking
window, not through decay-weighting history itself.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import (
    Market,
    Position,
    PositionHistory,
    ScoutForwardTrade,
    ScoutStageTransition,
    ScoutValidationWindow,
    TraderPipeline,
    TraderPipelineStage,
)
from app.db.session import db_session
from app.risk.calibration import wilson_interval

logger = logging.getLogger(__name__)

_CONFIDENCE_Z = Decimal("1.96")


@dataclass(frozen=True)
class ScoutTradeRecord:
    """One qualifying position's Stage-1-relevant features - plain values,
    not an ORM row, so the pure functions below stay DB-free. event_at is
    closed_at for a genuinely closed position, or the zombie's forced-loss
    moment (market.end_date + zombie_grace_days) for one still open long
    past its market's end - the moment it was judged a loss, same
    convention Workstream 3's decay clock uses.
    """

    won: bool
    cash_pnl: Decimal
    initial_value: Decimal
    event_at: datetime
    is_zombie: bool = False


@dataclass(frozen=True)
class ScreeningConfig:
    """Every Stage-1 tunable - see the module docstring."""

    min_trades_per_day: Decimal
    activity_lookback_days: int
    min_total_trades: int
    min_wilson_winrate: Decimal
    min_profitable_week_fraction: Decimal
    zombie_grace_days: int
    confidence_z: Decimal = _CONFIDENCE_Z

    @classmethod
    def from_settings(cls, settings: Settings) -> "ScreeningConfig":
        return cls(
            min_trades_per_day=settings.scout_min_trades_per_day,
            activity_lookback_days=settings.scout_activity_lookback_days,
            min_total_trades=settings.scout_min_total_trades,
            min_wilson_winrate=settings.scout_min_wilson_winrate,
            min_profitable_week_fraction=settings.scout_min_profitable_week_fraction,
            zombie_grace_days=settings.scout_zombie_grace_days,
        )


@dataclass(frozen=True)
class ScreeningResult:
    """A wallet's full Stage-1 breakdown - stored verbatim into
    trader_pipeline.metrics so "why did this wallet pass or fail" is
    always answerable from its own row.
    """

    total_trades: int
    zombie_count: int
    wins: int
    wilson_low: Decimal
    wilson_high: Decimal
    profit_factor: Decimal | None
    roi: Decimal | None
    activity_rate: Decimal
    profitable_week_fraction: Decimal
    failing_gates: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return len(self.failing_gates) == 0


@dataclass(frozen=True)
class ScreeningSummary:
    screened: int
    candidates: int
    rejected: int


def is_zombie(
    is_open: bool, market_end_date: datetime | None, now: datetime, grace_days: int
) -> bool:
    """A position still open long past its market's end_date, forced into
    the loss side of every Stage-1 stat (docs/SCOUT_DESIGN.md flag 6) - the
    exploit Workstream 3's zombie guard exists to close: pretending a
    still-open loser doesn't exist inflates every stat that counts it.
    """
    if not is_open or market_end_date is None:
        return False
    return now - market_end_date > timedelta(days=grace_days)


def profit_factor(records: list[ScoutTradeRecord]) -> Decimal | None:
    """sum(winning cash_pnl) / abs(sum(losing cash_pnl)) - None (undefined,
    not infinite) when there are no losses to divide by. A zombie's
    last-observed cash_pnl feeds the loss side regardless of its own sign
    (won is already forced False for every zombie record) - same reasoning
    as the win-rate guard, applied to a dollar-weighted stat instead of a
    binary one.
    """
    win_pnl_sum = sum((r.cash_pnl for r in records if r.won), Decimal(0))
    loss_pnl_sum = sum((r.cash_pnl for r in records if not r.won), Decimal(0))
    if loss_pnl_sum == 0:
        return None
    return win_pnl_sum / abs(loss_pnl_sum)


def roi(records: list[ScoutTradeRecord]) -> Decimal | None:
    """Total PnL / total capital deployed across the same zombie-guarded
    population - None when there's no capital to divide by.
    """
    total_initial = sum((r.initial_value for r in records), Decimal(0))
    if total_initial == 0:
        return None
    total_pnl = sum((r.cash_pnl for r in records), Decimal(0))
    return total_pnl / total_initial


def activity_rate(records: list[ScoutTradeRecord], now: datetime, lookback_days: int) -> Decimal:
    """Resolved-trade count in the trailing lookback_days, divided by the
    window length - a rate, not a raw count, comparable against
    SCOUT_MIN_TRADES_PER_DAY directly. Independent of the full-history
    window every other Stage-1 stat uses (docs/SCOUT_DESIGN.md flag 3).
    """
    window_start = now - timedelta(days=lookback_days)
    count = sum(1 for r in records if r.event_at >= window_start)
    return Decimal(count) / Decimal(lookback_days)


def weekly_profitable_fraction(records: list[ScoutTradeRecord]) -> Decimal:
    """Fraction of ISO calendar weeks with net-positive summed cash_pnl,
    among weeks with at least one qualifying position - a week with zero
    qualifying positions doesn't count toward either side (docs/
    SCOUT_DESIGN.md flag 3: this isn't a penalty for a slow week, only a
    losing one). Empty input scores 0 - nothing to be consistent about yet.
    """
    if not records:
        return Decimal(0)
    pnl_by_week: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for record in records:
        iso_year, iso_week, _ = record.event_at.isocalendar()
        pnl_by_week[(iso_year, iso_week)] += record.cash_pnl

    weeks_with_activity = len(pnl_by_week)
    profitable_weeks = sum(1 for pnl in pnl_by_week.values() if pnl > 0)
    return Decimal(profitable_weeks) / Decimal(weeks_with_activity)


def compute_screening_result(
    records: list[ScoutTradeRecord], now: datetime, config: ScreeningConfig
) -> ScreeningResult:
    """All five Stage-1 gates, evaluated as an AND (docs/SCOUT_DESIGN.md
    flag 5: win rate alone can't prove edge on a probability-priced
    market, hence profit factor/ROI/consistency alongside it) - profit
    factor and ROI are stored for the dashboard but are not gates
    themselves (the design doc names no minimum for either).
    """
    total = len(records)
    wins = sum(1 for r in records if r.won)
    wilson_low, wilson_high = wilson_interval(wins, total, config.confidence_z)
    act_rate = activity_rate(records, now, config.activity_lookback_days)
    week_frac = weekly_profitable_fraction(records)
    zombie_count = sum(1 for r in records if r.is_zombie)

    failing_gates: list[str] = []
    if act_rate < config.min_trades_per_day:
        failing_gates.append("ACTIVITY")
    if total < config.min_total_trades:
        failing_gates.append("TOTAL_TRADES")
    if wilson_low <= config.min_wilson_winrate:
        failing_gates.append("WILSON_WINRATE")
    if week_frac <= config.min_profitable_week_fraction:
        failing_gates.append("WEEKLY_CONSISTENCY")

    return ScreeningResult(
        total_trades=total,
        zombie_count=zombie_count,
        wins=wins,
        wilson_low=wilson_low,
        wilson_high=wilson_high,
        profit_factor=profit_factor(records),
        roi=roi(records),
        activity_rate=act_rate,
        profitable_week_fraction=week_frac,
        failing_gates=tuple(failing_gates),
    )


def _metrics_json(result: ScreeningResult) -> dict:
    """JSONB can't store Decimal directly - stringified, same convention
    app/paper/strategies.py's to_params() already uses for the same reason.
    """
    return {
        "total_trades": result.total_trades,
        "zombie_count": result.zombie_count,
        "wins": result.wins,
        "wilson_low": str(result.wilson_low),
        "wilson_high": str(result.wilson_high),
        "profit_factor": str(result.profit_factor) if result.profit_factor is not None else None,
        "roi": str(result.roi) if result.roi is not None else None,
        "activity_rate": str(result.activity_rate),
        "profitable_week_fraction": str(result.profitable_week_fraction),
        "failing_gates": list(result.failing_gates),
    }


def _screening_reason(result: ScreeningResult) -> str:
    """The dashboard drill-down's "why did this wallet move, and why"
    sentence for a Stage 1 verdict - built once, from the same numbers
    stored in metrics at this exact moment, never recomputed later.
    """
    if result.passed:
        return (
            f"passed historical screen: wilson_low={result.wilson_low:.4f} "
            f"activity_rate={result.activity_rate:.2f}/day "
            f"week_fraction={result.profitable_week_fraction:.2f} "
            f"n={result.total_trades}"
        )
    return f"failed historical screen: gates={list(result.failing_gates)} n={result.total_trades}"


def load_screenable_wallet_ids(session: Session) -> set[int]:
    """Every wallet with at least one position_history row - the Scout's
    real discovery population (docs/SCOUT_DESIGN.md flag 2): every wallet
    ever tracked, past or present, not the full public trader universe -
    position_history only exists for a wallet the positions collector has
    actually polled.
    """
    return set(session.execute(select(PositionHistory.wallet_id).distinct()).scalars().all())


def load_all_wallet_trade_records(
    session: Session, zombie_grace_days: int, now: datetime
) -> dict[int, list[ScoutTradeRecord]]:
    """Every wallet's closed + zombie positions in one bulk load each,
    grouped by wallet_id - mirrors app/optimization/scoring_category.py's
    loader shape. Zombie filtering happens in Python via is_zombie(), not
    in the SQL WHERE clause, so the exact same predicate this module tests
    is the one actually applied.
    """
    closed_rows = session.execute(
        select(
            Position.wallet_id, Position.cash_pnl, Position.initial_value, Position.closed_at
        ).where(Position.is_open.is_(False), Position.closed_at.is_not(None))
    ).all()

    open_rows = session.execute(
        select(Position.wallet_id, Position.cash_pnl, Position.initial_value, Market.end_date)
        .join(Market, Market.condition_id == Position.condition_id)
        .where(Position.is_open.is_(True), Market.end_date.is_not(None))
    ).all()

    records: dict[int, list[ScoutTradeRecord]] = defaultdict(list)
    for row in closed_rows:
        records[row.wallet_id].append(
            ScoutTradeRecord(
                won=row.cash_pnl > 0,
                cash_pnl=row.cash_pnl,
                initial_value=row.initial_value,
                event_at=row.closed_at,
                is_zombie=False,
            )
        )

    grace = timedelta(days=zombie_grace_days)
    for row in open_rows:
        if not is_zombie(True, row.end_date, now, zombie_grace_days):
            continue
        records[row.wallet_id].append(
            ScoutTradeRecord(
                won=False,
                cash_pnl=row.cash_pnl,
                initial_value=row.initial_value,
                event_at=row.end_date + grace,
                is_zombie=True,
            )
        )

    return dict(records)


def run_historical_screen(settings: Settings) -> ScreeningSummary:
    """The full Stage 1 pass: screen every wallet with position history,
    write its stage + full metrics breakdown to trader_pipeline. A wallet
    currently mid-pipeline (CANDIDATE/WATCHLIST/VALIDATED/DECAYING) is
    left untouched - only a never-screened or previously REJECTED wallet
    is (re-)evaluated here (docs/SCOUT_DESIGN.md flag 11: REJECTED is a
    status, not a life sentence; every other stage is governed by Stage
    2/3, not the daily screen).
    """
    config = ScreeningConfig.from_settings(settings)
    now = datetime.now(UTC)

    with db_session() as session:
        wallet_ids = load_screenable_wallet_ids(session)
        records_by_wallet = load_all_wallet_trade_records(session, config.zombie_grace_days, now)
        existing_rows = {
            row.wallet_id: row for row in session.execute(select(TraderPipeline)).scalars().all()
        }

        candidate_count = 0
        rejected_count = 0
        for wallet_id in wallet_ids:
            existing_row = existing_rows.get(wallet_id)
            if existing_row is not None and existing_row.stage != TraderPipelineStage.REJECTED:
                continue

            records = records_by_wallet.get(wallet_id, [])
            result = compute_screening_result(records, now, config)
            stage = TraderPipelineStage.CANDIDATE if result.passed else TraderPipelineStage.REJECTED
            metrics = _metrics_json(result)

            if stage == TraderPipelineStage.CANDIDATE:
                # The anchor Stage 2 uses for both "forward trades start
                # here" and "window 1 starts here" - set fresh every time a
                # wallet (re-)becomes CANDIDATE, never inherited from a
                # prior attempt. A wallet re-entering CANDIDATE after a
                # prior REJECTED verdict gets a clean slate (docs/
                # SCOUT_DESIGN.md flag 11: "no memory of the earlier
                # rejection held against it") - stale forward-tracking data
                # from that earlier attempt is deleted, not left to confuse
                # window/confirmation counting under the new attempt.
                metrics["candidate_since"] = now.isoformat()
                session.execute(
                    delete(ScoutForwardTrade).where(ScoutForwardTrade.wallet_id == wallet_id)
                )
                session.execute(
                    delete(ScoutValidationWindow).where(
                        ScoutValidationWindow.wallet_id == wallet_id
                    )
                )

            from_stage = existing_row.stage if existing_row is not None else None
            if from_stage != stage:
                # Only a genuine transition is logged - a wallet that fails
                # the screen again on a later day (REJECTED -> REJECTED) is
                # a repeated verdict, not a move, and would otherwise spam
                # the drill-down's history with one row per screen run.
                session.add(
                    ScoutStageTransition(
                        wallet_id=wallet_id,
                        from_stage=from_stage,
                        to_stage=stage,
                        reason=_screening_reason(result),
                        transitioned_at=now,
                    )
                )

            if existing_row is None:
                session.add(
                    TraderPipeline(
                        wallet_id=wallet_id,
                        stage=stage,
                        entered_stage_at=now,
                        metrics=metrics,
                        updated_at=now,
                    )
                )
            else:
                existing_row.stage = stage
                existing_row.entered_stage_at = now
                existing_row.metrics = metrics
                existing_row.updated_at = now

            if stage == TraderPipelineStage.CANDIDATE:
                candidate_count += 1
            else:
                rejected_count += 1

    summary = ScreeningSummary(
        screened=len(wallet_ids), candidates=candidate_count, rejected=rejected_count
    )
    logger.info(
        "scout: screened=%d candidates=%d rejected=%d",
        summary.screened,
        summary.candidates,
        summary.rejected,
    )
    return summary


async def run_screening_job() -> None:
    """Scheduler entrypoint - same asyncio.to_thread + get_effective_settings
    pattern every periodic job in this project uses. Not yet wired into a
    running scheduler (app/scout/main.py lands with Stage 2/3).
    """
    settings = get_effective_settings()
    await asyncio.to_thread(run_historical_screen, settings)

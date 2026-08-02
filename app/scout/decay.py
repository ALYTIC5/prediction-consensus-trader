"""Stage 3: decay monitoring - see docs/SCOUT_DESIGN.md.

Reuses app/scout/forward.py's close_ready_windows() unchanged (design flag
9/10: the exact same forward-tracking/window mechanism keeps running for a
VALIDATED wallet, it just now feeds decay detection instead of promotion) -
this module only decides what a freshly-closed window MEANS for a
VALIDATED/DECAYING wallet, using the window's point-estimate avg_forward_clv
against SCOUT_DECAY_THRESHOLD, never the CI bound Stage 2's promotion uses.

Consecutive-streak counting (flag 10): getting INTO DECAYING requires
SCOUT_DECAY_WINDOWS consecutive sub-threshold windows; getting OUT requires
only ONE healthy window (immediate recovery - cheap, since a false
demotion costs little and a wallet that's still actually good shouldn't
sit excluded from the copy list waiting for confirmation). Falling all the
way to REJECTED requires the SAME uninterrupted streak to reach
2 * SCOUT_DECAY_WINDOWS total - "a further SCOUT_DECAY_WINDOWS... decay
confirmed, not just a single bad window twice." Because any healthy window
breaks the streak and recovers immediately, a wallet can never be
DECAYING with a streak shorter than SCOUT_DECAY_WINDOWS - the streak has,
by construction, never been broken since DECAYING started.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import (
    ScoutStageTransition,
    ScoutValidationWindow,
    TraderPipeline,
    TraderPipelineStage,
)
from app.db.session import db_session
from app.scout.forward import ForwardConfig, close_ready_windows
from app.scout.ranking import crowdedness_by_wallet_id, scout_rank_score

logger = logging.getLogger(__name__)


def _transition(
    session: Session,
    pipeline_row: TraderPipeline,
    new_stage: str,
    now: datetime,
    reason: str,
    **extra_metrics: object,
) -> None:
    """Mutates the current-state row AND appends the append-only
    transition-history row the dashboard drill-down reads - see
    app/scout/forward.py's _transition() for the identical shape/reasoning.
    """
    from_stage = pipeline_row.stage
    pipeline_row.stage = new_stage
    pipeline_row.entered_stage_at = now
    pipeline_row.updated_at = now
    pipeline_row.metrics = {**(pipeline_row.metrics or {}), **extra_metrics}
    session.add(
        ScoutStageTransition(
            wallet_id=pipeline_row.wallet_id,
            from_stage=from_stage,
            to_stage=new_stage,
            reason=reason,
            transitioned_at=now,
        )
    )


def _update_metrics(pipeline_row: TraderPipeline, now: datetime, **extra_metrics: object) -> None:
    """Same metrics merge as _transition, without touching stage/
    entered_stage_at - used when a window closes but doesn't itself change
    the wallet's stage (still VALIDATED and healthy, or still DECAYING and
    not yet at the reject threshold).
    """
    pipeline_row.metrics = {**(pipeline_row.metrics or {}), **extra_metrics}
    pipeline_row.updated_at = now


def consecutive_sub_threshold_streak(
    avg_clv_values_most_recent_first: list[Decimal], decay_threshold: Decimal
) -> int:
    """Pure: counts how many windows in a row, starting from the most
    recent, have avg_forward_clv below decay_threshold - stops at the
    first healthy one. See the module docstring: because recovery is
    immediate, this streak is never partially broken while a wallet is
    DECAYING.
    """
    streak = 0
    for avg_clv in avg_clv_values_most_recent_first:
        if avg_clv < decay_threshold:
            streak += 1
        else:
            break
    return streak


def _wallet_sub_threshold_streak(session: Session, wallet_id: int, decay_threshold: Decimal) -> int:
    """Thin DB loader for consecutive_sub_threshold_streak() - a wallet's
    closed windows, most-recent-first.
    """
    windows = list(
        session.execute(
            select(ScoutValidationWindow.avg_forward_clv)
            .where(ScoutValidationWindow.wallet_id == wallet_id)
            .order_by(ScoutValidationWindow.window_ended_at.desc())
        ).scalars()
    )
    return consecutive_sub_threshold_streak(windows, decay_threshold)


def _wilson_low(pipeline_row: TraderPipeline) -> Decimal:
    raw = (pipeline_row.metrics or {}).get("wilson_low", "0")
    return Decimal(raw)


@dataclass(frozen=True)
class DecaySummary:
    demoted: int
    recovered: int
    rejected: int


def run_decay_check(settings: Settings) -> DecaySummary:
    """The full Stage 3 pass: close any ready windows for every VALIDATED/
    DECAYING wallet, then apply decay/recovery/reject. Also refreshes
    scout_rank_score (docs/SCOUT_DESIGN.md's Crowdedness section) on every
    window close, VALIDATED or DECAYING alike - a DECAYING wallet's rank
    score stays visible in trader_pipeline even though it's excluded from
    the copy list itself (a decaying trader isn't hidden, just not
    currently recommended).
    """
    config = ForwardConfig.from_settings(settings)
    now = datetime.now(UTC)

    with db_session() as session:
        tracked = (
            session.execute(
                select(TraderPipeline).where(
                    TraderPipeline.stage.in_(
                        [TraderPipelineStage.VALIDATED, TraderPipelineStage.DECAYING]
                    )
                )
            )
            .scalars()
            .all()
        )
        pipeline_by_wallet = {row.wallet_id: row for row in tracked}
        closed_windows = close_ready_windows(session, config, now, pipeline_by_wallet)
        crowdedness_of = crowdedness_by_wallet_id(session, set(pipeline_by_wallet.keys()))

        demoted = recovered = rejected = 0
        for window in closed_windows:
            pipeline_row = pipeline_by_wallet[window.wallet_id]
            is_healthy = window.avg_forward_clv >= config.decay_threshold
            crowdedness = crowdedness_of.get(window.wallet_id, Decimal(0))
            rank_score = scout_rank_score(
                _wilson_low(pipeline_row),
                window.avg_forward_clv,
                crowdedness,
                settings.scout_crowd_penalty_weight,
            )
            window_metrics = {
                "last_window_avg_clv": str(window.avg_forward_clv),
                "last_window_ci_low": str(window.ci_low),
                "last_window_ci_high": str(window.ci_high),
                "last_window_n": window.forward_trade_count,
                "crowdedness": str(crowdedness),
                "scout_rank_score": str(rank_score),
            }

            if pipeline_row.stage == TraderPipelineStage.VALIDATED:
                if is_healthy:
                    _update_metrics(pipeline_row, now, **window_metrics)
                    continue
                streak = _wallet_sub_threshold_streak(
                    session, window.wallet_id, config.decay_threshold
                )
                if streak >= config.decay_windows:
                    _transition(
                        session,
                        pipeline_row,
                        TraderPipelineStage.DECAYING,
                        now,
                        reason=(
                            f"rolling CLV {window.avg_forward_clv:.4f} < threshold "
                            f"{config.decay_threshold} for {streak} consecutive windows"
                        ),
                        decay_streak=streak,
                        **window_metrics,
                    )
                    demoted += 1
                    logger.info(
                        "scout: wallet %d VALIDATED -> DECAYING (rolling CLV %.4f < threshold "
                        "%.4f, %d consecutive windows)",
                        window.wallet_id,
                        window.avg_forward_clv,
                        config.decay_threshold,
                        streak,
                    )
                else:
                    _update_metrics(pipeline_row, now, decay_streak=streak, **window_metrics)

            else:  # DECAYING
                if is_healthy:
                    _transition(
                        session,
                        pipeline_row,
                        TraderPipelineStage.VALIDATED,
                        now,
                        reason=(
                            f"recovered: forward CLV {window.avg_forward_clv:.4f} >= "
                            f"threshold {config.decay_threshold}"
                        ),
                        decay_streak=0,
                        **window_metrics,
                    )
                    recovered += 1
                    logger.info(
                        "scout: wallet %d DECAYING -> VALIDATED (recovered, forward CLV %.4f)",
                        window.wallet_id,
                        window.avg_forward_clv,
                    )
                    continue
                streak = _wallet_sub_threshold_streak(
                    session, window.wallet_id, config.decay_threshold
                )
                if streak >= 2 * config.decay_windows:
                    _transition(
                        session,
                        pipeline_row,
                        TraderPipelineStage.REJECTED,
                        now,
                        reason=f"persistent decay: {streak} consecutive sub-threshold windows",
                        decay_streak=streak,
                        **window_metrics,
                    )
                    rejected += 1
                    logger.info(
                        "scout: wallet %d DECAYING -> REJECTED (persistent decay, %d "
                        "consecutive sub-threshold windows)",
                        window.wallet_id,
                        streak,
                    )
                else:
                    _update_metrics(pipeline_row, now, decay_streak=streak, **window_metrics)

    summary = DecaySummary(demoted=demoted, recovered=recovered, rejected=rejected)
    logger.info(
        "scout: decay check demoted=%d recovered=%d rejected=%d",
        summary.demoted,
        summary.recovered,
        summary.rejected,
    )
    return summary


async def run_decay_check_job() -> None:
    """Scheduler entrypoint - same asyncio.to_thread + get_effective_settings
    pattern every periodic job in this project uses.
    """
    settings = get_effective_settings()
    await asyncio.to_thread(run_decay_check, settings)

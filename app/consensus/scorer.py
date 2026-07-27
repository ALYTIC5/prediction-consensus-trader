"""Trader scoring orchestration: DB in, scores out, tracked set updated.

Kept separate from app/consensus/scoring.py's pure math on purpose - this
module does the loading/persisting, scoring.py does the arithmetic, so the
arithmetic stays trivially unit-testable without a database.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.consensus.scoring import ScoreBreakdown, ScoreInputs, ScoreWeights, compute_score
from app.db.models import LeaderboardSnapshot, TraderScore, Wallet
from app.db.session import db_session

logger = logging.getLogger(__name__)

_LOG_TOP_N = 10


def run_scoring() -> None:
    """Score every wallet seen in the lookback window and refresh is_tracked.

    Must run after leaderboard_snapshots has been written for this cycle -
    it scores whatever's already in the table, it doesn't fetch anything
    itself. Already runs inside asyncio.to_thread (called from
    app/collectors/leaderboard.py), so it's safe to do its own blocking
    settings fetch here too.
    """
    settings = get_effective_settings()
    weights = ScoreWeights(
        month=settings.score_weight_month,
        all_time=settings.score_weight_all_time,
        consistency=settings.score_weight_consistency,
    )
    captured_at = datetime.now(UTC)
    lookback_start = captured_at - timedelta(days=settings.scoring_lookback_days)

    with db_session() as session:
        score_inputs = _load_score_inputs(session, lookback_start)
        if not score_inputs:
            logger.info("scoring: no leaderboard_snapshots in lookback window, nothing to score")
            return

        breakdowns: dict[int, ScoreBreakdown] = {}
        for wallet_id, inputs in score_inputs.items():
            breakdown = compute_score(inputs, weights)
            breakdowns[wallet_id] = breakdown
            session.add(
                TraderScore(
                    wallet_id=wallet_id,
                    captured_at=captured_at,
                    month_component=breakdown.month_component,
                    all_time_component=breakdown.all_time_component,
                    consistency_component=breakdown.consistency_component,
                    score=breakdown.score,
                )
            )

        ranked = sorted(breakdowns.items(), key=lambda item: item[1].score, reverse=True)
        tracked_ids = {wallet_id for wallet_id, _ in ranked[: settings.tracked_wallets_limit]}

        # Same atomic "clear everyone, then re-mark" pattern the old
        # placeholder used - clear, then set only this cycle's top scorers.
        session.execute(update(Wallet).values(is_tracked=False))
        if tracked_ids:
            session.execute(
                update(Wallet).where(Wallet.id.in_(tracked_ids)).values(is_tracked=True)
            )

        top = ranked[:_LOG_TOP_N]
        addresses = dict(
            session.execute(
                select(Wallet.id, Wallet.address).where(
                    Wallet.id.in_([wallet_id for wallet_id, _ in top])
                )
            ).all()
        )

    logger.info("scoring: scored %d wallets, %d now tracked", len(breakdowns), len(tracked_ids))
    for rank, (wallet_id, breakdown) in enumerate(top, start=1):
        logger.info(
            "scoring: #%d %s score=%.4f (month=%.4f all_time=%.4f consistency=%.4f)",
            rank,
            addresses.get(wallet_id, wallet_id),
            breakdown.score,
            breakdown.month_component,
            breakdown.all_time_component,
            breakdown.consistency_component,
        )


def _load_score_inputs(session: Session, lookback_start: datetime) -> dict[int, ScoreInputs]:
    """Assemble one ScoreInputs per wallet from leaderboard_snapshots."""
    possible_appearances = session.execute(
        select(func.count(func.distinct(LeaderboardSnapshot.captured_at))).where(
            LeaderboardSnapshot.captured_at >= lookback_start
        )
    ).scalar_one()

    appearances = dict(
        session.execute(
            select(
                LeaderboardSnapshot.wallet_id,
                func.count(func.distinct(LeaderboardSnapshot.captured_at)),
            )
            .where(LeaderboardSnapshot.captured_at >= lookback_start)
            .group_by(LeaderboardSnapshot.wallet_id)
        ).all()
    )

    month_pnl = _latest_pnl_by_period(session, lookback_start, "MONTH")
    all_pnl = _latest_pnl_by_period(session, lookback_start, "ALL")

    wallet_ids = set(appearances) | set(month_pnl) | set(all_pnl)
    return {
        wallet_id: ScoreInputs(
            month_pnl=month_pnl.get(wallet_id, Decimal(0)),
            all_pnl=all_pnl.get(wallet_id, Decimal(0)),
            appearances=appearances.get(wallet_id, 0),
            possible_appearances=possible_appearances,
        )
        for wallet_id in wallet_ids
    }


def _latest_pnl_by_period(
    session: Session, lookback_start: datetime, time_period: str
) -> dict[int, Decimal]:
    """Each wallet's most recent pnl reading for one leaderboard time_period."""
    ranked = (
        select(
            LeaderboardSnapshot.wallet_id,
            LeaderboardSnapshot.pnl,
            func.row_number()
            .over(
                partition_by=LeaderboardSnapshot.wallet_id,
                order_by=LeaderboardSnapshot.captured_at.desc(),
            )
            .label("rn"),
        )
        .where(
            LeaderboardSnapshot.time_period == time_period,
            LeaderboardSnapshot.captured_at >= lookback_start,
        )
        .subquery()
    )
    rows = session.execute(select(ranked.c.wallet_id, ranked.c.pnl).where(ranked.c.rn == 1)).all()
    return dict(rows)

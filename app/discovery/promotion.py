"""Niche Stage 1: promote a (wallet, niche) pair to NicheTraderPipeline
CANDIDATE when it clears discovery_min_trades AND discovery_min_wilson_winrate
WITHIN that niche - regardless of the wallet's overall record. The
niche-scoped analogue of app/scout/screening.py's run_historical_screen,
reading from niche_wallet_stats (already aggregated by app/discovery/stats.py)
instead of recomputing from raw positions.

Same "predict-then-verify, only touch never-screened-or-REJECTED rows"
discipline as the global screen (docs/SCOUT_DESIGN.md flag 11): a
(wallet, niche) pair already in CANDIDATE/WATCHLIST/VALIDATED/DECAYING is
left alone here - only app/discovery/forward.py's forward-tracking moves it
from there.

The one real side effect this stage has beyond writing pipeline rows:
flipping Wallet.niche_tracked True the moment a wallet earns its first
niche-CANDIDATE anywhere - that's what makes app/collectors/positions.py
start polling its positions at all, which is what makes forward-tracking
possible for a wallet discovery found outside the leaderboard-seeded
tracked set.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.db.models import (
    NicheTraderPipeline,
    NicheTraderPipelineStage,
    NicheWalletStat,
    Wallet,
)
from app.db.session import db_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromotionSummary:
    evaluated: int
    candidates: int
    rejected: int
    newly_niche_tracked: int


def _load_existing_pipeline(session: Session) -> dict[tuple[int, str], NicheTraderPipeline]:
    rows = session.execute(select(NicheTraderPipeline)).scalars().all()
    return {(row.wallet_id, row.niche): row for row in rows}


def run_niche_promotion(settings: Settings) -> PromotionSummary:
    now = datetime.now(UTC)
    with db_session() as session:
        stats = session.execute(select(NicheWalletStat)).scalars().all()
        existing = _load_existing_pipeline(session)

        candidate_count = rejected_count = 0
        newly_tracked_wallet_ids: set[int] = set()

        for stat in stats:
            key = (stat.wallet_id, stat.niche)
            existing_row = existing.get(key)
            if existing_row is not None and existing_row.stage != NicheTraderPipelineStage.REJECTED:
                continue

            passed = (
                stat.resolved_n >= settings.discovery_min_trades
                and stat.wilson_low > settings.discovery_min_wilson_winrate
            )
            stage = (
                NicheTraderPipelineStage.CANDIDATE if passed else NicheTraderPipelineStage.REJECTED
            )
            metrics = {
                "resolved_n": stat.resolved_n,
                "wins": stat.wins,
                "wilson_low": str(stat.wilson_low),
                "wilson_high": str(stat.wilson_high),
                "roi": str(stat.roi) if stat.roi is not None else None,
            }
            if stage == NicheTraderPipelineStage.CANDIDATE:
                metrics["candidate_since"] = now.isoformat()
                newly_tracked_wallet_ids.add(stat.wallet_id)

            if existing_row is None:
                session.add(
                    NicheTraderPipeline(
                        wallet_id=stat.wallet_id,
                        niche=stat.niche,
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

            if stage == NicheTraderPipelineStage.CANDIDATE:
                candidate_count += 1
            else:
                rejected_count += 1

        newly_tracked = 0
        if newly_tracked_wallet_ids:
            result = session.execute(
                update(Wallet)
                .where(Wallet.id.in_(newly_tracked_wallet_ids), Wallet.niche_tracked.is_(False))
                .values(niche_tracked=True)
            )
            newly_tracked = (
                result.rowcount if result.rowcount is not None and result.rowcount > 0 else 0
            )

    summary = PromotionSummary(
        evaluated=len(stats),
        candidates=candidate_count,
        rejected=rejected_count,
        newly_niche_tracked=newly_tracked,
    )
    logger.info(
        "discovery: niche_promotion evaluated=%d candidates=%d rejected=%d newly_niche_tracked=%d",
        summary.evaluated,
        summary.candidates,
        summary.rejected,
        summary.newly_niche_tracked,
    )
    return summary

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
maintaining Wallet.niche_tracked - what makes app/collectors/positions.py
start polling a wallet at all, which is what makes forward-tracking
possible for a wallet discovery found outside the leaderboard-seeded
tracked set.

HARD CAPPED at discovery_max_niche_tracked_wallets, recomputed from
scratch every run - clear every niche_tracked flag, then set it only for
the current top-N (wilson_low) active (CANDIDATE/WATCHLIST/VALIDATED/
DECAYING) wallets, same pattern app/consensus/scorer.py already uses for
is_tracked. NOT optional: app/collectors/positions.py fires one concurrent
HTTP call per tracked wallet against the SAME global http_max_concurrency
semaphore (default 6) every other job in the process shares. An uncapped
niche_tracked set grew to 2,697 wallets within ~7 hours of this running in
production and starved the markets/coherence jobs into a multi-hour silent
hang (no exception - they just stopped acquiring the semaphore) - caught
live, not hypothetical. A wallet outside the cap keeps its pipeline row
and its already-recorded history; it simply stops being polled for new
positions until it re-earns a top-N spot.
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
    niche_tracked: int


def _load_existing_pipeline(session: Session) -> dict[tuple[int, str], NicheTraderPipeline]:
    rows = session.execute(select(NicheTraderPipeline)).scalars().all()
    return {(row.wallet_id, row.niche): row for row in rows}


def run_niche_promotion(settings: Settings) -> PromotionSummary:
    now = datetime.now(UTC)
    with db_session() as session:
        stats = session.execute(select(NicheWalletStat)).scalars().all()
        existing = _load_existing_pipeline(session)

        candidate_count = rejected_count = 0

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

        # Best wilson_low across each wallet's active niches - a wallet is
        # tracked at all (positions polled) or not, regardless of how many
        # niches it's active in; only forward.py's OPENED-event filtering
        # actually cares which niche a given trade belongs to.
        active_rows = session.execute(
            select(NicheTraderPipeline.wallet_id, NicheWalletStat.wilson_low)
            .join(
                NicheWalletStat,
                (NicheWalletStat.wallet_id == NicheTraderPipeline.wallet_id)
                & (NicheWalletStat.niche == NicheTraderPipeline.niche),
            )
            .where(NicheTraderPipeline.stage != NicheTraderPipelineStage.REJECTED)
        ).all()
        best_wilson_low: dict[int, object] = {}
        for wallet_id, wilson_low in active_rows:
            current = best_wilson_low.get(wallet_id)
            if current is None or wilson_low > current:
                best_wilson_low[wallet_id] = wilson_low

        ranked = sorted(best_wilson_low.items(), key=lambda item: item[1], reverse=True)
        top_wallet_ids = {
            wallet_id for wallet_id, _ in ranked[: settings.discovery_max_niche_tracked_wallets]
        }

        session.execute(update(Wallet).values(niche_tracked=False))
        niche_tracked = 0
        if top_wallet_ids:
            result = session.execute(
                update(Wallet).where(Wallet.id.in_(top_wallet_ids)).values(niche_tracked=True)
            )
            niche_tracked = result.rowcount if result.rowcount is not None else 0

    summary = PromotionSummary(
        evaluated=len(stats),
        candidates=candidate_count,
        rejected=rejected_count,
        niche_tracked=niche_tracked,
    )
    logger.info(
        "discovery: niche_promotion evaluated=%d candidates=%d rejected=%d niche_tracked=%d",
        summary.evaluated,
        summary.candidates,
        summary.rejected,
        summary.niche_tracked,
    )
    return summary

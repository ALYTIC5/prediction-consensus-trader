"""Market-niche-tagging walk: classify every resolved market we already
know about (see docs/discovery: PART 1's finding that resolution is now
authoritative, app/collectors/resolutions.py) into a niche.

Deliberately walks the LOCAL markets/market_resolutions tables, not Gamma's
market listing - the resolutions backfill already gave this project a
complete local inventory of resolved markets (60,028+ as of 2026-08-24), so
re-enumerating them from Gamma would be pure waste. The only new API calls
this stage makes are the tag lookups (get_markets_by_condition_ids,
include_tag=true - the exact same call app/collectors/markets.py already
makes and discards tags from).

Checkpointed by condition_id ordering (DiscoverySweepCheckpoint,
stage="niche_tagging"), not by "already in discovery_market_niches" - a
market that gets no niche match still needs to count as processed, or the
walk would re-fetch its tags every single cycle forever.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.polymarket import PolymarketClient
from app.collectors.schemas import GammaMarket
from app.config.settings import Settings
from app.db.models import DiscoveryMarketNiche, DiscoverySweepCheckpoint, Market, MarketResolution
from app.db.session import db_session
from app.discovery.niches import classify_market_niche

logger = logging.getLogger(__name__)

_STAGE = "niche_tagging"


@dataclass(frozen=True)
class WalkSummary:
    fetched: int
    classified: int
    checkpoint_advanced_to: str | None


def _load_checkpoint(session: Session) -> str | None:
    row = session.get(DiscoverySweepCheckpoint, _STAGE)
    return row.last_condition_id if row is not None else None


def _load_next_batch(session: Session, after: str | None, limit: int) -> list[str]:
    """Every resolved market's condition_id past the checkpoint cursor,
    ascending - the same resolved-market population V0.1's resolution
    pipeline already populates market_resolutions for.
    """
    stmt = (
        select(Market.condition_id)
        .join(MarketResolution, MarketResolution.condition_id == Market.condition_id)
        .order_by(Market.condition_id.asc())
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(Market.condition_id > after)
    return list(session.execute(stmt).scalars().all())


def _non_hidden_slugs(market: GammaMarket) -> list[str]:
    return [tag.slug for tag in market.tags if not tag.force_hide and tag.slug is not None]


def _upsert_niche(session: Session, market: GammaMarket, now: datetime) -> bool:
    slugs = _non_hidden_slugs(market)
    generic_sports = "sports" in slugs or "esports" in slugs
    match = classify_market_niche(slugs, market.question, generic_sports)
    if match is None:
        return False
    stmt = (
        pg_insert(DiscoveryMarketNiche)
        .values(
            condition_id=market.condition_id,
            niche=match.niche,
            match_method=match.method.value,
            matched_on=match.matched_on,
            computed_at=now,
        )
        .on_conflict_do_nothing(index_elements=[DiscoveryMarketNiche.condition_id])
    )
    session.execute(stmt)
    return True


def _save_checkpoint(
    session: Session, last_condition_id: str, batch_size: int, now: datetime
) -> None:
    stmt = (
        pg_insert(DiscoverySweepCheckpoint)
        .values(
            stage=_STAGE,
            last_condition_id=last_condition_id,
            markets_processed=batch_size,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[DiscoverySweepCheckpoint.stage],
            set_={
                "last_condition_id": last_condition_id,
                "markets_processed": DiscoverySweepCheckpoint.markets_processed + batch_size,
                "updated_at": now,
            },
        )
    )
    session.execute(stmt)


async def run_niche_tagging_walk(client: PolymarketClient, settings: Settings) -> WalkSummary:
    """One bounded pass: fetch up to discovery_walk_batch_size condition_ids
    past the checkpoint, classify each, advance the checkpoint. Bounded per
    call, same "cap it, let a scheduled job drain the backlog over several
    cycles" shape the resolutions backfill and the CLV horizon-fill both
    already use for the identical durability reason.
    """
    now = datetime.now(UTC)
    with db_session() as session:
        after = _load_checkpoint(session)
        condition_ids = _load_next_batch(session, after, settings.discovery_walk_batch_size)

    if not condition_ids:
        return WalkSummary(fetched=0, classified=0, checkpoint_advanced_to=after)

    markets = await client.get_markets_by_condition_ids(condition_ids)
    markets_by_condition_id = {m.condition_id: m for m in markets}

    classified = 0
    with db_session() as session:
        for condition_id in condition_ids:
            market = markets_by_condition_id.get(condition_id)
            if market is not None and _upsert_niche(session, market, now):
                classified += 1
        last_id = condition_ids[-1]
        _save_checkpoint(session, last_id, len(condition_ids), now)

    logger.info(
        "discovery: niche_tagging fetched=%d classified=%d checkpoint=%s",
        len(condition_ids),
        classified,
        last_id,
    )
    return WalkSummary(
        fetched=len(condition_ids), classified=classified, checkpoint_advanced_to=last_id
    )


async def run_niche_tagging_job(client: PolymarketClient, settings: Settings) -> None:
    await run_niche_tagging_walk(client, settings)

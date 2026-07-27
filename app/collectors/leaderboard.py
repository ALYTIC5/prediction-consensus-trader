"""Leaderboard collector: fetch top traders per period, persist to DB."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.polymarket import PolymarketClient
from app.collectors.schemas import LeaderboardEntry
from app.config.settings import Settings
from app.consensus.scorer import run_scoring
from app.db.models import LeaderboardSnapshot, Wallet
from app.db.session import db_session

logger = logging.getLogger(__name__)


async def collect(client: PolymarketClient, settings: Settings) -> None:
    """Fetch top traders for every configured period, persist, then score.

    Collection and judgment are deliberately separate steps: this function
    only fetches and stores what the API said; app/consensus/scorer.py
    decides who's actually worth tracking, based on the scores it computes
    from what just got stored.
    """
    captured_at = datetime.now(UTC)
    entries_by_period: dict[str, list[LeaderboardEntry]] = {}

    for period in settings.leaderboard_periods:
        entries = await client.get_top_traders(
            period,
            category=settings.leaderboard_category,
            order_by="PNL",
            top_n=settings.leaderboard_top_n,
        )
        logger.info("leaderboard: fetched %d traders for period=%s", len(entries), period)
        entries_by_period[period] = entries

    stored_count = await asyncio.to_thread(_persist, entries_by_period, captured_at, settings)
    logger.info("leaderboard: stored %d snapshot rows", stored_count)

    await asyncio.to_thread(run_scoring)


def _persist(
    entries_by_period: dict[str, list[LeaderboardEntry]],
    captured_at: datetime,
    settings: Settings,
) -> int:
    """Blocking DB work: upsert wallets, insert snapshots."""
    stored = 0
    with db_session() as session:
        for period, entries in entries_by_period.items():
            for entry in entries:
                wallet_id = _upsert_wallet(session, entry, captured_at)
                session.add(
                    LeaderboardSnapshot(
                        wallet_id=wallet_id,
                        captured_at=captured_at,
                        category=settings.leaderboard_category,
                        time_period=period,
                        order_by="PNL",
                        rank=entry.rank,
                        pnl=entry.pnl,
                        volume=entry.vol,
                    )
                )
                stored += 1
    return stored


def _upsert_wallet(session: Session, entry: LeaderboardEntry, captured_at: datetime) -> int:
    """UPSERT the wallet row, returning its id.

    An upsert (INSERT ... ON CONFLICT DO UPDATE) does the existence check and
    the write in a single atomic statement at the database level. A
    select-then-insert from application code is a race: two collectors (or
    two periods in the same cycle) can both see "no row yet" for the same
    address and both try to insert, and the second one crashes on the
    unique constraint. The upsert can't race with itself.
    """
    stmt = (
        pg_insert(Wallet)
        .values(
            address=entry.proxy_wallet,
            username=entry.user_name,
            x_username=entry.x_username,
            verified_badge=entry.verified_badge,
            last_seen_on_leaderboard_at=captured_at,
        )
        .on_conflict_do_update(
            index_elements=[Wallet.address],
            set_={
                "username": entry.user_name,
                "x_username": entry.x_username,
                "verified_badge": entry.verified_badge,
                "last_seen_on_leaderboard_at": captured_at,
            },
        )
        .returning(Wallet.id)
    )
    return session.execute(stmt).scalar_one()

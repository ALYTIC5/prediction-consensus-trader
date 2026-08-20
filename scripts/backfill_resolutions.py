"""One-off backfill: run the resolutions collector (app/collectors/
resolutions.py) repeatedly until every expired market has either a
market_resolutions row or is confirmed still-open/ambiguous.

For manual/on-demand use only - the 24/7 loop (app/main.py) already runs
this collector on its own interval; this just drains a large existing
backlog faster by looping calls back-to-back instead of waiting out
resolutions_interval_seconds between them, same one-off role
scripts/collect_once.py plays for the other collectors.
"""

import asyncio
import logging

from sqlalchemy import func, select

from app.collectors.polymarket import PolymarketClient
from app.collectors.resolutions import collect as collect_resolutions
from app.config.settings import get_settings
from app.db.models import Market, MarketResolution
from app.db.session import db_session
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)


def _remaining_backlog() -> int:
    with db_session() as session:
        already_resolved = select(MarketResolution.condition_id)
        return session.execute(
            select(func.count(Market.condition_id)).where(
                Market.closed.is_(False),
                Market.end_date.is_not(None),
                Market.end_date < func.now(),
                Market.condition_id.not_in(already_resolved),
            )
        ).scalar_one()


def _resolved_count() -> int:
    with db_session() as session:
        return session.execute(select(func.count(MarketResolution.condition_id))).scalar_one()


async def _run() -> None:
    settings = get_settings()
    client = PolymarketClient()
    start_backlog = await asyncio.to_thread(_remaining_backlog)
    start_resolved = await asyncio.to_thread(_resolved_count)
    logger.info(
        "backfill: starting - %d markets pending resolution check, %d already resolved",
        start_backlog,
        start_resolved,
    )
    try:
        pass_number = 0
        while True:
            pass_number += 1
            before = await asyncio.to_thread(_remaining_backlog)
            if before == 0:
                break
            await collect_resolutions(client, settings)
            after = await asyncio.to_thread(_remaining_backlog)
            resolved_now = await asyncio.to_thread(_resolved_count)
            logger.info(
                "backfill: pass %d done - backlog %d -> %d, total resolved=%d",
                pass_number,
                before,
                after,
                resolved_now,
            )
            if after == before:
                # A full pass made no progress at all (every remaining market
                # is genuinely still open, e.g. waiting on a real-world
                # outcome) - stop looping instead of hammering the same set
                # forever; the ordinary scheduled job will keep re-checking
                # these on its own interval.
                logger.info(
                    "backfill: no progress this pass - remaining %d markets are still-open "
                    "or ambiguous, stopping (the scheduled resolutions job will keep "
                    "re-checking them)",
                    after,
                )
                break
    finally:
        await client.aclose()

    final_backlog = await asyncio.to_thread(_remaining_backlog)
    final_resolved = await asyncio.to_thread(_resolved_count)
    logger.info(
        "backfill: finished - %d resolved total (+%d this run), %d still pending",
        final_resolved,
        final_resolved - start_resolved,
        final_backlog,
    )


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir, settings.log_to_file)
    asyncio.run(_run())


if __name__ == "__main__":
    main()

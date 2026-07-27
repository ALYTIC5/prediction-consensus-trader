"""Run one or more collectors exactly once, then exit.

For manual runs / cron / debugging - the 24/7 loop lives in app/main.py;
this runs the same collectors on demand instead of on a schedule.
"""

import argparse
import asyncio
import logging

from app.collectors.leaderboard import collect as collect_leaderboard
from app.collectors.markets import collect as collect_markets
from app.collectors.polymarket import PolymarketClient
from app.collectors.positions import collect as collect_positions
from app.config.settings import get_settings
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="run every collector")
    parser.add_argument("--leaderboard", action="store_true", help="run the leaderboard collector")
    parser.add_argument("--positions", action="store_true", help="run the positions collector")
    parser.add_argument("--markets", action="store_true", help="run the markets collector")
    args = parser.parse_args()

    if not (args.all or args.leaderboard or args.positions or args.markets):
        parser.error("nothing to do - pass --all, --leaderboard, --positions, and/or --markets")

    return args


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    client = PolymarketClient()
    try:
        # Fixed order regardless of flag order: leaderboard must run before
        # positions (tracked wallets must exist first), and markets last
        # (its work list is derived from open positions/markets).
        if args.all or args.leaderboard:
            logger.info("collect_once: running leaderboard")
            await collect_leaderboard(client, settings)
        if args.all or args.positions:
            logger.info("collect_once: running positions")
            await collect_positions(client, settings)
        if args.all or args.markets:
            logger.info("collect_once: running markets")
            await collect_markets(client, settings)
    finally:
        await client.aclose()


def main() -> None:
    args = _parse_args()
    setup_logging(get_settings().log_level)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

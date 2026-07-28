"""Service entry point: run all collectors forever until interrupted."""

import asyncio
import contextlib
import logging
import signal

from app.collectors.leaderboard import collect as collect_leaderboard
from app.collectors.markets import collect as collect_markets
from app.collectors.polymarket import PolymarketClient
from app.collectors.positions import collect as collect_positions
from app.config.settings import get_settings
from app.paper.engine import run_cycle as run_paper_cycle
from app.scheduler.runner import PeriodicJob, run_jobs
from app.signals.generator import generate as generate_signals
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)


async def _run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir, settings.log_to_file)

    client = PolymarketClient()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows' default event loop - Ctrl+C there
        # surfaces as KeyboardInterrupt instead, caught in main() below.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    try:
        # Tracked wallets must exist before the first position sweep, so run
        # the leaderboard collector once, synchronously, before scheduling.
        logger.info("startup: running leaderboard collector once before scheduling")
        await collect_leaderboard(client, settings)

        jobs = [
            PeriodicJob(
                name="leaderboard",
                run=lambda: collect_leaderboard(client, settings),
                interval_seconds=settings.leaderboard_interval_seconds,
            ),
            PeriodicJob(
                name="positions",
                run=lambda: collect_positions(client, settings),
                interval_seconds=settings.positions_interval_seconds,
            ),
            PeriodicJob(
                name="markets",
                run=lambda: collect_markets(client, settings),
                interval_seconds=settings.markets_interval_seconds,
            ),
            PeriodicJob(
                name="consensus",
                run=generate_signals,
                interval_seconds=settings.consensus_interval_seconds,
            ),
            PeriodicJob(
                name="paper",
                run=lambda: run_paper_cycle(settings),
                interval_seconds=settings.paper_interval_seconds,
            ),
        ]
        await run_jobs(jobs, stop_event)
    finally:
        await client.aclose()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # Fallback for platforms (e.g. Windows) where add_signal_handler
        # isn't available and Ctrl+C surfaces as KeyboardInterrupt instead.
        logger.info("interrupted, shutting down")


if __name__ == "__main__":
    main()

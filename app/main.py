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
from app.optimization.bandit import run_bandit_job
from app.optimization.clustering import run_clustering_job
from app.optimization.clv import run_clv_job
from app.optimization.crowdedness import run_crowdedness_job
from app.optimization.scoring_category import run_category_scoring_job
from app.paper.engine import run_cycle as run_paper_cycle
from app.scheduler.runner import PeriodicJob, run_jobs
from app.signals.generator import generate as generate_signals
from app.utils.logging import setup_logging
from app.utils.pruning import run_pruner_job

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
                run=run_paper_cycle,
                interval_seconds=settings.paper_interval_seconds,
            ),
            PeriodicJob(
                name="clustering",
                run=run_clustering_job,
                interval_seconds=settings.cluster_recompute_interval_hours * 3600,
            ),
            PeriodicJob(
                name="category_scoring",
                run=run_category_scoring_job,
                interval_seconds=settings.category_scoring_interval_seconds,
            ),
            PeriodicJob(
                name="crowdedness",
                run=run_crowdedness_job,
                interval_seconds=settings.crowdedness_recompute_interval_hours * 3600,
            ),
            PeriodicJob(
                name="clv",
                run=run_clv_job,
                interval_seconds=settings.clv_update_interval_seconds,
            ),
            PeriodicJob(
                name="bandit",
                run=run_bandit_job,
                interval_seconds=settings.adaptive_update_interval_seconds,
            ),
            PeriodicJob(
                name="pruner",
                run=run_pruner_job,
                interval_seconds=86400,
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

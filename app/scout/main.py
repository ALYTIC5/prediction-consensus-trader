"""The Scout: standalone trader-discovery service entry point.

Deployed as its OWN Railway service (docs/SCOUT_DESIGN.md flag 1) - never
run inside app.main's scheduler, same "a slowdown here must never affect
data collection" separation app.dashboard.main already has from
app.main. Makes zero external HTTP calls (no PolymarketClient - unlike
app/main.py, the Scout is a pure downstream consumer of data the
collectors already gathered): every job here only ever touches Postgres,
through app.db.session.db_session exactly like every other module in this
project - no Scout-specific database configuration exists, so it reads
DATABASE_URL from the identical Settings/env source collectors and
dashboard already use.

Start command (Railway custom start command for this service):
    uv run python -m app.scout.main
"""

import asyncio
import contextlib
import logging
import signal

from app.config.settings import get_settings
from app.db.revision import check_schema_current
from app.db.session import get_engine
from app.scheduler.runner import PeriodicJob, run_jobs
from app.scout.decay import run_decay_check_job
from app.scout.forward import run_forward_tracking_job
from app.scout.screening import run_screening_job
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)


async def _run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir, settings.log_to_file)

    # Fail the deploy, not every job tick forever - see app/db/revision.py.
    check_schema_current("scout", get_engine())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows' default event loop - Ctrl+C there
        # surfaces as KeyboardInterrupt instead, caught in main() below.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    jobs = [
        # Stage 1: full-history screen. Daily - see docs/SCOUT_DESIGN.md.
        PeriodicJob(
            name="scout_historical_screen",
            run=run_screening_job,
            interval_seconds=settings.scout_screen_interval_hours * 3600,
        ),
        # Stage 2: forward tracking + promotion. Hourly.
        PeriodicJob(
            name="scout_forward_tracking",
            run=run_forward_tracking_job,
            interval_seconds=settings.scout_forward_tracking_interval_seconds,
        ),
        # Stage 3: decay monitoring. Daily, on its own schedule
        # (scout_decay_check_interval_hours) - see that setting's comment
        # in app/config/settings.py for why this diverges from the design
        # doc's original "same cadence as Stage 2" language.
        PeriodicJob(
            name="scout_decay_check",
            run=run_decay_check_job,
            interval_seconds=settings.scout_decay_check_interval_hours * 3600,
        ),
    ]
    await run_jobs(jobs, stop_event, service="scout")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # Fallback for platforms (e.g. Windows) where add_signal_handler
        # isn't available and Ctrl+C surfaces as KeyboardInterrupt instead.
        logger.info("interrupted, shutting down")


if __name__ == "__main__":
    main()

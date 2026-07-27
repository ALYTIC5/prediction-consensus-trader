"""Hand-rolled async scheduler for periodic collector jobs.

Deliberately not a scheduling library (APScheduler etc.) - every job's
behavior (jitter, drift correction, shutdown responsiveness, error handling)
needs to be visible in a few dozen lines and testable without mocking a
framework's internals.
"""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PeriodicJob:
    """One job to run on a fixed interval, forever, until stop_event fires."""

    name: str
    # A callable, not a coroutine object - coroutines can only be awaited
    # once, so this must return a *fresh* one on every call.
    run: Callable[[], Awaitable[None]]
    interval_seconds: float
    jitter_fraction: float = 0.05


async def _run_job_loop(job: PeriodicJob, stop_event: asyncio.Event) -> None:
    """Run one job forever: run, sleep (interval - elapsed + jitter), repeat.

    Sleeping is done by waiting on stop_event with a timeout, not
    asyncio.sleep, so a shutdown request wakes the loop immediately instead
    of waiting out the rest of the interval.
    """
    logger.info("scheduler: starting %s every %.0fs", job.name, job.interval_seconds)
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            await job.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler: %s raised, will retry next interval", job.name)
        elapsed = time.monotonic() - started

        jitter = job.interval_seconds * job.jitter_fraction * random.uniform(-1, 1)
        sleep_for = max(0.0, job.interval_seconds - elapsed + jitter)

        # TimeoutError means the interval elapsed normally - loop again.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
    logger.info("scheduler: stopping %s", job.name)


async def run_jobs(jobs: list[PeriodicJob], stop_event: asyncio.Event) -> None:
    """Run every job concurrently until stop_event is set."""
    schedule = ", ".join(f"{job.name}@{job.interval_seconds:.0f}s" for job in jobs)
    logger.info("scheduler: running jobs: %s", schedule)
    await asyncio.gather(*(_run_job_loop(job, stop_event) for job in jobs))

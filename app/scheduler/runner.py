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

from app.scheduler.heartbeat import record_failure, record_success

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


async def _run_job_loop(job: PeriodicJob, stop_event: asyncio.Event, service: str) -> None:
    """Run one job forever: run, sleep (interval - elapsed + jitter), repeat.

    Sleeping is done by waiting on stop_event with a timeout, not
    asyncio.sleep, so a shutdown request wakes the loop immediately instead
    of waiting out the rest of the interval.

    Every run's outcome is recorded to job_heartbeats (record_success /
    record_failure) regardless of success or failure - this is the single
    choke point every job in every service passes through, so instrumenting
    it here covers all of them with no per-job changes. Run off the event
    loop via asyncio.to_thread since these are blocking DB calls (CLAUDE.md:
    blocking DB work called from async code goes through asyncio.to_thread).
    """
    logger.info("scheduler: starting %s every %.0fs", job.name, job.interval_seconds)
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            await job.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("scheduler: %s raised, will retry next interval", job.name)
            await asyncio.to_thread(record_failure, service, job.name, job.interval_seconds, exc)
        else:
            await asyncio.to_thread(record_success, service, job.name, job.interval_seconds)
        elapsed = time.monotonic() - started

        jitter = job.interval_seconds * job.jitter_fraction * random.uniform(-1, 1)
        sleep_for = max(0.0, job.interval_seconds - elapsed + jitter)

        # TimeoutError means the interval elapsed normally - loop again.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
    logger.info("scheduler: stopping %s", job.name)


async def run_jobs(jobs: list[PeriodicJob], stop_event: asyncio.Event, service: str) -> None:
    """Run every job concurrently until stop_event is set."""
    schedule = ", ".join(f"{job.name}@{job.interval_seconds:.0f}s" for job in jobs)
    logger.info("scheduler: running jobs: %s", schedule)
    await asyncio.gather(*(_run_job_loop(job, stop_event, service) for job in jobs))

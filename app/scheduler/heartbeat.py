"""Heartbeat writes for app.scheduler.runner - one row per (service, job)
in job_heartbeats, upserted on every run so a job's status is always exactly
"what happened last time this ran", never a growing log.

Split out of runner.py rather than inlined there so the runner's own tests
can monkeypatch record_success/record_failure without touching a database -
see runner.py's docstring on why that module stays framework-light and
DB-free itself.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import JobHeartbeat
from app.db.session import db_session

logger = logging.getLogger(__name__)

# Full tracebacks go to the log via logger.exception; this is only the
# summary stored alongside the heartbeat row, so a wall of stack frames
# never bloats job_heartbeats or the /health page it feeds.
_MAX_ERROR_LENGTH = 2000


def _error_summary(exc: Exception) -> str:
    summary = f"{type(exc).__name__}: {exc}"
    return summary[:_MAX_ERROR_LENGTH]


def _upsert(
    *,
    service: str,
    job_name: str,
    interval_seconds: float,
    status: str,
    last_success_at: datetime | None,
    last_failure_at: datetime | None,
    last_error: str | None,
) -> None:
    now = datetime.now(UTC)
    stmt = (
        pg_insert(JobHeartbeat)
        .values(
            service=service,
            job_name=job_name,
            interval_seconds=int(interval_seconds),
            last_status=status,
            last_run_at=now,
            last_success_at=last_success_at,
            last_failure_at=last_failure_at,
            last_error=last_error,
        )
        .on_conflict_do_update(
            index_elements=[JobHeartbeat.service, JobHeartbeat.job_name],
            set_={
                "interval_seconds": int(interval_seconds),
                "last_status": status,
                "last_run_at": now,
                # A success must not erase a still-relevant last_failure_at/
                # last_error (the /health page shows both), and a failure
                # must not erase the last time this job actually worked -
                # coalesce keeps whichever of the two branches didn't just
                # happen at its previous value.
                "last_success_at": last_success_at
                if last_success_at is not None
                else JobHeartbeat.last_success_at,
                "last_failure_at": last_failure_at
                if last_failure_at is not None
                else JobHeartbeat.last_failure_at,
                "last_error": last_error if status == "failed" else JobHeartbeat.last_error,
            },
        )
    )
    with db_session() as session:
        session.execute(stmt)


def record_success(service: str, job_name: str, interval_seconds: float) -> None:
    """Called by the scheduler runner after every successful job.run().

    Never raises: a heartbeat write failing must not be able to bring down
    the job loop that's calling it - a missing write just ages the row past
    staleness, which /healthz already catches.
    """
    try:
        _upsert(
            service=service,
            job_name=job_name,
            interval_seconds=interval_seconds,
            status="ok",
            last_success_at=datetime.now(UTC),
            last_failure_at=None,
            last_error=None,
        )
    except Exception:
        logger.exception("heartbeat: failed to record success for %s/%s", service, job_name)


def record_failure(service: str, job_name: str, interval_seconds: float, exc: Exception) -> None:
    """Called by the scheduler runner after every job.run() that raised."""
    try:
        _upsert(
            service=service,
            job_name=job_name,
            interval_seconds=interval_seconds,
            status="failed",
            last_success_at=None,
            last_failure_at=datetime.now(UTC),
            last_error=_error_summary(exc),
        )
    except Exception:
        logger.exception("heartbeat: failed to record failure for %s/%s", service, job_name)

"""System-level tables not tied to any collector or trading concept."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class AppState(Base):
    """Generic key/value store.

    Why this exists: collectors need a durable place to persist checkpoints
    (e.g. "last processed trade id") between runs, and a trivial table like
    this also serves as the smoke test that Alembic migrations actually
    reach the database end to end.
    """

    __tablename__ = "app_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class JobHeartbeat(Base):
    """One scheduled job's last-run status, written by every job in every
    service through app.scheduler.runner's single job loop - no per-job
    instrumentation needed, and no job can be silently dead without this
    row aging past 3x its own interval_seconds (see the Scout outage this
    was built for: 10 days of UndefinedTable errors on every tick, retried
    forever, with nothing anywhere able to tell "failing every cycle" apart
    from "idle").

    Current-state, upserted in place on (service, job_name) - same shape as
    TraderPipeline (app/db/models/scout.py) and for the same reason: only
    the latest status is ever queried, so there is nothing to prune and no
    max() scan needed to serve /healthz or the job-health page.

    interval_seconds is copied from the job's own PeriodicJob at write
    time, not looked up separately at query time - staleness is always
    computed against the value actually running, and a job whose interval
    changes in settings self-corrects on its very next successful tick.
    """

    __tablename__ = "job_heartbeats"

    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    last_status: Mapped[str] = mapped_column(String(16))
    last_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

"""Dashboard-facing tables: consensus run history and runtime tuning overrides."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class ConsensusRun(Base):
    """One row per signal-generation cycle - the funnel counts from that run.

    See docs/PHASE8_DESIGN.md: backs the Overview/Signals funnel cascade and
    its history strip. Written by app/signals/generator.py in the same
    transaction as everything else that cycle, alongside its existing
    funnel-line log message, not instead of it.
    """

    __tablename__ = "consensus_runs"
    __table_args__ = (Index("ix_consensus_runs_executed_at", "executed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    events: Mapped[int] = mapped_column()
    groups: Mapped[int] = mapped_column()
    market: Mapped[int] = mapped_column()
    liquidity: Mapped[int] = mapped_column()
    price: Mapped[int] = mapped_column()
    spread: Mapped[int] = mapped_column()
    breadth: Mapped[int] = mapped_column()
    quality: Mapped[int] = mapped_column()
    conviction: Mapped[int] = mapped_column()
    new_signals: Mapped[int] = mapped_column()
    reinforced: Mapped[int] = mapped_column()


class RuntimeOverride(Base):
    """Current value of one tunable setting - one row per key, overwritten
    in place on every change.

    Validated against app/config/adjustable.py's registry before being
    written by the (future) tuning POST handler; app/config/effective.py
    re-validates on every read, so a stale or corrupted row can never take
    down a scoring or generation cycle - it's just skipped and logged.
    """

    __tablename__ = "runtime_overrides"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OverrideAudit(Base):
    """Append-only log of every runtime_overrides change (apply or reset) -
    the Tuning page's audit trail.

    runtime_overrides itself only holds the current value per key
    (overwritten in place), so it structurally cannot answer "what changed,
    when" once a key has been touched twice - this table exists specifically
    to make every change traceable. old_value is None on a key's first-ever
    apply; new_value is None on a reset (the override was removed).
    """

    __tablename__ = "override_audit"
    __table_args__ = (Index("ix_override_audit_changed_at", "changed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(String(120))
    new_value: Mapped[str | None] = mapped_column(String(120))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

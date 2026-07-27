"""Consensus signals: what the engine detected, and its full evidence."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class SignalStatus(StrEnum):
    """Lifecycle states for a signal row."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"


class Signal(Base):
    """One consensus signal.

    Self-contained by design: every metric plus a full contributors list, so
    a signal is explainable from its own row alone without re-querying
    position_history (see CLAUDE.md Code conventions).

    status is a plain String(10), not a native Postgres enum - same reason
    as position_history.event_type in Phase 2: adding a status value later
    is an application change, not a migration.
    """

    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_condition_id_asset_status", "condition_id", "asset", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(66))
    asset: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(500))
    event_slug: Mapped[str] = mapped_column(String(300))
    # Always "BUY" today - a real column, not assumed, so a sell-side/
    # short-consensus concept later needs no migration.
    side: Mapped[str] = mapped_column(String(10), default="BUY")
    status: Mapped[str] = mapped_column(String(10), default=SignalStatus.ACTIVE)

    distinct_traders: Mapped[int] = mapped_column()
    weighted_score: Mapped[Decimal] = mapped_column(Money)
    combined_entry_value: Mapped[Decimal] = mapped_column(Money)
    average_entry_price: Mapped[Decimal] = mapped_column(Money)
    latest_market_price: Mapped[Decimal] = mapped_column(Money)

    # List of {address, username, weight, event_type, acted_size,
    # entry_price, detected_at} per contributing event.
    contributors: Mapped[list] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Fixed at creation - created_at + SIGNAL_TTL_HOURS - and never extended
    # by reinforcement (see docs/PHASE3_DESIGN.md flag 7).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

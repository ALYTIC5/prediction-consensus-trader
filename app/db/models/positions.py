"""Wallet positions: current state, event log, and event-type vocabulary."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class PositionEventType(StrEnum):
    """Kinds of change PositionHistory records for one wallet+asset."""

    OPENED = "OPENED"
    INCREASED = "INCREASED"
    DECREASED = "DECREASED"
    CLOSED = "CLOSED"


class Position(Base):
    """Current state of one wallet's stake in one outcome token.

    (wallet_id, asset) is the natural key every sync upserts against - a
    wallet holds at most one live row per outcome token at a time.
    """

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("wallet_id", "asset"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    asset: Mapped[str] = mapped_column(String(80))
    condition_id: Mapped[str] = mapped_column(String(66))
    outcome: Mapped[str] = mapped_column(String(100))
    outcome_index: Mapped[int] = mapped_column()
    size: Mapped[Decimal] = mapped_column(Money)
    avg_price: Mapped[Decimal] = mapped_column(Money)
    initial_value: Mapped[Decimal] = mapped_column(Money)
    current_value: Mapped[Decimal] = mapped_column(Money)
    cur_price: Mapped[Decimal] = mapped_column(Money)
    cash_pnl: Mapped[Decimal] = mapped_column(Money)
    percent_pnl: Mapped[Decimal] = mapped_column(Money)
    redeemable: Mapped[bool] = mapped_column(Boolean, default=False)
    neg_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(500))
    event_slug: Mapped[str] = mapped_column(String(300))
    # Flipped to False (with closed_at set) instead of deleting the row, so
    # history joins against this table never dangle.
    is_open: Mapped[bool] = mapped_column(Boolean, index=True, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PositionHistory(Base):
    """Append-only log of every detected change to a wallet's position.

    is_bootstrap matters: on a wallet's first ever sweep, every position it
    already holds looks like a brand-new OPENED event to us, even though the
    position itself may be months old. Rows from that first sweep are
    flagged is_bootstrap=True so phase 3 can filter them out of anything
    measuring reaction speed or entry timing - they're backfill, not signal.
    """

    __tablename__ = "position_history"
    __table_args__ = (
        Index("ix_position_history_condition_id_detected_at", "condition_id", "detected_at"),
        Index("ix_position_history_event_type_detected_at", "event_type", "detected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    asset: Mapped[str] = mapped_column(String(80))
    condition_id: Mapped[str] = mapped_column(String(66))
    # Plain String, not a PostgreSQL enum type - adding a new event type
    # later is an application-level change only, no migration required.
    event_type: Mapped[str] = mapped_column(String(10))
    is_bootstrap: Mapped[bool] = mapped_column(Boolean, default=False)
    size_before: Mapped[Decimal | None] = mapped_column(Money)
    size_after: Mapped[Decimal] = mapped_column(Money)
    avg_price: Mapped[Decimal] = mapped_column(Money)
    cur_price: Mapped[Decimal] = mapped_column(Money)
    outcome: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(500))
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

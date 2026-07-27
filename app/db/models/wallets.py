"""Wallets tracked for consensus signals, and their leaderboard history."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class Wallet(Base):
    """Current state of a wallet we know about, tracked or not.

    Rows are created the first time a wallet appears on a leaderboard page;
    is_tracked gates which wallets collectors actually poll for positions.
    """

    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Always stored lowercase - normalized by the collector layer
    # (see app/collectors/schemas.py) before it ever reaches the DB.
    address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(200))
    x_username: Mapped[str | None] = mapped_column(String(200))
    verified_badge: Mapped[bool] = mapped_column(Boolean, default=False)
    # Only tracked wallets get polled for positions - most wallets we see on
    # a leaderboard page are recorded for reference only, never followed.
    is_tracked: Mapped[bool] = mapped_column(Boolean, index=True, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_on_leaderboard_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeaderboardSnapshot(Base):
    """One leaderboard row at one point in time - append-only, never updated.

    Repeated snapshots of the same wallet/category/time_period let phase 3
    measure consistency over time (reliably top-100, or a one-off spike?)
    instead of only ever seeing a trader's latest rank.
    """

    __tablename__ = "leaderboard_snapshots"
    __table_args__ = (
        Index("ix_leaderboard_snapshots_time_period_captured_at", "time_period", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, server_default=func.now()
    )
    category: Mapped[str] = mapped_column(String(32))
    time_period: Mapped[str] = mapped_column(String(8))
    order_by: Mapped[str] = mapped_column(String(8))
    rank: Mapped[int | None] = mapped_column()
    pnl: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[Decimal] = mapped_column(Money)

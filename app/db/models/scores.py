"""Trader scoring history - one row per wallet per scoring cycle."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class TraderScore(Base):
    """One wallet's score at one scoring cycle - append-only, never updated.

    See docs/PHASE3_DESIGN.md section 1: score blends recent pnl, all-time
    pnl, and leaderboard-appearance consistency into a single [0, 1] weight
    used as that wallet's vote strength in the consensus engine.
    """

    __tablename__ = "trader_scores"
    __table_args__ = (Index("ix_trader_scores_wallet_id_captured_at", "wallet_id", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    month_component: Mapped[Decimal] = mapped_column(Money)
    all_time_component: Mapped[Decimal] = mapped_column(Money)
    consistency_component: Mapped[Decimal] = mapped_column(Money)
    score: Mapped[Decimal] = mapped_column(Money)

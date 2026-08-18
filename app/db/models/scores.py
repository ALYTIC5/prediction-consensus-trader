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


class WalletMarketMakerScore(Base):
    """One wallet's market-maker score at one scoring cycle - append-only,
    same "history, never updated in place" convention as TraderScore.

    See app/optimization/market_maker.py for the full detection design.
    score is in [0, 1] via a noisy-OR of the three components - high means
    "this wallet's positions look like inventory artifacts of providing
    liquidity, not directional views," and app/signals/generator.py uses
    the latest row per wallet to down-weight (or exclude) its contribution
    to consensus.
    """

    __tablename__ = "wallet_market_maker_scores"
    __table_args__ = (
        Index("ix_wallet_market_maker_scores_wallet_id_computed_at", "wallet_id", "computed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    holding_period_component: Mapped[Decimal] = mapped_column(Money)
    both_sides_component: Mapped[Decimal] = mapped_column(Money)
    breadth_depth_component: Mapped[Decimal] = mapped_column(Money)
    score: Mapped[Decimal] = mapped_column(Money)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

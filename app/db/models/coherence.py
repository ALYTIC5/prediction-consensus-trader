"""Coherence arbitrage: provable-mispricing detection and its paper fills.

See docs/COHERENCE_DESIGN.md for the full scan-type/data-model rationale.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class CoherenceOpportunityType(StrEnum):
    YES_NO_SUM_ASK = "YES_NO_SUM_ASK"  # actionable - buy both legs
    YES_NO_SUM_BID = "YES_NO_SUM_BID"  # detect-only - would need shorting
    MULTI_OUTCOME_ASK = "MULTI_OUTCOME_ASK"  # actionable - buy the full set
    MULTI_OUTCOME_BID = "MULTI_OUTCOME_BID"  # detect-only
    NESTED_LOGIC = "NESTED_LOGIC"  # detect-only - pairing heuristic is weak


class CoherenceOpportunity(Base):
    """One persisting coherence violation - one row per opportunity_key,
    updated across scan cycles while still detected, not one row per scan.

    opportunity_key is a stable identity (type + sorted leg condition_ids,
    hashed) so a scan that finds the same violation again UPDATES
    last_seen_at instead of inserting a duplicate - this is what makes
    duration (resolved_at - detected_at) meaningful rather than always
    reading as "one scan cycle".
    """

    __tablename__ = "coherence_opportunities"
    __table_args__ = (
        Index("ix_coherence_opportunities_resolved_at", "resolved_at"),
        Index("ix_coherence_opportunities_type_detected_at", "type", "detected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(20))
    # [{"condition_id": ..., "asset": ..., "outcome": ..., "side": "ask"|"bid"}, ...]
    legs: Mapped[list[dict]] = mapped_column(JSONB)
    gross_spread: Mapped[Decimal] = mapped_column(Money)
    # Shares walked per leg at most recent detection - the binding
    # constraint (smallest fillable size across every leg). None means no
    # size was fillable on at least one leg (NO_LIQUIDITY).
    size: Mapped[Decimal | None] = mapped_column(Money)
    net_profit: Mapped[Decimal | None] = mapped_column(Money)
    required_capital: Mapped[Decimal | None] = mapped_column(Money)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured: Mapped[bool] = mapped_column(Boolean, default=False)


class CoherenceFillStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class CoherenceFill(Base):
    """One leg of one captured coherence opportunity - the coherence
    portfolio's paper-trading record. Deliberately not a paper_trades row:
    every paper_trades row requires a NOT NULL signal_id (joined as an
    INNER JOIN by calibration/Brier elsewhere), and a coherence leg has no
    underlying Signal - a dedicated table avoids weakening that invariant
    for every other consumer of paper_trades. See
    docs/COHERENCE_DESIGN.md's data model section.
    """

    __tablename__ = "coherence_fills"
    __table_args__ = (Index("ix_coherence_fills_portfolio_id_status", "portfolio_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("coherence_opportunities.id"))
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id"))
    condition_id: Mapped[str] = mapped_column(String(66))
    asset: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10))

    entry_price: Mapped[Decimal] = mapped_column(Money)
    size: Mapped[Decimal] = mapped_column(Money)
    fee_paid: Mapped[Decimal] = mapped_column(Money)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    exit_price: Mapped[Decimal | None] = mapped_column(Money)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

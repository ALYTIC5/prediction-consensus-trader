"""The Scout: standalone trader-discovery service - see docs/SCOUT_DESIGN.md."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class TraderPipelineStage(StrEnum):
    """A wallet's current position in the Scout's predict-then-verify
    pipeline - see docs/SCOUT_DESIGN.md's pipeline diagram. Stored as a
    plain String, not a native Postgres enum (same reasoning
    position_history.event_type and signals.status already establish in
    this project): a new stage later is an application change, not a
    migration.
    """

    CANDIDATE = "CANDIDATE"
    WATCHLIST = "WATCHLIST"
    VALIDATED = "VALIDATED"
    DECAYING = "DECAYING"
    REJECTED = "REJECTED"


class TraderPipeline(Base):
    """One wallet's current stage in the Scout pipeline - current-state,
    not append-only (design flag 9): one row per wallet, upserted in place
    by whichever stage's logic currently owns that wallet (Stage 1 for
    CANDIDATE/REJECTED, Stage 2 for WATCHLIST, Stage 3 for VALIDATED/
    DECAYING - see docs/SCOUT_DESIGN.md flag 11 on why the daily Stage 1
    screen never overwrites a wallet mid-pipeline).

    metrics is the full computed breakdown behind the CURRENT stage
    decision (Wilson bound, profit factor, ROI, activity rate, weekly
    consistency, which gates failed if any) - "why did this wallet pass or
    fail" must always be answerable from this row alone, same
    "explainable from its own row" convention every other signal/score in
    this project already follows.
    """

    __tablename__ = "trader_pipeline"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), index=True)
    entered_stage_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScoutForwardTrade(Base):
    """One CANDIDATE/WATCHLIST/VALIDATED wallet's forward-tracked trade -
    the wallet-scoped analogue of SignalCLV (docs/SCOUT_DESIGN.md flag 7/9):
    same clv_value()/resolve_horizon_clv()/resolve_resolution_clv() pure
    formula (app/optimization/clv.py), but entry_price is the wallet's own
    real PositionHistory.avg_price at its actual OPENED event, never a
    simulated/delayed fill the way SignalCLV's is - the Scout is grading a
    wallet's real skill, not simulating a trade of its own.

    Append-only, one row per forward-tracked position opened from the
    moment a wallet enters CANDIDATE onward (Stage 2, not yet implemented -
    this table's schema lands now per the design doc so nothing downstream
    is ever a hardcoded value). Filled in over up to two passes, same
    "computed once time/resolution actually happen" shape SignalCLV uses:
    price_at_horizon/clv_horizon once SCOUT_CLV_HORIZON_HOURS has elapsed,
    price_at_resolution/clv_resolution once the market resolves
    unambiguously.
    """

    __tablename__ = "scout_forward_trades"
    __table_args__ = (Index("ix_scout_forward_trades_wallet_id_entry_at", "wallet_id", "entry_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    condition_id: Mapped[str] = mapped_column(String(66))
    asset: Mapped[str] = mapped_column(String(80))
    entry_price: Mapped[Decimal] = mapped_column(Money)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price_at_horizon: Mapped[Decimal | None] = mapped_column(Money)
    clv_horizon: Mapped[Decimal | None] = mapped_column(Money)
    price_at_resolution: Mapped[Decimal | None] = mapped_column(Money)
    clv_resolution: Mapped[Decimal | None] = mapped_column(Money)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

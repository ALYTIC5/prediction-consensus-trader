"""The Scout: standalone trader-discovery service - see docs/SCOUT_DESIGN.md."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
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
    moment a wallet enters CANDIDATE onward (Stage 2). Filled in over up to
    two passes, same "computed once time/resolution actually happen" shape
    SignalCLV uses: price_at_horizon/clv_horizon once
    SCOUT_CLV_HORIZON_HOURS has elapsed, price_at_resolution/clv_resolution
    once the market resolves unambiguously.

    position_history_id is the exact dedup key against the OPENED event
    that produced this row (unique) - a timestamp-based dedup (wallet_id +
    condition_id + asset + entry_at) would be fragile if two OPENED events
    ever landed at the identical instant; this is not.
    """

    __tablename__ = "scout_forward_trades"
    __table_args__ = (Index("ix_scout_forward_trades_wallet_id_entry_at", "wallet_id", "entry_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    position_history_id: Mapped[int] = mapped_column(ForeignKey("position_history.id"), unique=True)
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


class ScoutValidationWindow(Base):
    """One completed forward-tracking window's outcome - append-only, one
    row per wallet per closed window (docs/SCOUT_DESIGN.md flag 9: needed
    to count SCOUT_VALIDATION_CONFIRMATIONS/SCOUT_DECAY_WINDOWS consecutive
    passes, which trader_pipeline's current-state row alone can't hold).

    A window closes once it has run at least SCOUT_VALIDATION_DAYS AND
    accumulated at least SCOUT_MIN_FORWARD_TRADES forward trades with a
    known clv_horizon - never on a thin sample. passed = ci_low > 0 is
    Stage 2's promotion criterion specifically; Stage 3's decay check reads
    avg_forward_clv against SCOUT_DECAY_THRESHOLD directly and ignores
    passed (design flag 10: promotion needs a CI bound, decay reacts to
    the point estimate - the same window mechanism serves both).
    """

    __tablename__ = "scout_validation_windows"
    __table_args__ = (
        Index(
            "ix_scout_validation_windows_wallet_id_window_ended_at", "wallet_id", "window_ended_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    forward_trade_count: Mapped[int] = mapped_column()
    avg_forward_clv: Mapped[Decimal] = mapped_column(Money)
    ci_low: Mapped[Decimal] = mapped_column(Money)
    ci_high: Mapped[Decimal] = mapped_column(Money)
    passed: Mapped[bool] = mapped_column()
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScoutStageTransition(Base):
    """Append-only log of every stage change a wallet has ever made -
    trader_pipeline itself is current-state only (flag 9), so this is the
    only place "when did this wallet move, and why" is answerable from,
    exactly what the dashboard's wallet drill-down needs. from_stage is
    null for a wallet's very first CANDIDATE entry (nothing to transition
    from). reason is a short, human-readable sentence built from the same
    numbers already stored in trader_pipeline.metrics at the moment of the
    transition (e.g. "forward CLV window passed: avg=0.0312 CI_low=0.0104
    n=52") - written once, never recomputed, so it stays accurate even
    after metrics has since moved on.
    """

    __tablename__ = "scout_stage_transitions"
    __table_args__ = (
        Index(
            "ix_scout_stage_transitions_wallet_id_transitioned_at", "wallet_id", "transitioned_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    from_stage: Mapped[str | None] = mapped_column(String(16))
    to_stage: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

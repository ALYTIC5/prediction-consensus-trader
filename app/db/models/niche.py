"""Niche-aware wallet discovery tables - see app/discovery/*.py.

Parallel to, and deliberately independent of, the existing Scout pipeline
(app/db/models/scout.py): a niche-candidate wallet may never appear in
TraderPipeline at all (it can be mediocre overall and still be a genuine
niche specialist), so none of these tables reuse or modify Scout's schema -
this is "alongside", not "instead of", per the user's explicit instruction.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class DiscoveryMarketNiche(Base):
    """One resolved market's niche classification - see
    app/discovery/niches.py's classify_market_niche(). One row per
    condition_id, computed once and never recomputed (a market's tags and
    title don't change after it resolves), which makes the niche-tagging
    walk (app/discovery/walk.py) trivially resumable: a market already
    present here is simply skipped.
    """

    __tablename__ = "discovery_market_niches"

    condition_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    niche: Mapped[str] = mapped_column(String(32), index=True)
    match_method: Mapped[str] = mapped_column(String(16))
    matched_on: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DiscoverySweepCheckpoint(Base):
    """Resumable cursor for one sweep stage - one row per named stage
    (e.g. "niche_tagging", "trade_grading"), current-state not append-only.
    Exists so a killed/restarted sweep resumes from where it left off
    instead of re-walking everything already done - the exact durability
    gap the earlier background-task kills exposed (see the resolutions
    backfill's own per-cycle-cap fix for the same class of problem).
    """

    __tablename__ = "discovery_sweep_checkpoints"

    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_condition_id: Mapped[str | None] = mapped_column(String(66))
    markets_processed: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NicheTradeGrade(Base):
    """One historical trade, graded win/loss against its market's real
    resolution - the market-walk's raw material for a wallet's per-niche
    Wilson-low, sourced from GET /trades (Data API), not from
    PositionHistory (a niche-candidate wallet has none of that yet - it was
    never in the tracked/leaderboard set that populates PositionHistory,
    that's the whole point of walking markets instead).

    A deliberate simplification, documented rather than silently assumed:
    each qualifying trade is graded as its own single-leg bet (won if
    outcome_index matches the market's winning_outcome_index, sized at
    size*price staked), not netted against other trades the same wallet
    made in the same market - full cost-basis reconstruction across
    multiple fills needs a real position ledger, which by definition
    doesn't exist yet for a wallet discovery hasn't started tracking. This
    is the necessarily coarser proxy the market-walk approach trades for
    not needing a seed wallet list at all.

    Unique on (wallet_id, transaction_hash, asset): the natural dedup key a
    single trade fill has on-chain - re-sweeping the same market's trades
    is a no-op upsert, never a duplicate count.
    """

    __tablename__ = "niche_trade_grades"
    __table_args__ = (
        Index(
            "ix_niche_trade_grades_wallet_id_niche_wallet_tx_asset",
            "wallet_id",
            "transaction_hash",
            "asset",
            unique=True,
        ),
        Index("ix_niche_trade_grades_wallet_id_niche", "wallet_id", "niche"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    niche: Mapped[str] = mapped_column(String(32))
    condition_id: Mapped[str] = mapped_column(String(66))
    asset: Mapped[str] = mapped_column(String(80))
    outcome_index: Mapped[int] = mapped_column(Integer)
    won: Mapped[bool] = mapped_column()
    stake: Mapped[Decimal] = mapped_column(Money)
    price: Mapped[Decimal] = mapped_column(Money)
    transaction_hash: Mapped[str] = mapped_column(String(80))
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NicheWalletStat(Base):
    """One (wallet, niche)'s aggregate Stage-1-equivalent stats, recomputed
    from niche_trade_grades - current-state, one row per (wallet_id,
    niche), same "recompute, don't incrementally mutate" shape
    app/discovery/stats.py uses for the same idempotency reasons
    app/scout/screening.py's daily re-screen already establishes.
    """

    __tablename__ = "niche_wallet_stats"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), primary_key=True)
    niche: Mapped[str] = mapped_column(String(32), primary_key=True)
    resolved_n: Mapped[int] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer)
    wilson_low: Mapped[Decimal] = mapped_column(Money)
    wilson_high: Mapped[Decimal] = mapped_column(Money)
    roi: Mapped[Decimal | None] = mapped_column(Money)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NicheTraderPipelineStage(StrEnum):
    """Same four live stages as TraderPipelineStage (app/db/models/scout.py)
    - kept as a separate enum, not a shared import, so the two pipelines'
    stage vocabularies can diverge later without one file constraining the
    other.
    """

    CANDIDATE = "CANDIDATE"
    WATCHLIST = "WATCHLIST"
    VALIDATED = "VALIDATED"
    DECAYING = "DECAYING"
    REJECTED = "REJECTED"


class NicheTraderPipeline(Base):
    """One (wallet, niche)'s current stage - the niche-scoped analogue of
    TraderPipeline. Composite PK, not a wallet_id-only PK: the same wallet
    can be a CANDIDATE in one niche and REJECTED (or absent entirely) in
    another - that's the entire point of niche-scoping (docs the user's own
    "swisstony's MMA picks, not swisstony overall" framing).
    """

    __tablename__ = "niche_trader_pipeline"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), primary_key=True)
    niche: Mapped[str] = mapped_column(String(32), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), index=True)
    entered_stage_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NicheForwardTrade(Base):
    """Forward-tracked trade for a (wallet, niche) CANDIDATE/WATCHLIST -
    the niche-scoped analogue of ScoutForwardTrade, same reused
    clv_value()/resolve_horizon_clv()/resolve_resolution_clv() pure formula
    (app/optimization/clv.py). Sourced from PositionHistory exactly like
    ScoutForwardTrade is - once a wallet enters NicheTraderPipeline as
    CANDIDATE, Wallet.niche_tracked flips True (app/discovery/promotion.py),
    which is what makes the positions collector start polling it at all
    (app/collectors/positions.py), only from that moment forward.

    Unique on position_history_id, same reasoning as ScoutForwardTrade: one
    OPENED event belongs to exactly one market, which classifies to at most
    one niche, so no dedup ambiguity across niches is possible.
    """

    __tablename__ = "niche_forward_trades"
    __table_args__ = (
        Index("ix_niche_forward_trades_wallet_id_niche_entry_at", "wallet_id", "niche", "entry_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    niche: Mapped[str] = mapped_column(String(32))
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


class NicheValidationWindow(Base):
    """One completed (wallet, niche) forward-tracking window - the
    niche-scoped analogue of ScoutValidationWindow, identical shape and
    identical window-closing mechanism (app/discovery/forward.py mirrors
    app/scout/forward.py's close_ready_windows exactly, filtered by niche).
    """

    __tablename__ = "niche_validation_windows"
    __table_args__ = (
        Index(
            "ix_niche_validation_windows_wallet_id_niche_window_ended_at",
            "wallet_id",
            "niche",
            "window_ended_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    niche: Mapped[str] = mapped_column(String(32))
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    forward_trade_count: Mapped[int] = mapped_column()
    effective_forward_trade_count: Mapped[int | None] = mapped_column()
    avg_forward_clv: Mapped[Decimal] = mapped_column(Money)
    ci_low: Mapped[Decimal] = mapped_column(Money)
    ci_high: Mapped[Decimal] = mapped_column(Money)
    passed: Mapped[bool] = mapped_column()
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

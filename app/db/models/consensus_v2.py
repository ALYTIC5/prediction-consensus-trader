"""Consensus V2: whale-implied probability signals and their paper fills.

See docs/CONSENSUS_V2_DESIGN.md for the full design and why this is a
parallel signal source, not an extension of app/db/models/signals.py.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class SignalProb(Base):
    """One whale-consensus-probability reading that cleared the
    divergence+confidence filter for one binary market - the consensus_v2
    analogue of a `signals` row, but state-driven (current aggregate
    position) rather than event-driven, so this table is intentionally
    separate (docs/CONSENSUS_V2_DESIGN.md's flagged decision #1).

    p_consensus/p_market/confidence/n_clusters are frozen at signal
    creation time - never recomputed in place, same "a row is
    explainable from itself" rule every other signal-shaped table in
    this project follows. brier_consensus/brier_market/paired_diff are
    filled in once, at resolution (same "computed once time/resolution
    actually happen" convention as SignalCLV).
    """

    __tablename__ = "signal_prob"
    __table_args__ = (
        Index("ix_signal_prob_condition_id_created_at", "condition_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(66))
    asset: Mapped[str] = mapped_column(String(80))  # the scored outcome token
    outcome: Mapped[str] = mapped_column(String(100))
    event_slug: Mapped[str | None] = mapped_column(String(300))
    p_consensus: Mapped[Decimal] = mapped_column(Money)
    p_market: Mapped[Decimal] = mapped_column(Money)
    divergence: Mapped[Decimal] = mapped_column(Money)
    confidence: Mapped[Decimal] = mapped_column(Money)
    n_clusters: Mapped[int] = mapped_column()
    total_weight_usd: Mapped[Decimal] = mapped_column(Money)
    liquidity: Mapped[Decimal | None] = mapped_column(Money)
    spread: Mapped[Decimal | None] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Filled once, at resolution - see docs/CONSENSUS_V2_DESIGN.md section 4.
    outcome_value: Mapped[Decimal | None] = mapped_column(Money)
    brier_consensus: Mapped[Decimal | None] = mapped_column(Money)
    brier_market: Mapped[Decimal | None] = mapped_column(Money)
    paired_diff: Mapped[Decimal | None] = mapped_column(Money)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SignalProbTradeStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class SignalProbTrade(Base):
    """The consensus_prob portfolio's own paper trade - not a paper_trades
    row: that table's signal_id is a NOT NULL FK to `signals`, joined as
    an INNER JOIN by calibration/Brier elsewhere in this project, and
    those joins are keyed on the COUNT-based engine's own cluster-count
    bucketing - a consensus_v2 trade genuinely doesn't belong in that
    join (it isn't scored by cluster count at all), so excluding it via a
    dedicated table is correct, not just a workaround. Same pattern
    app/db/models/coherence.py's CoherenceFill already establishes for
    the identical reason.
    """

    __tablename__ = "signal_prob_trades"
    __table_args__ = (Index("ix_signal_prob_trades_portfolio_id_status", "portfolio_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id"))
    signal_prob_id: Mapped[int] = mapped_column(ForeignKey("signal_prob.id"))
    condition_id: Mapped[str] = mapped_column(String(66))
    asset: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10))

    entry_price: Mapped[Decimal] = mapped_column(Money)
    size: Mapped[Decimal] = mapped_column(Money)
    fee_paid: Mapped[Decimal] = mapped_column(Money)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    current_price: Mapped[Decimal | None] = mapped_column(Money)
    exit_price: Mapped[Decimal | None] = mapped_column(Money)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money)
    exit_reason: Mapped[str | None] = mapped_column(String(20))
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

"""Paper trading: named portfolio configurations and their simulated trades.

See docs/PHASE4_DESIGN.md. Every portfolio sees the same signal stream and
trades it under its own params - that comparability is the point of this
schema, not an afterthought (design section 1).
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class PaperTradeStatus(StrEnum):
    """Lifecycle states for a paper_trades row."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MISSED = "MISSED"


class PaperPortfolio(Base):
    """One named paper-trading configuration - the unit of comparison.

    params is a JSONB dict of paper_* setting overrides (see
    docs/PHASE4_DESIGN.md section 10) - only the keys a portfolio wants to
    diverge from the global default need to appear here. Three rows are
    seeded at creation (scripts/seed_portfolios.py): baseline, strict,
    conservative.
    """

    __tablename__ = "paper_portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    params: Mapped[dict] = mapped_column(JSONB)
    starting_bankroll: Mapped[Decimal] = mapped_column(Money)
    current_bankroll: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PaperTrade(Base):
    """One portfolio's decision on one signal - a fill, a miss, or a skip.

    Exactly one row per (portfolio_id, signal_id) ever (docs/PHASE4_DESIGN.md
    flag 2) - the row IS the decision, and its absence means "not yet
    decided," so no separate decided-flag is needed. signal_price is always
    populated (known the moment the signal is seen); entry/exit fields are
    populated only once the trade actually reaches that stage - a MISSED row
    never gets entry_price/size, a still-OPEN row never gets exit_price/
    realized_pnl. exit_reason is dual-purpose: on MISSED it's the fill/sizing
    rejection reason, on CLOSED it's the exit trigger.
    """

    __tablename__ = "paper_trades"
    __table_args__ = (Index("ix_paper_trades_portfolio_id_status", "portfolio_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id"))
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    condition_id: Mapped[str] = mapped_column(String(66))
    asset: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(100))
    # Plain String, not a native Postgres enum - same reasoning as
    # position_history.event_type and signals.status: a new status value
    # later is an application change, not a migration.
    status: Mapped[str] = mapped_column(String(10))

    size: Mapped[Decimal | None] = mapped_column(Money)
    entry_price: Mapped[Decimal | None] = mapped_column(Money)
    signal_price: Mapped[Decimal] = mapped_column(Money)
    slippage_paid: Mapped[Decimal | None] = mapped_column(Money)
    entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    current_price: Mapped[Decimal | None] = mapped_column(Money)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Money)

    exit_price: Mapped[Decimal | None] = mapped_column(Money)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money)
    exit_reason: Mapped[str | None] = mapped_column(String(64))
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Categorises why a MISSED trade was rejected: ENTRY_FILTER, SIZING, or
    # FILL. NULL for OPEN/CLOSED trades. Populated alongside exit_reason
    # which carries the specific reason within that category.
    rejection_reason: Mapped[str | None] = mapped_column(String(32))

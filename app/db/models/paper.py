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
    # BOOK_WALK when a real order_books snapshot was available at fill time
    # (app/paper/fills.py's walk_the_book), ESTIMATED when it fell back to
    # ask+slippage because no snapshot existed - null for MISSED/OPEN rows
    # that never reached a fill decision. Never inferred after the fact:
    # written once, at the moment compute_fill() (or its book-walk caller)
    # actually decides, so "was this fill real or estimated" is always
    # answerable from the row itself.
    fill_method: Mapped[str | None] = mapped_column(String(10))

    current_price: Mapped[Decimal | None] = mapped_column(Money)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Money)

    exit_price: Mapped[Decimal | None] = mapped_column(Money)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money)
    exit_reason: Mapped[str | None] = mapped_column(String(64))
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Cumulative Polymarket taker fee actually deducted from this trade:
    # the entry-fill fee, plus an exit-fill fee for every exit EXCEPT
    # market_resolved (redemption isn't a taker trade - see
    # app/paper/fees.py's docstring), added on close. realized_pnl above is
    # already NET of this. NULL means "predates fee modelling" (a trade
    # closed before this column existed) - never backfilled with a guessed
    # rate, so analysis can filter fee_paid IS NOT NULL to exclude those
    # rows rather than trust a fabricated historical number.
    fee_paid: Mapped[Decimal | None] = mapped_column(Money)

    # Categorises why a MISSED trade was rejected: ENTRY_FILTER, SIZING, or
    # FILL. NULL for OPEN/CLOSED trades. Populated alongside exit_reason
    # which carries the specific reason within that category.
    rejection_reason: Mapped[str | None] = mapped_column(String(32))

    # --- Phase 5: risk/sizing audit trail (docs/PHASE5_DESIGN.md section 8) ---
    # Denormalized from the signal at entry time, same as condition_id/asset/
    # outcome above - needed so max_correlated_exposure can group open
    # positions by event without a join (flag 8).
    event_slug: Mapped[str | None] = mapped_column(String(300))
    # Which sizer actually produced this trade's size - FIXED/TIERED/KELLY/
    # TIERED_FALLBACK. A KELLY portfolio's individual trades record
    # TIERED_FALLBACK (not TIERED) when their own calibration bucket was too
    # sparse to trust, distinguishing "fell back" from a portfolio that
    # deliberately chose TIERED (section 6).
    sizer_used: Mapped[str | None] = mapped_column(String(20))
    # Post-RISK_KELLY_FRACTION, pre-cap fraction actually used. Null unless
    # sizer_used=KELLY.
    kelly_fraction: Mapped[Decimal | None] = mapped_column(Money)
    # Calibrated probability used at decision time. Null unless
    # sizer_used=KELLY and the signal's calibration bucket had enough samples.
    p_hat: Mapped[Decimal | None] = mapped_column(Money)
    # p_hat - c at decision time. Stored even on a Kelly-rejected (skipped)
    # candidate so "there was no edge here" is answerable from the row
    # without recomputing it - null under the same condition as p_hat.
    edge: Mapped[Decimal | None] = mapped_column(Money)

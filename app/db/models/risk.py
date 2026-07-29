"""Risk layer audit trail: every deny/resize/halt RiskManager issues.

See docs/PHASE5_DESIGN.md section 8 / flag 10. Only deny/resize/halt
decisions are stored - a routine ALLOW leaves no row, matching CLAUDE.md's
"every rejected candidate is accounted for in the funnel log with a reason"
convention already applied to paper_trades.rejection_reason (a trade with no
matching risk_events row this cycle was allowed).
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class RiskDecisionType(StrEnum):
    """What RiskManager did with a candidate - ALLOW is never persisted."""

    RESIZE = "RESIZE"
    DENY = "DENY"


class RiskEvent(Base):
    """One RiskManager deny/resize/halt decision.

    decision/rule are plain String, not native Postgres enums - same
    reasoning as signals.status and paper_trades.status: a new rule or
    decision type later is an application change, not a migration. detail
    carries the numbers behind the decision (e.g. {"cap_pct": "0.10",
    "bankroll": "100.00", "requested": "12.00", "allowed": "10.00"}) so a row
    is self-explanatory without re-deriving the math later.
    """

    __tablename__ = "risk_events"
    __table_args__ = (
        Index("ix_risk_events_portfolio_id_occurred_at", "portfolio_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("paper_portfolios.id"))
    # Nullable: a pre_check denial (e.g. emergency_stop, min_bankroll_halt)
    # may fire before any specific signal is being evaluated.
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    rule: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(String(300))
    detail: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

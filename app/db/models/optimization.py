"""Phase 6 signal-quality tables - see docs/PHASE6_DESIGN.md."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class WalletCluster(Base):
    """One wallet's current independent-voice cluster assignment.

    One row per wallet, upserted in place on every daily clustering run -
    not an append-only history (docs/PHASE6_DESIGN.md workstream 1: "the
    current assignment is all consensus needs"). cluster_id is a content
    hash of the cluster's sorted member wallet addresses (design flag 1),
    not the community-detection algorithm's own output label - Louvain's
    labels are arbitrary and unstable across reruns even when membership
    hasn't changed, which would otherwise silently detach a cluster's
    Workstream 5 bandit reward history from itself every day.
    """

    __tablename__ = "wallet_clusters"
    __table_args__ = (Index("ix_wallet_clusters_cluster_id", "cluster_id"),)

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(16))
    cluster_size: Mapped[int] = mapped_column()
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SignalCLV(Base):
    """One signal's closing-line-value trajectory - docs/PHASE6_DESIGN.md
    workstream 2.

    Filled in over up to three separate passes, not all at once: a row is
    created the first time app/optimization/clv.py sees a signal, with
    entry_price always populated (falling back to signal.average_entry_price
    if no delayed snapshot exists yet - design flag 6); price_at_horizon/
    clv_horizon are null until CLV_HORIZON_HOURS has actually elapsed;
    price_at_resolution/clv_resolution are null until the market closes AND
    resolves unambiguously (never guessed - same convention app/paper/
    engine.py's detect_resolution already uses). One row per signal, not per
    portfolio - CLV measures the signal itself, not any one portfolio's fill.
    """

    __tablename__ = "signal_clv"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), unique=True, index=True)
    entry_price: Mapped[Decimal] = mapped_column(Money)
    price_at_horizon: Mapped[Decimal | None] = mapped_column(Money)
    price_at_resolution: Mapped[Decimal | None] = mapped_column(Money)
    clv_horizon: Mapped[Decimal | None] = mapped_column(Money)
    clv_resolution: Mapped[Decimal | None] = mapped_column(Money)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

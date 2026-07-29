"""Phase 6 signal-quality tables - see docs/PHASE6_DESIGN.md."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


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

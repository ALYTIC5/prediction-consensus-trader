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

    brier_raw (docs/PHASE6_DESIGN.md workstream 4) is filled in by
    app/optimization/brier.py alongside price_at_resolution - the Brier
    score of entry_price against the actual 0/1 outcome (price_at_resolution
    doubles as that outcome, since it's already exactly 1.0/0.0). There is
    deliberately no brier_calibrated column: a bucket's calibrated p_hat
    changes as more trades resolve, so a frozen column would silently go
    stale - app/optimization/brier.py computes that one fresh on every call.
    """

    __tablename__ = "signal_clv"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), unique=True, index=True)
    entry_price: Mapped[Decimal] = mapped_column(Money)
    price_at_horizon: Mapped[Decimal | None] = mapped_column(Money)
    price_at_resolution: Mapped[Decimal | None] = mapped_column(Money)
    clv_horizon: Mapped[Decimal | None] = mapped_column(Money)
    clv_resolution: Mapped[Decimal | None] = mapped_column(Money)
    brier_raw: Mapped[Decimal | None] = mapped_column(Money)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TraderCategoryScore(Base):
    """One wallet's per-category skill score - docs/PHASE6_DESIGN.md
    workstream 3's win-rate/shrinkage/decay/zombie machinery, applied per
    (wallet, category) instead of folded into the global TraderScore (see
    app/optimization/scoring_category.py). Append-only, one row per wallet
    per category per run - same "captured_at" style history TraderScore
    already uses, not an upserted-in-place "current state" table, so a
    wallet's category scores can be trended over time same as its overall
    score can.

    score is the Wilson lower bound of the shrunk, decay-weighted win rate
    - low/neutral (anchored to the category's own population mean, not the
    global one) for a wallet with little or no history in this category,
    high only once real decayed wins accumulate. resolved_trades is the
    decayed n that fed the score (fractional, not a raw count - each
    qualifying position contributes 0.5 ** (age_days / halflife_days), not
    a full 1).
    """

    __tablename__ = "trader_category_scores"
    __table_args__ = (Index("ix_trader_category_scores_wallet_category", "wallet_id", "category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"))
    category: Mapped[str] = mapped_column(String(32))
    score: Mapped[Decimal] = mapped_column(Money)
    resolved_trades: Mapped[Decimal] = mapped_column(Money)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WalletCrowdedness(Base):
    """One wallet's on-chain crowdedness score - docs/PHASE6_DESIGN.md
    workstream 7. Current-state, not append-only (flag 20): one row per
    wallet, deleted and reinserted on every daily
    app/optimization/crowdedness.py run, same shape as WalletCluster and
    ClusterBanditState - nothing here needs trending, only a live number
    the dashboard shows alongside a wallet's skill score.

    follower_reaction/leaderboard_fame/market_obviousness are the three
    [0,1] components; crowdedness is their configured weighted sum, also
    [0,1]. hidden_alpha is deliberately NOT a column here (flag 21) - it
    combines this row's crowdedness with a signal's own (often
    per-category, post-Workstream-6) skill_score fresh at the point of use,
    computed by hidden_alpha_score()/hidden_alpha_weighted_score() in
    app/optimization/crowdedness.py, never stored.
    """

    __tablename__ = "wallet_crowdedness"

    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), primary_key=True)
    follower_reaction: Mapped[Decimal] = mapped_column(Money)
    leaderboard_fame: Mapped[Decimal] = mapped_column(Money)
    market_obviousness: Mapped[Decimal] = mapped_column(Money)
    crowdedness: Mapped[Decimal] = mapped_column(Money)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClusterBanditState(Base):
    """One cluster's current Beta-Bernoulli posterior over positive-CLV
    signals, and the adaptive weight multiplier derived from it -
    docs/PHASE6_DESIGN.md workstream 5.

    Rebuilt from scratch on every app/optimization/bandit.py run, not
    updated incrementally - one row per cluster, same "current state, not
    a history" shape as wallet_clusters. cluster_id is the same stable,
    membership-hash id wallet_clusters uses (design flag 1) - that's the
    entire reason it's a content hash rather than Louvain's own output
    label: a cluster's reward history must survive daily reclustering as
    long as its membership hasn't actually changed.
    """

    __tablename__ = "cluster_bandit_state"

    cluster_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    alpha: Mapped[int] = mapped_column()
    beta: Mapped[int] = mapped_column()
    observations: Mapped[int] = mapped_column()
    adaptive_multiplier: Mapped[Decimal] = mapped_column(Money)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

# Every model must be imported here so Alembic's autogenerate (which scans
# Base.metadata) actually discovers it — a model defined but never imported
# is invisible to migrations.
from app.db.models.dashboard import ConsensusRun, OverrideAudit, RuntimeOverride
from app.db.models.markets import FeeRate, Market, MarketHistory, OrderBook, PriceSnapshot
from app.db.models.optimization import (
    ClusterBanditState,
    SignalCLV,
    TraderCategoryScore,
    WalletCluster,
    WalletCrowdedness,
)
from app.db.models.paper import PaperPortfolio, PaperTrade, PaperTradeStatus
from app.db.models.positions import Position, PositionEventType, PositionHistory
from app.db.models.risk import RiskDecisionType, RiskEvent
from app.db.models.scores import TraderScore
from app.db.models.scout import (
    ScoutForwardTrade,
    ScoutStageTransition,
    ScoutValidationWindow,
    TraderPipeline,
    TraderPipelineStage,
)
from app.db.models.signals import Signal, SignalStatus
from app.db.models.system import AppState, JobHeartbeat
from app.db.models.wallets import LeaderboardSnapshot, Wallet

__all__ = [
    "AppState",
    "ClusterBanditState",
    "ConsensusRun",
    "FeeRate",
    "JobHeartbeat",
    "LeaderboardSnapshot",
    "Market",
    "MarketHistory",
    "OrderBook",
    "OverrideAudit",
    "PaperPortfolio",
    "PaperTrade",
    "PaperTradeStatus",
    "Position",
    "PositionEventType",
    "PositionHistory",
    "PriceSnapshot",
    "RiskDecisionType",
    "RiskEvent",
    "RuntimeOverride",
    "ScoutForwardTrade",
    "ScoutStageTransition",
    "ScoutValidationWindow",
    "Signal",
    "SignalCLV",
    "SignalStatus",
    "TraderCategoryScore",
    "TraderPipeline",
    "TraderPipelineStage",
    "TraderScore",
    "Wallet",
    "WalletCluster",
    "WalletCrowdedness",
]

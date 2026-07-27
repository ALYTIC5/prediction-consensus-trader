# Every model must be imported here so Alembic's autogenerate (which scans
# Base.metadata) actually discovers it — a model defined but never imported
# is invisible to migrations.
from app.db.models.markets import Market, MarketHistory, PriceSnapshot
from app.db.models.positions import Position, PositionEventType, PositionHistory
from app.db.models.scores import TraderScore
from app.db.models.signals import Signal, SignalStatus
from app.db.models.system import AppState
from app.db.models.wallets import LeaderboardSnapshot, Wallet

__all__ = [
    "AppState",
    "LeaderboardSnapshot",
    "Market",
    "MarketHistory",
    "Position",
    "PositionEventType",
    "PositionHistory",
    "PriceSnapshot",
    "Signal",
    "SignalStatus",
    "TraderScore",
    "Wallet",
]

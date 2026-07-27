# Every model must be imported here so Alembic's autogenerate (which scans
# Base.metadata) actually discovers it — a model defined but never imported
# is invisible to migrations.
from app.db.models.system import AppState

__all__ = ["AppState"]

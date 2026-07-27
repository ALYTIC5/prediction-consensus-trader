"""System-level tables not tied to any collector or trading concept."""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class AppState(Base):
    """Generic key/value store.

    Why this exists: collectors need a durable place to persist checkpoints
    (e.g. "last processed trade id") between runs, and a trivial table like
    this also serves as the smoke test that Alembic migrations actually
    reach the database end to end.
    """

    __tablename__ = "app_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

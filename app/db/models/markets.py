"""Market metadata and its time-series history."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, Money


class Market(Base):
    """Current state of one market - one row per condition_id.

    outcomes/clob_token_ids are stored already parsed as JSONB, not the raw
    JSON-encoded strings Gamma sends on the wire (see docs/API_REFERENCE.md
    quirk) - the collector parses them once on the way in, so every
    downstream reader gets real lists, never a string to re-parse.
    """

    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(66), unique=True, index=True)
    gamma_id: Mapped[str] = mapped_column(String(32))
    question: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(String(300))
    event_slug: Mapped[str | None] = mapped_column(String(300))
    outcomes: Mapped[list[str]] = mapped_column(JSONB)
    clob_token_ids: Mapped[list[str]] = mapped_column(JSONB)
    # Normalized from Gamma's per-market "tags" array (see
    # app/collectors/categories.py) - one of Sports/Politics/Crypto/
    # Pop-Culture/Other. Not nullable: every market gets a value, "Other"
    # when nothing matches, so downstream category-keyed lookups (Workstream
    # 3's per-category wallet ranking) never have to special-case a null.
    category: Mapped[str] = mapped_column(String(32), server_default="Other")
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean)
    closed: Mapped[bool] = mapped_column(Boolean, index=True)
    accepting_orders: Mapped[bool] = mapped_column(Boolean, default=False)
    neg_risk: Mapped[bool | None] = mapped_column(Boolean)
    tick_size: Mapped[Decimal | None] = mapped_column(Money)
    liquidity: Mapped[Decimal | None] = mapped_column(Money)
    volume_24h: Mapped[Decimal | None] = mapped_column(Money)
    volume_total: Mapped[Decimal | None] = mapped_column(Money)
    best_bid: Mapped[Decimal | None] = mapped_column(Money)
    best_ask: Mapped[Decimal | None] = mapped_column(Money)
    spread: Mapped[Decimal | None] = mapped_column(Money)
    last_trade_price: Mapped[Decimal | None] = mapped_column(Money)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MarketHistory(Base):
    """Time series of a market's tradeable metrics - append-only, never updated."""

    __tablename__ = "market_history"
    __table_args__ = (
        Index("ix_market_history_condition_id_captured_at", "condition_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(66))
    liquidity: Mapped[Decimal | None] = mapped_column(Money)
    volume_24h: Mapped[Decimal | None] = mapped_column(Money)
    volume_total: Mapped[Decimal | None] = mapped_column(Money)
    best_bid: Mapped[Decimal | None] = mapped_column(Money)
    best_ask: Mapped[Decimal | None] = mapped_column(Money)
    spread: Mapped[Decimal | None] = mapped_column(Money)
    last_trade_price: Mapped[Decimal | None] = mapped_column(Money)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PriceSnapshot(Base):
    """Per-outcome-token price ticks - append-only."""

    __tablename__ = "prices"
    __table_args__ = (Index("ix_prices_asset_captured_at", "asset", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(66))
    # Outcome token ids are huge integers (clobTokenIds); kept as strings,
    # never cast to a numeric type - they're identifiers, not values.
    asset: Mapped[str] = mapped_column(String(80))
    price: Mapped[Decimal] = mapped_column(Money)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

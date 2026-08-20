"""Market metadata and its time-series history."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
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
    # "event:<event_slug>" or "catdate:<category>:<bucket>" or
    # "solo:<condition_id>" - see app/optimization/event_clustering.py.
    # Nullable: computed by a periodic job, not always fresh for a
    # brand-new market. Sized for the "event:" prefix plus event_slug's
    # own 300-char max.
    event_cluster_id: Mapped[str | None] = mapped_column(String(320), index=True)
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


class MarketResolution(Base):
    """Authoritative settlement record for one resolved market - written by
    app/collectors/resolutions.py once Gamma's single-market-by-id lookup
    (the only route that doesn't drop a resolved market, see
    app/collectors/schemas.py's GammaMarketResolution) reports
    uma_resolution_status == "resolved". Never written for a market whose
    status is still "proposed"/disputed/anything else in-flight - that's
    logged and left unresolved rather than guessed (see that collector's
    module docstring).

    outcome_prices is the full resolved price vector (raw strings, same
    order as Market.clob_token_ids and the same string-not-Decimal
    convention as OrderBook.levels - JSONB has no Decimal type), not just
    the winning side: a real, documented UMA outcome ("if the game is
    canceled entirely... resolve 50-50") has no single winner, so
    winning_asset/winning_outcome_index are nullable and left null for that
    case while outcome_prices still carries the true settlement value
    app/paper/engine.py needs to close a trade at its real value.
    """

    __tablename__ = "market_resolutions"

    condition_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    winning_asset: Mapped[str | None] = mapped_column(String(80))
    winning_outcome_index: Mapped[int | None] = mapped_column(Integer)
    outcome_prices: Mapped[list[str]] = mapped_column(JSONB)
    uma_resolution_status: Mapped[str] = mapped_column(String(32))
    # Nullable: Gamma's closedTime has been null on a small number of
    # observed resolved markets - never blocks recording the resolution
    # itself, just leaves this one field empty.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="gamma")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class OrderBook(Base):
    """One side of one outcome token's order book at one point in time -
    see app/collectors/orderbook.py and docs/API_REFERENCE.md's /book entry.

    One row per (asset, side, captured_at), not one row per level - levels
    is the full bid or ask ladder as JSONB (a list of {"price": "...",
    "size": "..."} objects, same string-decimal shape the API sends,
    parsed to Decimal only when app/paper/fills.py's walk_the_book() reads
    them), append-only. Only snapshotted for markets we hold a position in
    or have an active signal on (app/collectors/orderbook.py's work list) -
    this is not a general-purpose price feed, it exists so a paper fill can
    walk a real book instead of assuming one.
    """

    __tablename__ = "order_books"
    __table_args__ = (
        Index(
            "ix_order_books_condition_id_asset_captured_at", "condition_id", "asset", "captured_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(66))
    asset: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(4))  # "bid" | "ask"
    levels: Mapped[list[dict]] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FeeRate(Base):
    """Latest known taker fee rate for one outcome token - see
    app/collectors/fee_rates.py and docs/API_REFERENCE.md's /fee-rate
    entry.

    One row per (condition_id, asset), UPSERTed each cycle - unlike
    OrderBook this is not append-only history: only the current rate is
    ever needed to compute a fee, so there's nothing to gain from keeping
    old values around, same reasoning as Market's own single-row-per-
    condition_id shape.
    """

    __tablename__ = "fee_rates"

    condition_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    asset: Mapped[str] = mapped_column(String(80), primary_key=True)
    # base_fee basis points / 10000 - already a fraction (e.g. 0.07), never
    # stored as raw bps, so every reader uses the same unit as
    # app/paper/fees.py's compute_taker_fee expects.
    rate: Mapped[Decimal] = mapped_column(Money)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

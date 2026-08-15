"""Order book snapshot collector - see docs/API_REFERENCE.md's /book entry
and app/paper/fills.py's walk_the_book().

Not a general-purpose price feed: only snapshots outcome tokens for markets
we actually have capital or a live decision riding on - an open paper trade,
or an active signal that might get entered next cycle. Everything else never
needs a real book, and this is a brand-new, undocumented-rate-limit API
surface (see settings.py's orderbook_interval_seconds/
orderbook_max_tokens_per_cycle), so the work list stays as small as the
paper engine's own state already makes it.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.collectors.polymarket import PolymarketClient
from app.collectors.schemas import OrderBookResponse
from app.config.settings import Settings
from app.db.models import OrderBook, PaperTrade, PaperTradeStatus, Signal, SignalStatus
from app.db.session import db_session

logger = logging.getLogger(__name__)


async def collect(client: PolymarketClient, settings: Settings) -> None:
    """Snapshot the order book for every (condition_id, asset) we hold an
    open paper trade in or have an active signal on, capped at
    orderbook_max_tokens_per_cycle per cycle.
    """
    work_list = await asyncio.to_thread(_get_work_list, settings.orderbook_max_tokens_per_cycle)
    if not work_list:
        logger.info("orderbook: nothing to snapshot (no open trades or active signals)")
        return

    captured_at = datetime.now(UTC)
    fetched = 0
    stored = 0
    for condition_id, asset in work_list:
        book = await client.get_book(asset)
        fetched += 1
        if book is None:
            continue
        await asyncio.to_thread(_persist, condition_id, asset, book, captured_at)
        stored += 1

    logger.info("orderbook: work_list=%d fetched=%d stored=%d", len(work_list), fetched, stored)


def _get_work_list(max_tokens: int) -> list[tuple[str, str]]:
    """Every (condition_id, asset) pair from an OPEN paper trade or an
    ACTIVE signal, deduplicated - the exact set walk_the_book's callers
    (paper fills, and the credibility metric) will need a real book for.
    """
    with db_session() as session:
        open_trade_pairs = set(
            session.execute(
                select(PaperTrade.condition_id, PaperTrade.asset).where(
                    PaperTrade.status == PaperTradeStatus.OPEN
                )
            ).all()
        )
        active_signal_pairs = set(
            session.execute(
                select(Signal.condition_id, Signal.asset).where(
                    Signal.status == SignalStatus.ACTIVE
                )
            ).all()
        )
    pairs = sorted(open_trade_pairs | active_signal_pairs)
    return pairs[:max_tokens]


def _persist(condition_id: str, asset: str, book: OrderBookResponse, captured_at: datetime) -> None:
    """One row per side, levels as the wire-format {"price": str, "size":
    str} list - never parsed to Decimal here (JSONB can't store Decimal),
    only when walk_the_book() actually reads a row.
    """
    with db_session() as session:
        session.add(
            OrderBook(
                condition_id=condition_id,
                asset=asset,
                side="bid",
                levels=[{"price": str(lvl.price), "size": str(lvl.size)} for lvl in book.bids],
                captured_at=captured_at,
            )
        )
        session.add(
            OrderBook(
                condition_id=condition_id,
                asset=asset,
                side="ask",
                levels=[{"price": str(lvl.price), "size": str(lvl.size)} for lvl in book.asks],
                captured_at=captured_at,
            )
        )

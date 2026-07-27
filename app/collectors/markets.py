"""Market metadata collector: sync Gamma data for markets we actually care about."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.polymarket import PolymarketClient
from app.collectors.schemas import GammaMarket
from app.config.settings import Settings
from app.db.models import Market, MarketHistory, Position, PriceSnapshot
from app.db.session import db_session
from app.utils.time import parse_iso_datetime

logger = logging.getLogger(__name__)


async def collect(client: PolymarketClient, settings: Settings) -> None:
    """Sync Gamma metadata for every market we're currently exposed to."""
    condition_ids = await asyncio.to_thread(_get_work_list)
    if not condition_ids:
        logger.info("markets: nothing to sync (no open positions or open markets)")
        return

    markets = await client.get_markets_by_condition_ids(condition_ids)
    captured_at = datetime.now(UTC)
    stored_count = await asyncio.to_thread(_persist, markets, captured_at)

    logger.info(
        "markets: requested=%d returned=%d stored=%d",
        len(condition_ids),
        len(markets),
        stored_count,
    )


def _get_work_list() -> list[str]:
    """Every condition_id we still care about.

    Derived from the DB, not a config list: every market held in an open
    Position (so we keep pricing what our tracked wallets are actually in),
    unioned with every market we already know about that isn't closed yet
    (so a market doesn't go stale between the last position in it closing
    and us noticing it resolved).
    """
    with db_session() as session:
        open_position_ids = set(
            session.execute(
                select(Position.condition_id).distinct().where(Position.is_open.is_(True))
            ).scalars()
        )
        open_market_ids = set(
            session.execute(
                select(Market.condition_id).distinct().where(Market.closed.is_(False))
            ).scalars()
        )
    return sorted(open_position_ids | open_market_ids)


def _persist(markets: list[GammaMarket], captured_at: datetime) -> int:
    """Blocking DB work: upsert current market state, append history + prices."""
    with db_session() as session:
        for market in markets:
            _upsert_market(session, market, captured_at)
            session.add(
                MarketHistory(
                    condition_id=market.condition_id,
                    liquidity=market.liquidity_num,
                    volume_24h=market.volume_24hr,
                    volume_total=market.volume_num,
                    best_bid=market.best_bid,
                    best_ask=market.best_ask,
                    spread=market.spread,
                    last_trade_price=market.last_trade_price,
                    captured_at=captured_at,
                )
            )
            # Non-strict: parsed_clob_token_ids/parsed_outcome_prices already
            # degrade to [] on malformed input rather than raising (see
            # schemas.py), so a length mismatch between them should drop the
            # extra entries, not crash the whole sync over one bad market.
            for asset, price in zip(
                market.parsed_clob_token_ids, market.parsed_outcome_prices, strict=False
            ):
                session.add(
                    PriceSnapshot(
                        condition_id=market.condition_id,
                        asset=asset,
                        price=price,
                        captured_at=captured_at,
                    )
                )
    return len(markets)


def _upsert_market(session: Session, market: GammaMarket, captured_at: datetime) -> None:
    """UPSERT the market row - same insert-or-update-in-one-statement
    rationale as the wallet upsert in leaderboard.py: avoids a
    select-then-insert race across overlapping collector runs.
    """
    values = {
        "condition_id": market.condition_id,
        "gamma_id": market.id,
        "question": market.question,
        "slug": market.slug,
        "event_slug": market.event_slug,
        "outcomes": market.parsed_outcomes,
        "clob_token_ids": market.parsed_clob_token_ids,
        "end_date": parse_iso_datetime(market.end_date),
        "active": market.active,
        "closed": market.closed,
        "accepting_orders": market.accepting_orders,
        "neg_risk": market.neg_risk,
        "tick_size": market.order_price_min_tick_size,
        "liquidity": market.liquidity_num,
        "volume_24h": market.volume_24hr,
        "volume_total": market.volume_num,
        "best_bid": market.best_bid,
        "best_ask": market.best_ask,
        "spread": market.spread,
        "last_trade_price": market.last_trade_price,
        "last_synced_at": captured_at,
    }
    # first_seen_at is deliberately absent from values: on INSERT it falls
    # back to its server_default(now()); on UPDATE it's excluded below so an
    # existing market's original first-seen timestamp is never overwritten.
    stmt = pg_insert(Market).values(**values)
    update_values = {key: value for key, value in values.items() if key != "condition_id"}
    stmt = stmt.on_conflict_do_update(index_elements=[Market.condition_id], set_=update_values)
    session.execute(stmt)

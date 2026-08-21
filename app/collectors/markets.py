"""Market metadata collector: sync Gamma data for markets we actually care about."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.categories import normalize_category
from app.collectors.polymarket import PolymarketClient
from app.collectors.schemas import GammaMarket
from app.config.settings import Settings
from app.db.models import Market, MarketHistory, Position, PriceSnapshot
from app.db.session import db_session
from app.utils.time import parse_iso_datetime

logger = logging.getLogger(__name__)


async def collect(client: PolymarketClient, settings: Settings) -> None:
    """Sync Gamma metadata for every market we're currently exposed to.

    Gamma's condition_ids-filtered /markets silently drops a market from
    its response once archived, rather than returning it with closed=true
    (see docs/API_REFERENCE.md) - so any requested condition_id missing
    from the response is resolved via a bounded CLOB fallback instead of
    being left to sync forever as "not yet closed".
    """
    condition_ids = await asyncio.to_thread(
        _get_work_list,
        settings.markets_max_condition_ids_per_cycle,
        settings.markets_backlog_min_per_cycle,
    )
    if not condition_ids:
        logger.info("markets: nothing to sync (no open positions or open markets)")
        return

    markets = await client.get_markets_by_condition_ids(condition_ids)
    captured_at = datetime.now(UTC)
    stored_count = await asyncio.to_thread(_persist, markets, captured_at)

    missing_ids = sorted(set(condition_ids) - {m.condition_id for m in markets})
    fallback_count = 0
    if missing_ids:
        fallback_count = await _sync_via_clob_fallback(
            client, missing_ids[: settings.markets_clob_fallback_max_per_cycle], captured_at
        )

    logger.info(
        "markets: requested=%d returned=%d stored=%d missing=%d fallback_resolved=%d",
        len(condition_ids),
        len(markets),
        stored_count,
        len(missing_ids),
        fallback_count,
    )


def _get_work_list(max_condition_ids: int, backlog_min: int) -> list[str]:
    """Every condition_id we still care about, bounded to max_condition_ids.

    Derived from the DB, not a config list: every market held in an open
    Position whose underlying market isn't already known to be closed (so
    we keep pricing what our tracked wallets are actually exposed to),
    filled up to the remaining budget with markets we already know about
    that aren't closed yet, oldest-synced-first so that backlog rotates
    through instead of starving markets past the front of the list.

    open_position_ids is capped at (max_condition_ids - backlog_min), NOT
    max_condition_ids - a wallet not bothering to redeem a resolved
    position is real and common enough that this set has been observed
    over 80,000 entries in production, and without reserving backlog_min
    off the top for the "not currently held" pool first, that set alone
    permanently exhausts the entire per-cycle budget (remaining_budget
    pins to 0 forever) - confirmed live: markets never held by a tracked
    wallet's position, including some of the platform's highest-liquidity
    markets, went 5+ days without a single sync before this reservation
    existed.

    Three bugs already caught here, worth keeping in mind for any future edit:
    - The excluded-closed-markets filter is a subquery, never a Python list
      passed to not_in() - open positions routinely exceed Postgres's
      65535 bind-parameter limit, and a literal-list not_in() blew that
      limit in production, crashing this query every cycle. A subquery
      compiles to one NOT IN (SELECT ...) clause with zero added bind
      params regardless of table size.
    - Neither query below both DISTINCTs and ORDER BYs a column outside
      the SELECT list - Postgres rejects that combination outright
      ("ORDER BY expressions must appear in select list" - confirmed live
      before deploy, not guessed). The market query drops DISTINCT
      entirely (condition_id is already declared unique on that table, so
      it was always redundant); the position query drops ORDER BY instead
      (Position.condition_id is genuinely non-unique - two outcome tokens
      per market - so DISTINCT stays, and rotation-fairness is sacrificed
      there in favour of a query that actually runs).
    - backlog_min reservation (this edit): see the paragraph above - the
      open-positions query used to be capped at the FULL max_condition_ids,
      which meant it alone could consume the entire cycle budget and starve
      the backlog pool to zero indefinitely.
    """
    with db_session() as session:
        closed_condition_ids = select(Market.condition_id).where(Market.closed.is_(True))
        positions_budget = max(max_condition_ids - backlog_min, 0)
        open_position_ids = list(
            session.execute(
                select(Position.condition_id)
                .distinct()
                .where(
                    Position.is_open.is_(True),
                    Position.condition_id.not_in(closed_condition_ids),
                )
                .limit(positions_budget)
            ).scalars()
        )
        remaining_budget = max(max_condition_ids - len(open_position_ids), 0)
        open_market_ids = set(
            session.execute(
                select(Market.condition_id)
                .where(Market.closed.is_(False), Market.condition_id.not_in(open_position_ids))
                .order_by(Market.last_synced_at.asc().nulls_first())
                .limit(remaining_budget)
            ).scalars()
        )
    return sorted(set(open_position_ids) | open_market_ids)


async def _sync_via_clob_fallback(
    client: PolymarketClient, condition_ids: list[str], captured_at: datetime
) -> int:
    """Resolve condition_ids Gamma dropped by asking CLOB directly, and
    update just the resolution-state columns for each one found - never
    liquidity/volume/category, which CLOB doesn't carry the way Gamma does.
    """
    resolved: list[tuple[str, bool, bool, bool]] = []
    for condition_id in condition_ids:
        state = await client.get_market_state(condition_id)
        if state is not None:
            resolved.append((condition_id, state.active, state.closed, state.accepting_orders))
    if resolved:
        await asyncio.to_thread(_persist_clob_fallback, resolved, captured_at)
    return len(resolved)


def _persist_clob_fallback(
    resolved: list[tuple[str, bool, bool, bool]], captured_at: datetime
) -> None:
    """Blocking DB work: update active/closed/accepting_orders only, for
    markets already known to us (a market Gamma never told us about in the
    first place has no row to update, so this never inserts).
    """
    with db_session() as session:
        for condition_id, active, closed, accepting_orders in resolved:
            session.execute(
                update(Market)
                .where(Market.condition_id == condition_id)
                .values(
                    active=active,
                    closed=closed,
                    accepting_orders=accepting_orders,
                    last_synced_at=captured_at,
                )
            )


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
        "category": normalize_category(market.tags),
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

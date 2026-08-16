"""Taker fee rate collector - see docs/API_REFERENCE.md's /fee-rate entry
and app/paper/fees.py's compute_taker_fee().

Same scope and reasoning as app/collectors/orderbook.py: only snapshots
outcome tokens for markets we actually have capital or a live decision
riding on, and this is a brand-new, undocumented-rate-limit API surface, so
the work list stays as small as the paper engine's own state already makes
it.
"""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.collectors.polymarket import PolymarketClient
from app.config.settings import Settings
from app.db.models import FeeRate, PaperTrade, PaperTradeStatus, Signal, SignalStatus
from app.db.session import db_session

logger = logging.getLogger(__name__)


async def collect(client: PolymarketClient, settings: Settings) -> None:
    """Snapshot the taker fee rate for every (condition_id, asset) we hold
    an open paper trade in or have an active signal on, capped at
    fee_rate_max_tokens_per_cycle per cycle.
    """
    work_list = await asyncio.to_thread(_get_work_list, settings.fee_rate_max_tokens_per_cycle)
    if not work_list:
        logger.info("fee_rates: nothing to snapshot (no open trades or active signals)")
        return

    captured_at = datetime.now(UTC)
    fetched = 0
    stored = 0
    for condition_id, asset in work_list:
        rate = await client.get_fee_rate(asset)
        fetched += 1
        if rate is None:
            continue
        await asyncio.to_thread(_persist, condition_id, asset, rate, captured_at)
        stored += 1

    logger.info("fee_rates: work_list=%d fetched=%d stored=%d", len(work_list), fetched, stored)


def _get_work_list(max_tokens: int) -> list[tuple[str, str]]:
    """Every (condition_id, asset) pair from an OPEN paper trade or an
    ACTIVE signal, deduplicated - identical to
    app/collectors/orderbook.py's work list (the same fills need both a
    book and a fee rate).
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


def _persist(condition_id: str, asset: str, rate: Decimal, captured_at: datetime) -> None:
    """UPSERT the latest rate for this pair - same
    insert-or-update-in-one-statement rationale as app/collectors/
    markets.py's market upsert."""
    with db_session() as session:
        stmt = pg_insert(FeeRate).values(
            condition_id=condition_id, asset=asset, rate=rate, captured_at=captured_at
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[FeeRate.condition_id, FeeRate.asset],
            set_={"rate": rate, "captured_at": captured_at},
        )
        session.execute(stmt)

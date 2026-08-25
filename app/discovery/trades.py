"""Trade-grading walk: for every niche-tagged resolved market, fetch every
trade that ever happened on it (GET /trades, Data API) and grade each one
win/loss against the market's real resolution.

This is the actual "walk markets -> collect every distinct wallet" the user
asked for, in place of the leaderboard-seeded ~100-wallet population Part 1
diagnosed. No seed wallet list of any kind - every wallet that ever traded
a niche market surfaces here, whether or not it was ever near a leaderboard.

Checkpointed the same way as app/discovery/walk.py: by condition_id
ordering over discovery_market_niches (only markets walk.py already
classified - a market with no niche match never enters this stage at all,
since there's nothing to grade a wallet's niche performance against).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.polymarket import PolymarketClient
from app.collectors.schemas import TradeEntry
from app.config.settings import Settings
from app.db.models import (
    DiscoveryMarketNiche,
    DiscoverySweepCheckpoint,
    MarketResolution,
    NicheTradeGrade,
    Wallet,
)
from app.db.session import db_session

logger = logging.getLogger(__name__)

_STAGE = "trade_grading"


@dataclass(frozen=True)
class TradeWalkSummary:
    markets_fetched: int
    trades_graded: int
    wallets_touched: int
    checkpoint_advanced_to: str | None


def _load_checkpoint(session: Session) -> str | None:
    row = session.get(DiscoverySweepCheckpoint, _STAGE)
    return row.last_condition_id if row is not None else None


def _load_next_markets(
    session: Session, after: str | None, limit: int
) -> list[tuple[str, str, int | None]]:
    """(condition_id, niche, winning_outcome_index) for the next batch of
    niche-tagged, resolved markets past the checkpoint. winning_outcome_index
    is None for a genuine no-single-winner resolution (50-50 UMA outcomes,
    see MarketResolution's docstring) - those markets are still walked (so
    the checkpoint advances past them) but grade zero trades, since there is
    no winning side to grade a bet against.
    """
    stmt = (
        select(
            DiscoveryMarketNiche.condition_id,
            DiscoveryMarketNiche.niche,
            MarketResolution.winning_outcome_index,
        )
        .join(MarketResolution, MarketResolution.condition_id == DiscoveryMarketNiche.condition_id)
        .order_by(DiscoveryMarketNiche.condition_id.asc())
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(DiscoveryMarketNiche.condition_id > after)
    return list(session.execute(stmt).all())


def _upsert_wallet(session: Session, trade: TradeEntry) -> int:
    """UPSERT by address - same atomic insert-or-update rationale as
    app/collectors/leaderboard.py's _upsert_wallet. name/pseudonym are the
    only identity fields /trades carries (no username/x_username the way
    the leaderboard does) - used as a display-name fallback only if the
    wallet has no username yet, never overwriting one a leaderboard sighting
    already set.
    """
    display_name = trade.name or trade.pseudonym
    stmt = (
        pg_insert(Wallet)
        .values(
            address=trade.proxy_wallet,
            username=display_name,
            is_tracked=False,
            niche_tracked=False,
        )
        .on_conflict_do_nothing(index_elements=[Wallet.address])
        .returning(Wallet.id)
    )
    wallet_id = session.execute(stmt).scalar_one_or_none()
    if wallet_id is not None:
        return wallet_id
    existing_stmt = select(Wallet.id).where(Wallet.address == trade.proxy_wallet)
    return session.execute(existing_stmt).scalar_one()


def _grade_trade(
    trade: TradeEntry, niche: str, winning_outcome_index: int | None
) -> NicheTradeGrade | None:
    """BUY trades only - a SELL is exiting a position, not staking on an
    outcome, and can't be graded win/loss without full cost-basis netting
    this market-walk approach deliberately doesn't attempt (see
    NicheTradeGrade's docstring). None when the market has no single winner
    to grade against.
    """
    if trade.side != "BUY" or winning_outcome_index is None:
        return None
    return NicheTradeGrade(
        wallet_id=0,  # filled in by the caller once the wallet is upserted
        niche=niche,
        condition_id=trade.condition_id,
        asset=trade.asset,
        outcome_index=trade.outcome_index,
        won=trade.outcome_index == winning_outcome_index,
        stake=trade.size * trade.price,
        price=trade.price,
        transaction_hash=trade.transaction_hash,
        traded_at=datetime.fromtimestamp(trade.timestamp, tz=UTC),
    )


def _upsert_grade(session: Session, grade: NicheTradeGrade, now: datetime) -> None:
    stmt = (
        pg_insert(NicheTradeGrade)
        .values(
            wallet_id=grade.wallet_id,
            niche=grade.niche,
            condition_id=grade.condition_id,
            asset=grade.asset,
            outcome_index=grade.outcome_index,
            won=grade.won,
            stake=grade.stake,
            price=grade.price,
            transaction_hash=grade.transaction_hash,
            traded_at=grade.traded_at,
            computed_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[
                NicheTradeGrade.wallet_id,
                NicheTradeGrade.transaction_hash,
                NicheTradeGrade.asset,
            ]
        )
    )
    session.execute(stmt)


async def run_trade_grading_walk(client: PolymarketClient, settings: Settings) -> TradeWalkSummary:
    """One bounded pass over discovery_walk_batch_size niche-tagged
    resolved markets: fetch every trade, upsert the wallet, grade the
    trade, advance the checkpoint. Trade fetches for the batch run
    concurrently (same asyncio.gather shape app/collectors/positions.py
    already uses for its own per-cycle wallet sweep) - with
    return_exceptions=True, deliberately: a plain gather() lets ONE
    market's failure (a real 400 from Data API on a market it rejects,
    observed live in production) kill the entire batch and fail the whole
    discovery cycle, which is exactly what happened - discovery failed on
    every single cycle from 2026-08-25 07:46 onward until this fix. A
    failed market is logged and skipped (checkpoint still advances past
    it, same "processed, not guessed" treatment app/discovery/walk.py
    already applies to a market Gamma has never heard of).
    """
    now = datetime.now(UTC)
    with db_session() as session:
        after = _load_checkpoint(session)
        batch = _load_next_markets(session, after, settings.discovery_walk_batch_size)

    if not batch:
        return TradeWalkSummary(
            markets_fetched=0, trades_graded=0, wallets_touched=0, checkpoint_advanced_to=after
        )

    condition_ids = [row[0] for row in batch]
    raw_results = await asyncio.gather(
        *(client.get_trades_for_market(cid) for cid in condition_ids), return_exceptions=True
    )
    trade_lists: list[list[TradeEntry]] = []
    for condition_id, result in zip(condition_ids, raw_results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "discovery: trade_grading failed for condition_id=%s: %s", condition_id, result
            )
            trade_lists.append([])
        else:
            trade_lists.append(result)

    graded = 0
    wallets_touched: set[str] = set()
    with db_session() as session:
        for (_condition_id, niche, winning_outcome_index), trades in zip(
            batch, trade_lists, strict=True
        ):
            for trade in trades:
                grade = _grade_trade(trade, niche, winning_outcome_index)
                if grade is None:
                    continue
                wallet_id = _upsert_wallet(session, trade)
                grade.wallet_id = wallet_id
                _upsert_grade(session, grade, now)
                wallets_touched.add(trade.proxy_wallet)
                graded += 1
        last_id = condition_ids[-1]
        stmt = (
            pg_insert(DiscoverySweepCheckpoint)
            .values(
                stage=_STAGE,
                last_condition_id=last_id,
                markets_processed=len(condition_ids),
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[DiscoverySweepCheckpoint.stage],
                set_={
                    "last_condition_id": last_id,
                    "markets_processed": DiscoverySweepCheckpoint.markets_processed
                    + len(condition_ids),
                    "updated_at": now,
                },
            )
        )
        session.execute(stmt)

    logger.info(
        "discovery: trade_grading markets=%d trades_graded=%d wallets_touched=%d checkpoint=%s",
        len(condition_ids),
        graded,
        len(wallets_touched),
        last_id,
    )
    return TradeWalkSummary(
        markets_fetched=len(condition_ids),
        trades_graded=graded,
        wallets_touched=len(wallets_touched),
        checkpoint_advanced_to=last_id,
    )


async def run_trade_grading_job(client: PolymarketClient, settings: Settings) -> None:
    await run_trade_grading_walk(client, settings)

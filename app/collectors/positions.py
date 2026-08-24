"""Position collector: sweep tracked wallets, diff against last known state."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.collectors.diffing import PositionEvent, diff_positions
from app.collectors.polymarket import PolymarketClient
from app.collectors.schemas import PositionEntry
from app.config.settings import Settings
from app.db.models import Position, PositionHistory, Wallet
from app.db.session import db_session

logger = logging.getLogger(__name__)


async def collect(client: PolymarketClient, settings: Settings) -> None:
    """Sweep every tracked wallet's positions and record what changed."""
    wallets = await asyncio.to_thread(_get_tracked_wallets)
    if not wallets:
        logger.info("positions: nothing to sweep (no tracked wallets)")
        return

    results = await asyncio.gather(*(client.get_positions(address) for _, address in wallets))
    captured_at = datetime.now(UTC)
    entries_by_wallet_id = {
        wallet_id: entries for (wallet_id, _address), entries in zip(wallets, results, strict=True)
    }

    event_count = await asyncio.to_thread(_persist, entries_by_wallet_id, captured_at)
    logger.info("positions: swept %d wallets, %d events recorded", len(wallets), event_count)


def _get_tracked_wallets() -> list[tuple[int, str]]:
    """is_tracked (top-scored, global leaderboard-seeded) OR niche_tracked
    (app/discovery/promotion.py: earned a niche-CANDIDATE spot, regardless
    of overall score) - two independent reasons a wallet's positions need
    polling, unioned here so niche forward-tracking gets real
    PositionHistory rows without touching any other is_tracked-gated
    module's filter.
    """
    with db_session() as session:
        rows = session.execute(
            select(Wallet.id, Wallet.address).where(
                Wallet.is_tracked.is_(True) | Wallet.niche_tracked.is_(True)
            )
        ).all()
    return [(row.id, row.address) for row in rows]


def _persist(entries_by_wallet_id: dict[int, list[PositionEntry]], captured_at: datetime) -> int:
    """Blocking DB work: diff each wallet's positions, upsert state, log history."""
    total_events = 0
    with db_session() as session:
        for wallet_id, entries in entries_by_wallet_id.items():
            events = _sync_wallet(session, wallet_id, entries, captured_at)
            total_events += len(events)
    return total_events


def _sync_wallet(
    session: Session, wallet_id: int, entries: list[PositionEntry], captured_at: datetime
) -> list[PositionEvent]:
    """Diff one wallet's fresh snapshot against its last known state, persist both."""
    existing_rows = {
        row.asset: row
        for row in session.execute(
            select(Position).where(Position.wallet_id == wallet_id, Position.is_open.is_(True))
        ).scalars()
    }
    previous_sizes = {asset: row.size for asset, row in existing_rows.items()}

    # A wallet with zero Position rows at all (open or closed) has never
    # been swept before - every OPENED event this cycle is backfill, not a
    # freshly-detected entry, so it's flagged is_bootstrap for phase 3.
    is_bootstrap = (
        session.execute(
            select(func.count()).select_from(Position).where(Position.wallet_id == wallet_id)
        ).scalar_one()
        == 0
    )

    events = diff_positions(previous_sizes, entries)

    for entry in entries:
        _upsert_position(session, wallet_id, entry, captured_at)

    closed_assets = {event.asset for event in events if event.event_type == "CLOSED"}
    if closed_assets:
        session.execute(
            update(Position)
            .where(Position.wallet_id == wallet_id, Position.asset.in_(closed_assets))
            .values(is_open=False, closed_at=captured_at, size=0, last_updated_at=captured_at)
        )

    for event in events:
        if event.event_type == "CLOSED":
            # diff_positions can't know condition_id/outcome/title for a
            # closed asset (it only ever saw a size) - backfill them from
            # the row that's about to be marked closed.
            prior = existing_rows[event.asset]
            condition_id, outcome, title = prior.condition_id, prior.outcome, prior.title
        else:
            condition_id, outcome, title = event.condition_id, event.outcome, event.title

        session.add(
            PositionHistory(
                wallet_id=wallet_id,
                asset=event.asset,
                condition_id=condition_id,
                event_type=event.event_type,
                is_bootstrap=is_bootstrap,
                size_before=event.size_before,
                size_after=event.size_after,
                avg_price=event.avg_price,
                cur_price=event.cur_price,
                outcome=outcome,
                title=title,
                detected_at=captured_at,
            )
        )

    return events


def _upsert_position(
    session: Session, wallet_id: int, entry: PositionEntry, captured_at: datetime
) -> None:
    """UPSERT the position row - same insert-or-update-in-one-statement
    rationale as the wallet/market upserts elsewhere in app/collectors.
    """
    values = {
        "wallet_id": wallet_id,
        "asset": entry.asset,
        "condition_id": entry.condition_id,
        "outcome": entry.outcome,
        "outcome_index": entry.outcome_index,
        "size": entry.size,
        "avg_price": entry.avg_price,
        "initial_value": entry.initial_value,
        "current_value": entry.current_value,
        "cur_price": entry.cur_price,
        "cash_pnl": entry.cash_pnl,
        "percent_pnl": entry.percent_pnl,
        "redeemable": entry.redeemable,
        "neg_risk": entry.negative_risk,
        "title": entry.title,
        "event_slug": entry.event_slug,
        "is_open": True,
        "closed_at": None,
        "last_updated_at": captured_at,
    }
    stmt = pg_insert(Position).values(**values)
    update_values = {
        key: value for key, value in values.items() if key not in ("wallet_id", "asset")
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=[Position.wallet_id, Position.asset], set_=update_values
    )
    session.execute(stmt)

"""Authoritative resolution backfill.

For every market past end_date that isn't yet recorded in
market_resolutions, fetch its true settlement from Gamma's single-market-
by-id route (GET /markets/{id}) and record it. This is the fix for a
starvation bug: the markets collector (app/collectors/markets.py) syncs via
Gamma's condition_ids-filtered bulk /markets query, which silently drops a
market from its response once resolved rather than returning it with
closed=true (see docs/API_REFERENCE.md) - confirmed live to happen even for
a single-value filter, on real production markets, not just an artifact of
requesting several ids at once. Market.closed for those rows never flips,
so app/paper/engine.py's resolution check (via app/paper/resolution.py's
detect_resolution) never fires and open trades on resolved markets sit
open forever. GET /markets/{id} (a documented, path-based single-resource
lookup, not a filtered list) does not drop resolved markets and additionally
carries the fields a filtered query never returns at all: closedTime,
umaResolutionStatus, resolvedBy, automaticallyResolved.
"""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.collectors.polymarket import PolymarketClient
from app.collectors.schemas import GammaMarketResolution
from app.config.settings import Settings
from app.db.models import Market, MarketResolution
from app.db.session import db_session
from app.utils.time import parse_iso_datetime

logger = logging.getLogger(__name__)

_RESOLVED_STATUS = "resolved"
_WINNING_PRICE = Decimal("1")


class ResolutionClassification:
    """Result of classify_resolution - exactly one of the three shapes:

    - still_open=True: market genuinely hasn't resolved yet (closed=False
      on Gamma itself). Not an error, not written anywhere.
    - ambiguous=True: closed=True but the settlement can't be trusted yet
      (uma_resolution_status isn't "resolved", or the price/token vectors
      don't parse cleanly) - logged and left alone, never guessed.
    - otherwise: a real, confirmed resolution. winning_asset/
      winning_outcome_index are None only for a genuine no-single-winner
      outcome (e.g. a documented 50-50 cancellation rule) - outcome_prices
      still carries the true settlement value in that case.
    """

    def __init__(
        self,
        *,
        still_open: bool = False,
        ambiguous: bool = False,
        winning_asset: str | None = None,
        winning_outcome_index: int | None = None,
        outcome_prices: list[str] | None = None,
        uma_resolution_status: str | None = None,
        resolved_at: datetime | None = None,
    ) -> None:
        self.still_open = still_open
        self.ambiguous = ambiguous
        self.winning_asset = winning_asset
        self.winning_outcome_index = winning_outcome_index
        self.outcome_prices = outcome_prices
        self.uma_resolution_status = uma_resolution_status
        self.resolved_at = resolved_at


def classify_resolution(market: GammaMarketResolution) -> ResolutionClassification:
    """Pure classification of one GammaMarketResolution row - no DB, no
    network, so it can be unit tested directly (same convention as
    app/collectors/categories.py's normalize_category).
    """
    if not market.closed:
        return ResolutionClassification(still_open=True)

    if market.uma_resolution_status != _RESOLVED_STATUS:
        return ResolutionClassification(
            ambiguous=True, uma_resolution_status=market.uma_resolution_status
        )

    prices = market.parsed_outcome_prices
    assets = market.parsed_clob_token_ids
    if not prices or not assets or len(prices) != len(assets):
        return ResolutionClassification(
            ambiguous=True, uma_resolution_status=market.uma_resolution_status
        )

    winning_index = next((i for i, p in enumerate(prices) if p == _WINNING_PRICE), None)
    winning_asset = assets[winning_index] if winning_index is not None else None

    return ResolutionClassification(
        winning_asset=winning_asset,
        winning_outcome_index=winning_index,
        outcome_prices=[str(p) for p in prices],
        uma_resolution_status=market.uma_resolution_status,
        resolved_at=parse_iso_datetime(market.closed_time),
    )


async def collect(client: PolymarketClient, settings: Settings) -> None:
    """One bounded pass over expired-and-unresolved markets."""
    work_list = await asyncio.to_thread(_get_work_list, settings.resolutions_max_per_cycle)
    if not work_list:
        logger.info("resolutions: nothing to check (no expired markets pending resolution)")
        return

    market_rows = await asyncio.gather(
        *(client.get_market_by_id(gamma_id) for _condition_id, gamma_id in work_list)
    )

    resolved = still_open = ambiguous = not_found = 0
    to_persist: list[tuple[str, ResolutionClassification]] = []
    for (condition_id, gamma_id), market_row in zip(work_list, market_rows, strict=True):
        if market_row is None:
            not_found += 1
            logger.warning(
                "resolutions: gamma has no market for id=%s (condition_id=%s)",
                gamma_id,
                condition_id,
            )
            continue

        classification = classify_resolution(market_row)
        if classification.still_open:
            still_open += 1
            continue
        if classification.ambiguous:
            ambiguous += 1
            logger.warning(
                "resolutions: market %s closed but uma_resolution_status=%r not "
                "confirmed resolved - leaving open, not guessing",
                condition_id,
                classification.uma_resolution_status,
            )
            continue

        resolved += 1
        to_persist.append((condition_id, classification))

    if to_persist:
        await asyncio.to_thread(_persist, to_persist, datetime.now(UTC))

    logger.info(
        "resolutions: checked=%d resolved=%d still_open=%d ambiguous=%d not_found=%d",
        len(work_list),
        resolved,
        still_open,
        ambiguous,
        not_found,
    )


def _get_work_list(max_per_cycle: int) -> list[tuple[str, str]]:
    """Every (condition_id, gamma_id) pair for a market that's past
    end_date, not locally marked closed, and has no market_resolutions row
    yet - oldest end_date first, so a large backlog drains in age order.

    The excluded-ids filter is a subquery, never a Python list passed to
    not_in() - same reasoning as app/collectors/markets.py's _get_work_list
    (Postgres's 65535 bind-parameter limit).
    """
    with db_session() as session:
        already_resolved = select(MarketResolution.condition_id)
        rows = session.execute(
            select(Market.condition_id, Market.gamma_id)
            .where(
                Market.closed.is_(False),
                Market.end_date.is_not(None),
                Market.end_date < func.now(),
                Market.condition_id.not_in(already_resolved),
            )
            .order_by(Market.end_date.asc())
            .limit(max_per_cycle)
        ).all()
    return [(row[0], row[1]) for row in rows]


def _persist(to_persist: list[tuple[str, ResolutionClassification]], fetched_at: datetime) -> None:
    """Blocking DB work: upsert market_resolutions, then flip Market.closed
    (active=False, since a resolved market never accepts orders again) for
    the same rows - keeps the markets table's own closed flag in sync
    without waiting for that market's next turn in the ordinary markets
    collector's rotation.
    """
    with db_session() as session:
        for condition_id, classification in to_persist:
            _upsert_resolution(session, condition_id, classification, fetched_at)
            session.execute(
                update(Market)
                .where(Market.condition_id == condition_id)
                .values(closed=True, active=False, last_synced_at=fetched_at)
            )


def _upsert_resolution(
    session: Session,
    condition_id: str,
    classification: ResolutionClassification,
    fetched_at: datetime,
) -> None:
    values = {
        "condition_id": condition_id,
        "winning_asset": classification.winning_asset,
        "winning_outcome_index": classification.winning_outcome_index,
        "outcome_prices": classification.outcome_prices,
        "uma_resolution_status": classification.uma_resolution_status,
        "resolved_at": classification.resolved_at,
        "source": "gamma",
        "fetched_at": fetched_at,
    }
    stmt = pg_insert(MarketResolution).values(**values)
    update_values = {key: value for key, value in values.items() if key != "condition_id"}
    stmt = stmt.on_conflict_do_update(
        index_elements=[MarketResolution.condition_id], set_=update_values
    )
    session.execute(stmt)

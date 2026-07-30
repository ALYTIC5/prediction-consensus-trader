"""Signal generation: DB in, consensus engine, signals out.

Orchestration layer over app/consensus/engine.py - this module does the
loading, grouping, and persisting; the engine does the pure evaluation. See
docs/PHASE3_DESIGN.md section 4.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.collectors.categories import DEFAULT_CATEGORY
from app.config.effective import get_effective_settings
from app.config.settings import Settings, get_settings
from app.consensus.engine import (
    CandidateGroup,
    ConsensusConfig,
    ContributorEvent,
    MarketState,
    Rejection,
    RejectionReason,
    SignalDraft,
    evaluate_group,
)
from app.db.models import (
    ConsensusRun,
    Market,
    PositionHistory,
    PriceSnapshot,
    Signal,
    SignalStatus,
    Wallet,
    WalletCluster,
)
from app.db.session import db_session
from app.optimization.scoring_category import latest_category_scores

logger = logging.getLogger(__name__)

_FUNNEL_ORDER = [
    RejectionReason.MARKET_KNOWN,
    RejectionReason.MARKET_OPEN,
    RejectionReason.LIQUIDITY,
    RejectionReason.PRICE_BAND,
    RejectionReason.SPREAD,
    RejectionReason.BREADTH,
    RejectionReason.QUALITY,
    RejectionReason.CONVICTION,
]


@dataclass(frozen=True)
class _CycleResult:
    expired_count: int
    funnel_line: str
    new_signal_lines: list[str]


async def generate() -> None:
    """Run one full signal-generation cycle: expire, evaluate, persist, log.

    Wrapped in wait_for so a cycle that hangs (DB stall, connection-pool
    starvation, anything with no timeout of its own) raises TimeoutError
    instead of blocking this job's loop forever - the scheduler's per-job
    try/except then logs it and retries next interval, same as any other
    failure. See the settings field for why 600s.
    """
    now = datetime.now(UTC)
    timeout = get_settings().consensus_cycle_timeout_seconds
    result = await asyncio.wait_for(asyncio.to_thread(_run_cycle, now), timeout=timeout)

    if result.expired_count:
        logger.info("signals: expired %d signals", result.expired_count)
    logger.info(result.funnel_line)
    for line in result.new_signal_lines:
        logger.info(line)


def _run_cycle(now: datetime) -> _CycleResult:
    # Already running inside asyncio.to_thread, so it's safe to do this
    # module's own blocking settings/DB work here directly. Fetched fresh
    # every cycle - see docs/PHASE8_DESIGN.md flags 3-4 - so a tuning change
    # takes effect on the very next run, no restart required.
    settings = get_effective_settings()
    config = ConsensusConfig.from_settings(settings)

    with db_session() as session:
        _log_gap_since_last_run(session, now)

        expired_count = _expire_signals(session, now)

        events = _load_events(session, config, now)
        wallet_ids = {event.wallet_id for event in events}
        condition_ids = {event.condition_id for event in events}
        assets = {event.asset for event in events}

        wallets = _load_wallets(session, wallet_ids)
        # Category-aware weighting (docs/PHASE6_DESIGN.md workstream 3,
        # applied per category) - see _build_groups' docstring for why this
        # replaced the flat trader_scores lookup entirely for consensus
        # weighting purposes. TraderScore/compute_score is untouched and
        # still drives is_tracked selection in app/consensus/scorer.py.
        category_scores = latest_category_scores(session, wallet_ids)
        markets = _load_markets(session, condition_ids)
        prices = _latest_prices_by_asset(session, assets)
        cluster_of = _cluster_of_by_address(session, wallets)

        groups = _build_groups(events, wallets, category_scores, markets)

        results: list[SignalDraft | Rejection] = []
        new_lines: list[str] = []
        new_count = 0
        reinforced_count = 0

        for group in groups:
            market = markets.get(group.condition_id)
            current_price = prices.get(group.asset)
            market_state = _market_state(market, current_price)
            result = evaluate_group(group, market_state, config, now, cluster_of)
            results.append(result)

            if isinstance(result, SignalDraft):
                kind = _apply_draft(session, result, now, settings)
                if kind == "new":
                    new_count += 1
                    new_lines.append(_format_new_signal_line(result, market))
                else:
                    reinforced_count += 1

        survived_after = _funnel_counts(len(groups), results)
        funnel_line = _build_funnel_line(
            len(events), len(groups), survived_after, new_count, reinforced_count
        )
        session.add(
            ConsensusRun(
                executed_at=now,
                events=len(events),
                groups=len(groups),
                market=survived_after[RejectionReason.MARKET_OPEN],
                liquidity=survived_after[RejectionReason.LIQUIDITY],
                price=survived_after[RejectionReason.PRICE_BAND],
                spread=survived_after[RejectionReason.SPREAD],
                breadth=survived_after[RejectionReason.BREADTH],
                quality=survived_after[RejectionReason.QUALITY],
                conviction=survived_after[RejectionReason.CONVICTION],
                new_signals=new_count,
                reinforced=reinforced_count,
            )
        )

    return _CycleResult(
        expired_count=expired_count, funnel_line=funnel_line, new_signal_lines=new_lines
    )


def _log_gap_since_last_run(session: Session, now: datetime) -> None:
    """Always logged at WARNING - not because a normal ~5min gap is actually
    a problem, but because the previous silent-hang incident proved INFO-level
    logs are too easy to miss when a job dies quietly. A visible, unmissable
    trail of gaps is the point, even on the healthy path.
    """
    previous = session.execute(select(func.max(ConsensusRun.executed_at))).scalar_one()
    if previous is None:
        logger.warning("consensus: no previous run on record - this is the first cycle")
        return
    gap_seconds = (now - previous).total_seconds()
    logger.warning("consensus: %.0fs since previous successful run", gap_seconds)


def _expire_signals(session: Session, now: datetime) -> int:
    """TTL passed, or the signal's market has since closed."""
    closed_condition_ids = select(Market.condition_id).where(Market.closed.is_(True))
    result = session.execute(
        update(Signal)
        .where(Signal.status == SignalStatus.ACTIVE)
        .where(or_(Signal.expires_at <= now, Signal.condition_id.in_(closed_condition_ids)))
        .values(status=SignalStatus.EXPIRED)
    )
    return result.rowcount


def _load_events(session: Session, config: ConsensusConfig, now: datetime) -> list[PositionHistory]:
    """Fresh, non-bootstrap OPENED (+ INCREASED, if configured) events."""
    event_types = ["OPENED"]
    if config.consensus_include_increases:
        event_types.append("INCREASED")
    freshness_start = now - timedelta(hours=config.consensus_freshness_hours)

    rows = session.execute(
        select(PositionHistory).where(
            PositionHistory.event_type.in_(event_types),
            PositionHistory.is_bootstrap.is_(False),
            PositionHistory.detected_at >= freshness_start,
        )
    ).scalars()
    return list(rows)


def _load_wallets(session: Session, wallet_ids: set[int]) -> dict[int, Wallet]:
    if not wallet_ids:
        return {}
    rows = session.execute(select(Wallet).where(Wallet.id.in_(wallet_ids))).scalars()
    return {wallet.id: wallet for wallet in rows}


def _load_markets(session: Session, condition_ids: set[str]) -> dict[str, Market]:
    if not condition_ids:
        return {}
    rows = session.execute(select(Market).where(Market.condition_id.in_(condition_ids))).scalars()
    return {market.condition_id: market for market in rows}


def _cluster_of_by_address(session: Session, wallets: dict[int, Wallet]) -> dict[str, str]:
    """wallet address -> wallet_clusters.cluster_id, for every wallet
    contributing to this cycle's candidate groups. A wallet absent from
    the result has never been clustered with anyone and is its own
    singleton (docs/PHASE6_DESIGN.md flag 3) - _weighted_score() falls
    back to the wallet's own address as its cluster key in that case, so
    there's nothing to default here.
    """
    if not wallets:
        return {}
    wallet_ids = set(wallets.keys())
    rows = session.execute(
        select(WalletCluster.wallet_id, WalletCluster.cluster_id).where(
            WalletCluster.wallet_id.in_(wallet_ids)
        )
    ).all()
    return {wallets[wallet_id].address: cluster_id for wallet_id, cluster_id in rows}


def _latest_prices_by_asset(session: Session, assets: set[str]) -> dict[str, Decimal]:
    """Each asset's most recent prices row, regardless of age."""
    if not assets:
        return {}
    ranked = (
        select(
            PriceSnapshot.asset,
            PriceSnapshot.price,
            func.row_number()
            .over(partition_by=PriceSnapshot.asset, order_by=PriceSnapshot.captured_at.desc())
            .label("rn"),
        )
        .where(PriceSnapshot.asset.in_(assets))
        .subquery()
    )
    rows = session.execute(select(ranked.c.asset, ranked.c.price).where(ranked.c.rn == 1)).all()
    return dict(rows)


def _acted_size(row: PositionHistory) -> Decimal:
    """How much size actually moved: the full size for an open, the delta for an increase."""
    if row.event_type == "OPENED":
        return row.size_after
    return row.size_after - (row.size_before or Decimal(0))


def _entry_price(row: PositionHistory) -> Decimal:
    """avg_price, falling back to cur_price when avg_price isn't usable."""
    if row.avg_price and row.avg_price > 0:
        return row.avg_price
    return row.cur_price


def _build_groups(
    events: list[PositionHistory],
    wallets: dict[int, Wallet],
    category_scores: dict[tuple[int, str], Decimal],
    markets: dict[str, Market],
) -> list[CandidateGroup]:
    """Builds one CandidateGroup per (condition_id, asset), each
    contributor's weight resolved to that wallet's score IN THIS GROUP'S
    MARKET CATEGORY (docs/PHASE6_DESIGN.md workstream 3, applied per
    category) - not a flat global trader_scores.score.

    ==========================================================================
    THIS COMPOUNDS WITH PHASE 6 WORKSTREAM 1's CLUSTERING FIX. See
    app/consensus/engine.py's _weighted_score() docstring for the cluster
    side: within a cluster, only the MAX weight counts, one vote per
    independent voice. Combined with this category-aware weight: a signal's
    weighted_score is one vote per independent cluster, each weighted by
    that cluster's best member's score IN THIS SIGNAL'S CATEGORY - a
    five-wallet Sports syndicate contributes its single best Sports score to
    a Sports signal, and that same syndicate's best (likely low/neutral)
    Politics score to a Politics signal, never its Sports reputation
    borrowed into an unrelated category.
    ==========================================================================

    A wallet with no row for this category (score never computed, or this
    category has no population data yet) falls back to 0 - low/neutral,
    the same "nothing earned yet" default the old flat-score lookup used,
    never the wallet's global score (docs/PHASE6_DESIGN.md workstream 3: a
    wallet's category score must reflect ITS history in that category, not
    borrow strength from an unrelated one).
    """
    grouped: dict[tuple[str, str], list[ContributorEvent]] = defaultdict(list)
    meta: dict[tuple[str, str], tuple[str, str]] = {}

    for row in events:
        wallet = wallets.get(row.wallet_id)
        if wallet is None:
            continue
        key = (row.condition_id, row.asset)
        market = markets.get(row.condition_id)
        category = market.category if market is not None else DEFAULT_CATEGORY
        grouped[key].append(
            ContributorEvent(
                address=wallet.address,
                username=wallet.username,
                weight=category_scores.get((row.wallet_id, category), Decimal(0)),
                event_type=row.event_type,
                acted_size=_acted_size(row),
                entry_price=_entry_price(row),
                detected_at=row.detected_at,
            )
        )
        meta[key] = (row.outcome, row.title)

    groups = []
    for (condition_id, asset), contributor_events in grouped.items():
        outcome, title = meta[(condition_id, asset)]
        market = markets.get(condition_id)
        event_slug = market.event_slug if market and market.event_slug else ""
        groups.append(
            CandidateGroup(
                condition_id=condition_id,
                asset=asset,
                outcome=outcome,
                title=title,
                event_slug=event_slug,
                events=contributor_events,
            )
        )
    return groups


def _market_state(market: Market | None, current_price: Decimal | None) -> MarketState:
    if market is None:
        return MarketState(
            exists=False,
            closed=False,
            accepting_orders=False,
            end_date=None,
            liquidity=None,
            volume_24h=None,
            spread=None,
            current_price=None,
        )
    return MarketState(
        exists=True,
        closed=market.closed,
        accepting_orders=market.accepting_orders,
        end_date=market.end_date,
        liquidity=market.liquidity,
        volume_24h=market.volume_24h,
        spread=market.spread,
        current_price=current_price if current_price is not None else market.last_trade_price,
    )


def _apply_draft(session: Session, draft: SignalDraft, now: datetime, settings: Settings) -> str:
    """Duplicate detection: reinforce an existing ACTIVE signal, or insert a new one."""
    existing = (
        session.execute(
            select(Signal)
            .where(
                Signal.condition_id == draft.condition_id,
                Signal.asset == draft.asset,
                Signal.status == SignalStatus.ACTIVE,
            )
            .limit(1)
        )
        .scalars()
        .first()
    )

    contributors_json = [
        {
            "address": event.address,
            "username": event.username,
            "weight": str(event.weight),
            "event_type": event.event_type,
            "acted_size": str(event.acted_size),
            "entry_price": str(event.entry_price),
            "detected_at": event.detected_at.isoformat(),
        }
        for event in draft.contributors
    ]

    if existing is not None:
        existing.distinct_traders = draft.distinct_traders
        existing.weighted_score = draft.weighted_score
        existing.combined_entry_value = draft.combined_entry_value
        existing.average_entry_price = draft.average_entry_price
        existing.latest_market_price = draft.latest_market_price
        existing.contributors = contributors_json
        existing.updated_at = now
        return "reinforced"

    session.add(
        Signal(
            condition_id=draft.condition_id,
            asset=draft.asset,
            outcome=draft.outcome,
            title=draft.title,
            event_slug=draft.event_slug,
            side="BUY",
            status=SignalStatus.ACTIVE,
            distinct_traders=draft.distinct_traders,
            weighted_score=draft.weighted_score,
            combined_entry_value=draft.combined_entry_value,
            average_entry_price=draft.average_entry_price,
            latest_market_price=draft.latest_market_price,
            contributors=contributors_json,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=settings.signal_ttl_hours),
        )
    )
    return "new"


def _funnel_counts(
    group_count: int, results: list[SignalDraft | Rejection]
) -> dict[RejectionReason, int]:
    """Survivor count after each filter, cumulative - how many groups had
    NOT yet been rejected once this filter (or an earlier one) had run.
    """
    rejected_counts = Counter(result.reason for result in results if isinstance(result, Rejection))

    survived = group_count
    survived_after: dict[RejectionReason, int] = {}
    for reason in _FUNNEL_ORDER:
        survived -= rejected_counts.get(reason, 0)
        survived_after[reason] = survived
    return survived_after


def _build_funnel_line(
    event_count: int,
    group_count: int,
    survived_after: dict[RejectionReason, int],
    new_count: int,
    reinforced_count: int,
) -> str:
    return (
        f"funnel: {event_count} events -> {group_count} groups -> "
        f"market {survived_after[RejectionReason.MARKET_OPEN]} -> "
        f"liquidity {survived_after[RejectionReason.LIQUIDITY]} -> "
        f"price {survived_after[RejectionReason.PRICE_BAND]} -> "
        f"spread {survived_after[RejectionReason.SPREAD]} -> "
        f"breadth {survived_after[RejectionReason.BREADTH]} -> "
        f"quality {survived_after[RejectionReason.QUALITY]} -> "
        f"conviction {survived_after[RejectionReason.CONVICTION]} -> "
        f"new {new_count}, reinforced {reinforced_count}"
    )


def _format_new_signal_line(draft: SignalDraft, market: Market | None) -> str:
    liquidity = market.liquidity if market and market.liquidity is not None else Decimal(0)
    return (
        f"signal: {draft.title} [{draft.outcome}] traders={draft.distinct_traders} "
        f"weighted_score={draft.weighted_score:.4f} "
        f"combined_value=${draft.combined_entry_value:.2f} "
        f"entry={draft.average_entry_price:.4f} current={draft.latest_market_price:.4f} "
        f"liquidity=${liquidity:.2f}"
    )

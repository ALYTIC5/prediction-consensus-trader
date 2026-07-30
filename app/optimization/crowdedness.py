"""Workstream 7: on-chain crowdedness penalty - pure core + thin DB
loaders. See docs/PHASE6_DESIGN.md workstream 7.

Crowdedness is measured ONLY from on-chain data this project already
collects (position_history, leaderboard_snapshots, markets) - no social/
external data source, scraper, or third-party analytics dependency (that
approach is explicitly out of scope, per the workstream's own scope
discipline section).

Three [0,1] components, each documented at its own function:
- follower_reaction: cross-cluster copying speed/breadth (needs
  Workstream 1's wallet_clusters, which is why this workstream lands after
  it - see flag "why this depends on Workstreams 1 and 2").
- leaderboard_fame: public leaderboard visibility.
- market_obviousness: efficiency of the markets this wallet trades in.

hidden_alpha (skill_score minus a soft crowdedness penalty, clamped to
[0,1]) is a runtime combination, never stored (flag 21) - see
hidden_alpha_score()/hidden_alpha_weighted_score() below, the latter
mirroring app/optimization/bandit.py's effective_weighted_score() shape
exactly (per-wallet adjustment first, then max-per-cluster, then sum).
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import (
    LeaderboardSnapshot,
    Market,
    Position,
    PositionHistory,
    Wallet,
    WalletCluster,
    WalletCrowdedness,
)
from app.db.session import db_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrowdednessConfig:
    reaction_window_minutes: int
    reaction_saturation: Decimal
    liquidity_reference_usd: Decimal
    volume_reference_usd: Decimal
    spread_reference: Decimal
    weight_reaction: Decimal
    weight_fame: Decimal
    weight_obvious: Decimal
    leaderboard_top_n: int
    penalty_weight: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> "CrowdednessConfig":
        return cls(
            reaction_window_minutes=settings.crowd_reaction_window_minutes,
            reaction_saturation=settings.crowd_reaction_saturation,
            liquidity_reference_usd=settings.crowd_liquidity_reference_usd,
            volume_reference_usd=settings.crowd_volume_reference_usd,
            spread_reference=settings.crowd_spread_reference,
            weight_reaction=settings.crowd_weight_reaction,
            weight_fame=settings.crowd_weight_fame,
            weight_obvious=settings.crowd_weight_obvious,
            leaderboard_top_n=settings.leaderboard_top_n,
            penalty_weight=settings.crowd_penalty_weight,
        )


# --- follower_reaction ---


@dataclass(frozen=True)
class OpenedEvent:
    """One wallet's OPENED, non-bootstrap event on one outcome token - the
    only fields follower-counting needs. Mirrors app/optimization/
    cotrading.py's CoTradeEvent (same OPENED-only, wallet_id-keyed
    convention, flag 5/18), minus condition_id, which follower-counting
    doesn't need.
    """

    wallet_id: int
    asset: str
    detected_at: datetime


def compute_follower_reaction(
    events: list[OpenedEvent], cluster_of: dict[int, str], config: CrowdednessConfig
) -> dict[int, Decimal]:
    """wallet_id -> follower_reaction in [0,1].

    Groups events by asset (the same outcome token - matching on asset
    alone already means "same direction," flag 16, no separate check
    needed). For each event (as the "origin"), counts every OTHER event on
    the same asset, in a DIFFERENT cluster, landing within
    reaction_window_minutes afterward - a "follower". A wallet's
    follower_reaction is the average follower count across its own origin
    events, saturated to [0,1] via a clamped-linear scale (flag 17: not
    exponential - auditable over smooth).
    """
    by_asset: dict[str, list[OpenedEvent]] = defaultdict(list)
    for event in events:
        by_asset[event.asset].append(event)

    window = timedelta(minutes=config.reaction_window_minutes)
    follower_counts: dict[int, list[int]] = defaultdict(list)

    for asset_events in by_asset.values():
        for origin in asset_events:
            origin_cluster = cluster_of.get(origin.wallet_id, str(origin.wallet_id))
            count = 0
            for candidate in asset_events:
                if candidate.wallet_id == origin.wallet_id:
                    continue
                delta = candidate.detected_at - origin.detected_at
                if delta < timedelta(0) or delta > window:
                    continue
                candidate_cluster = cluster_of.get(candidate.wallet_id, str(candidate.wallet_id))
                if candidate_cluster == origin_cluster:
                    continue
                count += 1
            follower_counts[origin.wallet_id].append(count)

    return {
        wallet_id: min(
            (sum(Decimal(c) for c in counts) / Decimal(len(counts))) / config.reaction_saturation,
            Decimal(1),
        )
        for wallet_id, counts in follower_counts.items()
    }


def load_opened_events(session: Session, lookback_days: int, now: datetime) -> list[OpenedEvent]:
    """Every tracked wallet's OPENED, non-bootstrap event within the
    lookback window - a settings-bounded window, not a literal random
    subsample (flag 17), same auditability preference flags 6/10 already
    established.
    """
    window_start = now - timedelta(days=lookback_days)
    rows = session.execute(
        select(PositionHistory.wallet_id, PositionHistory.asset, PositionHistory.detected_at)
        .join(Wallet, PositionHistory.wallet_id == Wallet.id)
        .where(
            PositionHistory.event_type == "OPENED",
            PositionHistory.is_bootstrap.is_(False),
            PositionHistory.detected_at >= window_start,
            Wallet.is_tracked.is_(True),
        )
    ).all()
    return [
        OpenedEvent(wallet_id=row.wallet_id, asset=row.asset, detected_at=row.detected_at)
        for row in rows
    ]


def load_cluster_of_by_wallet_id(session: Session) -> dict[int, str]:
    """wallet_id -> cluster_id, straight from wallet_clusters - unlike
    app/optimization/clv.py's address_to_cluster_map (which the consensus-
    facing, address-keyed code needs), everything in this module works in
    wallet_id space until the very end (hidden_alpha_weighted_score, which
    operates on a signal's address-keyed contributors).
    """
    rows = session.execute(select(WalletCluster.wallet_id, WalletCluster.cluster_id)).all()
    return dict(rows)


# --- leaderboard_fame ---


def leaderboard_fame_score(
    ranks: list[int], possible_appearances: int, config: CrowdednessConfig
) -> Decimal:
    """rank_score (median rank, not best - flag: one lucky top-1 snapshot
    shouldn't alone read as "famous") multiplied by appearance_fraction -
    a wallet seen once at rank 1 should not score as famous as one that
    holds rank 1 consistently.
    """
    if not ranks or possible_appearances <= 0:
        return Decimal(0)

    sorted_ranks = sorted(ranks)
    mid = len(sorted_ranks) // 2
    if len(sorted_ranks) % 2 == 1:
        median_rank = Decimal(sorted_ranks[mid])
    else:
        median_rank = (Decimal(sorted_ranks[mid - 1]) + Decimal(sorted_ranks[mid])) / 2

    top_n = Decimal(config.leaderboard_top_n)
    rank_score = max(Decimal(0), Decimal(1) - (median_rank - 1) / (top_n - 1))
    appearance_fraction = min(Decimal(len(ranks)) / Decimal(possible_appearances), Decimal(1))
    return rank_score * appearance_fraction


def load_leaderboard_ranks(
    session: Session, lookback_days: int, now: datetime
) -> tuple[dict[int, list[int]], int]:
    """(wallet_id -> its non-null ranks in the window, possible_appearances
    - the count of distinct snapshot timestamps in the window). Same
    lookback window app/consensus/scorer.py already uses
    (scoring_lookback_days, reused rather than a new setting) and the same
    "distinct captured_at" possible_appearances shape that module's
    _load_score_inputs already establishes.
    """
    window_start = now - timedelta(days=lookback_days)
    possible_appearances = session.execute(
        select(func.count(func.distinct(LeaderboardSnapshot.captured_at))).where(
            LeaderboardSnapshot.captured_at >= window_start
        )
    ).scalar_one()

    rows = session.execute(
        select(LeaderboardSnapshot.wallet_id, LeaderboardSnapshot.rank).where(
            LeaderboardSnapshot.captured_at >= window_start,
            LeaderboardSnapshot.rank.is_not(None),
        )
    ).all()
    ranks_by_wallet: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        ranks_by_wallet[row.wallet_id].append(row.rank)
    return dict(ranks_by_wallet), possible_appearances


# --- market_obviousness ---


@dataclass(frozen=True)
class MarketEfficiency:
    liquidity: Decimal | None
    volume_24h: Decimal | None
    spread: Decimal | None


def _market_obviousness(market: MarketEfficiency, config: CrowdednessConfig) -> Decimal:
    """A market missing any field (not yet synced) contributes 0 for that
    field - "not yet measured" defaults to the least-informative value,
    same convention leaderboard_fame's zero-snapshot case uses.
    """
    liquidity_score = min(
        (market.liquidity or Decimal(0)) / config.liquidity_reference_usd, Decimal(1)
    )
    volume_score = min((market.volume_24h or Decimal(0)) / config.volume_reference_usd, Decimal(1))
    spread = market.spread if market.spread is not None else config.spread_reference
    spread_score = Decimal(1) - min(spread / config.spread_reference, Decimal(1))
    return (liquidity_score + volume_score + spread_score) / Decimal(3)


def market_obviousness_score(markets: list[MarketEfficiency], config: CrowdednessConfig) -> Decimal:
    """Average per-market efficiency across the distinct markets behind
    this wallet's currently-open positions. No open positions this cycle
    scores 0 - "nothing to measure," not "provably in thin markets."
    """
    if not markets:
        return Decimal(0)
    scores = [_market_obviousness(m, config) for m in markets]
    return sum(scores, Decimal(0)) / Decimal(len(scores))


def load_open_market_efficiency(session: Session) -> dict[int, list[MarketEfficiency]]:
    """wallet_id -> MarketEfficiency for each DISTINCT market behind its
    open positions (a wallet holding both outcomes of one market counts
    that market once, not twice - "the markets this wallet trades," not
    "the positions it holds").
    """
    rows = session.execute(
        select(
            Position.wallet_id,
            Position.condition_id,
            Market.liquidity,
            Market.volume_24h,
            Market.spread,
        )
        .join(Market, Market.condition_id == Position.condition_id)
        .join(Wallet, Wallet.id == Position.wallet_id)
        .where(Position.is_open.is_(True), Wallet.is_tracked.is_(True))
    ).all()

    seen: dict[int, set[str]] = defaultdict(set)
    result: dict[int, list[MarketEfficiency]] = defaultdict(list)
    for row in rows:
        if row.condition_id in seen[row.wallet_id]:
            continue
        seen[row.wallet_id].add(row.condition_id)
        result[row.wallet_id].append(
            MarketEfficiency(liquidity=row.liquidity, volume_24h=row.volume_24h, spread=row.spread)
        )
    return dict(result)


# --- crowdedness combination ---


@dataclass(frozen=True)
class CrowdednessBreakdown:
    follower_reaction: Decimal
    leaderboard_fame: Decimal
    market_obviousness: Decimal
    crowdedness: Decimal


def compute_crowdedness(
    follower_reaction: Decimal,
    leaderboard_fame: Decimal,
    market_obviousness: Decimal,
    config: CrowdednessConfig,
) -> CrowdednessBreakdown:
    crowdedness = (
        config.weight_reaction * follower_reaction
        + config.weight_fame * leaderboard_fame
        + config.weight_obvious * market_obviousness
    )
    crowdedness = min(max(crowdedness, Decimal(0)), Decimal(1))
    return CrowdednessBreakdown(
        follower_reaction=follower_reaction,
        leaderboard_fame=leaderboard_fame,
        market_obviousness=market_obviousness,
        crowdedness=crowdedness,
    )


# --- hidden alpha ---


def hidden_alpha_score(
    skill_score: Decimal, crowdedness: Decimal, penalty_weight: Decimal
) -> Decimal:
    """skill_score minus a SOFT crowdedness penalty (subtraction, never a
    hard cutoff or multiplier), clamped to [0,1] - a very-high-skill
    crowded wallet still scores respectably, never zeroed out on
    crowdedness alone (docs/PHASE6_DESIGN.md workstream 7).
    """
    raw = skill_score - penalty_weight * crowdedness
    return min(max(raw, Decimal(0)), Decimal(1))


def hidden_alpha_weighted_score(
    contributors: list[dict],
    cluster_of: dict[str, str],
    crowdedness_of: dict[str, Decimal],
    penalty_weight: Decimal,
) -> Decimal:
    """Mirrors app/optimization/bandit.py's effective_weighted_score() shape
    exactly: adjust each contributor's own weight first (there: multiply by
    the cluster's bandit multiplier; here: subtract the wallet's own
    crowdedness penalty via hidden_alpha_score), THEN keep only the max
    adjusted weight within each cluster, THEN sum across clusters - one
    vote per independent cluster, same as everywhere else in this phase.
    A wallet absent from crowdedness_of (never computed yet) defaults to 0
    crowdedness - no penalty, not a hard fallback to some other score.
    """
    max_by_cluster: dict[str, Decimal] = {}
    for contributor in contributors:
        address = contributor["address"]
        skill_score = Decimal(contributor["weight"])
        crowdedness = crowdedness_of.get(address, Decimal(0))
        adjusted = hidden_alpha_score(skill_score, crowdedness, penalty_weight)
        cluster_id = cluster_of.get(address, address)
        current = max_by_cluster.get(cluster_id)
        if current is None or adjusted > current:
            max_by_cluster[cluster_id] = adjusted
    return sum(max_by_cluster.values(), Decimal(0))


# --- orchestration ---


def run_crowdedness_updates(settings: Settings) -> int:
    """Rebuilds wallet_crowdedness from scratch for every tracked wallet
    with at least one measurable component (flag 20: current-state, delete
    + reinsert, same convention as wallet_clusters/cluster_bandit_state).
    """
    config = CrowdednessConfig.from_settings(settings)
    now = datetime.now(UTC)

    with db_session() as session:
        opened_events = load_opened_events(session, settings.crowd_reaction_lookback_days, now)
        cluster_of = load_cluster_of_by_wallet_id(session)
        ranks_by_wallet, possible_appearances = load_leaderboard_ranks(
            session, settings.scoring_lookback_days, now
        )
        markets_by_wallet = load_open_market_efficiency(session)
        tracked_wallet_ids = set(
            session.execute(select(Wallet.id).where(Wallet.is_tracked.is_(True))).scalars()
        )

    reaction_by_wallet = compute_follower_reaction(opened_events, cluster_of, config)

    rows = []
    for wallet_id in tracked_wallet_ids:
        follower_reaction = reaction_by_wallet.get(wallet_id, Decimal(0))
        fame = leaderboard_fame_score(
            ranks_by_wallet.get(wallet_id, []), possible_appearances, config
        )
        obviousness = market_obviousness_score(markets_by_wallet.get(wallet_id, []), config)
        breakdown = compute_crowdedness(follower_reaction, fame, obviousness, config)
        rows.append(
            WalletCrowdedness(
                wallet_id=wallet_id,
                follower_reaction=breakdown.follower_reaction,
                leaderboard_fame=breakdown.leaderboard_fame,
                market_obviousness=breakdown.market_obviousness,
                crowdedness=breakdown.crowdedness,
                computed_at=now,
            )
        )

    with db_session() as session:
        session.execute(delete(WalletCrowdedness))
        if rows:
            session.add_all(rows)

    logger.info("crowdedness: wrote %d wallet scores", len(rows))
    return len(rows)


async def run_crowdedness_job() -> None:
    """Scheduler entrypoint - same asyncio.to_thread + get_effective_settings
    pattern every other Phase 6 job uses.
    """
    settings = get_effective_settings()
    await asyncio.to_thread(run_crowdedness_updates, settings)


def crowdedness_by_address(session: Session) -> dict[str, Decimal]:
    """wallet address -> current crowdedness - what the "hidden_alpha"
    portfolio's entry filter reads every cycle. A wallet absent from this
    map (crowdedness never computed, or the wallet isn't tracked) defaults
    to 0 in hidden_alpha_weighted_score, not a penalty for missing data.
    """
    rows = session.execute(
        select(Wallet.address, WalletCrowdedness.crowdedness).join(
            WalletCrowdedness, WalletCrowdedness.wallet_id == Wallet.id
        )
    ).all()
    return dict(rows)

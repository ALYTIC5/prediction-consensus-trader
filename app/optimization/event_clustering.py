"""Event cluster assignment - correlated markets are not independent
observations.

Every statistical claim this project makes (portfolio win rate, calibration
bucket p_hat, Scout validation-window CI) implicitly assumes each trade is
an independent draw. That assumption breaks whenever multiple trades are
really the same underlying bet: 8 markets on 8 different games in one
tournament, or 5 markets on 5 outcomes of one election, mostly rise and
fall together because they share the same real-world driver - a team's
form, a candidate's polling. Counting them as 8 (or 5) independent
observations inflates every confidence interval and lets an insufficient-
sample guard pass when it shouldn't.

Clustering rule, in priority order:
1. Shared event_slug - Polymarket's own grouping of markets that belong to
   one event (a multi-outcome election, a tournament bracket). This is the
   strongest signal available and is used whenever present.
2. Fallback for markets with no event_slug: bucketed by
   (category, floor(end_date / EVENT_CLUSTER_DATE_BUCKET_HOURS)) - same
   category, resolving within the same fixed-width time window. Catches
   correlated markets Polymarket didn't explicitly link (e.g. many
   single-game sports markets resolving the same night).
3. A market with no event_slug AND no end_date (rare, but not impossible -
   see Market.end_date's nullable column) gets its own singleton cluster,
   keyed by its own condition_id - "no evidence of correlation" defaults
   to independent, same convention app/optimization/clustering.py's Louvain
   wallet clustering already uses for an isolated wallet.

Known failure modes (read before trusting a cluster count too far):
- False negative: two markets genuinely about the same real-world event
  but Polymarket gave them different event_slugs (does happen - event
  grouping is Polymarket's own editorial choice, not guaranteed complete).
  Effect: effective n is overstated for these - some real correlation goes
  uncounted.
- False positive: two markets that share a category and happen to resolve
  in the same bucket window but are economically unrelated (e.g. two
  unrelated crypto markets both resolving at a midnight UTC cutoff).
  Effect: effective n is understated - the conservative-direction error,
  and the one this module is willing to make when forced to choose, since
  overconfidence is the actual problem being guarded against here.
- The date-bucket fallback has a hard boundary: two markets one hour apart
  but straddling a bucket edge land in different clusters even though nothing
  about the underlying correlation changed at that instant. A fixed-width
  floor bucket is a deliberate simplicity trade-off, not an attempt at a
  true adjacency clustering over time.
- category itself is coarse (app/collectors/categories.py's five buckets) -
  a wide EVENT_CLUSTER_DATE_BUCKET_HOURS window widens the false-positive
  risk above proportionally to how much unrelated same-category volume
  exists in that window. Keep this window narrow; it is deliberately small
  by default (24h) for exactly this reason.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import Market, PaperTrade, ScoutForwardTrade
from app.db.session import db_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketClusterInput:
    """One market's clustering-relevant fields - plain values, not an ORM
    row, so assign_event_clusters stays DB-free and unit-testable.
    """

    condition_id: str
    event_slug: str | None
    category: str
    end_date: datetime | None


@dataclass(frozen=True)
class ClusterAssignment:
    condition_id: str
    event_cluster_id: str


def assign_event_clusters(
    markets: Sequence[MarketClusterInput], date_bucket_hours: int
) -> list[ClusterAssignment]:
    """The pure clustering rule - see the module docstring for the full
    priority order and failure modes. Cluster ids are human-readable, not
    a content hash: "event:<slug>" or "catdate:<category>:<bucket>" are
    directly auditable in a query result without a lookup table, and
    already stable/deterministic on their own (unlike the wallet-cluster
    hash in app/optimization/clustering.py, which needs a hash specifically
    because Louvain community membership isn't already a natural key).
    """
    assignments: list[ClusterAssignment] = []
    for market in markets:
        if market.event_slug:
            cluster_id = f"event:{market.event_slug}"
        elif market.end_date is not None:
            bucket_seconds = date_bucket_hours * 3600
            bucket_index = int(market.end_date.timestamp() // bucket_seconds)
            cluster_id = f"catdate:{market.category}:{bucket_index}"
        else:
            cluster_id = f"solo:{market.condition_id}"
        assignments.append(
            ClusterAssignment(condition_id=market.condition_id, event_cluster_id=cluster_id)
        )
    return assignments


def cluster_key_for(condition_id: str, event_cluster_id: str | None) -> str:
    """The grouping key effective_sample_size uses for one trade. A market
    that hasn't been clustered yet (event_cluster_id is None - the
    clustering job hasn't reached it, e.g. a brand-new market this exact
    cycle) falls back to a singleton key derived from its own condition_id,
    never merged with other not-yet-clustered markets just because they
    share the same placeholder - "unknown correlation" defaults to
    independent, same convention cluster_id_for's isolate case uses.
    """
    return event_cluster_id if event_cluster_id is not None else f"unclustered:{condition_id}"


def effective_sample_size(cluster_keys: Sequence[str]) -> int:
    """The number of distinct event clusters represented - this project's
    effective sample size wherever a trade/record count would otherwise be
    read as N independent observations.

    Chosen over a sqrt(n)-within-cluster down-weighting (the other option
    on the table): every trade sharing one event cluster must collapse to
    effective n=1, a hard invariant this project's own tests enforce, and
    sqrt(n) only equals 1 when n=1 - it cannot satisfy that for a cluster
    of any real size. Counting a cluster as exactly one observation is also
    the more conservative (and typically more honest) choice given how
    strongly correlated companion markets on the same real-world event
    usually are in practice - within-cluster correlation for prediction
    markets sharing a driver is often close to 1, not some intermediate
    value a sqrt() discount would imply.
    """
    return len(set(cluster_keys))


def load_condition_ids_needing_clusters(session: Session) -> set[str]:
    """Every condition_id actually used in a statistical claim (a paper
    trade or a Scout forward trade) that doesn't have a cluster assignment
    yet. Clustering only what's actually measured, not every market ever
    synced (67,000+ and counting) - same narrow, purpose-driven work-list
    convention as app/collectors/orderbook.py and app/collectors/
    fee_rates.py. Safe to IN-list directly (unlike app/collectors/
    markets.py's position-id lesson): trade/forward-trade condition_ids
    are observed in the hundreds, nowhere near Postgres's 65535
    bind-parameter limit.
    """
    trade_ids = set(session.execute(select(PaperTrade.condition_id).distinct()).scalars())
    forward_ids = set(session.execute(select(ScoutForwardTrade.condition_id).distinct()).scalars())
    all_ids = trade_ids | forward_ids
    if not all_ids:
        return set()
    already_clustered = set(
        session.execute(
            select(Market.condition_id).where(
                Market.condition_id.in_(all_ids), Market.event_cluster_id.is_not(None)
            )
        ).scalars()
    )
    return all_ids - already_clustered


def load_market_cluster_inputs(
    session: Session, condition_ids: set[str]
) -> list[MarketClusterInput]:
    if not condition_ids:
        return []
    rows = session.execute(
        select(Market.condition_id, Market.event_slug, Market.category, Market.end_date).where(
            Market.condition_id.in_(condition_ids)
        )
    ).all()
    return [
        MarketClusterInput(
            condition_id=row.condition_id,
            event_slug=row.event_slug,
            category=row.category,
            end_date=row.end_date,
        )
        for row in rows
    ]


def run_event_clustering(settings: Settings) -> int:
    """The full pipeline: find condition_ids used in a statistical claim
    that aren't clustered yet, assign them, write event_cluster_id.
    Incremental by construction (only ever queries/writes the not-yet-
    clustered subset) - a market's clustering inputs (event_slug, category,
    end_date) are fixed once Gamma first reports them, so there is nothing
    to gain from ever recomputing an existing assignment.
    """
    with db_session() as session:
        condition_ids = load_condition_ids_needing_clusters(session)
        inputs = load_market_cluster_inputs(session, condition_ids)

    assignments = assign_event_clusters(inputs, settings.event_cluster_date_bucket_hours)

    with db_session() as session:
        for assignment in assignments:
            session.execute(
                update(Market)
                .where(Market.condition_id == assignment.condition_id)
                .values(event_cluster_id=assignment.event_cluster_id)
            )

    cluster_count = len({a.event_cluster_id for a in assignments})
    logger.info(
        "event_clustering: assigned %d markets -> %d event clusters",
        len(assignments),
        cluster_count,
    )
    return len(assignments)


async def run_event_clustering_job() -> None:
    """Scheduler entrypoint - same asyncio.to_thread + get_effective_settings
    pattern every other Phase 6 job uses.
    """
    settings = get_effective_settings()
    await asyncio.to_thread(run_event_clustering, settings)

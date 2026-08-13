"""Community detection over the co-trading graph, and the wallet_clusters
write path. See docs/PHASE6_DESIGN.md workstream 1.

Only `networkx` is a new dependency, not `python-louvain` too: networkx
>=2.8 ships Louvain natively as
networkx.algorithms.community.louvain_communities, so the separate
python-louvain package adds nothing (design flag 4). Community detection
itself (assign_clusters) takes a plain wallet-address mapping and a plain
edge list - no DB - keeping only run_clustering() as the DB-touching
orchestration entrypoint, same split as app/optimization/cotrading.py.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import networkx as nx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import Wallet, WalletCluster
from app.db.session import db_session
from app.optimization.cotrading import (
    CoTradeEdge,
    CoTradingConfig,
    build_cotrading_edges,
    load_cotrade_events,
)

logger = logging.getLogger(__name__)

# Deterministic - the same graph must produce the same partition every run,
# not a different one each time purely from RNG (Louvain's own tie-breaking
# uses randomness internally). A fixed seed is what makes cluster_size and
# membership reproducible between two runs over identical position_history.
_LOUVAIN_SEED = 0


@dataclass(frozen=True)
class ClusterAssignment:
    wallet_id: int
    cluster_id: str
    cluster_size: int


@dataclass(frozen=True)
class ClusteringSummary:
    """What run_clustering() reports back - the numbers worth logging (and,
    per the operator's request, worth reading off after the first live run):
    how many independent clusters came out of how many tracked wallets, and
    how big the largest one is - a big cluster is a probable syndicate that
    was previously counted as that many independent voices.
    """

    tracked_wallet_count: int
    cluster_count: int
    largest_cluster_size: int


def cluster_id_for(member_addresses: list[str]) -> str:
    """A stable, content-derived id - sha256 of the sorted member addresses,
    truncated to 16 hex chars (docs/PHASE6_DESIGN.md flag 1). Two runs that
    produce an identical cluster get the identical id; a cluster whose
    membership actually changes gets a new one - correct, since a changed
    membership is a genuinely different independent-voice profile and
    shouldn't inherit an unrelated reward history (Workstream 5).

    Public (unlike most of this module's helpers): app/optimization/bandit.py
    also calls this directly, for a contributor wallet that isn't in
    wallet_clusters at all (never tracked, or tracked but not yet clustered).
    That wallet is its own singleton "cluster" by the exact same definition a
    real Louvain singleton already gets - cluster_id_for([address]) - so it
    always produces a bounded, deterministic id in the same 16-hex-char shape
    every other cluster_id has, never the wallet's raw (and much longer)
    address itself. Using the raw address there is what overflowed
    cluster_bandit_state.cluster_id's VARCHAR column and took the whole
    Workstream 5 bandit job down on every run - see bandit.py's
    _resolve_cluster_id().
    """
    joined = ",".join(sorted(address.lower() for address in member_addresses))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def assign_clusters(
    wallet_addresses: dict[int, str], edges: list[CoTradeEdge]
) -> list[ClusterAssignment]:
    """Partitions every tracked wallet (present in wallet_addresses) into
    clusters via Louvain community detection over the co-trading graph.
    A wallet with no edges at all ends up in its own singleton community -
    Louvain never merges an isolated node into another community, since
    doing so can only ever decrease modularity (docs/PHASE6_DESIGN.md flag
    3: no evidence of co-trading means no evidence of collusion, so
    "independent" is the correct default, not a workaround).
    """
    graph = nx.Graph()
    graph.add_nodes_from(wallet_addresses.keys())
    for edge in edges:
        if edge.wallet_a in wallet_addresses and edge.wallet_b in wallet_addresses:
            graph.add_edge(edge.wallet_a, edge.wallet_b, weight=edge.weight)

    communities = nx.algorithms.community.louvain_communities(
        graph, weight="weight", seed=_LOUVAIN_SEED
    )

    assignments: list[ClusterAssignment] = []
    for community in communities:
        member_addresses = [wallet_addresses[wallet_id] for wallet_id in community]
        cluster_id = cluster_id_for(member_addresses)
        cluster_size = len(community)
        for wallet_id in community:
            assignments.append(
                ClusterAssignment(
                    wallet_id=wallet_id, cluster_id=cluster_id, cluster_size=cluster_size
                )
            )
    return assignments


def load_tracked_wallets(session: Session) -> dict[int, str]:
    rows = session.execute(
        select(Wallet.id, Wallet.address).where(Wallet.is_tracked.is_(True))
    ).all()
    return {row.id: row.address for row in rows}


def run_clustering(settings: Settings) -> ClusteringSummary:
    """The full offline pipeline: load events + tracked wallets, build the
    co-trading graph, run community detection, replace wallet_clusters with
    the fresh assignment. Rebuilt from scratch every run, not an
    incremental update (docs/PHASE6_DESIGN.md workstream 1) - simpler, and
    self-correcting for wallets that stop (or start) being tracked.
    """
    config = CoTradingConfig(
        window_minutes=settings.cotrade_window_minutes,
        min_shared_markets=settings.cotrade_min_shared_markets,
    )

    with db_session() as session:
        events = load_cotrade_events(session)
        wallet_addresses = load_tracked_wallets(session)

    edges = build_cotrading_edges(events, config)
    assignments = assign_clusters(wallet_addresses, edges)

    now = datetime.now(UTC)
    with db_session() as session:
        session.execute(delete(WalletCluster))
        session.add_all(
            [
                WalletCluster(
                    wallet_id=a.wallet_id,
                    cluster_id=a.cluster_id,
                    cluster_size=a.cluster_size,
                    computed_at=now,
                )
                for a in assignments
            ]
        )

    cluster_count = len({a.cluster_id for a in assignments})
    largest_cluster_size = max((a.cluster_size for a in assignments), default=0)
    summary = ClusteringSummary(
        tracked_wallet_count=len(wallet_addresses),
        cluster_count=cluster_count,
        largest_cluster_size=largest_cluster_size,
    )
    logger.info(
        "clustering: %d tracked wallets -> %d clusters (largest %d)",
        summary.tracked_wallet_count,
        summary.cluster_count,
        summary.largest_cluster_size,
    )
    return summary


async def run_clustering_job() -> None:
    """Scheduler entrypoint - fetches effective settings fresh each run
    (same convention as app/signals/generator.py's generate() and
    app/paper/engine.py's run_cycle()) and runs the blocking DB/graph work
    in a thread, off the event loop.
    """
    settings = get_effective_settings()
    await asyncio.to_thread(run_clustering, settings)

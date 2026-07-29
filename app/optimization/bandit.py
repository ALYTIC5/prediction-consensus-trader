"""Workstream 5: adaptive whale selection - pure bandit core + a thin DB
loader/writer. See docs/PHASE6_DESIGN.md workstream 5.

Reward is binarized CLV (clv_horizon > 0), not paper PnL - CLV is
available for every signal within CLV_HORIZON_HOURS, long before enough
trades resolve for PnL to be a meaningful signal (the same reasoning
Workstream 2 gives for CLV over PnL generally, applied here).

The multiplier is the Beta posterior MEAN, not a literal per-cycle
Thompson-sampled draw (docs/PHASE6_DESIGN.md flag 13): every consensus
cycle weighs every cluster simultaneously, and the identical signal stream
is read by every non-challenger portfolio, so a randomized multiplier
would make results non-reproducible run to run for no benefit. "Thompson-
style" describes the Beta-Bernoulli conjugate update, not per-cycle
sampling.

Bandit state is recomputed from scratch every run, not updated
incrementally - same convention app/optimization/clustering.py already
uses for the same reason: simpler and self-correcting than tracking which
signals have already been folded in, and just as auditable ("this
cluster's multiplier reflects exactly these N observations, right now")
as an incremental alternative would be.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import ClusterBanditState, Signal, SignalCLV
from app.db.session import db_session
from app.optimization.clv import address_to_cluster_map

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BanditConfig:
    weight_min: Decimal
    weight_max: Decimal
    min_signals: int


@dataclass(frozen=True)
class BanditState:
    """alpha/beta are a Beta(1,1) (uniform) prior updated by +1 per
    observation - alpha on a positive-CLV signal, beta otherwise.
    """

    cluster_id: str
    alpha: int
    beta: int
    observations: int


@dataclass(frozen=True)
class BanditUpdateSummary:
    clusters_updated: int


def initial_state(cluster_id: str) -> BanditState:
    return BanditState(cluster_id=cluster_id, alpha=1, beta=1, observations=0)


def update_state(state: BanditState, positive: bool) -> BanditState:
    """One new observation folded in - alpha+1 on a positive-CLV signal,
    beta+1 otherwise.
    """
    return BanditState(
        cluster_id=state.cluster_id,
        alpha=state.alpha + (1 if positive else 0),
        beta=state.beta + (0 if positive else 1),
        observations=state.observations + 1,
    )


def compute_state_from_observations(cluster_id: str, observations: list[bool]) -> BanditState:
    state = initial_state(cluster_id)
    for positive in observations:
        state = update_state(state, positive)
    return state


def compute_multiplier(state: BanditState, config: BanditConfig) -> Decimal:
    """Below min_signals: exactly neutral (1.0) - not enough evidence to
    trust the posterior yet, so the cluster's base score is used unadjusted
    (docs/PHASE6_DESIGN.md workstream 5). At/above it: 2x the posterior
    mean - a 0.5 mean (a coin-flip CLV hit rate) maps to 1.0 (neutral),
    above/below scales weight up/down - clamped to [weight_min, weight_max]
    so a hot or cold streak can never fully dominate or silence a cluster.
    """
    if state.observations < config.min_signals:
        return Decimal("1.0")
    mean = Decimal(state.alpha) / Decimal(state.alpha + state.beta)
    multiplier = 2 * mean
    return max(config.weight_min, min(multiplier, config.weight_max))


def effective_weighted_score(
    contributors: list[dict], cluster_of: dict[str, str], multiplier_of: dict[str, Decimal]
) -> Decimal:
    """Recomputes weighted_score the same way app/consensus/engine.py's
    _weighted_score() does (max weight per cluster, summed across
    clusters) - but scaling each contributor's weight by its cluster's
    current adaptive multiplier first. This is how the "adaptive" challenger
    portfolio applies cluster multipliers (docs/PHASE6_DESIGN.md flag 14):
    at entry-filter time, from the signal's own stored contributors, never
    by forking signal generation - every portfolio still sees the exact
    same signal stream, only what counts as "enough" score differs.
    """
    max_weight_by_cluster: dict[str, Decimal] = {}
    for contributor in contributors:
        address = contributor["address"]
        cluster_id = cluster_of.get(address, address)
        multiplier = multiplier_of.get(cluster_id, Decimal("1.0"))
        weight = Decimal(contributor["weight"]) * multiplier
        current = max_weight_by_cluster.get(cluster_id)
        if current is None or weight > current:
            max_weight_by_cluster[cluster_id] = weight
    return sum(max_weight_by_cluster.values(), Decimal(0))


def load_cluster_observations(session: Session) -> dict[str, list[bool]]:
    """Every (cluster, positive-CLV?) observation from every signal whose
    clv_horizon is known - a signal co-signed by multiple independent
    clusters contributes one observation to EACH of them (same convention
    app/optimization/clv.py's aggregate_clv_by_cluster already uses).
    """
    rows = session.execute(
        select(Signal.contributors, SignalCLV.clv_horizon)
        .join(SignalCLV, SignalCLV.signal_id == Signal.id)
        .where(SignalCLV.clv_horizon.isnot(None))
    ).all()
    cluster_of = address_to_cluster_map(session)

    observations: dict[str, list[bool]] = {}
    for contributors, clv in rows:
        clusters_seen = {cluster_of.get(c["address"], c["address"]) for c in contributors}
        positive = clv > 0
        for cluster_id in clusters_seen:
            observations.setdefault(cluster_id, []).append(positive)
    return observations


def run_bandit_updates(settings: Settings) -> BanditUpdateSummary:
    """Rebuilds cluster_bandit_state from scratch from every signal with a
    known clv_horizon - see the module docstring for why "from scratch,"
    not incremental.
    """
    config = BanditConfig(
        weight_min=settings.adaptive_weight_min,
        weight_max=settings.adaptive_weight_max,
        min_signals=settings.adaptive_min_signals,
    )
    now = datetime.now(UTC)

    with db_session() as session:
        observations = load_cluster_observations(session)
        states = [
            compute_state_from_observations(cluster_id, obs)
            for cluster_id, obs in observations.items()
        ]

        session.execute(delete(ClusterBanditState))
        session.add_all(
            [
                ClusterBanditState(
                    cluster_id=state.cluster_id,
                    alpha=state.alpha,
                    beta=state.beta,
                    observations=state.observations,
                    adaptive_multiplier=compute_multiplier(state, config),
                    updated_at=now,
                )
                for state in states
            ]
        )

    summary = BanditUpdateSummary(clusters_updated=len(states))
    logger.info("bandit: clusters_updated=%d", summary.clusters_updated)
    return summary


async def run_bandit_job() -> None:
    """Scheduler entrypoint - same convention as every other Phase 5/6
    periodic job (fetch effective settings fresh, run the blocking DB work
    in a thread).
    """
    settings = get_effective_settings()
    await asyncio.to_thread(run_bandit_updates, settings)


def load_multipliers(session: Session) -> dict[str, Decimal]:
    """cluster_id -> current adaptive_multiplier - what the "adaptive"
    portfolio's entry filter reads every cycle (docs/PHASE6_DESIGN.md flag
    14). A cluster absent from this map (never observed, or never reached
    ADAPTIVE_MIN_SIGNALS) is neutral - effective_weighted_score() already
    defaults to 1.0 for any cluster_id missing from this dict.
    """
    rows = session.execute(
        select(ClusterBanditState.cluster_id, ClusterBanditState.adaptive_multiplier)
    ).all()
    return dict(rows)

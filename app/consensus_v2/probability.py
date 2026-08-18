"""Whale-consensus implied probability - pure, no DB, no network.

See docs/CONSENSUS_V2_DESIGN.md section 1 for the full formula, the
dominant-position cap, and every documented failure mode. This module
never sees a wallet or a raw position - the caller (app/consensus_v2/
scan.py) has already aggregated per-cluster, exactly the same DB/pure
split app/consensus/engine.py itself uses.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ClusterPosition:
    """One cluster's aggregated stake in one binary market's scored
    outcome - already summed across every member wallet's position in
    this market (app/optimization/clustering.py's wallet_clusters), and
    already resolved to the cluster's MAX member TraderCategoryScore for
    this market's category (docs/CONSENSUS_V2_DESIGN.md's flagged
    decision: category-aware, not the flat global score).
    """

    cluster_id: str
    position_usd: Decimal  # always >= 0 - direction lives in sign, not here
    sign: int  # +1 (holds the scored outcome) or -1 (holds the complement)
    score: Decimal  # cluster's max member TraderCategoryScore, in [0, 1]


@dataclass(frozen=True)
class ConsensusProbabilityResult:
    p_consensus: Decimal
    confidence: Decimal
    n_clusters: int
    total_weight_usd: Decimal


def cap_cluster_weights(weights: dict[str, Decimal], max_fraction: Decimal) -> dict[str, Decimal]:
    """Caps each cluster's weight (score x position_usd) at max_fraction
    of the FINAL total - "no one cluster is more than X% of the
    consensus" self-consistently, since capping one cluster shrinks the
    total, which can require capping it further (or capping others) in
    turn. Solved exactly, not by fixed-point iteration: peel clusters off
    largest-first, at each step checking whether the largest remaining
    ORIGINAL weight would still exceed max_fraction of the total that
    results from capping everything peeled off so far plus leaving
    everything else at its original weight. This is the standard closed-
    form "water-filling" cap and converges in at most len(weights) steps,
    exactly, not approximately - a naive iteration that freezes a
    cluster's value the first time it's capped is wrong, because the
    total keeps shrinking as more clusters get capped, which can put an
    already-capped cluster's frozen value back over the (now smaller) cap.

    With very few clusters (1-2), max_fraction may be mathematically
    unsatisfiable for everyone at once (2 clusters can't both be <=35% of
    their own total - one must hold >=50% by pigeonhole). In that case
    every cluster ends up capped and equally split - see
    docs/CONSENSUS_V2_DESIGN.md's "Dominant-position cap" section:
    CONFIDENCE (via n_clusters) is what actually protects downstream
    consumers when this happens, not this function.
    """
    if not weights:
        return {}
    items = sorted(weights.items(), key=lambda kv: -kv[1])
    n = len(items)

    k = 0  # number of largest clusters treated as "capped" (binding)
    while k < n and Decimal(1) - Decimal(k + 1) * max_fraction > 0:
        free_sum = sum((w for _, w in items[k + 1 :]), Decimal(0))
        denom = Decimal(1) - Decimal(k + 1) * max_fraction
        candidate_total = free_sum / denom
        candidate_weight = items[k][1]
        if candidate_weight > max_fraction * candidate_total:
            k += 1
        else:
            break

    if k == 0:
        return dict(weights)

    denom = Decimal(1) - Decimal(k) * max_fraction
    if k == n or denom <= 0:
        # Every cluster ended up capped (or capping them all still can't
        # satisfy max_fraction) - too few clusters for this fraction to
        # be achievable at all (see docstring). The only self-consistent
        # answer left is an equal split of the total.
        total = sum(weights.values(), Decimal(0))
        equal_share = total / Decimal(n)
        return {cid: equal_share for cid, _ in items}

    free_sum = sum((w for _, w in items[k:]), Decimal(0))

    total = free_sum / denom
    cap_value = max_fraction * total
    result = dict(weights)
    for cid, _ in items[:k]:
        result[cid] = cap_value
    return result


def compute_p_consensus(
    positions: Sequence[ClusterPosition], max_cluster_weight_fraction: Decimal
) -> Decimal | None:
    """See docs/CONSENSUS_V2_DESIGN.md section 1. None means "no opinion" -
    no qualifying positions, or the total weight is zero (every score was
    zero) - never a fabricated 0.5.
    """
    if not positions:
        return None
    raw_weights = {p.cluster_id: p.score * p.position_usd for p in positions}
    total_raw = sum(raw_weights.values(), Decimal(0))
    if total_raw <= 0:
        return None

    capped_weights = cap_cluster_weights(raw_weights, max_cluster_weight_fraction)
    denominator = sum(capped_weights.values(), Decimal(0))
    if denominator <= 0:
        return None
    numerator = sum((capped_weights[p.cluster_id] * p.sign for p in positions), Decimal(0))
    raw = numerator / denominator
    p_consensus = (raw + Decimal(1)) / Decimal(2)
    return max(Decimal(0), min(Decimal(1), p_consensus))


def compute_confidence(
    positions: Sequence[ClusterPosition],
    n_clusters_target: int,
    notional_target: Decimal,
) -> Decimal:
    """Four components in [0, 1], multiplied together (docs/
    CONSENSUS_V2_DESIGN.md section 1) - every component must be decent,
    not just the average, so one strong component can't mask a weak one.
    0 for an empty position set - no positions means no confidence,
    matching compute_p_consensus's own "no opinion" case.
    """
    if not positions:
        return Decimal(0)

    n_clusters = len(positions)
    n_clusters_component = min(Decimal(1), Decimal(n_clusters) / Decimal(n_clusters_target))

    weights = [p.score * p.position_usd for p in positions]
    total_weight = sum(weights, Decimal(0))
    if total_weight <= 0:
        return Decimal(0)

    # weighted_std of sign (a +/-1 series) under weights - full agreement
    # (every weight on the same sign) gives std=0 -> agreement=1; a 50/50
    # split by weight gives std=1 (the max possible for a +/-1 series) ->
    # agreement=0.
    mean_sign = (
        sum((w * p.sign for w, p in zip(weights, positions, strict=True)), Decimal(0))
        / total_weight
    )
    variance = (
        sum(
            (
                w * (Decimal(p.sign) - mean_sign) ** 2
                for w, p in zip(weights, positions, strict=True)
            ),
            Decimal(0),
        )
        / total_weight
    )
    std = variance.sqrt()
    agreement_component = max(Decimal(0), Decimal(1) - std)

    total_notional = sum((p.position_usd for p in positions), Decimal(0))
    total_weight_component = min(Decimal(1), total_notional / notional_target)

    score_dispersion_component = (
        sum((p.position_usd * p.score for p in positions), Decimal(0)) / total_notional
        if total_notional > 0
        else Decimal(0)
    )

    return (
        n_clusters_component
        * agreement_component
        * total_weight_component
        * score_dispersion_component
    )


def compute_consensus_probability(
    positions: Sequence[ClusterPosition],
    max_cluster_weight_fraction: Decimal,
    n_clusters_target: int,
    notional_target: Decimal,
) -> ConsensusProbabilityResult | None:
    """The one entrypoint app/consensus_v2/scan.py needs - P_consensus and
    CONFIDENCE computed together (they share the same input, and a caller
    should never accidentally compute one without the other).
    """
    p_consensus = compute_p_consensus(positions, max_cluster_weight_fraction)
    if p_consensus is None:
        return None
    confidence = compute_confidence(positions, n_clusters_target, notional_target)
    return ConsensusProbabilityResult(
        p_consensus=p_consensus,
        confidence=confidence,
        n_clusters=len(positions),
        total_weight_usd=sum((p.position_usd for p in positions), Decimal(0)),
    )

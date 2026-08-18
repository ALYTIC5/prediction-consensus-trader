"""Paired Brier score - the honest edge metric - pure, no DB, no network.

See docs/CONSENSUS_V2_DESIGN.md section 4. Answers "do our whales beat the
market price?" directly, independent of any portfolio's own P&L - a
portfolio can look profitable on a small, lucky, correlated sample exactly
like app/optimization/event_clustering.py's whole premise warns against,
which is why this aggregates with event-clustered standard errors instead
of treating every resolved market as an independent observation.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.optimization.event_clustering import cluster_key_for, effective_sample_size


@dataclass(frozen=True)
class ResolvedRecord:
    """One resolved market's paired comparison inputs - plain values, not
    an ORM row, so compute_paired_brier stays DB-free.
    """

    condition_id: str
    event_cluster_id: str | None
    p_consensus: Decimal
    p_market: Decimal
    outcome: Decimal  # 0 or 1


@dataclass(frozen=True)
class PairedBrierResult:
    """mean_difference = mean(market − consensus) Brier score, positive
    means consensus beat the market on average. t_statistic is None when
    there are fewer than 2 distinct event clusters (no variance estimate
    possible - same honest "can't compute this yet" as app/paper/
    metrics.py's Sharpe on a single trade) or when cluster means happen to
    have zero variance.
    """

    mean_difference: Decimal
    t_statistic: Decimal | None
    nominal_n: int
    effective_n: int


def brier_score(probability: Decimal, outcome: Decimal) -> Decimal:
    """(probability - outcome)^2 - same standard formula
    app/optimization/brier.py's brier_score() already uses; duplicated
    here (not imported) since this module is deliberately self-contained
    and the formula is a one-liner, not worth a cross-package dependency.
    """
    return (probability - outcome) ** 2


def compute_paired_brier(records: Sequence[ResolvedRecord]) -> PairedBrierResult:
    """Groups paired Brier differences by event cluster
    (app/optimization/event_clustering.py), computes each cluster's own
    mean difference, then reports the mean AND standard error OF THOSE
    CLUSTER MEANS - not of the raw per-market differences, which would
    treat correlated markets (same election, same tournament) as
    independent evidence and understate uncertainty exactly the way
    U.3's effective-n work exists to prevent.
    """
    if not records:
        return PairedBrierResult(Decimal(0), None, 0, 0)

    nominal_n = len(records)
    cluster_keys = [cluster_key_for(r.condition_id, r.event_cluster_id) for r in records]
    effective_n = effective_sample_size(cluster_keys)

    diffs_by_cluster: dict[str, list[Decimal]] = {}
    for record, key in zip(records, cluster_keys, strict=True):
        diff = brier_score(record.p_market, record.outcome) - brier_score(
            record.p_consensus, record.outcome
        )
        diffs_by_cluster.setdefault(key, []).append(diff)

    cluster_means = [
        sum(diffs, Decimal(0)) / Decimal(len(diffs)) for diffs in diffs_by_cluster.values()
    ]
    mean_difference = sum(cluster_means, Decimal(0)) / Decimal(len(cluster_means))

    if len(cluster_means) < 2:
        return PairedBrierResult(mean_difference, None, nominal_n, effective_n)

    variance = sum(((m - mean_difference) ** 2 for m in cluster_means), Decimal(0)) / Decimal(
        len(cluster_means) - 1
    )
    std = variance.sqrt()
    if std == 0:
        return PairedBrierResult(mean_difference, None, nominal_n, effective_n)

    stderr = std / Decimal(len(cluster_means)).sqrt()
    t_statistic = mean_difference / stderr
    return PairedBrierResult(mean_difference, t_statistic, nominal_n, effective_n)


def kelly_gate_passes(result: PairedBrierResult, min_effective_n: int, min_t_stat: Decimal) -> bool:
    """docs/CONSENSUS_V2_DESIGN.md section 3: P_consensus only REPLACES
    Kelly's empirical calibration-bucket p_hat once the paired Brier
    result shows the consensus genuinely beats the market, on enough
    independent (cluster) evidence to trust it. Until this passes,
    consensus_prob signals fall back to TIERED sizing exactly like every
    other portfolio does when Kelly's own existing calibration gate isn't
    cleared - no new failure mode.
    """
    if result.t_statistic is None:
        return False
    return (
        result.effective_n >= min_effective_n
        and result.mean_difference > 0
        and result.t_statistic >= min_t_stat
    )

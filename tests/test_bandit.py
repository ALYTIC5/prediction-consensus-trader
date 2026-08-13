"""Unit tests for app/optimization/bandit.py's pure core - no DB, no
network. See docs/PHASE6_DESIGN.md workstream 5.
"""

from decimal import Decimal

from app.db.models import ClusterBanditState
from app.optimization.bandit import (
    BanditConfig,
    _resolve_cluster_id,
    compute_multiplier,
    compute_state_from_observations,
    effective_weighted_score,
    initial_state,
    update_state,
)

DEFAULT_CONFIG = BanditConfig(weight_min=Decimal("0.5"), weight_max=Decimal("2.0"), min_signals=30)


def _config(**overrides) -> BanditConfig:
    fields = DEFAULT_CONFIG.__dict__.copy()
    fields.update(overrides)
    return BanditConfig(**fields)


# --- below min-signals: exactly neutral ---


def test_below_min_signals_multiplier_is_exactly_one():
    # 29 positive observations - one short of the 30-signal gate.
    state = compute_state_from_observations("cluster-a", [True] * 29)
    assert compute_multiplier(state, DEFAULT_CONFIG) == Decimal("1.0")


def test_zero_observations_multiplier_is_exactly_one():
    state = initial_state("cluster-a")
    assert compute_multiplier(state, DEFAULT_CONFIG) == Decimal("1.0")


def test_at_min_signals_the_gate_engages():
    # Exactly 30 observations, all positive - the gate should now engage
    # (posterior mean pulls well above 0.5, multiplier > 1.0).
    state = compute_state_from_observations("cluster-a", [True] * 30)
    assert compute_multiplier(state, DEFAULT_CONFIG) > Decimal("1.0")


# --- a cluster with consistently positive CLV gets a higher multiplier, bounded ---


def test_consistently_positive_cluster_gets_a_higher_multiplier():
    state = compute_state_from_observations("cluster-a", [True] * 100)
    multiplier = compute_multiplier(state, DEFAULT_CONFIG)
    assert multiplier > Decimal("1.0")
    assert multiplier <= DEFAULT_CONFIG.weight_max


def test_extremely_positive_cluster_is_clamped_to_weight_max():
    # 1000 observations, all positive - posterior mean approaches (but
    # never reaches) 1.0 under a Beta(1,1) prior, so 2x that approaches but
    # never quite hits 2.0 unclamped. A tighter ceiling makes the clamp
    # itself unambiguous to assert on.
    config = _config(weight_max=Decimal("1.5"))
    state = compute_state_from_observations("cluster-a", [True] * 1000)
    multiplier = compute_multiplier(state, config)
    assert multiplier == Decimal("1.5")


def test_tighter_bounds_still_clamp_a_hot_cluster():
    config = _config(weight_max=Decimal("1.2"))
    state = compute_state_from_observations("cluster-a", [True] * 1000)
    assert compute_multiplier(state, config) == Decimal("1.2")


# --- a cold cluster gets a lower multiplier, bounded ---


def test_consistently_negative_cluster_gets_a_lower_multiplier():
    state = compute_state_from_observations("cluster-a", [False] * 100)
    multiplier = compute_multiplier(state, DEFAULT_CONFIG)
    assert multiplier < Decimal("1.0")
    assert multiplier >= DEFAULT_CONFIG.weight_min


def test_extremely_negative_cluster_is_clamped_to_weight_min():
    state = compute_state_from_observations("cluster-a", [False] * 1000)
    multiplier = compute_multiplier(state, DEFAULT_CONFIG)
    assert multiplier == DEFAULT_CONFIG.weight_min


def test_tighter_bounds_still_clamp_a_cold_cluster():
    config = _config(weight_min=Decimal("0.8"))
    state = compute_state_from_observations("cluster-a", [False] * 1000)
    assert compute_multiplier(state, config) == Decimal("0.8")


# --- a coin-flip cluster is neutral ---


def test_fifty_fifty_cluster_is_neutral():
    state = compute_state_from_observations("cluster-a", [True, False] * 50)
    assert compute_multiplier(state, DEFAULT_CONFIG) == Decimal("1.0")


# --- update_state / compute_state_from_observations plumbing ---


def test_update_state_increments_alpha_on_positive():
    state = initial_state("cluster-a")
    state = update_state(state, positive=True)
    assert state.alpha == 2  # started at 1 (uniform prior)
    assert state.beta == 1
    assert state.observations == 1


def test_update_state_increments_beta_on_negative():
    state = initial_state("cluster-a")
    state = update_state(state, positive=False)
    assert state.alpha == 1
    assert state.beta == 2
    assert state.observations == 1


def test_compute_state_from_observations_matches_manual_folding():
    manual = initial_state("cluster-a")
    for positive in [True, True, False]:
        manual = update_state(manual, positive)

    bulk = compute_state_from_observations("cluster-a", [True, True, False])
    assert bulk == manual


# --- effective_weighted_score: the adaptive challenger's entry-filter recompute ---


def _contributor(address: str, weight: str) -> dict:
    return {"address": address, "weight": weight}


def test_effective_weighted_score_scales_by_cluster_multiplier():
    contributors = [_contributor("0xA", "0.6")]
    cluster_of = {"0xA": "cluster-a"}
    multiplier_of = {"cluster-a": Decimal("1.5")}

    score = effective_weighted_score(contributors, cluster_of, multiplier_of)
    assert score == Decimal("0.9")  # 0.6 * 1.5


def test_effective_weighted_score_dedupes_by_cluster_using_max():
    # Two wallets in the same cluster - only the max (post-multiplier)
    # weight counts, same as app/consensus/engine.py's _weighted_score().
    contributors = [_contributor("0xA", "0.6"), _contributor("0xB", "0.4")]
    cluster_of = {"0xA": "cluster-a", "0xB": "cluster-a"}
    multiplier_of = {"cluster-a": Decimal("1.0")}

    score = effective_weighted_score(contributors, cluster_of, multiplier_of)
    assert score == Decimal("0.6")


def test_effective_weighted_score_unknown_cluster_defaults_to_neutral_multiplier():
    contributors = [_contributor("0xA", "0.6")]
    cluster_of = {"0xA": "cluster-a"}
    multiplier_of: dict[str, Decimal] = {}  # cluster-a never observed yet

    score = effective_weighted_score(contributors, cluster_of, multiplier_of)
    assert score == Decimal("0.6")  # unchanged - multiplier defaults to 1.0


def test_effective_weighted_score_unclustered_wallet_is_its_own_singleton():
    contributors = [_contributor("0xA", "0.6"), _contributor("0xB", "0.6")]
    cluster_of: dict[str, str] = {}  # neither wallet has been clustered
    multiplier_of: dict[str, Decimal] = {}

    score = effective_weighted_score(contributors, cluster_of, multiplier_of)
    assert score == Decimal("1.2")  # two independent singleton clusters, summed


# --- _resolve_cluster_id: the cluster_bandit_state.cluster_id VARCHAR
# truncation regression (collectors/bandit was dead on every run because
# this used to fall back to the raw, unbounded wallet address) ---

# A real Polymarket wallet address: "0x" + 40 hex chars = 42 chars total -
# well past the column's old 16-char width, and still past its current
# 64-char one if this regressed to something unbounded again.
_UNCLUSTERED_ADDRESS = "0x" + "a" * 40


def test_unclustered_wallet_gets_a_bounded_deterministic_id():
    cluster_id = _resolve_cluster_id(_UNCLUSTERED_ADDRESS, cluster_of={})

    max_len = ClusterBanditState.__table__.c.cluster_id.type.length
    assert len(cluster_id) <= max_len, (
        f"cluster_id {cluster_id!r} ({len(cluster_id)} chars) would overflow "
        f"cluster_bandit_state.cluster_id (VARCHAR({max_len})) - this is exactly "
        f"the truncation that killed the bandit job"
    )
    assert cluster_id != _UNCLUSTERED_ADDRESS, "must never fall back to the raw address itself"


def test_unclustered_wallet_gets_the_same_id_every_time():
    # Deterministic, not random - two runs over the same (still-unclustered)
    # wallet must agree, or its bandit observations would scatter across a
    # different synthetic "cluster" on every run.
    first = _resolve_cluster_id(_UNCLUSTERED_ADDRESS, cluster_of={})
    second = _resolve_cluster_id(_UNCLUSTERED_ADDRESS, cluster_of={})
    assert first == second


def test_unclustered_wallet_id_matches_a_real_singleton_clusters_scheme():
    # A wallet_clusters singleton (tracked, but Louvain gave it no edges)
    # gets cluster_id_for([address]) too (see app/optimization/clustering.py's
    # assign_clusters) - an unclustered contributor should be indistinguishable
    # from that case, not a different id shape.
    from app.optimization.clustering import cluster_id_for

    assert _resolve_cluster_id(_UNCLUSTERED_ADDRESS, cluster_of={}) == cluster_id_for(
        [_UNCLUSTERED_ADDRESS]
    )


def test_clustered_wallet_uses_its_real_cluster_id_not_the_fallback():
    cluster_of = {_UNCLUSTERED_ADDRESS: "real-cluster-hash"}
    assert _resolve_cluster_id(_UNCLUSTERED_ADDRESS, cluster_of) == "real-cluster-hash"

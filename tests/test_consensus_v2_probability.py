"""Tests for app/consensus_v2/probability.py - pure, no DB, no network."""

from decimal import Decimal

from app.consensus_v2.probability import (
    ClusterPosition,
    cap_cluster_weights,
    compute_confidence,
    compute_consensus_probability,
    compute_p_consensus,
)

MAX_FRACTION = Decimal("0.35")


def _pos(cluster_id, position_usd, sign, score=Decimal("0.5")):
    return ClusterPosition(cluster_id=cluster_id, position_usd=position_usd, sign=sign, score=score)


# --- compute_p_consensus: hand-computed ---


def test_unanimous_yes_gives_probability_one():
    positions = [
        _pos("a", Decimal("100"), 1),
        _pos("b", Decimal("50"), 1),
    ]
    p = compute_p_consensus(positions, MAX_FRACTION)
    assert p == Decimal("1")


def test_unanimous_no_gives_probability_zero():
    positions = [
        _pos("a", Decimal("100"), -1),
        _pos("b", Decimal("50"), -1),
    ]
    p = compute_p_consensus(positions, MAX_FRACTION)
    assert p == Decimal("0")


def test_sign_handling_mixed_positions_hand_computed():
    """cluster A: score 0.5, position 100, sign +1 -> weight 50.
    cluster B: score 0.5, position 60, sign -1 -> weight 30.
    Neither exceeds the 35% cap (50/80=62.5%... wait it DOES exceed - use
    a case that doesn't trigger the cap to isolate sign handling).
    total weight = 80 (uncapped, both under an effectively-unbounded cap
    for this test since we only care about the raw sign arithmetic here -
    use a generous cap so neither is touched).
    numerator = 50*1 + 30*-1 = 20. raw = 20/80 = 0.25.
    P = (0.25 + 1) / 2 = 0.625.
    """
    positions = [
        _pos("a", Decimal("100"), 1),
        _pos("b", Decimal("60"), -1),
    ]
    p = compute_p_consensus(positions, Decimal("1"))  # no capping
    assert p == Decimal("0.625")


def test_empty_positions_returns_none():
    assert compute_p_consensus([], MAX_FRACTION) is None


def test_zero_total_score_returns_none():
    positions = [_pos("a", Decimal("100"), 1, score=Decimal("0"))]
    assert compute_p_consensus(positions, MAX_FRACTION) is None


# --- dominant-position cap ---


def test_cap_cluster_weights_caps_dominant_cluster():
    """4 clusters, so a 35% cap is actually satisfiable (with only 2
    clusters, one must always hold >=50% by pigeonhole - not what this
    test is checking).
    """
    weights = {
        "whale": Decimal("1000"),
        "b": Decimal("50"),
        "c": Decimal("50"),
        "d": Decimal("50"),
    }
    capped = cap_cluster_weights(weights, Decimal("0.35"))
    total = sum(capped.values())
    assert capped["whale"] <= Decimal("0.35") * total + Decimal("0.0001")
    assert capped["b"] == Decimal("50")  # untouched, never exceeded the cap


def test_cap_cluster_weights_no_op_when_already_balanced():
    weights = {"a": Decimal("100"), "b": Decimal("100"), "c": Decimal("100")}
    capped = cap_cluster_weights(weights, Decimal("0.35"))
    # each is exactly 1/3 ~= 33.3%, under the 35% cap - untouched.
    assert capped == weights


def test_single_dominant_position_reduces_its_own_influence_on_p_consensus():
    """A whale-dominated set (one huge YES position, one tiny NO) should
    land closer to neutral than an uncapped naive ratio would, because the
    whale's weight is capped before the ratio is computed.
    """
    positions = [
        _pos("whale", Decimal("100000"), 1),
        _pos("small", Decimal("100"), -1),
    ]
    uncapped = compute_p_consensus(positions, Decimal("1"))
    capped = compute_p_consensus(positions, Decimal("0.35"))
    assert uncapped > Decimal("0.99")  # whale totally dominates without a cap
    assert capped < uncapped  # capping pulls it back toward neutral


# --- compute_confidence ---


def test_confidence_zero_for_no_positions():
    assert compute_confidence([], n_clusters_target=5, notional_target=Decimal("5000")) == 0


def test_confidence_lower_when_clusters_disagree_on_direction():
    agreeing = [
        _pos("a", Decimal("1000"), 1, score=Decimal("0.8")),
        _pos("b", Decimal("1000"), 1, score=Decimal("0.8")),
    ]
    disagreeing = [
        _pos("a", Decimal("1000"), 1, score=Decimal("0.8")),
        _pos("b", Decimal("1000"), -1, score=Decimal("0.8")),
    ]
    conf_agree = compute_confidence(agreeing, n_clusters_target=2, notional_target=Decimal("1000"))
    conf_disagree = compute_confidence(
        disagreeing, n_clusters_target=2, notional_target=Decimal("1000")
    )
    assert conf_disagree < conf_agree


def test_confidence_lower_with_fewer_clusters_than_target():
    few = [_pos("a", Decimal("5000"), 1, score=Decimal("0.9"))]
    many = [_pos(f"c{i}", Decimal("5000"), 1, score=Decimal("0.9")) for i in range(5)]
    conf_few = compute_confidence(few, n_clusters_target=5, notional_target=Decimal("1000"))
    conf_many = compute_confidence(many, n_clusters_target=5, notional_target=Decimal("1000"))
    assert conf_few < conf_many


def test_confidence_lower_with_thin_total_notional():
    thin = [_pos("a", Decimal("10"), 1, score=Decimal("0.9"))]
    thick = [_pos("a", Decimal("50000"), 1, score=Decimal("0.9"))]
    conf_thin = compute_confidence(thin, n_clusters_target=1, notional_target=Decimal("5000"))
    conf_thick = compute_confidence(thick, n_clusters_target=1, notional_target=Decimal("5000"))
    assert conf_thin < conf_thick


# --- compute_consensus_probability: the combined entrypoint ---


def test_consensus_probability_none_when_no_positions():
    assert (
        compute_consensus_probability(
            [], MAX_FRACTION, n_clusters_target=5, notional_target=Decimal("5000")
        )
        is None
    )


def test_consensus_probability_bundles_p_and_confidence():
    positions = [
        _pos("a", Decimal("1000"), 1, score=Decimal("0.8")),
        _pos("b", Decimal("1000"), 1, score=Decimal("0.8")),
    ]
    result = compute_consensus_probability(
        positions, MAX_FRACTION, n_clusters_target=2, notional_target=Decimal("1000")
    )
    assert result is not None
    assert result.p_consensus == Decimal("1")
    assert result.n_clusters == 2
    assert result.total_weight_usd == Decimal("2000")
    assert result.confidence > 0

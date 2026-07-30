"""Unit tests for app/optimization/crowdedness.py's pure core - no DB, no
network. See docs/PHASE6_DESIGN.md workstream 7.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.optimization.crowdedness import (
    CrowdednessConfig,
    MarketEfficiency,
    OpenedEvent,
    compute_follower_reaction,
    hidden_alpha_score,
    hidden_alpha_weighted_score,
    leaderboard_fame_score,
    market_obviousness_score,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

DEFAULT_CONFIG = CrowdednessConfig(
    reaction_window_minutes=15,
    reaction_saturation=Decimal("5"),
    liquidity_reference_usd=Decimal("50000"),
    volume_reference_usd=Decimal("20000"),
    spread_reference=Decimal("0.10"),
    weight_reaction=Decimal("0.5"),
    weight_fame=Decimal("0.3"),
    weight_obvious=Decimal("0.2"),
    leaderboard_top_n=100,
    penalty_weight=Decimal("0.3"),
)


def _event(wallet_id: int, asset: str = "assetA", offset_minutes: int = 0) -> OpenedEvent:
    return OpenedEvent(
        wallet_id=wallet_id, asset=asset, detected_at=NOW + timedelta(minutes=offset_minutes)
    )


# --- compute_follower_reaction ---


def test_cross_cluster_same_asset_in_window_counts_as_a_follower():
    origin = _event(1, offset_minutes=0)
    follower = _event(2, offset_minutes=5)
    cluster_of = {1: "cluster-a", 2: "cluster-b"}

    scores = compute_follower_reaction([origin, follower], cluster_of, DEFAULT_CONFIG)
    # avg follower count for wallet 1 is 1 (wallet 2), saturation=5 -> 0.2
    assert scores[1] == Decimal("0.2")


def test_same_cluster_wallet_is_excluded():
    origin = _event(1, offset_minutes=0)
    same_cluster = _event(2, offset_minutes=5)
    cluster_of = {1: "cluster-a", 2: "cluster-a"}

    scores = compute_follower_reaction([origin, same_cluster], cluster_of, DEFAULT_CONFIG)
    assert scores[1] == Decimal("0")


def test_different_asset_is_excluded():
    origin = _event(1, asset="assetA", offset_minutes=0)
    different_asset = _event(2, asset="assetB", offset_minutes=5)
    cluster_of = {1: "cluster-a", 2: "cluster-b"}

    scores = compute_follower_reaction([origin, different_asset], cluster_of, DEFAULT_CONFIG)
    assert scores[1] == Decimal("0")


def test_out_of_window_is_excluded():
    origin = _event(1, offset_minutes=0)
    late = _event(2, offset_minutes=20)  # window is 15 minutes
    cluster_of = {1: "cluster-a", 2: "cluster-b"}

    scores = compute_follower_reaction([origin, late], cluster_of, DEFAULT_CONFIG)
    assert scores[1] == Decimal("0")


def test_reaction_before_origin_is_excluded():
    origin = _event(1, offset_minutes=10)
    earlier = _event(2, offset_minutes=0)  # reacted BEFORE the origin opened
    cluster_of = {1: "cluster-a", 2: "cluster-b"}

    scores = compute_follower_reaction([origin, earlier], cluster_of, DEFAULT_CONFIG)
    assert scores[1] == Decimal("0")


def test_unclustered_wallets_default_to_singleton_clusters():
    """Two wallets absent from cluster_of are each their own singleton -
    still counted as cross-cluster followers of each other, same
    "independent until proven otherwise" default engine.py/bandit.py use.
    """
    origin = _event(1, offset_minutes=0)
    follower = _event(2, offset_minutes=5)
    scores = compute_follower_reaction([origin, follower], {}, DEFAULT_CONFIG)
    assert scores[1] == Decimal("0.2")


def test_reaction_saturates_at_one():
    origin = _event(1, offset_minutes=0)
    followers = [_event(w, offset_minutes=1) for w in range(2, 12)]  # 10 followers >> saturation=5
    cluster_of = {w: f"cluster-{w}" for w in range(1, 12)}

    scores = compute_follower_reaction([origin, *followers], cluster_of, DEFAULT_CONFIG)
    assert scores[1] == Decimal("1")


# --- leaderboard_fame_score ---


def test_fame_rises_with_higher_rank():
    low_rank_fame = leaderboard_fame_score([1], possible_appearances=1, config=DEFAULT_CONFIG)
    high_rank_fame = leaderboard_fame_score([90], possible_appearances=1, config=DEFAULT_CONFIG)
    assert low_rank_fame > high_rank_fame


def test_fame_rises_with_steadier_appearance():
    steady = leaderboard_fame_score([5, 5, 5], possible_appearances=3, config=DEFAULT_CONFIG)
    sporadic = leaderboard_fame_score([5], possible_appearances=3, config=DEFAULT_CONFIG)
    assert steady > sporadic


def test_fame_uses_median_not_best_rank():
    """One lucky rank-1 snapshot among mostly poor ranks shouldn't score as
    famous as a wallet that's consistently near the top.
    """
    one_lucky_spike = leaderboard_fame_score(
        [1, 95, 95], possible_appearances=3, config=DEFAULT_CONFIG
    )
    consistently_good = leaderboard_fame_score(
        [10, 12, 11], possible_appearances=3, config=DEFAULT_CONFIG
    )
    assert consistently_good > one_lucky_spike


def test_no_appearances_scores_zero_fame():
    assert leaderboard_fame_score([], possible_appearances=10, config=DEFAULT_CONFIG) == Decimal(
        "0"
    )
    assert leaderboard_fame_score([1], possible_appearances=0, config=DEFAULT_CONFIG) == Decimal(
        "0"
    )


# --- market_obviousness_score ---


def test_obviousness_rises_with_tighter_spread():
    tight = market_obviousness_score(
        [MarketEfficiency(liquidity=Decimal(0), volume_24h=Decimal(0), spread=Decimal("0.01"))],
        DEFAULT_CONFIG,
    )
    wide = market_obviousness_score(
        [MarketEfficiency(liquidity=Decimal(0), volume_24h=Decimal(0), spread=Decimal("0.09"))],
        DEFAULT_CONFIG,
    )
    assert tight > wide


def test_obviousness_rises_with_higher_liquidity():
    liquid = market_obviousness_score(
        [
            MarketEfficiency(
                liquidity=Decimal("50000"), volume_24h=Decimal(0), spread=Decimal("0.10")
            )
        ],
        DEFAULT_CONFIG,
    )
    illiquid = market_obviousness_score(
        [
            MarketEfficiency(
                liquidity=Decimal("1000"), volume_24h=Decimal(0), spread=Decimal("0.10")
            )
        ],
        DEFAULT_CONFIG,
    )
    assert liquid > illiquid


def test_obviousness_rises_with_higher_volume():
    high_volume = market_obviousness_score(
        [
            MarketEfficiency(
                liquidity=Decimal(0), volume_24h=Decimal("20000"), spread=Decimal("0.10")
            )
        ],
        DEFAULT_CONFIG,
    )
    low_volume = market_obviousness_score(
        [MarketEfficiency(liquidity=Decimal(0), volume_24h=Decimal("500"), spread=Decimal("0.10"))],
        DEFAULT_CONFIG,
    )
    assert high_volume > low_volume


def test_no_open_positions_scores_zero_obviousness():
    assert market_obviousness_score([], DEFAULT_CONFIG) == Decimal("0")


def test_missing_market_fields_default_to_least_informative():
    missing = market_obviousness_score(
        [MarketEfficiency(liquidity=None, volume_24h=None, spread=None)], DEFAULT_CONFIG
    )
    assert missing == Decimal("0")


# --- hidden_alpha ---


def test_hidden_alpha_subtracts_the_penalty():
    score = hidden_alpha_score(
        skill_score=Decimal("0.8"), crowdedness=Decimal("0.5"), penalty_weight=Decimal("0.3")
    )
    assert score == Decimal("0.65")  # 0.8 - 0.3*0.5


def test_hidden_alpha_clamps_to_zero_not_negative():
    score = hidden_alpha_score(
        skill_score=Decimal("0.1"), crowdedness=Decimal("1.0"), penalty_weight=Decimal("0.5")
    )
    assert score == Decimal("0")


def test_hidden_alpha_clamps_to_one_not_above():
    score = hidden_alpha_score(
        skill_score=Decimal("1.0"), crowdedness=Decimal("0"), penalty_weight=Decimal("0.5")
    )
    assert score == Decimal("1")


def test_skilled_but_crowded_wallet_ranks_below_equally_skilled_uncrowded():
    same_skill = Decimal("0.9")
    crowded = hidden_alpha_score(
        same_skill, crowdedness=Decimal("0.8"), penalty_weight=Decimal("0.3")
    )
    uncrowded = hidden_alpha_score(
        same_skill, crowdedness=Decimal("0.0"), penalty_weight=Decimal("0.3")
    )
    assert crowded < uncrowded


def test_soft_penalty_lets_a_very_skilled_crowded_wallet_still_score_respectably():
    """The penalty is a subtraction, not a hard cutoff or multiplier - a
    top-skill wallet with heavy following should still clear a reasonable
    bar, never get zeroed out on crowdedness alone.
    """
    score = hidden_alpha_score(
        skill_score=Decimal("1.0"), crowdedness=Decimal("1.0"), penalty_weight=Decimal("0.3")
    )
    assert score == Decimal("0.7")
    assert score > Decimal("0.5")


# --- hidden_alpha_weighted_score ---


def _contributor(address: str, weight: str) -> dict:
    return {"address": address, "weight": weight}


def test_weighted_score_applies_penalty_before_cluster_dedup():
    contributors = [_contributor("0xA", "0.6"), _contributor("0xB", "0.4")]
    cluster_of = {"0xA": "cluster-1", "0xB": "cluster-1"}
    crowdedness_of = {"0xA": Decimal("1.0"), "0xB": Decimal("0.0")}
    # 0xA: 0.6 - 0.3*1.0 = 0.3; 0xB: 0.4 - 0.3*0.0 = 0.4 -> max is 0xB's 0.4
    score = hidden_alpha_weighted_score(contributors, cluster_of, crowdedness_of, Decimal("0.3"))
    assert score == Decimal("0.4")


def test_weighted_score_sums_across_independent_clusters():
    contributors = [_contributor("0xA", "0.6"), _contributor("0xB", "0.6")]
    cluster_of: dict[str, str] = {}  # both singletons
    crowdedness_of: dict[str, Decimal] = {}
    score = hidden_alpha_weighted_score(contributors, cluster_of, crowdedness_of, Decimal("0.3"))
    assert score == Decimal("1.2")  # no crowdedness data -> no penalty, 0.6 + 0.6


def test_weighted_score_unknown_wallet_defaults_to_no_penalty():
    contributors = [_contributor("0xA", "0.5")]
    score = hidden_alpha_weighted_score(contributors, {}, {}, Decimal("0.3"))
    assert score == Decimal("0.5")

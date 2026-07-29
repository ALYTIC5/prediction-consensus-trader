"""Unit tests for app/optimization/clustering.py's community detection -
no DB, network graph logic only. See docs/PHASE6_DESIGN.md workstream 1.
"""

from app.optimization.clustering import assign_clusters
from app.optimization.cotrading import CoTradeEdge


def _wallets(*ids: int) -> dict[int, str]:
    return {wallet_id: f"0xwallet{wallet_id}" for wallet_id in ids}


def test_a_tight_cluster_of_four_wallets_collapses_to_one_component():
    wallets = _wallets(1, 2, 3, 4)
    # A fully-connected clique, heavily weighted - every pair co-traded a
    # lot together.
    edges = [
        CoTradeEdge(wallet_a=1, wallet_b=2, weight=10, shared_markets=10),
        CoTradeEdge(wallet_a=1, wallet_b=3, weight=10, shared_markets=10),
        CoTradeEdge(wallet_a=1, wallet_b=4, weight=10, shared_markets=10),
        CoTradeEdge(wallet_a=2, wallet_b=3, weight=10, shared_markets=10),
        CoTradeEdge(wallet_a=2, wallet_b=4, weight=10, shared_markets=10),
        CoTradeEdge(wallet_a=3, wallet_b=4, weight=10, shared_markets=10),
    ]

    assignments = assign_clusters(wallets, edges)

    cluster_ids = {a.cluster_id for a in assignments}
    assert len(cluster_ids) == 1
    assert {a.wallet_id for a in assignments} == {1, 2, 3, 4}
    assert all(a.cluster_size == 4 for a in assignments)


def test_isolated_wallets_with_no_edges_stay_singletons():
    wallets = _wallets(1, 2, 3)

    assignments = assign_clusters(wallets, edges=[])

    assert len(assignments) == 3
    assert len({a.cluster_id for a in assignments}) == 3
    assert all(a.cluster_size == 1 for a in assignments)


def test_two_separate_pairs_produce_two_distinct_clusters():
    wallets = _wallets(1, 2, 3, 4)
    edges = [
        CoTradeEdge(wallet_a=1, wallet_b=2, weight=5, shared_markets=5),
        CoTradeEdge(wallet_a=3, wallet_b=4, weight=5, shared_markets=5),
    ]

    assignments = assign_clusters(wallets, edges)
    by_wallet = {a.wallet_id: a.cluster_id for a in assignments}

    assert by_wallet[1] == by_wallet[2]
    assert by_wallet[3] == by_wallet[4]
    assert by_wallet[1] != by_wallet[3]


def test_cluster_id_is_stable_across_reruns_with_identical_membership():
    """docs/PHASE6_DESIGN.md flag 1 - the whole point of hashing membership
    instead of using Louvain's own output label: an unchanged cluster must
    get the same id every time, not a re-shuffled one.
    """
    wallets = _wallets(1, 2)
    edges = [CoTradeEdge(wallet_a=1, wallet_b=2, weight=5, shared_markets=5)]

    first_run = assign_clusters(wallets, edges)
    second_run = assign_clusters(wallets, edges)

    first_ids = {a.wallet_id: a.cluster_id for a in first_run}
    second_ids = {a.wallet_id: a.cluster_id for a in second_run}
    assert first_ids == second_ids


def test_cluster_id_is_unaffected_by_an_unrelated_isolated_wallet():
    """Adding a wallet with no edges to anyone must not change an existing
    cluster's id - that cluster's own membership hasn't changed.
    """
    edges = [CoTradeEdge(wallet_a=1, wallet_b=2, weight=5, shared_markets=5)]

    pair_only = assign_clusters(_wallets(1, 2), edges)
    pair_id = {a.cluster_id for a in pair_only}

    with_isolated_third = assign_clusters(_wallets(1, 2, 3), edges)
    same_pair_id = {a.cluster_id for a in with_isolated_third if a.wallet_id in (1, 2)}

    assert pair_id == same_pair_id


def test_cluster_id_changes_when_membership_actually_changes():
    """docs/PHASE6_DESIGN.md flag 1's other half: a cluster whose real
    membership changes must get a NEW id, not inherit the old one - a
    changed membership is a genuinely different independent-voice profile.
    """
    pair_edges = [CoTradeEdge(wallet_a=1, wallet_b=2, weight=5, shared_markets=5)]
    pair_only = assign_clusters(_wallets(1, 2), pair_edges)
    pair_id = {a.cluster_id for a in pair_only}

    trio_edges = pair_edges + [
        CoTradeEdge(wallet_a=1, wallet_b=3, weight=5, shared_markets=5),
        CoTradeEdge(wallet_a=2, wallet_b=3, weight=5, shared_markets=5),
    ]
    trio = assign_clusters(_wallets(1, 2, 3), trio_edges)
    trio_id = {a.cluster_id for a in trio}

    assert len(trio_id) == 1  # all three actually merged into one cluster
    assert trio_id != pair_id

"""Unit tests for app/optimization/cotrading.py's pure graph logic - no DB,
no network. See docs/PHASE6_DESIGN.md workstream 1.
"""

from datetime import UTC, datetime, timedelta

from app.optimization.cotrading import CoTradeEvent, CoTradingConfig, build_cotrading_edges

NOW = datetime(2026, 1, 1, tzinfo=UTC)

DEFAULT_CONFIG = CoTradingConfig(window_minutes=15, min_shared_markets=3)


def _event(
    wallet_id: int, condition_id: str, asset: str = "YES", offset_minutes: float = 0
) -> CoTradeEvent:
    return CoTradeEvent(
        wallet_id=wallet_id,
        condition_id=condition_id,
        asset=asset,
        detected_at=NOW + timedelta(minutes=offset_minutes),
    )


def test_two_wallets_sharing_enough_markets_in_window_get_an_edge():
    events = [
        _event(1, "market-a", offset_minutes=0),
        _event(2, "market-a", offset_minutes=5),
        _event(1, "market-b", offset_minutes=0),
        _event(2, "market-b", offset_minutes=10),
        _event(1, "market-c", offset_minutes=0),
        _event(2, "market-c", offset_minutes=-8),
    ]

    edges = build_cotrading_edges(events, DEFAULT_CONFIG)

    assert len(edges) == 1
    edge = edges[0]
    assert {edge.wallet_a, edge.wallet_b} == {1, 2}
    assert edge.shared_markets == 3
    assert edge.weight == 3


def test_too_few_shared_markets_produces_no_edge():
    # Only 2 shared markets within the window - min_shared_markets is 3.
    events = [
        _event(1, "market-a", offset_minutes=0),
        _event(2, "market-a", offset_minutes=5),
        _event(1, "market-b", offset_minutes=0),
        _event(2, "market-b", offset_minutes=5),
    ]

    edges = build_cotrading_edges(events, DEFAULT_CONFIG)

    assert edges == []


def test_wide_time_gap_does_not_count_as_co_occurrence():
    # Same 3 markets, but every pair is 30 minutes apart - outside the
    # 15-minute window, so none of them qualify at all.
    events = [
        _event(1, "market-a", offset_minutes=0),
        _event(2, "market-a", offset_minutes=30),
        _event(1, "market-b", offset_minutes=0),
        _event(2, "market-b", offset_minutes=30),
        _event(1, "market-c", offset_minutes=0),
        _event(2, "market-c", offset_minutes=30),
    ]

    edges = build_cotrading_edges(events, DEFAULT_CONFIG)

    assert edges == []


def test_exactly_at_the_window_boundary_counts():
    events = [
        _event(1, "market-a", offset_minutes=0),
        _event(2, "market-a", offset_minutes=15),
        _event(1, "market-b", offset_minutes=0),
        _event(2, "market-b", offset_minutes=15),
        _event(1, "market-c", offset_minutes=0),
        _event(2, "market-c", offset_minutes=15),
    ]

    edges = build_cotrading_edges(events, DEFAULT_CONFIG)

    assert len(edges) == 1
    assert edges[0].shared_markets == 3


def test_same_wallet_never_pairs_with_itself():
    events = [
        _event(1, "market-a", offset_minutes=0),
        _event(1, "market-a", asset="NO", offset_minutes=1),
    ]

    edges = build_cotrading_edges(events, DEFAULT_CONFIG)

    assert edges == []


def test_different_outcome_tokens_on_the_same_market_still_count_once():
    # Wallets co-trading BOTH outcomes of the same market only contributes
    # one distinct market toward the threshold, not two - it's still one
    # coincidence, not two independent coincidences.
    events = [
        _event(1, "market-a", asset="YES", offset_minutes=0),
        _event(2, "market-a", asset="YES", offset_minutes=1),
        _event(1, "market-a", asset="NO", offset_minutes=0),
        _event(2, "market-a", asset="NO", offset_minutes=1),
    ]

    edges = build_cotrading_edges(events, CoTradingConfig(window_minutes=15, min_shared_markets=1))

    assert len(edges) == 1
    assert edges[0].shared_markets == 1
    assert edges[0].weight == 2  # two qualifying instances (YES and NO), one market


def test_three_wallets_in_one_group_produce_three_pairwise_edges():
    events = [
        _event(1, "market-a", offset_minutes=0),
        _event(2, "market-a", offset_minutes=1),
        _event(3, "market-a", offset_minutes=2),
        _event(1, "market-b", offset_minutes=0),
        _event(2, "market-b", offset_minutes=1),
        _event(3, "market-b", offset_minutes=2),
        _event(1, "market-c", offset_minutes=0),
        _event(2, "market-c", offset_minutes=1),
        _event(3, "market-c", offset_minutes=2),
    ]

    edges = build_cotrading_edges(events, DEFAULT_CONFIG)

    pairs = {(e.wallet_a, e.wallet_b) for e in edges}
    assert pairs == {(1, 2), (1, 3), (2, 3)}

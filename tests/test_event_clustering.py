"""Tests for app/optimization/event_clustering.py - pure, no DB, no network."""

from datetime import UTC, datetime

from app.optimization.event_clustering import (
    MarketClusterInput,
    assign_event_clusters,
    cluster_key_for,
    effective_sample_size,
)


def test_shared_event_slug_clusters_together() -> None:
    markets = [
        MarketClusterInput("0xa", "us-election-2028", "Politics", None),
        MarketClusterInput("0xb", "us-election-2028", "Politics", None),
        MarketClusterInput("0xc", "different-event", "Politics", None),
    ]
    assignments = assign_event_clusters(markets, date_bucket_hours=24)
    by_id = {a.condition_id: a.event_cluster_id for a in assignments}

    assert by_id["0xa"] == by_id["0xb"]
    assert by_id["0xa"] != by_id["0xc"]
    assert by_id["0xa"] == "event:us-election-2028"


def test_no_event_slug_falls_back_to_category_date_bucket() -> None:
    same_bucket = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    also_same_bucket = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)
    different_bucket = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    markets = [
        MarketClusterInput("0xa", None, "Sports", same_bucket),
        MarketClusterInput("0xb", None, "Sports", also_same_bucket),
        MarketClusterInput("0xc", None, "Sports", different_bucket),
    ]
    assignments = assign_event_clusters(markets, date_bucket_hours=24)
    by_id = {a.condition_id: a.event_cluster_id for a in assignments}

    assert by_id["0xa"] == by_id["0xb"]
    assert by_id["0xa"] != by_id["0xc"]


def test_different_category_same_date_bucket_does_not_cluster() -> None:
    same_time = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    markets = [
        MarketClusterInput("0xa", None, "Sports", same_time),
        MarketClusterInput("0xb", None, "Crypto", same_time),
    ]
    assignments = assign_event_clusters(markets, date_bucket_hours=24)
    by_id = {a.condition_id: a.event_cluster_id for a in assignments}

    assert by_id["0xa"] != by_id["0xb"]


def test_no_event_slug_and_no_end_date_gets_singleton() -> None:
    markets = [
        MarketClusterInput("0xa", None, "Other", None),
        MarketClusterInput("0xb", None, "Other", None),
    ]
    assignments = assign_event_clusters(markets, date_bucket_hours=24)
    by_id = {a.condition_id: a.event_cluster_id for a in assignments}

    assert by_id["0xa"] != by_id["0xb"]
    assert by_id["0xa"] == "solo:0xa"


def test_cluster_key_for_uses_event_cluster_id_when_present() -> None:
    assert cluster_key_for("0xa", "event:x") == "event:x"


def test_cluster_key_for_falls_back_to_singleton_when_unclustered() -> None:
    key_a = cluster_key_for("0xa", None)
    key_b = cluster_key_for("0xb", None)

    assert key_a != key_b
    assert key_a == "unclustered:0xa"


def test_effective_sample_size_one_event_collapses_to_one() -> None:
    """All trades in one event cluster: effective n=1, not the trade count."""
    keys = [cluster_key_for("0x" + str(i), "event:same") for i in range(300)]
    assert effective_sample_size(keys) == 1


def test_effective_sample_size_distinct_events_preserves_n() -> None:
    """Every trade in its own distinct cluster: effective n equals nominal n."""
    keys = [cluster_key_for("0x" + str(i), f"event:{i}") for i in range(12)]
    assert effective_sample_size(keys) == 12


def test_effective_sample_size_mixed_clusters() -> None:
    """8 trades: 5 in one cluster, 3 in another distinct cluster -> effective n=2."""
    keys = [cluster_key_for(f"0x{i}", "event:a") for i in range(5)] + [
        cluster_key_for(f"0x{i}", "event:b") for i in range(5, 8)
    ]
    assert effective_sample_size(keys) == 2


def test_effective_sample_size_empty_input() -> None:
    assert effective_sample_size([]) == 0

"""Unit tests for app/risk/calibration.py's pure core - no DB, no network.

Bucketing (records with different features land in different buckets, and
ones outside every band are dropped rather than invented into one), the
Wilson interval against known reference values, the min-sample gate that
makes get_p_hat honestly return None on sparse data, and the rolling-window
recency mechanism's actual effect (docs/PHASE5_DESIGN.md sections 4/6).
"""

from decimal import Decimal

import pytest

from app.risk.calibration import (
    CalibrationConfig,
    ResolvedTradeRecord,
    SignalFeatures,
    compute_bucket_stats,
    get_p_hat,
    wilson_interval,
)

DEFAULT_CONFIG = CalibrationConfig(
    cluster_bands=(
        (Decimal("2"), Decimal("3")),
        (Decimal("3"), Decimal("5")),
        (Decimal("5"), Decimal("999999")),
    ),
    score_bands=(
        (Decimal("1.0"), Decimal("1.5")),
        (Decimal("1.5"), Decimal("2.0")),
        (Decimal("2.0"), Decimal("999999")),
    ),
    price_bands=(
        (Decimal("0"), Decimal("0.3")),
        (Decimal("0.3"), Decimal("0.7")),
        (Decimal("0.7"), Decimal("1")),
    ),
    min_samples_per_bucket=5,
    window_days=90,
)


def _record(
    cluster_count: int = 3,
    weighted_score: Decimal = Decimal("1.2"),
    average_entry_price: Decimal = Decimal("0.5"),
    won: bool = True,
) -> ResolvedTradeRecord:
    return ResolvedTradeRecord(
        cluster_count=cluster_count,
        weighted_score=weighted_score,
        average_entry_price=average_entry_price,
        won=won,
    )


def _features(
    cluster_count: int = 3,
    weighted_score: Decimal = Decimal("1.2"),
    average_entry_price: Decimal = Decimal("0.5"),
) -> SignalFeatures:
    return SignalFeatures(
        cluster_count=cluster_count,
        weighted_score=weighted_score,
        average_entry_price=average_entry_price,
    )


# --- bucketing ---


def test_records_with_different_features_land_in_different_buckets():
    low_score = _record(weighted_score=Decimal("1.2"), won=True)
    high_score = _record(weighted_score=Decimal("2.5"), won=False)
    stats = compute_bucket_stats([low_score] * 5 + [high_score] * 5, DEFAULT_CONFIG)
    assert len(stats) == 2

    p_low = get_p_hat(stats, _features(weighted_score=Decimal("1.2")), DEFAULT_CONFIG)
    p_high = get_p_hat(stats, _features(weighted_score=Decimal("2.5")), DEFAULT_CONFIG)
    assert p_low.p_hat == Decimal("1")
    assert p_high.p_hat == Decimal("0")


def test_records_sharing_all_three_bands_are_pooled_into_one_bucket():
    records = [_record(won=True)] * 3 + [_record(won=False)] * 2
    stats = compute_bucket_stats(records, DEFAULT_CONFIG)
    assert len(stats) == 1
    (bucket,) = stats.values()
    assert bucket.n == 5
    assert bucket.wins == 3
    assert bucket.p_hat == Decimal("0.6")


def test_bucketing_keys_on_cluster_count_not_raw_wallet_count():
    """docs/PHASE6_DESIGN.md workstream 1/4: bucketing is by independent
    CLUSTER count. A syndicate of 5 wallets that Workstream 1 collapses to
    1 cluster must bucket as cluster_count=1 (below the lowest cluster
    band's min of 2 here - not bucketable at all), not as 5 wallets landing
    comfortably in the "5+" band the way raw wallet counting would.
    """
    five_wallets_one_cluster = _record(cluster_count=1, won=True)
    stats = compute_bucket_stats([five_wallets_one_cluster] * 10, DEFAULT_CONFIG)
    # cluster_count=1 is below the lowest cluster band (2-3) - correctly
    # un-bucketable, proving the field really does drive bucketing.
    assert stats == {}

    five_independent_clusters = _record(cluster_count=5, won=True)
    stats = compute_bucket_stats([five_independent_clusters] * 10, DEFAULT_CONFIG)
    assert len(stats) == 1  # lands in the 5+ band


def test_record_below_the_lowest_band_on_any_dimension_is_dropped():
    # weighted_score=0.5 is below the lowest score band's min (1.0) - not
    # bucketable at all, same as a signal that would never reach the paper
    # engine under default entry filters.
    records = [_record(weighted_score=Decimal("0.5"))] * 10
    stats = compute_bucket_stats(records, DEFAULT_CONFIG)
    assert stats == {}


# --- Wilson interval on known counts ---


def test_wilson_interval_matches_known_reference_values():
    # n=10, x=5 (p_hat=0.5) at z=1.96 is a commonly cited textbook example:
    # 95% Wilson interval approx [0.237, 0.763].
    low, high = wilson_interval(wins=5, n=10, z=Decimal("1.96"))
    assert float(low) == pytest.approx(0.2366, abs=0.001)
    assert float(high) == pytest.approx(0.7634, abs=0.001)


def test_wilson_interval_widens_with_fewer_samples():
    low_small, high_small = wilson_interval(wins=1, n=2, z=Decimal("1.96"))
    low_large, high_large = wilson_interval(wins=50, n=100, z=Decimal("1.96"))
    assert (high_small - low_small) > (high_large - low_large)


def test_wilson_interval_accepts_decimal_wins_and_n():
    """app/optimization/scoring_category.py feeds this a decay-weighted,
    already-shrunk (wins, n) pair - fractional by construction, not a raw
    int trade count - so the signature widened to accept Decimal too.
    """
    int_low, int_high = wilson_interval(wins=5, n=10, z=Decimal("1.96"))
    decimal_low, decimal_high = wilson_interval(
        wins=Decimal("5"), n=Decimal("10"), z=Decimal("1.96")
    )
    assert decimal_low == int_low
    assert decimal_high == int_high


def test_wilson_interval_is_bounded_to_zero_one():
    low, high = wilson_interval(wins=0, n=3, z=Decimal("1.96"))
    assert Decimal("0") <= low <= high <= Decimal("1")
    low, high = wilson_interval(wins=3, n=3, z=Decimal("1.96"))
    assert Decimal("0") <= low <= high <= Decimal("1")


# --- the min-sample gate returns None ---


def test_bucket_below_min_samples_returns_none():
    records = [_record(won=True)] * 4  # min_samples_per_bucket is 5
    stats = compute_bucket_stats(records, DEFAULT_CONFIG)
    assert get_p_hat(stats, _features(), DEFAULT_CONFIG) is None


def test_bucket_at_min_samples_returns_a_result():
    records = [_record(won=True)] * 5  # exactly at the gate
    stats = compute_bucket_stats(records, DEFAULT_CONFIG)
    result = get_p_hat(stats, _features(), DEFAULT_CONFIG)
    assert result is not None
    assert result.n == 5
    assert result.p_hat == Decimal("1")


def test_empty_bucket_returns_none():
    stats = compute_bucket_stats([], DEFAULT_CONFIG)
    assert get_p_hat(stats, _features(), DEFAULT_CONFIG) is None


def test_features_below_lowest_band_return_none_even_with_rich_stats():
    records = [_record(cluster_count=3, won=True)] * 20
    stats = compute_bucket_stats(records, DEFAULT_CONFIG)
    # cluster_count=1 is below the lowest cluster band's min (2).
    below = _features(cluster_count=1)
    assert get_p_hat(stats, below, DEFAULT_CONFIG) is None


# --- recency weighting behaves ---


def test_recency_window_changes_p_hat_when_stale_trades_are_excluded():
    """compute_bucket_stats/get_p_hat have no time-awareness themselves -
    recency is entirely a function of which records the caller passes in
    (app/risk/calibration.py's load_resolved_trade_records only loads
    trades whose exit_at falls inside CALIBRATION_WINDOW_DAYS). Demonstrated
    here by showing the same bucket's p_hat differs depending on whether a
    stale, different-regime batch is included or has already aged out of
    the window - proving the rolling window actually changes the answer,
    not just that a setting exists.
    """
    config = CalibrationConfig(
        cluster_bands=DEFAULT_CONFIG.cluster_bands,
        score_bands=DEFAULT_CONFIG.score_bands,
        price_bands=DEFAULT_CONFIG.price_bands,
        min_samples_per_bucket=2,
        window_days=90,
    )
    recent_wins = [_record(won=True)] * 2
    stale_losses = [_record(won=False)] * 3  # would be outside the window

    stats_including_stale = compute_bucket_stats(recent_wins + stale_losses, config)
    stats_window_only = compute_bucket_stats(recent_wins, config)

    p_including_stale = get_p_hat(stats_including_stale, _features(), config).p_hat
    p_window_only = get_p_hat(stats_window_only, _features(), config).p_hat

    assert p_window_only == Decimal("1")
    assert p_including_stale < p_window_only

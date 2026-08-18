"""Tests for app/consensus_v2/paired_brier.py - pure, no DB, no network."""

import math
from decimal import Decimal

from app.consensus_v2.paired_brier import (
    PairedBrierResult,
    ResolvedRecord,
    brier_score,
    compute_paired_brier,
    kelly_gate_passes,
)


def _record(condition_id, event_cluster_id, p_consensus, p_market, outcome):
    return ResolvedRecord(
        condition_id=condition_id,
        event_cluster_id=event_cluster_id,
        p_consensus=p_consensus,
        p_market=p_market,
        outcome=outcome,
    )


def test_brier_score_matches_hand_computation():
    assert brier_score(Decimal("0.7"), Decimal("1")) == Decimal("0.09")
    assert brier_score(Decimal("0.7"), Decimal("0")) == Decimal("0.49")


def test_paired_brier_on_known_outcomes_hand_computed():
    """1 resolved market: consensus said 0.8, market said 0.6, outcome=1.
    brier_consensus = (0.8-1)^2 = 0.04. brier_market = (0.6-1)^2 = 0.16.
    diff = market - consensus = 0.16 - 0.04 = 0.12 (consensus beat market).
    """
    records = [_record("0xa", None, Decimal("0.8"), Decimal("0.6"), Decimal("1"))]
    result = compute_paired_brier(records)
    assert result.mean_difference == Decimal("0.12")
    assert result.nominal_n == 1
    assert result.effective_n == 1
    assert result.t_statistic is None  # can't compute variance from 1 cluster


def test_paired_brier_empty_input_is_neutral():
    result = compute_paired_brier([])
    assert result.mean_difference == Decimal("0")
    assert result.t_statistic is None
    assert result.nominal_n == 0
    assert result.effective_n == 0


def test_paired_brier_negative_when_market_beats_consensus():
    """consensus 0.5, market 0.9 (closer to the true outcome=1): consensus
    is worse, so mean_difference should be negative.
    """
    records = [_record("0xa", None, Decimal("0.5"), Decimal("0.9"), Decimal("1"))]
    result = compute_paired_brier(records)
    assert result.mean_difference < 0


def test_event_clustered_standard_errors_collapse_correlated_markets():
    """10 resolved markets, ALL in the same event cluster, all with the
    identical paired diff: nominal n=10 but effective n=1 - the
    aggregation must treat this as ONE cluster mean, not 10 independent
    observations, so t_statistic must be None (can't estimate variance
    from a single cluster) even though 10 raw rows exist.
    """
    records = [
        _record(f"0x{i}", "event:same-election", Decimal("0.7"), Decimal("0.5"), Decimal("1"))
        for i in range(10)
    ]
    result = compute_paired_brier(records)
    assert result.nominal_n == 10
    assert result.effective_n == 1
    assert result.t_statistic is None
    # the point estimate is still the correct per-market diff, since every
    # market in the one cluster has the identical value.
    expected_diff = brier_score(Decimal("0.5"), Decimal("1")) - brier_score(
        Decimal("0.7"), Decimal("1")
    )
    assert result.mean_difference == expected_diff


def test_event_clustered_standard_errors_with_distinct_clusters():
    """4 resolved markets, each its own distinct event cluster, with
    varying paired diffs: effective n=4, t_statistic computable, and the
    mean is the plain average across the 4 (single-market) cluster means.
    """
    diffs_and_records = []
    values = [Decimal("0.10"), Decimal("0.05"), Decimal("0.15"), Decimal("0.08")]
    for i, target_diff in enumerate(values):
        # engineer p_consensus/p_market so market-brier - consensus-brier
        # == target_diff, outcome=1: brier(p) = (p-1)^2. Use p_consensus=1
        # (brier=0), p_market = 1 - sqrt(target_diff).
        p_market = Decimal(str(1 - math.sqrt(float(target_diff))))
        diffs_and_records.append(
            _record(f"0x{i}", f"event:{i}", Decimal("1"), p_market, Decimal("1"))
        )

    result = compute_paired_brier(diffs_and_records)
    assert result.nominal_n == 4
    assert result.effective_n == 4
    assert result.t_statistic is not None
    # mean should be close to the mean of the engineered diffs (float roundtrip via sqrt/str)
    expected_mean = sum(values, Decimal(0)) / Decimal(4)
    assert abs(result.mean_difference - expected_mean) < Decimal("0.001")


def test_mixed_clusters_partial_collapse():
    """6 markets: 3 in one cluster (identical diff), 3 in distinct
    clusters - effective n=4 (1 + 3), not 6.
    """
    records = [
        _record(f"0xa{i}", "event:shared", Decimal("0.7"), Decimal("0.5"), Decimal("1"))
        for i in range(3)
    ] + [
        _record(f"0xb{i}", f"event:solo-{i}", Decimal("0.7"), Decimal("0.5"), Decimal("1"))
        for i in range(3)
    ]
    result = compute_paired_brier(records)
    assert result.nominal_n == 6
    assert result.effective_n == 4


# --- Kelly gate ---


def test_kelly_gate_blocked_without_t_statistic():
    result = PairedBrierResult(Decimal("0.1"), None, nominal_n=1, effective_n=1)
    assert kelly_gate_passes(result, min_effective_n=30, min_t_stat=Decimal("1.96")) is False


def test_kelly_gate_blocked_below_min_effective_n():
    result = PairedBrierResult(Decimal("0.1"), Decimal("3.0"), nominal_n=100, effective_n=10)
    assert kelly_gate_passes(result, min_effective_n=30, min_t_stat=Decimal("1.96")) is False


def test_kelly_gate_blocked_when_consensus_does_not_beat_market():
    result = PairedBrierResult(Decimal("-0.05"), Decimal("2.5"), nominal_n=50, effective_n=40)
    assert kelly_gate_passes(result, min_effective_n=30, min_t_stat=Decimal("1.96")) is False


def test_kelly_gate_blocked_below_min_t_stat():
    result = PairedBrierResult(Decimal("0.05"), Decimal("1.0"), nominal_n=50, effective_n=40)
    assert kelly_gate_passes(result, min_effective_n=30, min_t_stat=Decimal("1.96")) is False


def test_kelly_gate_passes_when_every_condition_clears():
    result = PairedBrierResult(Decimal("0.05"), Decimal("2.5"), nominal_n=50, effective_n=40)
    assert kelly_gate_passes(result, min_effective_n=30, min_t_stat=Decimal("1.96")) is True

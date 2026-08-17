"""Unit tests for app/risk/sizing_kelly.py - pure, no DB, no network.

Hand-computed Kelly cases, the no-edge skip, fee adjustment, quarter-Kelly
scaling, the per-position cap, and the sparse-bucket-falls-back-to-tiered
composition app/paper/engine.py relies on (docs/PHASE5_DESIGN.md section 5).
"""

from decimal import Decimal

from app.risk.calibration import (
    CalibrationConfig,
    ResolvedTradeRecord,
    SignalFeatures,
    compute_bucket_stats,
    get_p_hat,
)
from app.risk.sizing_kelly import (
    KellySizingConfig,
    KellySizingRequest,
    KellySizingSkipReason,
    size_kelly_position,
)
from app.risk.sizing_tiered import TieredSizingConfig, TieredSizingRequest, size_tiered_position

DEFAULT_CONFIG = KellySizingConfig(
    kelly_fraction=Decimal("0.25"), fee_pct=Decimal("0"), max_position_pct=Decimal("0.10")
)


def _config(**overrides) -> KellySizingConfig:
    fields = DEFAULT_CONFIG.__dict__.copy()
    fields.update(overrides)
    return KellySizingConfig(**fields)


# --- the Kelly formula on hand-computed cases ---


def test_hand_computed_kelly_case():
    # p_hat=0.60, c=0.50 -> edge=0.10, f_kelly=0.10/0.50=0.20,
    # quarter-Kelly scaled=0.05, on a $1000 bankroll -> $50, well under cap.
    result = size_kelly_position(
        KellySizingRequest(
            current_bankroll=Decimal("1000"), p_hat=Decimal("0.60"), fill_price=Decimal("0.50")
        ),
        DEFAULT_CONFIG,
    )
    assert result.edge == Decimal("0.10")
    assert result.kelly_fraction_used == Decimal("0.05")
    assert result.target_notional == Decimal("50.00")
    assert result.skipped_reason is None


def test_second_hand_computed_kelly_case():
    # p_hat=0.75, c=0.60 -> edge=0.15, f_kelly=0.15/0.40=0.375,
    # quarter-Kelly scaled=0.09375, on $1000 -> $93.75, under the $100 cap.
    result = size_kelly_position(
        KellySizingRequest(
            current_bankroll=Decimal("1000"), p_hat=Decimal("0.75"), fill_price=Decimal("0.60")
        ),
        DEFAULT_CONFIG,
    )
    assert result.edge == Decimal("0.15")
    assert result.kelly_fraction_used == Decimal("0.09375")
    assert result.target_notional == Decimal("93.75")


# --- f <= 0 skips ---


def test_negative_edge_skips():
    # p_hat=0.40, c=0.50 -> edge=-0.10, f_kelly negative - no trade.
    result = size_kelly_position(
        KellySizingRequest(
            current_bankroll=Decimal("1000"), p_hat=Decimal("0.40"), fill_price=Decimal("0.50")
        ),
        DEFAULT_CONFIG,
    )
    assert result.target_notional is None
    assert result.kelly_fraction_used is None
    assert result.skipped_reason == KellySizingSkipReason.NO_EDGE
    assert result.edge == Decimal("-0.10")  # still recorded, even on skip


def test_zero_edge_skips():
    # p_hat exactly equal to c - f_kelly is exactly 0, not a trade (the
    # model requires strictly positive edge, per "f <= 0 skips").
    result = size_kelly_position(
        KellySizingRequest(
            current_bankroll=Decimal("1000"), p_hat=Decimal("0.50"), fill_price=Decimal("0.50")
        ),
        DEFAULT_CONFIG,
    )
    assert result.target_notional is None
    assert result.skipped_reason == KellySizingSkipReason.NO_EDGE
    assert result.edge == Decimal("0")


def test_price_at_or_above_certainty_skips():
    # c_adjusted >= 1 - paying at or above the certain-win price, no valid
    # edge computation is possible regardless of p_hat.
    result = size_kelly_position(
        KellySizingRequest(
            current_bankroll=Decimal("1000"), p_hat=Decimal("0.99"), fill_price=Decimal("1.00")
        ),
        DEFAULT_CONFIG,
    )
    assert result.target_notional is None
    assert result.skipped_reason == KellySizingSkipReason.NO_EDGE


# --- fee adjustment lowers f ---


def test_fee_adjustment_lowers_kelly_fraction():
    request = KellySizingRequest(
        current_bankroll=Decimal("1000"), p_hat=Decimal("0.60"), fill_price=Decimal("0.50")
    )
    no_fee = size_kelly_position(request, _config(fee_pct=Decimal("0")))
    with_fee = size_kelly_position(request, _config(fee_pct=Decimal("0.02")))

    assert with_fee.kelly_fraction_used < no_fee.kelly_fraction_used
    assert with_fee.edge < no_fee.edge
    assert with_fee.target_notional < no_fee.target_notional


def test_fee_large_enough_to_erase_edge_skips():
    # Same p_hat/price as the first hand-computed case, but a fee large
    # enough to push c_adjusted past p_hat entirely.
    result = size_kelly_position(
        KellySizingRequest(
            current_bankroll=Decimal("1000"), p_hat=Decimal("0.60"), fill_price=Decimal("0.50")
        ),
        _config(fee_pct=Decimal("0.30")),  # c_adjusted = 0.65 > p_hat 0.60
    )
    assert result.target_notional is None
    assert result.skipped_reason == KellySizingSkipReason.NO_EDGE


# --- quarter-Kelly scaling ---


def test_kelly_fraction_scales_full_kelly_by_the_configured_fraction():
    request = KellySizingRequest(
        current_bankroll=Decimal("1000"), p_hat=Decimal("0.60"), fill_price=Decimal("0.50")
    )
    quarter = size_kelly_position(request, _config(kelly_fraction=Decimal("0.25")))
    half = size_kelly_position(request, _config(kelly_fraction=Decimal("0.50")))

    # Full Kelly here is 0.20 (see test_hand_computed_kelly_case) - quarter
    # and half must scale it exactly, not just "be smaller than."
    assert quarter.kelly_fraction_used == Decimal("0.05")
    assert half.kelly_fraction_used == Decimal("0.10")
    assert half.kelly_fraction_used == quarter.kelly_fraction_used * 2


# --- the cap overrides ---


def test_cap_overrides_a_large_kelly_fraction():
    # p_hat=0.90, c=0.50 -> edge=0.40, f_kelly=0.40/0.50=0.80, quarter-Kelly
    # scaled=0.20 - on a $1000 bankroll that's $200, but the 10% cap only
    # allows $100.
    result = size_kelly_position(
        KellySizingRequest(
            current_bankroll=Decimal("1000"), p_hat=Decimal("0.90"), fill_price=Decimal("0.50")
        ),
        DEFAULT_CONFIG,
    )
    assert result.kelly_fraction_used == Decimal("0.20")  # the uncapped fraction
    assert result.target_notional == Decimal("100.00")  # but the cap wins


def test_cap_is_a_no_op_when_kelly_fraction_already_fits():
    result = size_kelly_position(
        KellySizingRequest(
            current_bankroll=Decimal("1000"), p_hat=Decimal("0.60"), fill_price=Decimal("0.50")
        ),
        DEFAULT_CONFIG,
    )
    assert result.target_notional == Decimal("50.00")  # well under the $100 cap


# --- sparse bucket -> tiered fallback (assert sizer_used) ---


def test_sparse_bucket_falls_back_to_tiered_sizing():
    """The composition app/paper/engine.py relies on: a bucket with fewer
    than min_samples_per_bucket resolved trades yields no p_hat at all
    (None), and the caller - never this module - falls back to Stage 2
    tiered sizing for that one signal, recording sizer_used=
    "TIERED_FALLBACK" (never a hard failure, and never plain "TIERED",
    which is reserved for a portfolio that chose it deliberately).
    """
    calibration_config = CalibrationConfig(
        cluster_bands=((Decimal("2"), Decimal("999")),),
        score_bands=((Decimal("1.0"), Decimal("999")),),
        price_bands=((Decimal("0"), Decimal("1")),),
        min_samples_per_bucket=30,
        window_days=90,
    )
    # Only 2 resolved trades in this bucket - far below the 30-sample gate.
    records = [
        ResolvedTradeRecord(
            condition_id="0xcond0",
            event_cluster_id=None,
            cluster_count=3,
            weighted_score=Decimal("1.5"),
            average_entry_price=Decimal("0.5"),
            won=True,
        ),
        ResolvedTradeRecord(
            condition_id="0xcond1",
            event_cluster_id=None,
            cluster_count=3,
            weighted_score=Decimal("1.5"),
            average_entry_price=Decimal("0.5"),
            won=False,
        ),
    ]
    bucket_stats = compute_bucket_stats(records, calibration_config)
    features = SignalFeatures(
        cluster_count=3, weighted_score=Decimal("1.5"), average_entry_price=Decimal("0.5")
    )

    calibration_result = get_p_hat(bucket_stats, features, calibration_config)
    assert calibration_result is None  # sparse - Kelly has nothing to say

    tiered_config = TieredSizingConfig(
        bands=((Decimal("1.0"), Decimal("999999"), Decimal("0.02")),),
        max_position_pct=Decimal("0.10"),
    )
    tiered_result = size_tiered_position(
        TieredSizingRequest(current_bankroll=Decimal("1000"), weighted_score=Decimal("1.5")),
        tiered_config,
    )
    sizer_used = "KELLY" if calibration_result is not None else "TIERED_FALLBACK"

    assert sizer_used == "TIERED_FALLBACK"
    assert tiered_result.target_notional is not None


def test_well_calibrated_bucket_uses_kelly_not_fallback():
    """The mirror image of the sparse case - enough resolved trades in the
    bucket (>= min_samples_per_bucket) means get_p_hat returns a real
    result, and the caller uses KELLY, not a fallback.
    """
    calibration_config = CalibrationConfig(
        cluster_bands=((Decimal("2"), Decimal("999")),),
        score_bands=((Decimal("1.0"), Decimal("999")),),
        price_bands=((Decimal("0"), Decimal("1")),),
        min_samples_per_bucket=3,
        window_days=90,
    )
    records = [
        ResolvedTradeRecord(
            condition_id=f"0xcond{i}",
            event_cluster_id=None,
            cluster_count=3,
            weighted_score=Decimal("1.5"),
            average_entry_price=Decimal("0.5"),
            won=won,
        )
        for i, won in enumerate((True, True, False))
    ]
    bucket_stats = compute_bucket_stats(records, calibration_config)
    features = SignalFeatures(
        cluster_count=3, weighted_score=Decimal("1.5"), average_entry_price=Decimal("0.5")
    )

    calibration_result = get_p_hat(bucket_stats, features, calibration_config)
    assert calibration_result is not None
    sizer_used = "KELLY" if calibration_result is not None else "TIERED_FALLBACK"
    assert sizer_used == "KELLY"

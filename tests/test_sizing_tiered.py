"""Unit tests for app/risk/sizing_tiered.py - pure, no DB, no network.

Covers the band-to-fraction mapping (docs/PHASE5_DESIGN.md section 3),
its deterministic edge behavior, the below-lowest-band floor/skip toggle,
and the per-position cap that still applies even to a high-consensus band.
"""

from decimal import Decimal

from app.risk.sizing_tiered import (
    TieredSizingConfig,
    TieredSizingRequest,
    TieredSizingSkipReason,
    fraction_for,
    size_tiered_position,
)

DEFAULT_BANDS = (
    (Decimal("1.0"), Decimal("1.5"), Decimal("0.005")),
    (Decimal("1.5"), Decimal("2.0"), Decimal("0.01")),
    (Decimal("2.0"), Decimal("3.0"), Decimal("0.02")),
    (Decimal("3.0"), Decimal("999999"), Decimal("0.03")),
)

DEFAULT_CONFIG = TieredSizingConfig(
    bands=DEFAULT_BANDS, max_position_pct=Decimal("0.10"), floor_below_lowest_band=True
)


def _config(**overrides) -> TieredSizingConfig:
    fields = DEFAULT_CONFIG.__dict__.copy()
    fields.update(overrides)
    return TieredSizingConfig(**fields)


# --- each band maps to the right fraction ---


def test_first_band_maps_to_its_fraction():
    assert fraction_for(Decimal("1.2"), DEFAULT_BANDS) == Decimal("0.005")


def test_second_band_maps_to_its_fraction():
    assert fraction_for(Decimal("1.7"), DEFAULT_BANDS) == Decimal("0.01")


def test_third_band_maps_to_its_fraction():
    assert fraction_for(Decimal("2.5"), DEFAULT_BANDS) == Decimal("0.02")


def test_top_open_ended_band_maps_to_its_fraction():
    assert fraction_for(Decimal("5.0"), DEFAULT_BANDS) == Decimal("0.03")


def test_size_tiered_position_uses_the_mapped_fraction():
    result = size_tiered_position(
        TieredSizingRequest(current_bankroll=Decimal("1000"), weighted_score=Decimal("2.5")),
        DEFAULT_CONFIG,
    )
    assert result.fraction_used == Decimal("0.02")
    assert result.target_notional == Decimal("20.00")
    assert result.skipped_reason is None


# --- the cap overrides a high band ---


def test_cap_overrides_high_band():
    # Top band (0.03) on a $10,000 bankroll would be $300, but a 1% cap
    # limits any single position to $100.
    config = _config(max_position_pct=Decimal("0.01"))
    result = size_tiered_position(
        TieredSizingRequest(current_bankroll=Decimal("10000"), weighted_score=Decimal("5.0")),
        config,
    )
    assert result.fraction_used == Decimal("0.03")  # the band chosen, uncapped
    assert result.target_notional == Decimal("100.00")  # but the output is capped
    assert result.skipped_reason is None


def test_cap_is_a_no_op_when_band_fraction_already_fits():
    result = size_tiered_position(
        TieredSizingRequest(current_bankroll=Decimal("1000"), weighted_score=Decimal("1.2")),
        DEFAULT_CONFIG,
    )
    assert result.target_notional == Decimal("5.00")  # 0.5% of 1000, well under the 10% cap


# --- scores below the lowest band: floor or skip per config ---


def test_below_lowest_band_floors_by_default():
    result = size_tiered_position(
        TieredSizingRequest(current_bankroll=Decimal("1000"), weighted_score=Decimal("0.5")),
        DEFAULT_CONFIG,
    )
    assert result.skipped_reason is None
    assert result.fraction_used == Decimal("0.005")  # the lowest band's floor
    assert result.target_notional == Decimal("5.00")


def test_below_lowest_band_skips_when_configured():
    config = _config(floor_below_lowest_band=False)
    result = size_tiered_position(
        TieredSizingRequest(current_bankroll=Decimal("1000"), weighted_score=Decimal("0.5")),
        config,
    )
    assert result.target_notional is None
    assert result.fraction_used is None
    assert result.skipped_reason == TieredSizingSkipReason.BELOW_LOWEST_BAND


def test_fraction_for_below_lowest_band_returns_none():
    assert fraction_for(Decimal("0.99"), DEFAULT_BANDS) is None


# --- band edges resolve deterministically ---


def test_shared_edge_resolves_to_the_higher_band_not_the_lower():
    # 1.5 sits exactly on the boundary between "1.0-1.5" and "1.5-2.0" -
    # half-open bands mean it belongs to the band it's the floor of.
    assert fraction_for(Decimal("1.5"), DEFAULT_BANDS) == Decimal("0.01")
    assert fraction_for(Decimal("2.0"), DEFAULT_BANDS) == Decimal("0.02")


def test_lowest_band_floor_edge_is_included_not_below_lowest():
    assert fraction_for(Decimal("1.0"), DEFAULT_BANDS) == Decimal("0.005")


def test_score_at_or_above_a_finite_top_edge_uses_the_top_band():
    finite_bands = (
        (Decimal("1.0"), Decimal("2.0"), Decimal("0.01")),
        (Decimal("2.0"), Decimal("3.0"), Decimal("0.02")),
    )
    assert fraction_for(Decimal("3.0"), finite_bands) == Decimal("0.02")
    assert fraction_for(Decimal("100"), finite_bands) == Decimal("0.02")


def test_band_lookup_is_deterministic_across_repeated_calls():
    results = {fraction_for(Decimal("1.5"), DEFAULT_BANDS) for _ in range(50)}
    assert results == {Decimal("0.01")}

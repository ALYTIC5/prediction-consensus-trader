"""Unit tests for app/optimization/clv.py's pure decision logic - no DB,
no network. See docs/PHASE6_DESIGN.md workstream 2.
"""

from decimal import Decimal

from app.optimization.clv import (
    ResolutionClv,
    _band_index,
    average_contributor_crowdedness,
    clv_value,
    resolve_horizon_clv,
    resolve_resolution_clv,
)
from app.paper.resolution import ResolutionOutcome

# --- clv_value: known price trajectories, hand-computed ---


def test_favourable_move_yields_positive_clv():
    # Entered at 0.40, price later at 0.55 - a 0.15 favourable move.
    assert clv_value(entry_price=Decimal("0.40"), reference_price=Decimal("0.55")) == Decimal(
        "0.15"
    )


def test_unfavourable_move_yields_negative_clv():
    # Entered at 0.60, price later at 0.45 - a 0.15 unfavourable move.
    assert clv_value(entry_price=Decimal("0.60"), reference_price=Decimal("0.45")) == Decimal(
        "-0.15"
    )


def test_no_move_yields_zero_clv():
    assert clv_value(entry_price=Decimal("0.50"), reference_price=Decimal("0.50")) == Decimal("0")


# --- resolve_horizon_clv: skipped (not errored) when there's no price yet ---


def test_horizon_clv_computed_when_a_price_exists():
    result = resolve_horizon_clv(entry_price=Decimal("0.40"), horizon_price=Decimal("0.55"))
    assert result == Decimal("0.15")


def test_horizon_clv_is_none_not_an_error_when_no_price_yet():
    # Not enough price history yet (horizon hasn't arrived, or no snapshot
    # exists there yet) - this must return None, never raise.
    result = resolve_horizon_clv(entry_price=Decimal("0.40"), horizon_price=None)
    assert result is None


def test_horizon_clv_sign_matches_direction_of_the_move():
    favourable = resolve_horizon_clv(entry_price=Decimal("0.30"), horizon_price=Decimal("0.70"))
    unfavourable = resolve_horizon_clv(entry_price=Decimal("0.70"), horizon_price=Decimal("0.30"))
    assert favourable > 0
    assert unfavourable < 0


# --- resolve_resolution_clv: WON/LOST/AMBIGUOUS/NOT_RESOLVED ---


def test_resolution_clv_won_uses_price_of_one():
    result = resolve_resolution_clv(entry_price=Decimal("0.40"), resolution=ResolutionOutcome.WON)
    assert result == ResolutionClv(
        price_at_resolution=Decimal("1.0"), clv_resolution=Decimal("0.60")
    )


def test_resolution_clv_lost_uses_price_of_zero():
    result = resolve_resolution_clv(entry_price=Decimal("0.40"), resolution=ResolutionOutcome.LOST)
    assert result == ResolutionClv(
        price_at_resolution=Decimal("0.0"), clv_resolution=Decimal("-0.40")
    )


def test_resolution_clv_is_none_when_ambiguous():
    # Market closed but resolution can't be inferred - never guessed.
    result = resolve_resolution_clv(
        entry_price=Decimal("0.40"), resolution=ResolutionOutcome.AMBIGUOUS
    )
    assert result is None


def test_resolution_clv_is_none_when_not_resolved():
    result = resolve_resolution_clv(
        entry_price=Decimal("0.40"), resolution=ResolutionOutcome.NOT_RESOLVED
    )
    assert result is None


# --- _band_index: the shared band-lookup helper aggregation relies on ---


def test_band_index_below_lowest_band_is_none():
    bands = [(Decimal("1.0"), Decimal("1.5")), (Decimal("1.5"), Decimal("2.0"))]
    assert _band_index(Decimal("0.5"), bands) is None


def test_band_index_matches_the_containing_band():
    bands = [(Decimal("1.0"), Decimal("1.5")), (Decimal("1.5"), Decimal("2.0"))]
    assert _band_index(Decimal("1.2"), bands) == 0
    assert _band_index(Decimal("1.7"), bands) == 1


def test_band_index_at_or_above_the_top_band_uses_the_top_band():
    bands = [(Decimal("1.0"), Decimal("1.5")), (Decimal("1.5"), Decimal("2.0"))]
    assert _band_index(Decimal("5.0"), bands) == 1


# --- average_contributor_crowdedness: workstream 7 forward validation ---


def test_average_crowdedness_across_contributors():
    contributors = [{"address": "0xA"}, {"address": "0xB"}]
    crowdedness_of = {"0xA": Decimal("0.2"), "0xB": Decimal("0.8")}
    assert average_contributor_crowdedness(contributors, crowdedness_of) == Decimal("0.5")


def test_average_crowdedness_missing_wallet_defaults_to_zero():
    contributors = [{"address": "0xA"}, {"address": "0xB"}]
    crowdedness_of = {"0xA": Decimal("1.0")}  # 0xB never computed
    assert average_contributor_crowdedness(contributors, crowdedness_of) == Decimal("0.5")


def test_average_crowdedness_single_contributor():
    contributors = [{"address": "0xA"}]
    crowdedness_of = {"0xA": Decimal("0.7")}
    assert average_contributor_crowdedness(contributors, crowdedness_of) == Decimal("0.7")

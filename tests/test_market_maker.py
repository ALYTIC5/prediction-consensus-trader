"""Tests for app/optimization/market_maker.py - pure, no DB, no network."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.optimization.market_maker import (
    WalletPositionEvent,
    compute_breadth_depth_component,
    compute_holding_period_component,
    compute_market_maker_score,
    compute_wallet_features,
    consensus_weight_multiplier,
    score_wallet,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _opened(condition_id, outcome, at, size=Decimal("100"), price=Decimal("0.5")):
    return WalletPositionEvent(
        condition_id=condition_id,
        outcome=outcome,
        event_type="OPENED",
        size_before=None,
        size_after=size,
        avg_price=price,
        detected_at=at,
    )


def _closed(condition_id, outcome, at):
    return WalletPositionEvent(
        condition_id=condition_id,
        outcome=outcome,
        event_type="CLOSED",
        size_before=Decimal("100"),
        size_after=Decimal("0"),
        avg_price=Decimal("0.5"),
        detected_at=at,
    )


# --- compute_wallet_features ---


def test_both_sides_ratio_hand_computed():
    """3 markets touched, 2 of them with both Yes and No opened -> 2/3."""
    events = [
        _opened("0xa", "Yes", NOW),
        _opened("0xa", "No", NOW + timedelta(minutes=1)),
        _opened("0xb", "Yes", NOW),
        _opened("0xb", "No", NOW + timedelta(minutes=1)),
        _opened("0xc", "Yes", NOW),
    ]
    features = compute_wallet_features(1, events)
    assert features is not None
    assert features.breadth == 3
    assert features.both_sides_ratio == Decimal("2") / Decimal("3")


def test_holding_period_pairs_opened_and_closed():
    events = [
        _opened("0xa", "Yes", NOW),
        _closed("0xa", "Yes", NOW + timedelta(hours=10)),
    ]
    features = compute_wallet_features(1, events)
    assert features is not None
    assert features.median_holding_hours == Decimal("10")


def test_no_completed_round_trip_has_none_holding_period():
    events = [_opened("0xa", "Yes", NOW)]  # never closed
    features = compute_wallet_features(1, events)
    assert features is not None
    assert features.median_holding_hours is None


def test_empty_events_returns_none():
    assert compute_wallet_features(1, []) is None


# --- compute_holding_period_component ---


def test_holding_period_component_high_for_fast_flipper():
    # wallet median 1h vs population median 100h -> ratio 0.01, component ~0.99
    component = compute_holding_period_component(Decimal("1"), Decimal("100"))
    assert component > Decimal("0.98")


def test_holding_period_component_zero_for_normal_holder():
    component = compute_holding_period_component(Decimal("100"), Decimal("100"))
    assert component == Decimal("0")


def test_holding_period_component_zero_when_no_evidence():
    assert compute_holding_period_component(None, Decimal("100")) == Decimal("0")
    assert compute_holding_period_component(Decimal("10"), None) == Decimal("0")


# --- compute_breadth_depth_component ---


def test_breadth_depth_component_high_for_wide_shallow_trader():
    # breadth 30 (at reference), depth $0 (far below reference $500)
    component = compute_breadth_depth_component(30, Decimal("0"), 30, Decimal("500"))
    assert component == Decimal("1")


def test_breadth_depth_component_low_for_narrow_deep_trader():
    # breadth 1 (far below reference 30), depth $5000 (far above reference)
    component = compute_breadth_depth_component(1, Decimal("5000"), 30, Decimal("500"))
    assert component == Decimal("0")


# --- compute_market_maker_score: noisy-OR ---


def test_market_maker_score_high_from_both_sides_alone():
    """A wallet with strong both-sides evidence but normal holding period
    and normal breadth/depth still scores high - any ONE strong signal is
    enough (noisy-OR, not an average that would dilute it).
    """
    score = compute_market_maker_score(
        both_sides_ratio=Decimal("0.9"),
        holding_period_component=Decimal("0"),
        breadth_depth_component=Decimal("0"),
    )
    assert score == Decimal("0.9")


def test_market_maker_score_low_when_all_components_low():
    score = compute_market_maker_score(
        both_sides_ratio=Decimal("0.05"),
        holding_period_component=Decimal("0"),
        breadth_depth_component=Decimal("0.05"),
    )
    assert score < Decimal("0.15")


def test_market_maker_score_combines_multiple_weak_signals():
    """Several moderate signals compound higher than any one alone, but
    not to certainty.
    """
    score = compute_market_maker_score(Decimal("0.5"), Decimal("0.5"), Decimal("0.5"))
    assert score == Decimal("1") - Decimal("0.5") ** 3
    assert Decimal("0.8") < score < Decimal("0.9")


# --- score_wallet: the full pipeline ---


def test_wallet_holding_both_sides_of_many_markets_scores_high():
    """A market maker: holds both outcomes of nearly every market it
    touches, with normal-ish holding periods and moderate breadth/depth.
    """
    events = []
    for i in range(10):
        cid = f"0x{i}"
        events.append(_opened(cid, "Yes", NOW))
        events.append(_opened(cid, "No", NOW + timedelta(minutes=5)))
        events.append(_closed(cid, "Yes", NOW + timedelta(hours=50)))
        events.append(_closed(cid, "No", NOW + timedelta(hours=50)))

    result = score_wallet(
        wallet_id=1,
        events=events,
        population_median_holding_hours=Decimal("50"),  # same as this wallet - no holding signal
        breadth_reference=30,
        depth_reference=Decimal("500"),
    )
    assert result is not None
    assert result.both_sides_component == Decimal("1")
    assert result.score > Decimal("0.9")


def test_conviction_trader_with_long_holds_scores_low():
    """A directional trader: one outcome per market, held far longer than
    the population median, narrow breadth, large depth.
    """
    events = []
    for i in range(3):
        cid = f"0x{i}"
        events.append(_opened(cid, "Yes", NOW, size=Decimal("10000")))
        events.append(_closed(cid, "Yes", NOW + timedelta(days=60)))

    result = score_wallet(
        wallet_id=1,
        events=events,
        population_median_holding_hours=Decimal("50"),  # this wallet holds MUCH longer
        breadth_reference=30,
        depth_reference=Decimal("500"),
    )
    assert result is not None
    assert result.both_sides_component == Decimal("0")
    assert result.holding_period_component == Decimal(
        "0"
    )  # held longer than population, not shorter
    assert result.score < Decimal("0.1")


# --- consensus_weight_multiplier ---


class _FakeSettings:
    def __init__(self, threshold, mode):
        self.market_maker_score_threshold = threshold
        self.market_maker_mode = mode


def test_multiplier_unadjusted_below_threshold():
    settings = _FakeSettings(Decimal("0.6"), "downweight")
    assert consensus_weight_multiplier(Decimal("0.5"), settings) == Decimal("1")


def test_multiplier_downweight_mode_scales_continuously():
    settings = _FakeSettings(Decimal("0.6"), "downweight")
    assert consensus_weight_multiplier(Decimal("0.9"), settings) == Decimal("0.1")


def test_multiplier_exclude_mode_zeroes_out():
    settings = _FakeSettings(Decimal("0.6"), "exclude")
    assert consensus_weight_multiplier(Decimal("0.9"), settings) == Decimal("0")

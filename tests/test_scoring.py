"""Unit tests for app/consensus/scoring.py - pure, no DB, no network."""

from decimal import Decimal

from app.consensus.scoring import ScoreInputs, ScoreWeights, compute_score

DEFAULT_WEIGHTS = ScoreWeights(
    month=Decimal("0.45"), all_time=Decimal("0.25"), consistency=Decimal("0.30")
)


def _inputs(**overrides: object) -> ScoreInputs:
    defaults: dict[str, object] = {
        "month_pnl": Decimal("0"),
        "all_pnl": Decimal("0"),
        "appearances": 0,
        "possible_appearances": 0,
    }
    defaults.update(overrides)
    return ScoreInputs(**defaults)


def test_zero_pnl_scores_zero_on_pnl_components() -> None:
    breakdown = compute_score(
        _inputs(month_pnl=Decimal("0"), all_pnl=Decimal("0")), DEFAULT_WEIGHTS
    )
    assert breakdown.month_component == Decimal("0")
    assert breakdown.all_time_component == Decimal("0")


def test_negative_pnl_scores_zero_on_pnl_components() -> None:
    breakdown = compute_score(
        _inputs(month_pnl=Decimal("-5000"), all_pnl=Decimal("-100000")), DEFAULT_WEIGHTS
    )
    assert breakdown.month_component == Decimal("0")
    assert breakdown.all_time_component == Decimal("0")


def test_million_pnl_saturates_near_one() -> None:
    breakdown = compute_score(_inputs(month_pnl=Decimal("1000000")), DEFAULT_WEIGHTS)
    assert breakdown.month_component > Decimal("0.999")
    assert breakdown.month_component <= Decimal("1")


def test_pnl_component_never_exceeds_one_far_past_saturation() -> None:
    breakdown = compute_score(_inputs(month_pnl=Decimal("50000000")), DEFAULT_WEIGHTS)
    assert breakdown.month_component == Decimal("1")


def test_consistency_is_appearances_over_possible() -> None:
    breakdown = compute_score(_inputs(appearances=7, possible_appearances=14), DEFAULT_WEIGHTS)
    assert breakdown.consistency_component == Decimal("0.5")


def test_consistency_capped_at_one() -> None:
    # appearances should never exceed possible_appearances in real data,
    # but the cap must hold even if it somehow does.
    breakdown = compute_score(_inputs(appearances=20, possible_appearances=14), DEFAULT_WEIGHTS)
    assert breakdown.consistency_component == Decimal("1")


def test_consistency_zero_possible_appearances_scores_zero() -> None:
    breakdown = compute_score(_inputs(appearances=0, possible_appearances=0), DEFAULT_WEIGHTS)
    assert breakdown.consistency_component == Decimal("0")


def test_weights_are_respected() -> None:
    inputs = _inputs(
        month_pnl=Decimal("1000000"),
        all_pnl=Decimal("0"),
        appearances=0,
        possible_appearances=10,
    )

    all_on_month = ScoreWeights(month=Decimal("1"), all_time=Decimal("0"), consistency=Decimal("0"))
    breakdown = compute_score(inputs, all_on_month)
    assert breakdown.score == breakdown.month_component

    all_on_consistency = ScoreWeights(
        month=Decimal("0"), all_time=Decimal("0"), consistency=Decimal("1")
    )
    breakdown_consistency_only = compute_score(inputs, all_on_consistency)
    assert breakdown_consistency_only.score == Decimal("0")


def test_high_pnl_low_consistency_scores_below_steady_moderate_performer() -> None:
    one_hit_wonder = compute_score(
        _inputs(
            month_pnl=Decimal("5000000"),
            all_pnl=Decimal("5000000"),
            appearances=1,
            possible_appearances=100,
        ),
        DEFAULT_WEIGHTS,
    )
    steady_performer = compute_score(
        _inputs(
            month_pnl=Decimal("50000"),
            all_pnl=Decimal("50000"),
            appearances=100,
            possible_appearances=100,
        ),
        DEFAULT_WEIGHTS,
    )
    assert steady_performer.score > one_hit_wonder.score

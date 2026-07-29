"""Unit tests for app/optimization/brier.py's pure formula - no DB, no
network. See docs/PHASE6_DESIGN.md workstream 4.
"""

from decimal import Decimal

from app.optimization.brier import brier_score

# --- Brier on known prob/outcome pairs ---


def test_perfect_prediction_scores_zero():
    assert brier_score(probability=Decimal("1.0"), outcome=Decimal("1.0")) == Decimal("0")
    assert brier_score(probability=Decimal("0.0"), outcome=Decimal("0.0")) == Decimal("0")


def test_worst_possible_prediction_scores_one():
    assert brier_score(probability=Decimal("1.0"), outcome=Decimal("0.0")) == Decimal("1")
    assert brier_score(probability=Decimal("0.0"), outcome=Decimal("1.0")) == Decimal("1")


def test_hand_computed_case_won():
    # Confidently right-ish: predicted 0.70, actually won (1.0).
    # (0.70 - 1.0)^2 = 0.09
    assert brier_score(probability=Decimal("0.70"), outcome=Decimal("1.0")) == Decimal("0.09")


def test_hand_computed_case_lost():
    # Confidently right-ish the other way: predicted 0.30, actually lost (0.0).
    # (0.30 - 0.0)^2 = 0.09
    assert brier_score(probability=Decimal("0.30"), outcome=Decimal("0.0")) == Decimal("0.09")


def test_coinflip_prediction_scores_a_quarter_regardless_of_outcome():
    # 0.50 is the textbook "I have no idea" prediction - (0.5-1)^2 = (0.5-0)^2 = 0.25.
    assert brier_score(probability=Decimal("0.50"), outcome=Decimal("1.0")) == Decimal("0.25")
    assert brier_score(probability=Decimal("0.50"), outcome=Decimal("0.0")) == Decimal("0.25")


def test_confidently_wrong_scores_worse_than_hedged():
    confidently_wrong = brier_score(probability=Decimal("0.90"), outcome=Decimal("0.0"))
    hedged = brier_score(probability=Decimal("0.50"), outcome=Decimal("0.0"))
    assert confidently_wrong > hedged

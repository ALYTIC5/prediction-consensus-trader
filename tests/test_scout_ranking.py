"""Unit tests for app/scout/ranking.py's pure scout_rank_score - no DB, no
network. See docs/SCOUT_DESIGN.md's Crowdedness section.
"""

from decimal import Decimal

from app.scout.ranking import scout_rank_score


def test_rank_score_combines_wilson_low_and_forward_clv():
    score = scout_rank_score(
        wilson_low=Decimal("0.55"),
        avg_forward_clv=Decimal("0.02"),
        crowdedness=Decimal("0"),
        penalty_weight=Decimal("0.3"),
    )
    assert score == Decimal("0.57")  # 0.55 + 0.02 - 0


def test_rank_score_subtracts_the_crowdedness_penalty():
    score = scout_rank_score(
        wilson_low=Decimal("0.55"),
        avg_forward_clv=Decimal("0.02"),
        crowdedness=Decimal("1.0"),
        penalty_weight=Decimal("0.3"),
    )
    assert score == Decimal("0.27")  # 0.57 - 0.3*1.0


def test_rank_score_is_a_soft_penalty_never_zeroed_by_crowdedness_alone():
    high_skill_high_crowd = scout_rank_score(
        wilson_low=Decimal("0.9"),
        avg_forward_clv=Decimal("0.1"),
        crowdedness=Decimal("1.0"),
        penalty_weight=Decimal("0.3"),
    )
    assert high_skill_high_crowd > Decimal("0.5")  # still ranks respectably


def test_rank_score_floors_at_zero_not_negative():
    score = scout_rank_score(
        wilson_low=Decimal("0.1"),
        avg_forward_clv=Decimal("-0.05"),
        crowdedness=Decimal("1.0"),
        penalty_weight=Decimal("1.0"),
    )
    assert score == Decimal("0")


def test_uncrowded_wallet_ranks_above_equally_skilled_crowded_one():
    shared_wilson_low = Decimal("0.6")
    shared_forward_clv = Decimal("0.03")
    uncrowded = scout_rank_score(
        shared_wilson_low, shared_forward_clv, Decimal("0"), Decimal("0.3")
    )
    crowded = scout_rank_score(
        shared_wilson_low, shared_forward_clv, Decimal("0.9"), Decimal("0.3")
    )
    assert uncrowded > crowded

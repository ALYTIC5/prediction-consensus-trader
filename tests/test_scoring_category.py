"""Unit tests for app/optimization/scoring_category.py's pure core - no DB,
no network. See docs/PHASE6_DESIGN.md workstream 3's win-rate/shrinkage/
decay/zombie machinery, applied per (wallet, category).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.optimization.scoring_category import (
    CategoryTradeRecord,
    RankingConfig,
    WalletCategoryTally,
    _age_days,
    compute_category_score,
    decayed_tallies,
    population_means_by_category,
)
from app.risk.calibration import wilson_interval

DEFAULT_CONFIG = RankingConfig(
    halflife_days=Decimal("30"),
    prior_strength=Decimal("10"),
    zombie_grace_days=14,
)


def _record(
    wallet_id: int, category: str, won: bool, age_days: Decimal = Decimal("0")
) -> CategoryTradeRecord:
    return CategoryTradeRecord(wallet_id=wallet_id, category=category, won=won, age_days=age_days)


# --- decayed_tallies ---


def test_zero_age_contributes_full_weight():
    tallies = decayed_tallies(
        [_record(1, "Sports", won=True, age_days=Decimal("0"))], Decimal("30")
    )
    tally = tallies[(1, "Sports")]
    assert tally.wins == Decimal("1")
    assert tally.n == Decimal("1")


def test_one_halflife_out_contributes_half_weight():
    tallies = decayed_tallies(
        [_record(1, "Sports", won=True, age_days=Decimal("30"))], Decimal("30")
    )
    tally = tallies[(1, "Sports")]
    assert tally.wins == Decimal("0.5")
    assert tally.n == Decimal("0.5")


def test_loss_contributes_to_n_but_not_wins():
    tallies = decayed_tallies(
        [_record(1, "Sports", won=False, age_days=Decimal("0"))], Decimal("30")
    )
    tally = tallies[(1, "Sports")]
    assert tally.wins == Decimal("0")
    assert tally.n == Decimal("1")


def test_records_group_independently_by_wallet_and_category():
    records = [
        _record(1, "Sports", won=True),
        _record(1, "Politics", won=False),
        _record(2, "Sports", won=False),
    ]
    tallies = decayed_tallies(records, Decimal("30"))
    assert set(tallies.keys()) == {(1, "Sports"), (1, "Politics"), (2, "Sports")}
    assert tallies[(1, "Sports")].wins == Decimal("1")
    assert tallies[(1, "Politics")].wins == Decimal("0")


# --- population_means_by_category ---


def test_population_mean_averages_per_wallet_rates_not_pooled_counts():
    # Wallet 1: 100% (1/1). Wallet 2: 0% (0/1). A pooled tally would give
    # 50% either way here, so this doesn't distinguish the two - but with
    # wallet 3 at 3/3 (100%) added, a per-wallet average of [1.0, 0.0, 1.0]
    # = 0.667 differs from a pooled tally of 4 wins / 5 total = 0.8.
    tallies = {
        (1, "Sports"): WalletCategoryTally(wins=Decimal("1"), n=Decimal("1")),
        (2, "Sports"): WalletCategoryTally(wins=Decimal("0"), n=Decimal("1")),
        (3, "Sports"): WalletCategoryTally(wins=Decimal("3"), n=Decimal("3")),
    }
    means = population_means_by_category(tallies)
    expected = (Decimal("1") + Decimal("0") + Decimal("1")) / Decimal("3")
    assert means["Sports"] == expected


def test_population_mean_excludes_wallets_with_zero_n():
    tallies = {
        (1, "Sports"): WalletCategoryTally(wins=Decimal("1"), n=Decimal("1")),
        (2, "Sports"): WalletCategoryTally(wins=Decimal("0"), n=Decimal("0")),
    }
    means = population_means_by_category(tallies)
    assert means["Sports"] == Decimal("1")


def test_categories_with_no_data_are_absent():
    means = population_means_by_category({})
    assert means == {}


# --- compute_category_score: the five task scenarios ---


def test_specialist_scores_high_in_its_category_and_low_in_an_absent_one():
    """A wallet with a strong Sports record and zero Politics history scores
    high on Sports, low/neutral on Politics - the GMX "ROI-per-asset" lesson
    this workstream exists for.
    """
    sports_tally = WalletCategoryTally(wins=Decimal("18"), n=Decimal("20"))  # 90% raw
    no_history = WalletCategoryTally(wins=Decimal("0"), n=Decimal("0"))

    sports_score = compute_category_score(sports_tally, Decimal("0.5"), DEFAULT_CONFIG)
    politics_score = compute_category_score(no_history, Decimal("0.5"), DEFAULT_CONFIG)

    assert sports_score > politics_score
    assert sports_score > Decimal("0.55")
    # No history anchors exactly to the category's own population mean
    # shrunk pair, not to zero and not to the wallet's Sports strength.
    assert politics_score < Decimal("0.3")


def test_shrinkage_pulls_a_small_sample_toward_the_population_mean():
    """A wallet with a tiny sample (2/2, 100% raw) should score meaningfully
    below 1.0 once shrunk toward a much lower population mean - shrinkage
    doing its job, not just the raw rate passing through.
    """
    tiny_perfect_sample = WalletCategoryTally(wins=Decimal("2"), n=Decimal("2"))
    low_population_mean = Decimal("0.3")

    score = compute_category_score(tiny_perfect_sample, low_population_mean, DEFAULT_CONFIG)
    assert score < Decimal("0.7")  # far below the raw 100% rate

    # The same tiny sample against a HIGH population mean shrinks upward
    # instead, proving the pull direction follows the mean, not a fixed bias.
    high_population_mean = Decimal("0.9")
    score_high_mean = compute_category_score(
        tiny_perfect_sample, high_population_mean, DEFAULT_CONFIG
    )
    assert score_high_mean > score


def test_large_sample_resists_shrinkage_more_than_a_small_one():
    """docs/PHASE6_DESIGN.md workstream 3 flag 11: shrinkage is continuous,
    not a hard gate - more real observations should pull a wallet's score
    further from the population mean than fewer, for the same raw rate.
    """
    population_mean = Decimal("0.5")
    small_sample = WalletCategoryTally(wins=Decimal("9"), n=Decimal("10"))  # 90% raw
    large_sample = WalletCategoryTally(wins=Decimal("90"), n=Decimal("100"))  # same 90% raw

    small_score = compute_category_score(small_sample, population_mean, DEFAULT_CONFIG)
    large_score = compute_category_score(large_sample, population_mean, DEFAULT_CONFIG)
    assert large_score > small_score


def test_broad_history_generalist_scores_near_each_categorys_own_mean():
    """A wallet trading at roughly the population rate in several
    categories should score close to (not far above or below) each
    category's own mean, in every one of them - a genuine generalist, not a
    specialist mis-scored as neutral or a phantom standout.
    """
    sports_mean, politics_mean, crypto_mean = Decimal("0.5"), Decimal("0.4"), Decimal("0.6")
    generalist_sports = WalletCategoryTally(wins=Decimal("10"), n=Decimal("20"))  # 50%
    generalist_politics = WalletCategoryTally(wins=Decimal("8"), n=Decimal("20"))  # 40%
    generalist_crypto = WalletCategoryTally(wins=Decimal("12"), n=Decimal("20"))  # 60%

    sports_score = compute_category_score(generalist_sports, sports_mean, DEFAULT_CONFIG)
    politics_score = compute_category_score(generalist_politics, politics_mean, DEFAULT_CONFIG)
    crypto_score = compute_category_score(generalist_crypto, crypto_mean, DEFAULT_CONFIG)

    # Each score sits within a reasonable band of its own category's mean -
    # not identical (the Wilson lower bound always sits a bit below the raw
    # rate) but clearly tracking it, in every category, not just one.
    for score, mean in (
        (sports_score, sports_mean),
        (politics_score, politics_mean),
        (crypto_score, crypto_mean),
    ):
        assert mean - Decimal("0.2") < score < mean


def test_zero_prior_strength_would_disable_shrinkage_entirely():
    """Sanity check on the formula itself: prior_strength=0 means the shrunk
    pair equals the raw pair exactly, regardless of population_mean.
    """
    config = RankingConfig(
        halflife_days=Decimal("30"), prior_strength=Decimal("0"), zombie_grace_days=14
    )
    tally = WalletCategoryTally(wins=Decimal("5"), n=Decimal("10"))
    expected = wilson_interval(Decimal("5"), Decimal("10"), Decimal("1.96"))[0]
    assert compute_category_score(tally, Decimal("0.9"), config) == expected


# --- _age_days ---


def test_age_days_is_zero_for_the_same_instant():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert _age_days(now, now) == Decimal("0")


def test_age_days_never_goes_negative_for_a_future_timestamp():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    future = now + timedelta(days=5)
    assert _age_days(future, now) == Decimal("0")


def test_age_days_matches_hand_computed_value():
    now = datetime(2026, 1, 31, tzinfo=UTC)
    past = now - timedelta(days=15)
    assert _age_days(past, now) == Decimal("15")

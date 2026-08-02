"""Unit tests for app/scout/forward.py's pure Stage-2 core - no DB, no
network. See docs/SCOUT_DESIGN.md.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.scout.forward import (
    WindowVerdict,
    clv_window_stats,
    evaluate_window,
    window_ready_to_close,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
VALIDATION_DAYS = 14
MIN_FORWARD_TRADES = 40


# --- clv_window_stats: hand-computed cases ---


def test_clv_window_stats_matches_hand_computation():
    values = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]
    stats = clv_window_stats(values, Decimal("1.96"))

    mean = Decimal("0.02")
    variance = Decimal("0.0001")  # sample variance, n-1 denominator
    stderr = variance.sqrt() / Decimal(3).sqrt()
    margin = Decimal("1.96") * stderr

    assert stats.n == 3
    assert stats.avg_clv == mean
    assert stats.ci_low == mean - margin
    assert stats.ci_high == mean + margin


def test_clv_window_stats_zero_variance_collapses_ci_to_the_mean():
    values = [Decimal("0.02")] * 10
    stats = clv_window_stats(values, Decimal("1.96"))
    assert stats.avg_clv == Decimal("0.02")
    assert stats.ci_low == Decimal("0.02")
    assert stats.ci_high == Decimal("0.02")


def test_clv_window_stats_empty_is_neutral_not_an_error():
    stats = clv_window_stats([], Decimal("1.96"))
    assert stats.n == 0
    assert stats.avg_clv == Decimal(0)
    assert stats.ci_low == Decimal(0)
    assert stats.ci_high == Decimal(0)


def test_clv_window_stats_single_value_has_no_usable_variance():
    stats = clv_window_stats([Decimal("0.05")], Decimal("1.96"))
    assert stats.n == 1
    assert stats.ci_low == stats.avg_clv == stats.ci_high == Decimal("0.05")


def test_wider_spread_produces_a_wider_confidence_interval():
    tight = clv_window_stats([Decimal("0.019"), Decimal("0.02"), Decimal("0.021")], Decimal("1.96"))
    wide = clv_window_stats([Decimal("-0.05"), Decimal("0.02"), Decimal("0.09")], Decimal("1.96"))
    assert (tight.ci_high - tight.ci_low) < (wide.ci_high - wide.ci_low)


# --- window_ready_to_close ---


def test_window_not_ready_before_validation_days_elapse():
    window_start = NOW - timedelta(days=VALIDATION_DAYS - 1)
    assert (
        window_ready_to_close(
            window_start, NOW, VALIDATION_DAYS, MIN_FORWARD_TRADES, MIN_FORWARD_TRADES
        )
        is False
    )


def test_thin_forward_sample_defers_judgment_even_after_enough_time():
    # Enough time has elapsed, but nowhere near enough forward trades yet.
    window_start = NOW - timedelta(days=VALIDATION_DAYS + 5)
    ready = window_ready_to_close(window_start, NOW, VALIDATION_DAYS, 3, MIN_FORWARD_TRADES)
    assert ready is False


def test_window_ready_once_both_time_and_sample_size_clear():
    window_start = NOW - timedelta(days=VALIDATION_DAYS)
    ready = window_ready_to_close(
        window_start, NOW, VALIDATION_DAYS, MIN_FORWARD_TRADES, MIN_FORWARD_TRADES
    )
    assert ready is True


def test_window_keeps_tracking_past_validation_days_until_enough_trades():
    # 20 days elapsed (past the 14-day mark) but still short of the
    # min-forward-trades floor - the window must still not be ready.
    window_start = NOW - timedelta(days=20)
    ready = window_ready_to_close(
        window_start, NOW, VALIDATION_DAYS, MIN_FORWARD_TRADES - 1, MIN_FORWARD_TRADES
    )
    assert ready is False


# --- evaluate_window: promotion criterion ---


def test_strong_positive_forward_clv_over_enough_trades_promotes():
    values = [Decimal("0.03")] * MIN_FORWARD_TRADES  # consistently positive, big sample
    window_start = NOW - timedelta(days=VALIDATION_DAYS)
    assert (
        window_ready_to_close(window_start, NOW, VALIDATION_DAYS, len(values), MIN_FORWARD_TRADES)
        is True
    )
    stats = clv_window_stats(values, Decimal("1.96"))
    assert evaluate_window(stats) == WindowVerdict.PASS


def test_negative_forward_clv_rejects():
    values = [Decimal("-0.02")] * MIN_FORWARD_TRADES
    stats = clv_window_stats(values, Decimal("1.96"))
    assert evaluate_window(stats) == WindowVerdict.FAIL


def test_flat_forward_clv_around_zero_rejects():
    # Mean essentially zero, CI straddling zero - not a real edge.
    values = [Decimal("0.01"), Decimal("-0.01")] * 20
    stats = clv_window_stats(values, Decimal("1.96"))
    assert stats.ci_low <= Decimal(0)
    assert evaluate_window(stats) == WindowVerdict.FAIL


def test_verdict_uses_the_ci_lower_bound_not_the_raw_mean():
    # Positive mean, but with high enough variance that the CI dips to/
    # below zero - must fail even though the point estimate looks fine.
    values = [Decimal("0.5"), Decimal("-0.4"), Decimal("0.5"), Decimal("-0.4")] * 10
    stats = clv_window_stats(values, Decimal("1.96"))
    assert stats.avg_clv > Decimal(0)
    assert stats.ci_low < Decimal(0)
    assert evaluate_window(stats) == WindowVerdict.FAIL

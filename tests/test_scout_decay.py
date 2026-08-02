"""Unit tests for app/scout/decay.py's pure Stage-3 core - no DB, no
network. See docs/SCOUT_DESIGN.md.
"""

from decimal import Decimal

from app.scout.decay import consecutive_sub_threshold_streak

THRESHOLD = Decimal("0")
DECAY_WINDOWS = 2


def test_no_windows_yet_has_zero_streak():
    assert consecutive_sub_threshold_streak([], THRESHOLD) == 0


def test_all_healthy_windows_have_zero_streak():
    values = [Decimal("0.02"), Decimal("0.01"), Decimal("0.03")]
    assert consecutive_sub_threshold_streak(values, THRESHOLD) == 0


def test_single_sub_threshold_window_is_not_enough_to_decay():
    # Most recent first: one bad window, streak=1 - below decay_windows=2.
    values = [Decimal("-0.01")]
    streak = consecutive_sub_threshold_streak(values, THRESHOLD)
    assert streak == 1
    assert streak < DECAY_WINDOWS


def test_validated_wallet_going_cold_demotes_to_decaying():
    # Two consecutive sub-threshold windows, most recent first - meets
    # SCOUT_DECAY_WINDOWS (default 2), triggering VALIDATED -> DECAYING.
    values = [Decimal("-0.01"), Decimal("-0.02")]
    streak = consecutive_sub_threshold_streak(values, THRESHOLD)
    assert streak == 2
    assert streak >= DECAY_WINDOWS


def test_persistent_decay_rejects():
    # Four consecutive sub-threshold windows = 2 * decay_windows - the
    # "further SCOUT_DECAY_WINDOWS while DECAYING" threshold for REJECTED.
    values = [Decimal("-0.01"), Decimal("-0.02"), Decimal("-0.015"), Decimal("-0.03")]
    streak = consecutive_sub_threshold_streak(values, THRESHOLD)
    assert streak == 4
    assert streak >= 2 * DECAY_WINDOWS


def test_streak_stops_counting_at_the_first_healthy_window():
    # Most recent first: 2 bad, then healthy, then more bad further back -
    # only the unbroken run from the most recent window counts.
    values = [
        Decimal("-0.01"),
        Decimal("-0.02"),
        Decimal("0.05"),
        Decimal("-0.03"),
        Decimal("-0.04"),
    ]
    assert consecutive_sub_threshold_streak(values, THRESHOLD) == 2


def test_a_single_healthy_window_recovers_immediately_regardless_of_history():
    # Even after a long losing streak, the MOST RECENT window being
    # healthy means zero current streak - immediate recovery (design flag
    # 10: cheap to recover, unlike the CI-bound promotion criterion).
    values = [
        Decimal("0.01"),
        Decimal("-0.05"),
        Decimal("-0.05"),
        Decimal("-0.05"),
        Decimal("-0.05"),
    ]
    assert consecutive_sub_threshold_streak(values, THRESHOLD) == 0


def test_threshold_boundary_is_strictly_below_not_at_or_below():
    # Exactly at the decay threshold counts as healthy, not sub-threshold -
    # mirrors run_decay_check's own is_healthy = avg_forward_clv >= threshold.
    values = [Decimal("0")]  # exactly at THRESHOLD=0
    assert consecutive_sub_threshold_streak(values, THRESHOLD) == 0


def test_custom_negative_threshold():
    # A wallet can be configured to tolerate small negative CLV before
    # counting as decaying (SCOUT_DECAY_THRESHOLD is settings-driven, not
    # hardcoded to 0).
    lenient_threshold = Decimal("-0.05")
    values = [Decimal("-0.02"), Decimal("-0.03")]  # negative but above -0.05
    assert consecutive_sub_threshold_streak(values, lenient_threshold) == 0

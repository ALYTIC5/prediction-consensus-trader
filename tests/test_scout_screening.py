"""Unit tests for app/scout/screening.py's pure Stage-1 core - no DB, no
network. See docs/SCOUT_DESIGN.md.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.risk.calibration import wilson_interval
from app.scout.screening import (
    ScoutTradeRecord,
    ScreeningConfig,
    activity_rate,
    compute_screening_result,
    is_zombie,
    profit_factor,
    roi,
    weekly_profitable_fraction,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

DEFAULT_CONFIG = ScreeningConfig(
    min_trades_per_day=Decimal("5"),
    activity_lookback_days=14,
    min_total_trades=50,
    min_wilson_winrate=Decimal("0.52"),
    min_profitable_week_fraction=Decimal("0.5"),
    zombie_grace_days=14,
)


def _record(
    won: bool,
    cash_pnl: str = "10",
    initial_value: str = "100",
    days_ago: int = 0,
    is_zombie_flag: bool = False,
) -> ScoutTradeRecord:
    return ScoutTradeRecord(
        won=won,
        cash_pnl=Decimal(cash_pnl),
        initial_value=Decimal(initial_value),
        event_at=NOW - timedelta(days=days_ago),
        is_zombie=is_zombie_flag,
    )


def _weekly(count: int, won: bool, cash_pnl: str, week_offset: int) -> list[ScoutTradeRecord]:
    """count records, one per ISO week, at weeks [week_offset,
    week_offset + count). Every days_ago is an exact multiple of 7 from
    NOW, so it lands on the same day-of-week as NOW every time - the ISO
    week number shifts by exactly 1 per index with zero ambiguity near a
    calendar-week boundary. Callers pick disjoint week_offset ranges to
    guarantee no two groups ever land in the same ISO week.
    """
    return [_record(won, cash_pnl=cash_pnl, days_ago=7 * (week_offset + i)) for i in range(count)]


# --- is_zombie ---


def test_is_zombie_true_when_open_long_past_end_date_plus_grace():
    end_date = NOW - timedelta(days=30)
    assert is_zombie(True, end_date, NOW, grace_days=14) is True


def test_is_zombie_false_when_within_grace_period():
    end_date = NOW - timedelta(days=10)  # 10 days < 14-day grace
    assert is_zombie(True, end_date, NOW, grace_days=14) is False


def test_is_zombie_false_when_not_open():
    end_date = NOW - timedelta(days=30)
    assert is_zombie(False, end_date, NOW, grace_days=14) is False


def test_is_zombie_false_when_market_has_no_end_date():
    assert is_zombie(True, None, NOW, grace_days=14) is False


def test_is_zombie_exactly_at_grace_boundary_is_not_yet_zombie():
    end_date = NOW - timedelta(days=14)  # exactly at grace, not beyond it
    assert is_zombie(True, end_date, NOW, grace_days=14) is False


# --- Wilson bound on known counts ---


def test_wilson_bound_matches_known_reference_via_compute_screening_result():
    # 60/100 wins, spread across disjoint weeks so activity/consistency
    # don't confound this - isolating the Wilson-bound wiring specifically.
    records = _weekly(60, True, "10", week_offset=0) + _weekly(40, False, "-5", week_offset=100)
    result = compute_screening_result(records, NOW, DEFAULT_CONFIG)
    expected_low, expected_high = wilson_interval(60, 100, Decimal("1.96"))
    assert result.wilson_low == expected_low
    assert result.wilson_high == expected_high


def test_wilson_bound_ranks_8_of_10_below_800_of_1000():
    small = compute_screening_result(
        _weekly(8, True, "10", 0) + _weekly(2, False, "-5", 20),
        NOW,
        DEFAULT_CONFIG,
    )
    large = compute_screening_result(
        _weekly(800, True, "10", 0) + _weekly(200, False, "-5", 900),
        NOW,
        DEFAULT_CONFIG,
    )
    assert small.wilson_low < large.wilson_low  # same 80% raw rate, very different confidence


# --- activity floor ---


def test_activity_rate_below_floor_rejects_a_low_frequency_wallet():
    # Plenty of historical volume (60 trades, easily clears total-trades
    # and win-rate gates) but only 1 trade in the last 14 days.
    old_records = _weekly(59, True, "10", week_offset=20)
    recent_record = _record(True, cash_pnl="10", days_ago=1)
    records = old_records + [recent_record]

    result = compute_screening_result(records, NOW, DEFAULT_CONFIG)
    assert result.activity_rate < DEFAULT_CONFIG.min_trades_per_day
    assert "ACTIVITY" in result.failing_gates
    assert result.passed is False


def test_activity_rate_computation_is_a_plain_rate():
    records = [_record(True, days_ago=d) for d in range(14)]  # one per day, 14 days
    rate = activity_rate(records, NOW, lookback_days=14)
    assert rate == Decimal("1")  # 14 trades / 14 days


# --- zombie guard ---


def test_zombie_records_are_forced_losses_pulling_down_the_wilson_bound():
    # 10 genuinely closed positions, ALL wins - looks like a perfect
    # historical record on its own.
    clean_wins = _weekly(10, True, "10", week_offset=0)
    clean_only = compute_screening_result(clean_wins, NOW, DEFAULT_CONFIG)

    # Add zombie positions - still open long past their market's end_date,
    # forced to count as losses (won=False) even though their last mark-
    # to-market cash_pnl is nominally positive and nothing was ever
    # "closed" - exactly what the loader does for a real zombie row.
    zombies = [_record(False, cash_pnl="5", days_ago=3 + i, is_zombie_flag=True) for i in range(10)]
    with_zombies = compute_screening_result(clean_wins + zombies, NOW, DEFAULT_CONFIG)

    assert with_zombies.zombie_count == 10
    assert with_zombies.wins == clean_only.wins  # zombies never counted as wins...
    assert with_zombies.wilson_low < clean_only.wilson_low  # ...so the bound drops


def test_zombie_pnl_feeds_the_loss_side_of_profit_factor_regardless_of_sign():
    # A zombie's mark-to-market cash_pnl can still be nominally positive -
    # it must still land in the LOSS bucket of profit_factor (won=False is
    # what routes it, not the sign of cash_pnl).
    records = [
        _record(True, cash_pnl="100", days_ago=0),
        _record(False, cash_pnl="5", days_ago=1, is_zombie_flag=True),  # positive but forced loss
    ]
    pf = profit_factor(records)
    # win side = 100, loss side = |5| = 5 -> profit_factor = 20
    assert pf == Decimal("20")


# --- weekly consistency ---


def test_consistency_requires_a_majority_of_profitable_weeks():
    # 3 profitable weeks, 2 losing weeks, guaranteed disjoint -> 3/5 = 0.6.
    records = _weekly(3, True, "10", week_offset=0) + _weekly(2, False, "-10", week_offset=50)
    fraction = weekly_profitable_fraction(records)
    assert fraction == Decimal("3") / Decimal("5")
    assert fraction > DEFAULT_CONFIG.min_profitable_week_fraction


def test_consistency_fails_without_a_majority():
    # 2 profitable weeks, 3 losing weeks, guaranteed disjoint -> 2/5 = 0.4.
    records = _weekly(2, True, "10", week_offset=0) + _weekly(3, False, "-10", week_offset=50)
    fraction = weekly_profitable_fraction(records)
    assert fraction == Decimal("2") / Decimal("5")
    assert fraction <= DEFAULT_CONFIG.min_profitable_week_fraction


def test_a_week_with_no_trades_does_not_count_toward_either_side():
    # Exactly 2 distinct weeks have any qualifying trade at all; a week
    # with zero trades in between is simply absent, not a loss.
    records = [_record(True, cash_pnl="10", days_ago=0), _record(True, cash_pnl="10", days_ago=7)]
    assert weekly_profitable_fraction(records) == Decimal("1")  # both weeks profitable


# --- profit_factor / roi ---


def test_profit_factor_none_when_no_losses():
    records = [_record(True, cash_pnl="10", days_ago=0)]
    assert profit_factor(records) is None


def test_roi_none_when_no_capital_deployed():
    assert roi([]) is None


def test_roi_matches_hand_computation():
    records = [
        _record(True, cash_pnl="20", initial_value="100", days_ago=0),
        _record(False, cash_pnl="-10", initial_value="100", days_ago=1),
    ]
    assert roi(records) == Decimal("10") / Decimal("200")


# --- the full gate: a genuinely strong, active wallet passes ---


def test_strong_active_wallet_passes_to_candidate():
    # >= 5 trades/day sustained over the 14-day activity window means >= 70
    # trades in that window alone - well above the 50-trade total floor on
    # its own, plus a thin slice of older winning weeks for good measure.
    recent_active_wins = [
        _record(True, cash_pnl="20", days_ago=d % 14) for d in range(75)
    ]  # ~5.4/day over the last 14 days
    historical_wins = _weekly(5, True, "20", week_offset=10)
    historical_losses = _weekly(2, False, "-5", week_offset=200)

    records = recent_active_wins + historical_wins + historical_losses
    result = compute_screening_result(records, NOW, DEFAULT_CONFIG)

    assert result.total_trades >= 50
    assert result.wilson_low > DEFAULT_CONFIG.min_wilson_winrate
    assert result.activity_rate >= DEFAULT_CONFIG.min_trades_per_day
    assert result.profitable_week_fraction > DEFAULT_CONFIG.min_profitable_week_fraction
    assert result.failing_gates == ()
    assert result.passed is True


def test_weak_wallet_fails_and_records_which_gates():
    # Deliberately thin: only 10 trades, well under the 50-trade floor.
    records = _weekly(6, True, "10", 0) + _weekly(4, False, "-10", 50)
    result = compute_screening_result(records, NOW, DEFAULT_CONFIG)
    assert result.passed is False
    assert "TOTAL_TRADES" in result.failing_gates

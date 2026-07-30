"""Unit tests for app/paper/exits_scalp.py's pure core - no DB, no network.

The "greed" portfolio's whole reason to exist: same signal stream, same
entry filters as baseline, only the exit rule differs.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.paper.exits_scalp import ScalpExitConfig, ScalpExitReason, check_scalp_exit

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ENTERED_AT = NOW - timedelta(hours=1)

DEFAULT_CONFIG = ScalpExitConfig(
    take_profit=Decimal("0.03"),
    breakeven_tolerance=Decimal("0.005"),
    max_hold_hours=72,
)


def test_reaching_take_profit_closes_immediately():
    entry_price = Decimal("0.50")
    pessimistic_price = entry_price + Decimal("0.03")  # exactly at the threshold
    reason = check_scalp_exit(entry_price, pessimistic_price, ENTERED_AT, NOW, DEFAULT_CONFIG)
    assert reason == ScalpExitReason.TAKE_PROFIT


def test_below_take_profit_does_not_close():
    entry_price = Decimal("0.50")
    pessimistic_price = entry_price + Decimal("0.02")  # short of the 0.03 threshold
    reason = check_scalp_exit(entry_price, pessimistic_price, ENTERED_AT, NOW, DEFAULT_CONFIG)
    assert reason is None


def test_underwater_recovery_within_tolerance_closes_at_breakeven():
    entry_price = Decimal("0.50")
    # Down 0.003 - within the 0.005 tolerance ("bounce back to even").
    pessimistic_price = entry_price - Decimal("0.003")
    reason = check_scalp_exit(entry_price, pessimistic_price, ENTERED_AT, NOW, DEFAULT_CONFIG)
    assert reason == ScalpExitReason.BREAKEVEN


def test_underwater_beyond_tolerance_does_not_close():
    entry_price = Decimal("0.50")
    # Down 0.02 - well outside the 0.005 tolerance, not a "bounce back."
    pessimistic_price = entry_price - Decimal("0.02")
    reason = check_scalp_exit(entry_price, pessimistic_price, ENTERED_AT, NOW, DEFAULT_CONFIG)
    assert reason is None


def test_exactly_at_breakeven_does_not_trigger_the_breakeven_reason():
    """gain_per_share == 0 is neither a profit nor underwater - the
    breakeven trigger is specifically for a SMALL LOSS that bounced back
    close to even, not a price sitting exactly on the fill price.
    """
    entry_price = Decimal("0.50")
    reason = check_scalp_exit(entry_price, entry_price, ENTERED_AT, NOW, DEFAULT_CONFIG)
    assert reason is None


def test_neither_trigger_fires_hits_max_hold_backstop():
    entry_price = Decimal("0.50")
    pessimistic_price = entry_price + Decimal("0.01")  # small unrealized gain, below take-profit
    entered_long_ago = NOW - timedelta(hours=73)  # past the 72h max_hold_hours
    reason = check_scalp_exit(entry_price, pessimistic_price, entered_long_ago, NOW, DEFAULT_CONFIG)
    assert reason == ScalpExitReason.MAX_HOLD


def test_max_hold_does_not_fire_before_the_window_elapses():
    entry_price = Decimal("0.50")
    pessimistic_price = entry_price + Decimal("0.01")
    entered_recently = NOW - timedelta(hours=1)
    reason = check_scalp_exit(entry_price, pessimistic_price, entered_recently, NOW, DEFAULT_CONFIG)
    assert reason is None


def test_max_hold_backstop_fires_even_with_no_price_data():
    """The backstop is time-only - a trade must never get stuck open
    forever just because price data went missing.
    """
    entered_long_ago = NOW - timedelta(hours=100)
    reason = check_scalp_exit(Decimal("0.50"), None, entered_long_ago, NOW, DEFAULT_CONFIG)
    assert reason == ScalpExitReason.MAX_HOLD


def test_no_price_data_and_within_hold_window_stays_open():
    entered_recently = NOW - timedelta(hours=1)
    reason = check_scalp_exit(Decimal("0.50"), None, entered_recently, NOW, DEFAULT_CONFIG)
    assert reason is None


def test_take_profit_fires_before_max_hold_even_late_in_the_window():
    """Priority order: take-profit is checked before the backstop, even on
    a trade that's almost at its max-hold deadline.
    """
    entry_price = Decimal("0.50")
    pessimistic_price = entry_price + Decimal("0.03")
    entered_almost_at_deadline = NOW - timedelta(hours=71, minutes=59)
    reason = check_scalp_exit(
        entry_price, pessimistic_price, entered_almost_at_deadline, NOW, DEFAULT_CONFIG
    )
    assert reason == ScalpExitReason.TAKE_PROFIT


def test_small_take_profit_closes_early_even_though_price_would_later_resolve_much_higher():
    """The intended behaviour: a scalp closes the instant its tiny margin
    is there, even on a trade that - if held - would eventually resolve
    strongly positive (e.g. toward 1.0). This is "greed" 's whole point:
    take the small win now rather than hold for the bigger, less certain
    one baseline would wait for.
    """
    entry_price = Decimal("0.40")
    pessimistic_price_now = entry_price + Decimal("0.03")  # take-profit threshold, right now
    hypothetical_eventual_resolution_price = Decimal("1.0")  # would be a huge win if held

    reason = check_scalp_exit(entry_price, pessimistic_price_now, ENTERED_AT, NOW, DEFAULT_CONFIG)
    assert reason == ScalpExitReason.TAKE_PROFIT
    # The much larger eventual price is never even consulted by a stateless
    # per-cycle check - proving the exit is unconditional on what happens
    # to the price afterward, exactly the tradeoff scalping makes.
    assert pessimistic_price_now < hypothetical_eventual_resolution_price

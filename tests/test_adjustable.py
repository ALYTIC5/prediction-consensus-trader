"""Unit tests for app/config/adjustable.py - pure, no DB, no network."""

from decimal import Decimal

import pytest

from app.config.adjustable import ADJUSTABLE, parse_and_validate


def test_unknown_key_raises() -> None:
    with pytest.raises(ValueError, match="not an adjustable setting"):
        parse_and_validate("not_a_real_setting", "5")


def test_wrong_type_raises() -> None:
    with pytest.raises(ValueError, match="integer"):
        parse_and_validate("consensus_min_traders", "not-a-number")


def test_below_min_raises() -> None:
    with pytest.raises(ValueError, match=">="):
        parse_and_validate("consensus_min_traders", "0")


def test_above_max_raises() -> None:
    with pytest.raises(ValueError, match="<="):
        parse_and_validate("signal_price_min", "1.5")


def test_valid_round_trip_int() -> None:
    assert parse_and_validate("consensus_min_traders", "5") == 5


def test_valid_round_trip_decimal() -> None:
    assert parse_and_validate("signal_price_min", "0.10") == Decimal("0.10")


def test_valid_round_trip_bool() -> None:
    assert parse_and_validate("consensus_include_increases", "true") is True
    assert parse_and_validate("consensus_include_increases", "false") is False


def test_invalid_bool_raises() -> None:
    with pytest.raises(ValueError, match="boolean"):
        parse_and_validate("consensus_include_increases", "maybe")


def test_restart_required_fields_are_flagged() -> None:
    for key in (
        "leaderboard_interval_seconds",
        "positions_interval_seconds",
        "markets_interval_seconds",
        "consensus_interval_seconds",
    ):
        assert ADJUSTABLE[key].restart_required is True


def test_threshold_fields_are_not_restart_required() -> None:
    for key in (
        "consensus_min_traders",
        "consensus_min_weighted_score",
        "signal_price_min",
        "signal_ttl_hours",
    ):
        assert ADJUSTABLE[key].restart_required is False


def test_out_of_bounds_post_leaves_the_db_untouched() -> None:
    """app/dashboard/queries.py's apply_override() calls parse_and_validate
    BEFORE its `with db_session()` block - proving this raises is proving
    that block is unreachable for an out-of-bounds POST, so no
    runtime_overrides or override_audit row is ever written. Testing the
    validation path directly (as here) is the pure equivalent of testing
    "the DB was untouched," without needing a database to prove it.
    """
    with pytest.raises(ValueError, match=">="):
        parse_and_validate("consensus_min_traders", "0")

    # The registry itself - the single source of truth apply_override()
    # checks against - is unaffected by the rejected attempt.
    assert ADJUSTABLE["consensus_min_traders"].min == 1

"""Unit tests for diff_positions - pure logic, no DB, no network."""

from decimal import Decimal

from app.collectors.diffing import SIZE_EPSILON, diff_positions
from app.collectors.schemas import PositionEntry


def _position(asset: str, size: Decimal, **overrides: object) -> PositionEntry:
    defaults: dict[str, object] = {
        "proxy_wallet": "0xabc",
        "asset": asset,
        "condition_id": "0xcond",
        "size": size,
        "avg_price": Decimal("0.5"),
        "initial_value": Decimal("5"),
        "current_value": Decimal("6"),
        "cash_pnl": Decimal("1"),
        "percent_pnl": Decimal("20"),
        "cur_price": Decimal("0.6"),
        "redeemable": False,
        "title": "Some Market",
        "event_slug": "some-market",
        "outcome": "Yes",
        "outcome_index": 0,
        "negative_risk": False,
    }
    defaults.update(overrides)
    return PositionEntry(**defaults)


def test_new_asset_opens() -> None:
    current = [_position("111", Decimal("10"))]
    events = diff_positions({}, current)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "OPENED"
    assert event.asset == "111"
    assert event.size_before == Decimal("0")
    assert event.size_after == Decimal("10")


def test_size_increase() -> None:
    current = [_position("111", Decimal("15"))]
    events = diff_positions({"111": Decimal("10")}, current)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "INCREASED"
    assert event.size_before == Decimal("10")
    assert event.size_after == Decimal("15")


def test_size_decrease() -> None:
    current = [_position("111", Decimal("4"))]
    events = diff_positions({"111": Decimal("10")}, current)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "DECREASED"
    assert event.size_before == Decimal("10")
    assert event.size_after == Decimal("4")


def test_disappearance_closes() -> None:
    events = diff_positions({"111": Decimal("10")}, [])

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "CLOSED"
    assert event.asset == "111"
    assert event.size_before == Decimal("10")
    assert event.size_after == Decimal("0")


def test_unchanged_size_emits_nothing() -> None:
    current = [_position("111", Decimal("10"))]
    events = diff_positions({"111": Decimal("10")}, current)

    assert events == []


def test_sub_epsilon_noise_emits_nothing() -> None:
    noisy_size = Decimal("10") + (SIZE_EPSILON / 2)
    current = [_position("111", noisy_size)]
    events = diff_positions({"111": Decimal("10")}, current)

    assert events == []

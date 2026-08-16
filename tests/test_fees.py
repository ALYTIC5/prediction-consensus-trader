"""Tests for app/paper/fees.py - the documented Polymarket taker-fee
formula (fee = size × rate × price × (1 - price)), pure, no DB, no network.
"""

from decimal import Decimal

from app.paper.fees import compute_taker_fee


def test_fee_matches_hand_computed_formula() -> None:
    # Buy 100 shares at 0.60, 5% rate: 100 * 0.05 * 0.60 * 0.40 = 1.2
    fee = compute_taker_fee(price=Decimal("0.60"), size=Decimal("100"), rate=Decimal("0.05"))
    assert fee == Decimal("1.2")


def test_fee_is_symmetric_around_extreme_prices() -> None:
    # p and (1-p) are interchangeable in the formula - fee at 0.1 == fee at 0.9.
    low = compute_taker_fee(price=Decimal("0.1"), size=Decimal("100"), rate=Decimal("0.05"))
    high = compute_taker_fee(price=Decimal("0.9"), size=Decimal("100"), rate=Decimal("0.05"))
    assert low == high


def test_fee_peaks_at_50_percent_price() -> None:
    at_half = compute_taker_fee(price=Decimal("0.5"), size=Decimal("100"), rate=Decimal("0.05"))
    off_half = compute_taker_fee(price=Decimal("0.3"), size=Decimal("100"), rate=Decimal("0.05"))
    assert at_half > off_half


def test_fee_is_zero_at_price_zero_or_one() -> None:
    # A trade at the extremes (p=0 or p=1) never happens in practice, but
    # the formula itself is zero there by construction - worth pinning down
    # since it's the boundary the fee schedule's own docs describe.
    assert compute_taker_fee(price=Decimal("0"), size=Decimal("100"), rate=Decimal("0.05")) == 0
    assert compute_taker_fee(price=Decimal("1"), size=Decimal("100"), rate=Decimal("0.05")) == 0


def test_fee_scales_linearly_with_size() -> None:
    single = compute_taker_fee(price=Decimal("0.6"), size=Decimal("1"), rate=Decimal("0.05"))
    hundred = compute_taker_fee(price=Decimal("0.6"), size=Decimal("100"), rate=Decimal("0.05"))
    assert hundred == single * 100


def test_fee_scales_linearly_with_rate() -> None:
    # Documented category rates: geopolitics is 0 (fee-free); double the
    # rate should exactly double the fee, price/size held constant.
    single_rate = compute_taker_fee(price=Decimal("0.6"), size=Decimal("100"), rate=Decimal("0.04"))
    double_rate = compute_taker_fee(price=Decimal("0.6"), size=Decimal("100"), rate=Decimal("0.08"))
    geopolitics = compute_taker_fee(price=Decimal("0.6"), size=Decimal("100"), rate=Decimal("0"))
    assert geopolitics == 0
    assert double_rate == single_rate * 2

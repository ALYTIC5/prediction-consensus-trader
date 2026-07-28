"""Exhaustive unit tests for app/paper/fills.py - pure, no DB, no network.

This is the module that determines whether paper-trading results are
truthful, so it gets the most thorough tests in the project: every
assumption in the fill model (ask not mid/bid, size-scaled slippage, the
simulated-delay lookup, drift rejection, the no-liquidity/fallback paths,
and the drift boundary itself) has a dedicated test proving it, not just an
end-to-end smoke check.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.paper.fills import FillConfig, FillReason, FillRequest, MarketSnapshot, compute_fill

NOW = datetime(2026, 1, 1, tzinfo=UTC)

DEFAULT_CONFIG = FillConfig(
    entry_delay_seconds=30,
    slippage_k=Decimal("0.5"),
    slippage_max=Decimal("0.15"),
    no_delayed_snapshot_penalty=Decimal("0.05"),
    max_entry_price_drift=Decimal("0.15"),
)


def _snapshot(
    captured_at: datetime,
    ask: Decimal | None = None,
    bid: Decimal | None = None,
    liquidity: Decimal | None = None,
    price: Decimal | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        captured_at=captured_at, ask=ask, bid=bid, liquidity=liquidity, price=price
    )


def _request(
    signal_price: Decimal = Decimal("0.50"),
    order_notional: Decimal = Decimal("100"),
    detected_at: datetime = NOW,
) -> FillRequest:
    return FillRequest(
        signal_price=signal_price, order_notional=order_notional, detected_at=detected_at
    )


def test_fills_at_ask_not_mid_or_bid() -> None:
    """Base price must come from the ask - never the bid or the midpoint."""
    ask = Decimal("0.60")
    bid = Decimal("0.50")
    mid = Decimal("0.55")
    liquidity = Decimal("1000000000")  # enormous, so slippage is negligible
    order_notional = Decimal("10")

    current = _snapshot(NOW, ask=ask, bid=bid, liquidity=liquidity, price=mid)
    # Same values at the delay threshold - avoids conflating this test with
    # the fallback no-delayed-snapshot penalty (see test_fallback_path...).
    delayed = _snapshot(NOW + timedelta(seconds=30), ask=ask, bid=bid, price=mid)

    result = compute_fill(
        _request(signal_price=ask, order_notional=order_notional),
        [current, delayed],
        DEFAULT_CONFIG,
        now=NOW + timedelta(seconds=30),
    )

    expected_penalty = DEFAULT_CONFIG.slippage_k * order_notional / liquidity
    expected_fill_price = ask + expected_penalty

    assert result.filled is True
    assert result.reason == FillReason.FILLED
    assert result.fill_price == expected_fill_price
    # Sanity: nowhere near the bid or the mid.
    assert abs(result.fill_price - bid) > Decimal("0.05")
    assert abs(result.fill_price - mid) > Decimal("0.03")


def test_slippage_scales_with_order_size() -> None:
    """A bigger order against the same liquidity pays more slippage."""
    liquidity = Decimal("20000")
    small_order = Decimal("100")
    large_order = Decimal("5000")

    def _penalty(order_notional: Decimal) -> Decimal:
        current = _snapshot(NOW, ask=Decimal("0.50"), liquidity=liquidity)
        result = compute_fill(
            _request(signal_price=Decimal("0.50"), order_notional=order_notional),
            [current],
            DEFAULT_CONFIG,
            now=NOW,
        )
        assert result.slippage_paid is not None
        return result.slippage_paid

    assert _penalty(large_order) > _penalty(small_order)


def test_slippage_is_harsher_in_thin_markets() -> None:
    """Same order, thinner market -> bigger penalty - the whole point of
    scaling slippage by order_notional / liquidity.
    """
    order_notional = Decimal("1000")
    thin_liquidity = Decimal("20000")
    thick_liquidity = Decimal("200000")

    def _penalty(liquidity: Decimal) -> Decimal:
        current = _snapshot(NOW, ask=Decimal("0.50"), liquidity=liquidity)
        delayed = _snapshot(NOW + timedelta(seconds=30), ask=Decimal("0.50"))
        result = compute_fill(
            _request(signal_price=Decimal("0.50"), order_notional=order_notional),
            [current, delayed],
            DEFAULT_CONFIG,
            now=NOW + timedelta(seconds=30),
        )
        assert result.slippage_paid is not None
        return result.slippage_paid

    thin_penalty = _penalty(thin_liquidity)
    thick_penalty = _penalty(thick_liquidity)

    assert thin_penalty > thick_penalty
    # Exact math, not just an inequality - proves the formula, not just its direction.
    assert thin_penalty == DEFAULT_CONFIG.slippage_k * order_notional / thin_liquidity
    assert thick_penalty == DEFAULT_CONFIG.slippage_k * order_notional / thick_liquidity


def test_slippage_clamped_at_max_for_enormous_order() -> None:
    """An order whose raw penalty would blow past slippage_max is capped,
    not left to produce an absurd fill price.
    """
    ask = Decimal("0.50")
    liquidity = Decimal("1000")
    enormous_order = Decimal("10000000")  # raw penalty would be 5000, way over the 0.15 cap

    current = _snapshot(NOW, ask=ask, liquidity=liquidity)
    delayed = _snapshot(NOW + timedelta(seconds=30), ask=ask)  # avoid fallback penalty noise

    result = compute_fill(
        _request(signal_price=ask, order_notional=enormous_order),
        [current, delayed],
        DEFAULT_CONFIG,
        now=NOW + timedelta(seconds=30),
    )

    assert result.filled is True
    assert result.slippage_paid == DEFAULT_CONFIG.slippage_max
    assert result.fill_price == ask + DEFAULT_CONFIG.slippage_max


def test_missed_when_price_drifted_beyond_max() -> None:
    """The move already happened - no fill, not a fill at a stale price."""
    signal_price = Decimal("0.50")
    drifted_ask = Decimal("0.70")  # 40% move, config allows only 15%

    current = _snapshot(NOW, ask=drifted_ask, liquidity=Decimal("10000"))

    result = compute_fill(
        _request(signal_price=signal_price, order_notional=Decimal("100")),
        [current],
        DEFAULT_CONFIG,
        now=NOW,
    )

    assert result.filled is False
    assert result.fill_price is None
    assert result.slippage_paid is None
    assert result.reason == FillReason.MISSED_DRIFT


def test_delayed_snapshot_uses_later_price_not_entry_price() -> None:
    """A qualifying delayed snapshot's price drives the fill - the
    entry-time snapshot's own ask must be ignored once a delayed one exists.
    """
    entry_ask = Decimal("0.50")
    later_price = Decimal("0.55")  # within drift tolerance of signal_price=0.50

    current = _snapshot(NOW, ask=entry_ask, liquidity=Decimal("10000"))
    # No ask on the delayed snapshot - only `price` is ever historized for
    # later points in time (see MarketSnapshot's docstring) - this is the
    # realistic shape of a later snapshot, not a special case.
    delayed = _snapshot(NOW + timedelta(seconds=30), price=later_price)

    result = compute_fill(
        _request(signal_price=Decimal("0.50"), order_notional=Decimal("100")),
        [current, delayed],
        DEFAULT_CONFIG,
        now=NOW + timedelta(seconds=30),
    )

    assert result.filled is True
    assert result.reason == FillReason.FILLED
    expected_penalty = DEFAULT_CONFIG.slippage_k * Decimal("100") / Decimal("10000")
    assert result.fill_price == later_price + expected_penalty
    # Not anywhere near what the entry-time ask would have produced.
    assert result.fill_price != entry_ask + expected_penalty


def test_fallback_path_fills_with_larger_penalty_when_no_delayed_snapshot() -> None:
    """No snapshot exists at/after the delay threshold - still fills, but
    with the extra no_delayed_snapshot_penalty added, and a different reason
    so the trade record shows it was a fallback fill.
    """
    ask = Decimal("0.50")
    liquidity = Decimal("10000")
    order_notional = Decimal("100")

    current = _snapshot(NOW, ask=ask, liquidity=liquidity)

    result = compute_fill(
        _request(signal_price=ask, order_notional=order_notional, detected_at=NOW),
        [current],
        DEFAULT_CONFIG,
        now=NOW,  # entry_delay_seconds=30 hasn't elapsed - nothing else to look up
    )

    base_penalty = DEFAULT_CONFIG.slippage_k * order_notional / liquidity
    expected_penalty = base_penalty + DEFAULT_CONFIG.no_delayed_snapshot_penalty

    assert result.filled is True
    assert result.reason == FillReason.FALLBACK_FILLED
    assert result.slippage_paid == expected_penalty
    assert result.fill_price == ask + expected_penalty
    # Larger than what the same order would have paid with a real delayed snapshot.
    assert result.slippage_paid > base_penalty


def test_zero_liquidity_returns_no_liquidity() -> None:
    current = _snapshot(NOW, ask=Decimal("0.50"), liquidity=Decimal("0"))

    result = compute_fill(
        _request(order_notional=Decimal("100")), [current], DEFAULT_CONFIG, now=NOW
    )

    assert result.filled is False
    assert result.fill_price is None
    assert result.slippage_paid is None
    assert result.reason == FillReason.NO_LIQUIDITY


def test_near_zero_liquidity_returns_no_liquidity() -> None:
    current = _snapshot(NOW, ask=Decimal("0.50"), liquidity=Decimal("0.5"))

    result = compute_fill(
        _request(order_notional=Decimal("100")), [current], DEFAULT_CONFIG, now=NOW
    )

    assert result.filled is False
    assert result.reason == FillReason.NO_LIQUIDITY


def test_missing_liquidity_returns_no_liquidity() -> None:
    current = _snapshot(NOW, ask=Decimal("0.50"), liquidity=None)

    result = compute_fill(
        _request(order_notional=Decimal("100")), [current], DEFAULT_CONFIG, now=NOW
    )

    assert result.filled is False
    assert result.reason == FillReason.NO_LIQUIDITY


def test_drift_exactly_at_limit_still_fills() -> None:
    """ "Beyond" the limit rejects - exactly at it does not."""
    signal_price = Decimal("1.00")
    at_limit_ask = signal_price * (1 + DEFAULT_CONFIG.max_entry_price_drift)  # exactly 1.15

    current = _snapshot(NOW, ask=at_limit_ask, liquidity=Decimal("1000000"))

    result = compute_fill(
        _request(signal_price=signal_price, order_notional=Decimal("10")),
        [current],
        DEFAULT_CONFIG,
        now=NOW,
    )

    assert result.filled is True
    assert result.reason == FillReason.FALLBACK_FILLED


def test_drift_one_tick_beyond_limit_misses() -> None:
    signal_price = Decimal("1.00")
    beyond_limit_ask = signal_price * (1 + DEFAULT_CONFIG.max_entry_price_drift) + Decimal("0.0001")

    current = _snapshot(NOW, ask=beyond_limit_ask, liquidity=Decimal("1000000"))

    result = compute_fill(
        _request(signal_price=signal_price, order_notional=Decimal("10")),
        [current],
        DEFAULT_CONFIG,
        now=NOW,
    )

    assert result.filled is False
    assert result.reason == FillReason.MISSED_DRIFT

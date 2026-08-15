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

from app.paper.fills import (
    BookLevel,
    FillConfig,
    FillMethod,
    FillReason,
    FillRequest,
    MarketSnapshot,
    compute_fill,
    walk_the_book,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

DEFAULT_CONFIG = FillConfig(
    entry_delay_seconds=30,
    slippage_k=Decimal("0.5"),
    slippage_max=Decimal("0.15"),
    no_delayed_snapshot_penalty=Decimal("0.05"),
    max_entry_price_drift=Decimal("0.15"),
    max_book_depth_fraction=Decimal("0.20"),
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


# --- walk_the_book(): the order-book-walking VWAP model ---


def test_walk_the_book_single_level_matches_hand_computation() -> None:
    """Order fits entirely within the top level - avg price is just that
    level's price, no averaging needed.
    """
    book = [BookLevel(price=Decimal("0.60"), size=Decimal("100"))]

    avg = walk_the_book(book, size_usd=Decimal("30"), max_depth_fraction=Decimal("1.0"))

    assert avg == Decimal("0.60")


def test_walk_the_book_spans_two_levels_matches_hand_computation() -> None:
    """$9 against a $6 top level (10 shares @ 0.60) then spilling into a
    second level (0.61) - hand-computed size-weighted average.
    """
    book = [
        BookLevel(price=Decimal("0.60"), size=Decimal("10")),  # $6.00 of depth
        BookLevel(price=Decimal("0.61"), size=Decimal("100")),  # plenty more
    ]
    size_usd = Decimal("9")

    avg = walk_the_book(book, size_usd, max_depth_fraction=Decimal("1.0"))

    # Level 1: all 10 shares for $6.00. Remaining $3.00 buys 3/0.61 shares
    # of level 2. Hand-computed size-weighted average:
    remaining_shares = Decimal("3") / Decimal("0.61")
    expected_shares = Decimal("10") + remaining_shares
    expected_avg = Decimal("9") / expected_shares
    assert avg == expected_avg
    # Sanity: between the two level prices, closer to the thin top level's
    # price than a naive 50/50 average would suggest.
    assert Decimal("0.60") < avg < Decimal("0.61")


def test_walk_the_book_order_larger_than_book_returns_none() -> None:
    """The book's total depth genuinely can't fill the order - NO_LIQUIDITY,
    not a partial-fill fiction.
    """
    book = [BookLevel(price=Decimal("0.60"), size=Decimal("10"))]  # $6.00 total

    avg = walk_the_book(book, size_usd=Decimal("100"), max_depth_fraction=Decimal("1.0"))

    assert avg is None


def test_walk_the_book_empty_book_returns_none() -> None:
    assert walk_the_book([], size_usd=Decimal("10"), max_depth_fraction=Decimal("1.0")) is None


def test_walk_the_book_depth_cap_limits_order_size() -> None:
    """Plenty of raw depth to fill the order, but it exceeds
    max_depth_fraction of that depth - rejected anyway, because consuming
    that much of the visible book would move the market past what the
    levels can honestly describe.
    """
    book = [BookLevel(price=Decimal("0.60"), size=Decimal("1000"))]  # $600 of depth
    size_usd = Decimal("500")  # comfortably fillable in raw terms...

    capped = walk_the_book(
        book, size_usd, max_depth_fraction=Decimal("0.20")
    )  # ...but > 20% of $600
    uncapped = walk_the_book(book, size_usd, max_depth_fraction=Decimal("1.0"))

    assert capped is None
    assert uncapped == Decimal("0.60")


def test_walk_the_book_exactly_at_depth_cap_fills() -> None:
    """The boundary itself: exactly max_depth_fraction of visible depth
    still fills - "beyond" is what's rejected, not "at."
    """
    book = [BookLevel(price=Decimal("0.50"), size=Decimal("100"))]  # $50 of depth
    size_usd = Decimal("10")  # exactly 20% of $50

    avg = walk_the_book(book, size_usd, max_depth_fraction=Decimal("0.20"))

    assert avg == Decimal("0.50")


def test_walk_the_book_zero_or_negative_size_returns_none() -> None:
    book = [BookLevel(price=Decimal("0.50"), size=Decimal("100"))]
    assert walk_the_book(book, Decimal("0"), Decimal("1.0")) is None
    assert walk_the_book(book, Decimal("-5"), Decimal("1.0")) is None


# --- compute_fill() with a real book: BOOK_WALK path ---


def test_compute_fill_uses_book_walk_price_when_book_available() -> None:
    """A book is passed and can fill the order - fill_price comes from
    walk_the_book(), not the ask+slippage-constant model, and fill_method
    records that it was a real book walk.
    """
    ask = Decimal("0.60")
    current = _snapshot(NOW, ask=ask, liquidity=Decimal("1000000"))
    book = [
        BookLevel(price=Decimal("0.60"), size=Decimal("10")),
        BookLevel(price=Decimal("0.61"), size=Decimal("100")),
    ]
    order_notional = Decimal("9")

    result = compute_fill(
        _request(signal_price=ask, order_notional=order_notional),
        [current],
        DEFAULT_CONFIG,
        now=NOW,
        book=book,
    )

    expected = walk_the_book(book, order_notional, DEFAULT_CONFIG.max_book_depth_fraction)
    assert result.filled is True
    assert result.fill_method == FillMethod.BOOK_WALK
    assert result.fill_price == expected
    # Real slippage_paid is the walked price above the reference, not the
    # flat/size-scaled constant the estimated model would have produced.
    assert result.slippage_paid == expected - ask


def test_compute_fill_book_too_thin_rejects_not_estimates() -> None:
    """A book IS available but can't honestly fill the order (exceeds
    depth or genuinely too thin) - rejected as NO_LIQUIDITY. Must NOT
    silently fall back to the estimated model - that would be exactly the
    dishonesty this feature exists to remove.
    """
    ask = Decimal("0.60")
    current = _snapshot(NOW, ask=ask, liquidity=Decimal("1000000"))
    thin_book = [BookLevel(price=Decimal("0.60"), size=Decimal("1"))]  # $0.60 of depth

    result = compute_fill(
        _request(signal_price=ask, order_notional=Decimal("1000")),
        [current],
        DEFAULT_CONFIG,
        now=NOW,
        book=thin_book,
    )

    assert result.filled is False
    assert result.fill_price is None
    assert result.reason == FillReason.NO_LIQUIDITY
    assert result.fill_method is None


def test_compute_fill_drift_rejection_unaffected_by_book() -> None:
    """Drift rejection still happens before any book is even considered -
    a book can't rescue a fill whose reference price already moved too far.
    """
    signal_price = Decimal("0.50")
    drifted_ask = Decimal("0.70")  # 40% move, config allows only 15%
    current = _snapshot(NOW, ask=drifted_ask, liquidity=Decimal("10000"))
    book = [BookLevel(price=drifted_ask, size=Decimal("1000"))]

    result = compute_fill(
        _request(signal_price=signal_price, order_notional=Decimal("100")),
        [current],
        DEFAULT_CONFIG,
        now=NOW,
        book=book,
    )

    assert result.filled is False
    assert result.reason == FillReason.MISSED_DRIFT


# --- fallback path (no book at all): fill_method tagging ---


def test_fallback_path_sets_fill_method_estimated() -> None:
    """No book snapshot exists for this market at fill time (book=None,
    the default) - falls back to the old ask+slippage model, tagged
    FillMethod.ESTIMATED so it's always distinguishable from a real fill.
    """
    ask = Decimal("0.50")
    current = _snapshot(NOW, ask=ask, liquidity=Decimal("10000"))
    delayed = _snapshot(NOW + timedelta(seconds=30), ask=ask)

    result = compute_fill(
        _request(signal_price=ask, order_notional=Decimal("100")),
        [current, delayed],
        DEFAULT_CONFIG,
        now=NOW + timedelta(seconds=30),
    )

    assert result.filled is True
    assert result.fill_method == FillMethod.ESTIMATED


def test_missed_and_no_liquidity_results_carry_no_fill_method() -> None:
    """A trade that never actually filled has no fill_method, real or
    estimated - there's nothing to label.
    """
    current = _snapshot(NOW, ask=Decimal("0.50"), liquidity=Decimal("0"))

    result = compute_fill(
        _request(order_notional=Decimal("100")), [current], DEFAULT_CONFIG, now=NOW
    )

    assert result.filled is False
    assert result.fill_method is None

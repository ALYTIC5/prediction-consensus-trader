"""Unit tests for app/consensus/engine.py - pure, no DB, no network."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.consensus.engine import (
    CandidateGroup,
    ConsensusConfig,
    ContributorEvent,
    MarketState,
    Rejection,
    RejectionReason,
    SignalDraft,
    _average_entry_price,
    _combined_entry_value,
    _total_acted_size,
    _weighted_score,
    evaluate_group,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

DEFAULT_CONFIG = ConsensusConfig(
    consensus_freshness_hours=48,
    consensus_include_increases=True,
    consensus_min_traders=3,
    consensus_min_weighted_score=Decimal("1.0"),
    consensus_min_combined_value_usd=Decimal("500"),
    signal_min_liquidity_usd=Decimal("5000"),
    signal_min_volume_24h_usd=Decimal("1000"),
    signal_price_min=Decimal("0.05"),
    signal_price_max=Decimal("0.95"),
    signal_max_spread=Decimal("0.05"),
    signal_min_hours_to_end=12,
)

DEFAULT_MARKET = MarketState(
    exists=True,
    closed=False,
    accepting_orders=True,
    end_date=NOW + timedelta(hours=100),
    liquidity=Decimal("10000"),
    volume_24h=Decimal("5000"),
    spread=Decimal("0.02"),
    current_price=Decimal("0.50"),
)


def _event(
    address: str,
    weight: Decimal = Decimal("0.5"),
    acted_size: Decimal = Decimal("1000"),
    entry_price: Decimal = Decimal("1.0"),
    event_type: str = "OPENED",
    username: str | None = None,
    detected_at: datetime = NOW,
) -> ContributorEvent:
    return ContributorEvent(
        address=address,
        username=username,
        weight=weight,
        event_type=event_type,
        acted_size=acted_size,
        entry_price=entry_price,
        detected_at=detected_at,
    )


def _group(
    events: list[ContributorEvent],
    condition_id: str = "0xcond1",
    asset: str = "asset1",
    outcome: str = "Yes",
    title: str = "Will this test pass?",
    event_slug: str = "will-this-test-pass",
) -> CandidateGroup:
    return CandidateGroup(
        condition_id=condition_id,
        asset=asset,
        outcome=outcome,
        title=title,
        event_slug=event_slug,
        events=events,
    )


def _happy_group() -> CandidateGroup:
    """3 distinct traders, comfortably clearing breadth/quality/conviction,
    so tests targeting an earlier (market-level) filter only ever fail on
    the one thing being tested.
    """
    return _group(
        [
            _event(
                "0xA", weight=Decimal("0.6"), acted_size=Decimal("1000"), entry_price=Decimal("1.0")
            ),
            _event(
                "0xB", weight=Decimal("0.6"), acted_size=Decimal("1000"), entry_price=Decimal("1.0")
            ),
            _event(
                "0xC", weight=Decimal("0.6"), acted_size=Decimal("1000"), entry_price=Decimal("1.0")
            ),
        ]
    )


# --- Happy path ---


def test_happy_path_three_distinct_traders_yields_signal_draft() -> None:
    events = [
        _event(
            "0xA", weight=Decimal("0.5"), acted_size=Decimal("1000"), entry_price=Decimal("0.40")
        ),
        _event(
            "0xB", weight=Decimal("0.3"), acted_size=Decimal("1000"), entry_price=Decimal("0.50")
        ),
        _event(
            "0xC", weight=Decimal("0.4"), acted_size=Decimal("1000"), entry_price=Decimal("0.60")
        ),
    ]

    result = evaluate_group(_group(events), DEFAULT_MARKET, DEFAULT_CONFIG, NOW)

    assert isinstance(result, SignalDraft)
    assert result.distinct_traders == 3
    assert result.weighted_score == Decimal("1.2")
    assert result.combined_entry_value == Decimal("1500")  # 400 + 500 + 600
    assert result.average_entry_price == Decimal("0.5")  # 1500 / 3000
    assert result.latest_market_price == Decimal("0.50")
    assert len(result.contributors) == 3


# --- Filter a: market_known ---


def test_market_known_rejects_when_market_does_not_exist() -> None:
    unknown_market = MarketState(
        exists=False,
        closed=False,
        accepting_orders=False,
        end_date=None,
        liquidity=None,
        volume_24h=None,
        spread=None,
        current_price=None,
    )

    result = evaluate_group(_happy_group(), unknown_market, DEFAULT_CONFIG, NOW)

    assert isinstance(result, Rejection)
    assert result.reason == RejectionReason.MARKET_KNOWN
    # Metrics are still computed from the group alone - rejections are debuggable too.
    assert result.distinct_traders == 3
    assert result.weighted_score == Decimal("1.8")
    assert result.latest_market_price is None


# --- Filter b: market_open ---


@pytest.mark.parametrize(
    ("closed", "accepting_orders", "end_date_offset_hours", "expect_pass"),
    [
        (True, True, 100, False),  # closed market
        (False, False, 100, False),  # not accepting orders
        (False, True, 11, False),  # end_date just under the 12h minimum
        (False, True, 12, True),  # end_date exactly at the boundary
        (False, True, 100, True),  # comfortably open
    ],
)
def test_market_open_filter(
    closed: bool, accepting_orders: bool, end_date_offset_hours: int, expect_pass: bool
) -> None:
    market = replace(
        DEFAULT_MARKET,
        closed=closed,
        accepting_orders=accepting_orders,
        end_date=NOW + timedelta(hours=end_date_offset_hours),
    )

    result = evaluate_group(_happy_group(), market, DEFAULT_CONFIG, NOW)

    if expect_pass:
        assert isinstance(result, SignalDraft)
    else:
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.MARKET_OPEN


# --- Filter c: liquidity ---


@pytest.mark.parametrize(
    ("liquidity", "volume_24h", "expect_pass"),
    [
        (Decimal("4999.999999"), Decimal("1000"), False),  # liquidity just below
        (Decimal("5000"), Decimal("999.999999"), False),  # volume just below
        (Decimal("5000"), Decimal("1000"), True),  # both exactly at threshold
    ],
)
def test_liquidity_filter(liquidity: Decimal, volume_24h: Decimal, expect_pass: bool) -> None:
    market = replace(DEFAULT_MARKET, liquidity=liquidity, volume_24h=volume_24h)

    result = evaluate_group(_happy_group(), market, DEFAULT_CONFIG, NOW)

    if expect_pass:
        assert isinstance(result, SignalDraft)
    else:
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.LIQUIDITY


# --- Filter d: price_band ---


@pytest.mark.parametrize(
    ("price", "expect_pass"),
    [
        (Decimal("0.04"), False),
        (Decimal("0.05"), True),
        (Decimal("0.95"), True),
        (Decimal("0.96"), False),
    ],
)
def test_price_band_filter(price: Decimal, expect_pass: bool) -> None:
    market = replace(DEFAULT_MARKET, current_price=price)

    result = evaluate_group(_happy_group(), market, DEFAULT_CONFIG, NOW)

    if expect_pass:
        assert isinstance(result, SignalDraft)
    else:
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.PRICE_BAND


# --- Filter e: spread ---


@pytest.mark.parametrize(
    ("spread", "expect_pass"),
    [
        (Decimal("0.0500001"), False),
        (Decimal("0.05"), True),
    ],
)
def test_spread_filter(spread: Decimal, expect_pass: bool) -> None:
    market = replace(DEFAULT_MARKET, spread=spread)

    result = evaluate_group(_happy_group(), market, DEFAULT_CONFIG, NOW)

    if expect_pass:
        assert isinstance(result, SignalDraft)
    else:
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.SPREAD


# --- Filter f: breadth ---


def test_breadth_filter_two_traders_rejects() -> None:
    events = [
        _event(
            "0xA", weight=Decimal("0.6"), acted_size=Decimal("1000"), entry_price=Decimal("1.0")
        ),
        _event(
            "0xB", weight=Decimal("0.6"), acted_size=Decimal("1000"), entry_price=Decimal("1.0")
        ),
    ]

    result = evaluate_group(_group(events), DEFAULT_MARKET, DEFAULT_CONFIG, NOW)

    assert isinstance(result, Rejection)
    assert result.reason == RejectionReason.BREADTH


def test_breadth_filter_three_traders_passes() -> None:
    result = evaluate_group(_happy_group(), DEFAULT_MARKET, DEFAULT_CONFIG, NOW)
    assert isinstance(result, SignalDraft)


# --- Filter g: quality ---


@pytest.mark.parametrize(
    ("third_weight", "expect_pass"),
    [
        (Decimal("0.19"), False),  # 0.5 + 0.3 + 0.19 = 0.99
        (Decimal("0.20"), True),  # 0.5 + 0.3 + 0.20 = 1.00
    ],
)
def test_quality_filter(third_weight: Decimal, expect_pass: bool) -> None:
    events = [
        _event(
            "0xA", weight=Decimal("0.5"), acted_size=Decimal("1000"), entry_price=Decimal("1.0")
        ),
        _event(
            "0xB", weight=Decimal("0.3"), acted_size=Decimal("1000"), entry_price=Decimal("1.0")
        ),
        _event("0xC", weight=third_weight, acted_size=Decimal("1000"), entry_price=Decimal("1.0")),
    ]

    result = evaluate_group(_group(events), DEFAULT_MARKET, DEFAULT_CONFIG, NOW)

    if expect_pass:
        assert isinstance(result, SignalDraft)
    else:
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.QUALITY


# --- Filter h: conviction ---


@pytest.mark.parametrize(
    ("third_size", "expect_pass"),
    [
        (Decimal("99"), False),  # 200 + 200 + 99 = 499
        (Decimal("100"), True),  # 200 + 200 + 100 = 500
    ],
)
def test_conviction_filter(third_size: Decimal, expect_pass: bool) -> None:
    events = [
        _event("0xA", weight=Decimal("0.4"), acted_size=Decimal("200"), entry_price=Decimal("1.0")),
        _event("0xB", weight=Decimal("0.4"), acted_size=Decimal("200"), entry_price=Decimal("1.0")),
        _event("0xC", weight=Decimal("0.4"), acted_size=third_size, entry_price=Decimal("1.0")),
    ]

    result = evaluate_group(_group(events), DEFAULT_MARKET, DEFAULT_CONFIG, NOW)

    if expect_pass:
        assert isinstance(result, SignalDraft)
    else:
        assert isinstance(result, Rejection)
        assert result.reason == RejectionReason.CONVICTION


# --- Opened-then-increased is one trader, one weight, both events' value ---


def test_open_then_increase_counts_as_one_trader_at_max_weight() -> None:
    events = [
        _event(
            "0xA",
            weight=Decimal("0.6"),
            event_type="OPENED",
            acted_size=Decimal("500"),
            entry_price=Decimal("0.40"),
        ),
        _event(
            "0xA",
            weight=Decimal("0.8"),  # rescored higher by the time it increased
            event_type="INCREASED",
            acted_size=Decimal("300"),
            entry_price=Decimal("0.45"),
        ),
        _event(
            "0xB", weight=Decimal("0.5"), acted_size=Decimal("1000"), entry_price=Decimal("0.50")
        ),
        _event(
            "0xC", weight=Decimal("0.5"), acted_size=Decimal("1000"), entry_price=Decimal("0.50")
        ),
    ]

    result = evaluate_group(_group(events), DEFAULT_MARKET, DEFAULT_CONFIG, NOW)

    assert isinstance(result, SignalDraft)
    assert result.distinct_traders == 3  # 0xA counted once, not twice
    assert result.weighted_score == Decimal("0.8") + Decimal("0.5") + Decimal(
        "0.5"
    )  # max(0.6, 0.8)
    # Both of 0xA's events still contribute to combined_entry_value.
    assert result.combined_entry_value == Decimal("200") + Decimal("135") + Decimal(
        "500"
    ) + Decimal("500")
    assert len(result.contributors) == 4


# --- Aggregate helpers, targeted directly ---


def test_weighted_score_dedupes_by_wallet_using_max_weight() -> None:
    events = [
        _event("0xA", weight=Decimal("0.6")),
        _event("0xA", weight=Decimal("0.8")),
        _event("0xB", weight=Decimal("0.3")),
    ]

    distinct_traders, weighted_score = _weighted_score(events)

    assert distinct_traders == 2
    assert weighted_score == Decimal("0.8") + Decimal("0.3")


def test_combined_entry_value_sums_every_event_not_deduped() -> None:
    events = [
        _event("0xA", acted_size=Decimal("10"), entry_price=Decimal("0.5")),
        _event("0xA", acted_size=Decimal("10"), entry_price=Decimal("0.5")),
    ]

    assert _combined_entry_value(events) == Decimal("10")


def test_total_acted_size_sums_every_event() -> None:
    events = [
        _event("0xA", acted_size=Decimal("10")),
        _event("0xB", acted_size=Decimal("20")),
    ]

    assert _total_acted_size(events) == Decimal("30")


def test_average_entry_price_is_size_weighted() -> None:
    # 1 share at 0.10 and 999 shares at 0.90 should land near 0.90, not
    # halfway between the two prices.
    combined_value = Decimal("1") * Decimal("0.10") + Decimal("999") * Decimal("0.90")
    total_size = Decimal("1000")

    average = _average_entry_price(combined_value, total_size)

    assert average > Decimal("0.89")


def test_average_entry_price_zero_size_does_not_divide_by_zero() -> None:
    assert _average_entry_price(Decimal("0"), Decimal("0")) == Decimal("0")

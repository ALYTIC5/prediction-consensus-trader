"""Unit tests for app/collectors/resolutions.py's classify_resolution -
pure, no DB, no network. Same convention as tests/test_categories.py.
"""

from datetime import UTC, datetime

from app.collectors.resolutions import classify_resolution
from app.collectors.schemas import GammaMarketResolution


def _market(
    closed: bool,
    uma_resolution_status: str | None = None,
    outcome_prices: str | None = None,
    clob_token_ids: str | None = None,
    closed_time: str | None = None,
) -> GammaMarketResolution:
    return GammaMarketResolution(
        id="1",
        question="Q?",
        conditionId="0xabc",
        slug="q",
        active=True,
        closed=closed,
        umaResolutionStatus=uma_resolution_status,
        outcomePrices=outcome_prices,
        clobTokenIds=clob_token_ids,
        closedTime=closed_time,
    )


def test_still_open_market_is_untouched():
    market = _market(closed=False)
    result = classify_resolution(market)
    assert result.still_open is True
    assert result.ambiguous is False


def test_resolved_market_updates_correctly():
    market = _market(
        closed=True,
        uma_resolution_status="resolved",
        outcome_prices='["0", "1"]',
        clob_token_ids='["tokA", "tokB"]',
        closed_time="2026-07-30T20:55:30Z",
    )
    result = classify_resolution(market)
    assert result.still_open is False
    assert result.ambiguous is False
    assert result.winning_asset == "tokB"
    assert result.winning_outcome_index == 1
    assert result.outcome_prices == ["0", "1"]
    assert result.resolved_at == datetime(2026, 7, 30, 20, 55, 30, tzinfo=UTC)


def test_closed_but_not_yet_uma_resolved_is_ambiguous_and_left_open():
    """A market can be closed=true while UMA's resolution is still
    "proposed"/disputed/etc - not a confirmed settlement, so it must be
    logged and left alone, never guessed.
    """
    market = _market(
        closed=True,
        uma_resolution_status="proposed",
        outcome_prices='["0", "1"]',
        clob_token_ids='["tokA", "tokB"]',
    )
    result = classify_resolution(market)
    assert result.ambiguous is True
    assert result.still_open is False
    assert result.uma_resolution_status == "proposed"
    assert result.winning_asset is None


def test_closed_and_resolved_but_unparseable_prices_is_ambiguous():
    market = _market(
        closed=True,
        uma_resolution_status="resolved",
        outcome_prices=None,
        clob_token_ids='["tokA", "tokB"]',
    )
    result = classify_resolution(market)
    assert result.ambiguous is True


def test_closed_and_resolved_but_mismatched_vector_lengths_is_ambiguous():
    market = _market(
        closed=True,
        uma_resolution_status="resolved",
        outcome_prices='["0", "1"]',
        clob_token_ids='["tokA"]',
    )
    result = classify_resolution(market)
    assert result.ambiguous is True


def test_genuine_no_winner_outcome_is_resolved_not_ambiguous():
    """A documented UMA rule (e.g. "if the game is canceled, resolve
    50-50") is a real, confirmed resolution with no single winner - not an
    ambiguous one. winning_asset/index are None but outcome_prices still
    carries the true settlement value.
    """
    market = _market(
        closed=True,
        uma_resolution_status="resolved",
        outcome_prices='["0.5", "0.5"]',
        clob_token_ids='["tokA", "tokB"]',
    )
    result = classify_resolution(market)
    assert result.ambiguous is False
    assert result.still_open is False
    assert result.winning_asset is None
    assert result.winning_outcome_index is None
    assert result.outcome_prices == ["0.5", "0.5"]

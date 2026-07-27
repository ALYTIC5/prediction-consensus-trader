"""PolymarketClient tests against httpx.MockTransport - real request/response
plumbing, canned bodies, zero network.
"""

import json
from decimal import Decimal

import httpx
import pytest

from app.collectors.http import PolymarketHTTP
from app.collectors.polymarket import PolymarketClient


def _client_for(handler) -> tuple[PolymarketClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = PolymarketClient(http=PolymarketHTTP(client=http_client))
    return client, http_client


def _leaderboard_entry(rank: int, address: str) -> dict:
    return {
        "rank": str(rank),
        "proxyWallet": address,
        "userName": f"user{rank}",
        "vol": "1000.5",
        "pnl": "50.25",
        "xUsername": None,
        "verifiedBadge": False,
    }


@pytest.mark.asyncio
async def test_leaderboard_pagination_and_field_coercion() -> None:
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        limit = int(request.url.params["limit"])
        offset = int(request.url.params["offset"])
        body = [_leaderboard_entry(offset + i + 1, f"0xABC{offset + i:04d}") for i in range(limit)]
        return httpx.Response(200, json=body)

    client, http_client = _client_for(handler)
    try:
        results = await client.get_top_traders("MONTH", top_n=75)
    finally:
        await http_client.aclose()

    assert len(requests_seen) == 2
    assert requests_seen[0].url.params["limit"] == "50"
    assert requests_seen[0].url.params["offset"] == "0"
    assert requests_seen[1].url.params["limit"] == "25"
    assert requests_seen[1].url.params["offset"] == "50"

    assert len(results) == 75
    assert results[0].rank == 1
    assert isinstance(results[0].rank, int)
    assert results[0].proxy_wallet == "0xabc0000"  # lowercased from 0xABC0000


@pytest.mark.asyncio
async def test_positions_parse_and_ignore_unknown_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "proxyWallet": "0xDEF",
                    "asset": "12345",
                    "conditionId": "0xCOND",
                    "size": "10.5",
                    "avgPrice": "0.5",
                    "initialValue": "5",
                    "currentValue": "6",
                    "cashPnl": "1",
                    "percentPnl": "20",
                    "curPrice": "0.6",
                    "redeemable": False,
                    "title": "Some Market",
                    "eventSlug": "some-event",
                    "outcome": "Yes",
                    "outcomeIndex": 0,
                    "negativeRisk": False,
                    "unknownField": "surprise-from-upstream",
                }
            ],
        )

    client, http_client = _client_for(handler)
    try:
        results = await client.get_positions("0xDEF")
    finally:
        await http_client.aclose()

    assert len(results) == 1
    position = results[0]
    assert position.condition_id == "0xcond"  # lowercased
    assert position.size == Decimal("10.5")
    assert position.outcome == "Yes"


@pytest.mark.asyncio
async def test_retries_once_on_500_then_succeeds() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500, json={"error": "internal"})
        return httpx.Response(200, json=[_leaderboard_entry(1, "0xAAA")])

    client, http_client = _client_for(handler)
    try:
        results = await client.get_top_traders("DAY", top_n=1)
    finally:
        await http_client.aclose()

    assert call_count == 2
    assert len(results) == 1
    assert results[0].proxy_wallet == "0xaaa"


@pytest.mark.asyncio
async def test_markets_chunking_and_json_string_parsing() -> None:
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        ids = request.url.params.get_list("condition_ids")
        body = [
            {
                "id": str(i),
                "question": f"Q{i}?",
                "conditionId": cid,
                "slug": f"slug-{i}",
                "active": True,
                "closed": False,
                "outcomes": json.dumps(["Yes", "No"]),
                "outcomePrices": json.dumps(["0.4", "0.6"]),
                "clobTokenIds": json.dumps(["111", "222"]),
            }
            for i, cid in enumerate(ids)
        ]
        return httpx.Response(200, json=body)

    condition_ids = [f"0xCOND{i}" for i in range(25)]
    client, http_client = _client_for(handler)
    try:
        markets = await client.get_markets_by_condition_ids(condition_ids)
    finally:
        await http_client.aclose()

    assert len(requests_seen) == 2
    assert len(requests_seen[0].url.params.get_list("condition_ids")) == 20
    assert len(requests_seen[1].url.params.get_list("condition_ids")) == 5

    assert len(markets) == 25
    market = markets[0]
    assert market.parsed_outcomes == ["Yes", "No"]
    assert market.parsed_outcome_prices == [Decimal("0.4"), Decimal("0.6")]
    assert all(isinstance(price, Decimal) for price in market.parsed_outcome_prices)
    assert market.parsed_clob_token_ids == ["111", "222"]

"""Typed client for the Polymarket endpoints the collectors need."""

from collections.abc import Sequence

import httpx

from app.collectors.http import PolymarketHTTP
from app.collectors.schemas import GammaMarket, LeaderboardEntry, OrderBookResponse, PositionEntry
from app.config.settings import get_settings

_VALID_TIME_PERIODS = {"DAY", "WEEK", "MONTH", "ALL"}


class PolymarketClient:
    """Pages through leaderboard/positions/markets, honouring documented limits."""

    _LEADERBOARD_PAGE_SIZE = 50
    _LEADERBOARD_MAX_OFFSET = 1000
    _POSITIONS_PAGE_SIZE = 500
    _POSITIONS_MAX_OFFSET = 10000
    _MARKETS_CHUNK_SIZE = 20

    def __init__(self, http: PolymarketHTTP | None = None) -> None:
        self._http = http or PolymarketHTTP()
        self._settings = get_settings()

    async def aclose(self) -> None:
        """Close the underlying HTTP transport."""
        await self._http.aclose()

    async def get_top_traders(
        self,
        time_period: str,
        category: str | None = None,
        order_by: str = "PNL",
        top_n: int | None = None,
    ) -> list[LeaderboardEntry]:
        """Page /v1/leaderboard 50 at a time, up to top_n and the documented max offset."""
        if time_period not in _VALID_TIME_PERIODS:
            raise ValueError(
                f"invalid time_period {time_period!r}, must be one of {sorted(_VALID_TIME_PERIODS)}"
            )
        category = category or self._settings.leaderboard_category
        top_n = self._settings.leaderboard_top_n if top_n is None else top_n

        results: list[LeaderboardEntry] = []
        offset = 0
        while offset <= self._LEADERBOARD_MAX_OFFSET and len(results) < top_n:
            limit = min(self._LEADERBOARD_PAGE_SIZE, top_n - len(results))
            data = await self._http.get_json(
                f"{self._settings.data_api_base}/v1/leaderboard",
                params={
                    "timePeriod": time_period,
                    "category": category,
                    "orderBy": order_by,
                    "limit": limit,
                    "offset": offset,
                },
            )
            page = [LeaderboardEntry.model_validate(row) for row in data]
            results.extend(page)
            if len(page) < limit:
                break
            offset += limit
        return results[:top_n]

    async def get_positions(self, address: str) -> list[PositionEntry]:
        """Page /positions 500 at a time until a short page comes back."""
        results: list[PositionEntry] = []
        offset = 0
        while offset <= self._POSITIONS_MAX_OFFSET:
            data = await self._http.get_json(
                f"{self._settings.data_api_base}/positions",
                params={
                    "user": address,
                    "sizeThreshold": str(self._settings.positions_size_threshold),
                    "limit": self._POSITIONS_PAGE_SIZE,
                    "offset": offset,
                },
            )
            page = [PositionEntry.model_validate(row) for row in data]
            results.extend(page)
            if len(page) < self._POSITIONS_PAGE_SIZE:
                break
            offset += self._POSITIONS_PAGE_SIZE
        return results

    async def get_book(self, token_id: str) -> OrderBookResponse | None:
        """Fetch one outcome token's order book. None means no orderbook
        exists for this token (404 - an illiquid/inactive market, per
        docs/API_REFERENCE.md) - an expected, non-retryable outcome, never
        raised as an error.
        """
        try:
            data = await self._http.get_json(
                f"{self._settings.clob_api_base}/book", params={"token_id": token_id}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return OrderBookResponse.model_validate(data)

    async def get_markets_by_condition_ids(self, condition_ids: Sequence[str]) -> list[GammaMarket]:
        """Fetch markets by condition ID, 20 per request, as repeated query params."""
        markets: list[GammaMarket] = []
        for i in range(0, len(condition_ids), self._MARKETS_CHUNK_SIZE):
            chunk = condition_ids[i : i + self._MARKETS_CHUNK_SIZE]
            data = await self._http.get_json(
                f"{self._settings.gamma_api_base}/markets",
                # include_tag=true is required to get the "tags" array back
                # at all - verified live, not guessed (see
                # app/collectors/schemas.py's GammaTag docstring). Documented
                # as a /markets request param in docs/API_REFERENCE.md.
                params=[("condition_ids", cid) for cid in chunk] + [("include_tag", "true")],
            )
            markets.extend(GammaMarket.model_validate(row) for row in data)
        return markets

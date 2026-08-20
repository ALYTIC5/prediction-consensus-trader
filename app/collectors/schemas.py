"""Response schemas for Polymarket's public read APIs.

Field names, types, and quirks here are transcribed from
docs/API_REFERENCE.md, not guessed - see that file for the verified source
(endpoint, param, and response shape) behind every field.
"""

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PolymarketResponseModel(BaseModel):
    """Base for all Polymarket API response models.

    extra="ignore": upstream can add new response fields at any time without
    that breaking us - we only care about the fields we've declared.
    populate_by_name=True: lets callers construct instances with either the
    Python (snake_case) field name or the API's camelCase alias.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class LeaderboardEntry(PolymarketResponseModel):
    """One row from GET /v1/leaderboard."""

    rank: int | None
    proxy_wallet: str = Field(alias="proxyWallet")
    user_name: str | None = Field(default=None, alias="userName")
    vol: Decimal
    pnl: Decimal
    x_username: str | None = Field(default=None, alias="xUsername")
    verified_badge: bool = Field(default=False, alias="verifiedBadge")

    @field_validator("rank", mode="before")
    @classmethod
    def _coerce_rank(cls, value: Any) -> int | None:
        """The API returns rank as a string; null/empty means unranked."""
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        return int(value)

    @field_validator("proxy_wallet", mode="after")
    @classmethod
    def _lowercase_proxy_wallet(cls, value: str) -> str:
        return value.lower()


class PositionEntry(PolymarketResponseModel):
    """One row from GET /positions."""

    proxy_wallet: str = Field(alias="proxyWallet")
    asset: str
    condition_id: str = Field(alias="conditionId")
    size: Decimal
    avg_price: Decimal = Field(alias="avgPrice")
    initial_value: Decimal = Field(alias="initialValue")
    current_value: Decimal = Field(alias="currentValue")
    cash_pnl: Decimal = Field(alias="cashPnl")
    percent_pnl: Decimal = Field(alias="percentPnl")
    cur_price: Decimal = Field(alias="curPrice")
    redeemable: bool
    title: str
    event_slug: str = Field(alias="eventSlug")
    outcome: str
    outcome_index: int = Field(alias="outcomeIndex")
    negative_risk: bool = Field(alias="negativeRisk")

    @field_validator("asset", "condition_id", mode="after")
    @classmethod
    def _lowercase_hex(cls, value: str) -> str:
        return value.lower()


class GammaEvent(PolymarketResponseModel):
    """Minimal shape of an entry in a Gamma Market's nested "events" list."""

    slug: str | None = None
    neg_risk: bool | None = Field(default=None, alias="negRisk")


class GammaTag(PolymarketResponseModel):
    """One entry in a Gamma Market's "tags" list.

    Only present in the response when the request includes
    include_tag=true (see app/collectors/polymarket.py's
    get_markets_by_condition_ids) - verified live against the API, not
    guessed: /markets with no params never returns tags at all.
    """

    id: str | None = None
    label: str | None = None
    slug: str | None = None
    force_hide: bool = Field(default=False, alias="forceHide")


class GammaMarket(PolymarketResponseModel):
    """One row from GET /markets (Gamma API).

    outcomes / outcome_prices / clob_token_ids are kept as the raw strings
    the API actually sends (see docs/API_REFERENCE.md quirk section) and
    parsed on demand via the parsed_* properties, rather than parsed eagerly
    as fields - a malformed raw string should never fail model validation,
    only fail (safely, to []) when someone asks for the parsed value.
    """

    id: str
    question: str
    condition_id: str = Field(alias="conditionId")
    slug: str
    end_date: str | None = Field(default=None, alias="endDate")
    active: bool
    closed: bool
    accepting_orders: bool = Field(default=False, alias="acceptingOrders")
    liquidity_num: Decimal | None = Field(default=None, alias="liquidityNum")
    volume_num: Decimal | None = Field(default=None, alias="volumeNum")
    volume_24hr: Decimal | None = Field(default=None, alias="volume24hr")
    best_bid: Decimal | None = Field(default=None, alias="bestBid")
    best_ask: Decimal | None = Field(default=None, alias="bestAsk")
    spread: Decimal | None = None
    last_trade_price: Decimal | None = Field(default=None, alias="lastTradePrice")
    order_price_min_tick_size: Decimal | None = Field(default=None, alias="orderPriceMinTickSize")
    events: list[GammaEvent] = Field(default_factory=list)
    tags: list[GammaTag] = Field(default_factory=list)

    outcomes: str | None = None
    outcome_prices: str | None = Field(default=None, alias="outcomePrices")
    clob_token_ids: str | None = Field(default=None, alias="clobTokenIds")

    @property
    def parsed_outcomes(self) -> list[str]:
        """outcomes as a list, e.g. ["Yes", "No"]. [] if missing/malformed."""
        return self._parse_json_list(self.outcomes)

    @property
    def parsed_clob_token_ids(self) -> list[str]:
        """clob_token_ids as a list of token-id strings. [] if missing/malformed."""
        return self._parse_json_list(self.clob_token_ids)

    @property
    def parsed_outcome_prices(self) -> list[Decimal]:
        """outcome_prices as Decimals. [] if missing/malformed - never raises."""
        try:
            raw = json.loads(self.outcome_prices) if self.outcome_prices else []
            if not isinstance(raw, list):
                return []
            return [Decimal(str(price)) for price in raw]
        except (json.JSONDecodeError, TypeError, ValueError, InvalidOperation):
            return []

    @property
    def neg_risk(self) -> bool | None:
        """Derived from the first nested event, not a top-level market field."""
        return self.events[0].neg_risk if self.events else None

    @property
    def event_slug(self) -> str | None:
        """Derived from the first nested event, not a top-level market field."""
        return self.events[0].slug if self.events else None

    @staticmethod
    def _parse_json_list(raw: str | None) -> list[str]:
        try:
            parsed = json.loads(raw) if raw else []
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []


class GammaMarketResolution(GammaMarket):
    """One market row from GET /markets/{id} (Gamma API, path-based single-
    resource lookup, id = Market.gamma_id) - verified live 2026-08-19 (see
    docs/API_REFERENCE.md). Same shape as GammaMarket plus the settlement
    fields only this route carries.

    This is the authoritative resolution source: the condition_ids/id-
    filtered bulk /markets query (get_markets_by_condition_ids) silently
    drops a market from its response once resolved - confirmed live to
    happen even for a single-value filter, not just multi-value ones (see
    docs/API_REFERENCE.md) - but this single-id path route does not.
    """

    closed_time: str | None = Field(default=None, alias="closedTime")
    uma_resolution_status: str | None = Field(default=None, alias="umaResolutionStatus")
    resolved_by: str | None = Field(default=None, alias="resolvedBy")
    automatically_resolved: bool | None = Field(default=None, alias="automaticallyResolved")


class OrderBookLevel(PolymarketResponseModel):
    """One price level from GET /book (CLOB API) - see docs/API_REFERENCE.md.

    price/size arrive as strings on the wire, never floats - Decimal here,
    same as every other money-shaped field in this project.
    """

    price: Decimal
    size: Decimal


class OrderBookResponse(PolymarketResponseModel):
    """GET /book's full response - verified against docs.polymarket.com
    2026-08-14 (see docs/API_REFERENCE.md). bids sorted price descending
    (best bid first), asks sorted price ascending (best ask first) - the
    API's own documented order, never re-sorted here so a caller walking
    the list front-to-back is walking price-priority order by construction.
    """

    market: str
    asset_id: str
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    min_order_size: Decimal | None = None
    tick_size: Decimal | None = None
    neg_risk: bool | None = None
    last_trade_price: Decimal | None = None


class ClobMarketState(PolymarketResponseModel):
    """One row from GET /markets/{condition_id} (CLOB API) - verified live
    2026-08-16 (see docs/API_REFERENCE.md). Fallback source for markets
    Gamma's condition_ids-filtered /markets silently drops once archived:
    CLOB keeps reporting active/closed/accepting_orders for a resolved
    market long after Gamma stops returning it. Field names arrive in
    snake_case already, unlike Gamma's camelCase - no aliases needed. Only
    the resolution-state fields are modeled; CLOB doesn't carry
    liquidity/volume/category the way Gamma does, so this is never used to
    update those.
    """

    condition_id: str
    active: bool
    closed: bool
    accepting_orders: bool = False


class FeeRateResponse(PolymarketResponseModel):
    """GET /fee-rate's response (CLOB API) - verified live 2026-08-16 (see
    docs/API_REFERENCE.md). base_fee is basis points as an integer, e.g. 700
    for crypto's documented 7% taker rate - divide by 10000 to get the
    fraction app/paper/fees.py's compute_taker_fee expects.
    """

    base_fee: int

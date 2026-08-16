# Polymarket API reference notes

Findings from verifying endpoints against docs.polymarket.com before writing
code against them (per CLAUDE.md working agreement). Update this file
whenever a new endpoint is verified or an assumption turns out wrong. If a
later prompt asserts something that contradicts what's recorded here, that
contradiction gets flagged, not silently resolved either way.

## Base URLs (from api-reference/introduction)

- **Gamma API** — `https://gamma-api.polymarket.com` — market/event discovery
  and metadata.
- **CLOB API** — `https://clob.polymarket.com` — live market state, order
  placement/management.
- **Data API** — `https://data-api.polymarket.com` — account/market activity
  after the fact (positions, trades, leaderboard).
- Also exist: Relayer API, Bridge API, WebSocket APIs (not yet needed).
- The introduction page doesn't spell out auth requirements or a shared
  pagination/envelope convention in one place — each endpoint's own page was
  checked individually for that instead of assuming a project-wide rule.

## Data API — `GET https://data-api.polymarket.com/v1/leaderboard`

Auth: **not required**.

| Parameter | Type | Allowed values | Default | Max |
|---|---|---|---|---|
| `category` | string | OVERALL, POLITICS, SPORTS, ESPORTS, CRYPTO, CULTURE, MENTIONS, WEATHER, ECONOMICS, TECH, FINANCE | OVERALL | — |
| `timePeriod` | string | DAY, WEEK, MONTH, ALL | DAY | — |
| `orderBy` | string | PNL, VOL | PNL | — |
| `limit` | integer | 1–50 | 25 | 50 |
| `offset` | integer | 0–1000 | 0 | 1000 |
| `user` | string (address) | 0x-prefixed, 40 hex chars | — | — |
| `userName` | string | any | — | — |

Response fields we care about: `rank` (string), `proxyWallet` (string,
address), `userName` (string), `vol` (number), `pnl` (number).
Also present: `profileImage`, `xUsername`, `verifiedBadge` (bool).

Quirk: `limit` maxes out at **50** — hard ceiling, not clamped-with-warning
like some other endpoints. Ranking the full trader population requires
paging via `offset` (itself capped at 1000, so only the top 1050 ranked
traders are reachable through this endpoint at all).

## Data API — `GET https://data-api.polymarket.com/positions`

Auth: **not required** (`security: []` in spec).

| Parameter | Type | Allowed values | Default | Max |
|---|---|---|---|---|
| `user` | string (Address) | 0x-prefixed 40 hex chars | **required, no default** | — |
| `market` | array (Hash64) | comma-separated condition IDs | — | — |
| `eventId` | array (integer) | comma-separated event IDs, ≥1 | — | — |
| `sizeThreshold` | number | ≥0 | 1 | — |
| `redeemable` | boolean | true/false | false | — |
| `mergeable` | boolean | true/false | false | — |
| `limit` | integer | 0–500 | 100 | 500 |
| `offset` | integer | 0–10000 | 0 | 10000 |
| `sortBy` | string | CURRENT, INITIAL, TOKENS, CASHPNL, PERCENTPNL, TITLE, RESOLVING, PRICE, AVGPRICE | TOKENS | — |
| `sortDirection` | string | ASC, DESC | DESC | — |
| `title` | string | any | — | 100 chars |

Response (200): array of Position objects. Fields we care about:
`proxyWallet`, `asset`, `conditionId`, `size`, `avgPrice`, `curPrice`,
`initialValue`, `currentValue`, `cashPnl`, `percentPnl`, `outcome`,
`outcomeIndex`, `redeemable`, `mergeable`, `title`, `slug`, `endDate`.

Quirk: `sizeThreshold` **defaults to 1**, not 0 — a naive call silently
drops any position sized below 1 share/contract unless the caller overrides
it explicitly. Error responses (400/401/500) return `{"error": ...}`.

## Gamma API — `GET https://gamma-api.polymarket.com/markets`

Auth: **not required**.

| Parameter | Type | Notes |
|---|---|---|
| `limit` | integer, ≥0 | results per page |
| `offset` | integer, ≥0 | pagination offset |
| `order` | string | comma-separated field list |
| `ascending` | boolean | sort direction |
| `id` | array[integer] | market IDs |
| `slug` | array[string] | market slugs |
| `clob_token_ids` | array[string] | CLOB token IDs |
| `condition_ids` | array[string] | condition IDs |
| `market_maker_address` | array[string] | — |
| `liquidity_num_min` / `_max` | number | — |
| `volume_num_min` / `_max` | number | — |
| `start_date_min` / `_max` | string (date-time) | — |
| `end_date_min` / `_max` | string (date-time) | — |
| `tag_id` | integer | — |
| `related_tags` | boolean | — |
| `cyom` | boolean | create-your-own-market filter |
| `uma_resolution_status` | string | — |
| `game_id` | string | — |
| `sports_market_types` | array[string] | — |
| `rewards_min_size` | number | — |
| `question_ids` | array[string] | — |
| `include_tag` | boolean | — |
| `closed` | boolean | default `false` |

**No `active` parameter exists**, even though `active` is a field on the
returned Market object (see rule below). Response: `array[Market]`, HTTP 200.

### Quirk — `outcomes`, `outcomePrices`, `clobTokenIds` are JSON-encoded strings, not arrays

Checked specifically because it changes how every consuming module has to
parse these fields. Per the current Gamma OpenAPI schema, all three are
declared as:

- `outcomes`: `type: string` (nullable)
- `outcomePrices`: `type: string` (nullable)
- `clobTokenIds`: `type: string` (nullable)

They are **strings containing JSON**, e.g. `outcomes` looks like
`"[\"Yes\",\"No\"]"` on the wire, not a native JSON array like
`["Yes", "No"]`. Every collector reading these three fields must
`json.loads()` them before use — treating them as already-parsed lists will
break at runtime (str indexing gives characters, not outcome names).

Project rule (existing, restated): to filter on `active`, fetch with the
documented parameters (e.g. `closed=false`) and filter the returned list
client-side on the `active` field after parsing. Never send an undocumented
query parameter — see CLAUDE.md working agreement.

### Quirk — archived markets silently vanish from `condition_ids`-filtered responses

Confirmed live (2026-08-13/16): a market that has fully resolved and been
archived is not returned with `closed: true` when queried by
`condition_ids=<its own condition_id>` — it is simply absent from the
response array, as if the id didn't exist. Root-caused as the reason
`app/collectors/markets.py`'s "not yet closed" work list only ever grows:
a resolved market's `closed` flag in our DB never flips because Gamma stops
telling us about it at all. `GET https://clob.polymarket.com/markets/{id}`
(below) still reports `closed: true` correctly for the same market and is
used as the fallback source of truth when this happens.

## Rate limits (api-reference/rate-limits)

| Endpoint | Limit |
|---|---|
| Data API `/trades` | 200 req / 10s |
| Data API `/positions` | 150 req / 10s |
| Gamma API `/markets` | 300 req / 10s |

Mechanism: Cloudflare-based, sliding time window. Docs state requests over
the limit are **throttled (delayed/queued), not immediately rejected** —
different from a hard 429 rejection model, relevant for how collectors
should back off (tenacity retry/backoff is still warranted, but a 429 isn't
necessarily the first symptom of hitting the ceiling).

No documented rate-limit response headers (no `X-RateLimit-*` equivalent
noted on this page) — nothing to read for adaptive throttling; a fixed
request budget per endpoint is the only lever available.

The leaderboard endpoint (`/v1/leaderboard`) has no rate limit documented on
this page specifically — flagging as an open question rather than assuming
it inherits the `/positions` figure.

## Data API — `GET https://data-api.polymarket.com/trades`

Verified against the current Data API OpenAPI spec (previously, 2026-07-27).

- `limit` (default 100, max 10000, clamped above max), `offset`, `user`,
  `market`, `eventId`, `side`, `start`/`end` (epoch timestamps) all
  documented and confirmed.

## CLOB API — `GET https://clob.polymarket.com/markets`

Confirmed live (previously, 2026-07-27): returns `{"data": [...]}`, an array
of market objects (`active`, `closed`, `question`, `tokens`, etc.).
Cursor-based pagination via `next_cursor`.

## CLOB API — `GET https://clob.polymarket.com/markets/{condition_id}`

Verified live via curl (2026-08-16) — docs.polymarket.com's own reference
pages for this path were fragmentary/inconsistent (a market-data page 404'd,
`get-clob-market-info` turned out to describe a different, narrower endpoint
at `/clob-markets/{condition_id}` returning only trading params like `gst`/
`r`/`t`, and `get-market-by-id` turned out to be Gamma's `/markets/{id}` on
Gamma's internal numeric id, not our condition_id). Falling back to direct
verification: single market object, per-item shape identical to the bulk
`GET /markets` endpoint above (`active`, `closed`, `accepting_orders`,
`condition_id`, `question`, etc.). Unknown condition_id → `404`.

This is our fallback source of resolution state for a market that Gamma's
condition_ids-filtered `/markets` has silently dropped from its response
(see the Gamma section above) — the markets collector
(`app/collectors/markets.py`) uses it to update just `active`/`closed`/
`accepting_orders` for any requested condition_id missing from Gamma's
response, never liquidity/volume/category, which this endpoint doesn't
carry the way Gamma's does.

## CLOB API — `GET https://clob.polymarket.com/fee-rate`

Verified against docs.polymarket.com/trading/fees and
docs.polymarket.com/api-reference/market-data/get-fee-rate (2026-08-16).

Query params: `token_id` (string, optional) — one outcome token's CLOB
token ID.

Response: `{"base_fee": 30}` — basis points, integer. Divide by 10000 for
the fraction used in the fee formula below. 400 on an invalid token_id, 404
when no fee rate exists for the market.

**Fee formula (docs/trading/fees):** `fee = C × feeRate × p × (1 - p)`,
where `C` is shares traded and `p` is the execution price — charged **only
on taker fills at trade execution** (both entries and any exit that's a
real market sell: take-profit, stop-loss, signal-expiry, scalp). Makers pay
0%. Symmetric, peaking at p=0.5, zero at the extremes.

Documented category rates (`docs/trading/fees`, current as of the above
verification date — this schedule has changed more than once since
Polymarket introduced taker fees in January 2026, which is exactly why this
project queries `/fee-rate` live per market rather than hardcoding this
table): crypto 0.07, sports 0.05, economics/culture/weather/other 0.05,
finance/politics/mentions/tech 0.04, geopolitics 0 (fee-free). Note this
does **not** line up 1:1 with this project's own `normalize_category`
buckets (`app/collectors/categories.py`) — e.g. our "Politics" bucket folds
in geopolitics, which Polymarket's own schedule prices at 0% vs politics'
4% — one more reason the live per-token endpoint is used instead of our
category field.

**Redemption is not a taker trade and pays no fee.** Confirmed via
docs/concepts/resolution and docs/trading (redemption pages): resolving a
market and redeeming winning tokens for $1 (or letting losing tokens expire
worthless) goes through the CTF collateral adapter, not the CLOB order
book — neither `docs/trading/fees` nor the resolution docs mention any fee
on it. See app/paper/fees.py's docstring for how this maps onto our exit
reasons.

## CLOB API — `GET https://clob.polymarket.com/book`

Verified against docs.polymarket.com/api-reference/market-data/get-order-book
(2026-08-14).

Query params: `token_id` (string, required) — one outcome token's CLOB
token ID (`markets.clob_token_ids[i]`, same ids the `prices` table's
`asset` column already stores).

Response:

```json
{
  "market": "string",
  "asset_id": "string",
  "timestamp": "string",
  "hash": "string",
  "bids": [{"price": "string", "size": "string"}],
  "asks": [{"price": "string", "size": "string"}],
  "min_order_size": "string",
  "tick_size": "string",
  "neg_risk": true,
  "last_trade_price": "string"
}
```

`price`/`size` on every level are strings, not numbers — parse to Decimal,
never float. Bids sorted price **descending** (best bid first), asks sorted
price **ascending** (best ask first) — a level-walk for a BUY order consumes
`asks` from the front.

No rate limit documented on this endpoint's own page. A known upstream
quirk (Polymarket/py-clob-client#180): `/book` has been reported to return
stale, degenerate levels (e.g. 0.99/0.01) for some markets while other
endpoints (`last_trade_price`) stay accurate — not something we can fix on
our end, but a reason to treat an empty or one-sided book as `NO_LIQUIDITY`
rather than trusting it blindly.

404 means no orderbook exists for that token_id (illiquid/inactive market) -
expected, not an error to retry.

---

Verified on 2026-08-14.

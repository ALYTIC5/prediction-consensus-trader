# Polymarket API reference notes

Findings from verifying endpoints against docs.polymarket.com before writing
code against them (per CLAUDE.md working agreement). Update this file
whenever a new endpoint is verified or an assumption turns out wrong.

## Gamma API — `GET https://gamma-api.polymarket.com/markets`

Verified against the current Gamma OpenAPI spec (2026-07-27).

- Query parameters that exist: `limit`, `offset`, `order`, `ascending`,
  `id`, `slug`, `clob_token_ids`, `condition_ids`, `market_maker_address`,
  `liquidity_num_min/max`, `volume_num_min/max`, `start_date_min/max`,
  `end_date_min/max`, `tag_id`, `related_tags`, `cyom`,
  `uma_resolution_status`, `game_id`, `sports_market_types`,
  `rewards_min_size`, `question_ids`, `include_tag`, `closed`.
- **`active` is NOT a query parameter**, even though `active` is a field on
  the returned Market object. Sending `active=true` would be an undocumented
  parameter — likely silently ignored by the server, but unverified, so we
  don't rely on it.
- Project rule: to filter on `active`, fetch with the documented parameters
  (e.g. `closed=false`) and filter the returned list client-side on the
  `active` field. This applies generally — see CLAUDE.md working agreement.

## Data API — `GET https://data-api.polymarket.com/trades`

Verified against the current Data API OpenAPI spec (2026-07-27).

- `limit` (default 100, max 10000, clamped above max), `offset`, `user`,
  `market`, `eventId`, `side`, `start`/`end` (epoch timestamps) all
  documented and confirmed.

## CLOB API — `GET https://clob.polymarket.com/markets`

Confirmed live (2026-07-27): returns `{"data": [...]}`, an array of market
objects (`active`, `closed`, `question`, `tokens`, etc.). Cursor-based
pagination via `next_cursor`.

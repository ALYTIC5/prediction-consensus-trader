"""Read-only Polymarket connectivity check.

No account, no wallet, no orders - this only reads public data. It sends
three plain GET requests to public endpoints and reports whether each is
reachable from here. It never authenticates and never touches trading
endpoints.

Endpoints verified against docs.polymarket.com on 2026-07-27 (see
docs/API_REFERENCE.md). Gamma's /markets has no documented "active" query
parameter, so this script does not send one.
"""

import sys

import httpx

ENDPOINTS = [
    (
        "Gamma markets",
        "https://gamma-api.polymarket.com/markets",
        {"limit": 2, "closed": "false"},
    ),
    (
        "Data API trades",
        "https://data-api.polymarket.com/trades",
        {"limit": 1},
    ),
    (
        "CLOB markets",
        "https://clob.polymarket.com/markets",
        {},
    ),
]

BLOCK_CODES = {401, 403, 451}


def main() -> int:
    any_blocked = False

    with httpx.Client(timeout=10.0) as client:
        for name, url, params in ENDPOINTS:
            try:
                response = client.get(url, params=params)
            except httpx.HTTPError as exc:
                print(f"[ERROR] {name} ({url}): {exc}")
                continue

            if response.status_code == 200:
                print(f"[OK]    {name} ({url}): 200")
            elif response.status_code in BLOCK_CODES:
                print(f"[BLOCK] {name} ({url}): {response.status_code}")
                any_blocked = True
            else:
                print(f"[ERROR] {name} ({url}): unexpected status {response.status_code}")

    print()
    if any_blocked:
        print(
            "One or more endpoints returned a BLOCK status (401/403/451). "
            "This is consistent with the operator's region (Netherlands) being "
            "blocked from Polymarket's off-chain APIs, and means the collector "
            "layer needs an on-chain fallback path (reading Polygon contract "
            "state directly) instead of these public REST endpoints."
        )
    else:
        print("No BLOCK responses. Public off-chain APIs are reachable as-is.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

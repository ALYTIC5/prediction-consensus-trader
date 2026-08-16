"""Polymarket taker-fee model - pure, no DB, no network.

Verified against docs.polymarket.com/trading/fees (2026-08-16), not
guessed: fee = size × rate × price × (1 - price), charged whenever a taker
order matches on the CLOB (maker fills are always 0%, and this project's
paper engine always models itself as the taker, both entering via
walk_the_book and exiting via a market sell - see app/paper/engine.py).
`rate` is per-market, sourced from the live GET /fee-rate endpoint (see
app/collectors/fee_rates.py), never a single hardcoded constant - the
schedule already varies 0%-7% by category and has changed more than once
since Polymarket introduced it in January 2026.

Crucially, redeeming a resolved market's winning tokens for $1 (or letting
losing tokens expire worthless) is NOT a CLOB trade - it's a settlement via
the CTF collateral adapter, and docs/trading/fees and docs/concepts/
resolution make no mention of any fee on it. So a MARKET_RESOLVED exit
(app/paper/engine.py's ExitReason) never calls this function; every other
exit (take-profit, stop-loss, signal-expiry, any SCALP exit) is a real
market sell and does.
"""

from decimal import Decimal


def compute_taker_fee(price: Decimal, size: Decimal, rate: Decimal) -> Decimal:
    """fee = size × rate × price × (1 - price) - the documented formula,
    applied to one leg (one CLOB fill) at a time. Symmetric and peaks at
    price=0.5; zero at price=0 or price=1 by construction, same as the
    documented fee table's own extremes.
    """
    return size * rate * price * (Decimal(1) - price)

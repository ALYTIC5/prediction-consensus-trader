"""Small, pure Jinja filters shared across dashboard templates."""

import json
from datetime import UTC, datetime
from typing import Any

from markupsafe import Markup


def relative_time(value: datetime | None) -> str:
    """e.g. "3m ago" for the past, "in 3m" for the future. "never" if None.

    Both directions matter: heartbeats/tape/history read past ("3m ago"),
    but signal expiry and market end dates are future ("in 3m") - a signal
    that hasn't expired yet must never render as "0s ago".
    """
    if value is None:
        return "never"

    delta_seconds = (datetime.now(UTC) - value).total_seconds()
    future = delta_seconds < 0
    seconds = abs(delta_seconds)

    if seconds < 60:
        magnitude = f"{int(seconds)}s"
    else:
        minutes = seconds / 60
        if minutes < 60:
            magnitude = f"{int(minutes)}m"
        else:
            hours = minutes / 60
            if hours < 24:
                magnitude = f"{int(hours)}h"
            else:
                days = hours / 24
                magnitude = f"{int(days)}d"

    return f"in {magnitude}" if future else f"{magnitude} ago"


def short_address(address: str | None) -> str:
    """0x1234...abcd - used everywhere a wallet address is displayed inline."""
    if not address or len(address) <= 12:
        return address or "-"
    return f"{address[:6]}…{address[-4:]}"


def to_json(value: Any) -> Markup:
    """JSON-serialize for embedding in a <script> tag - wrapped in Markup so
    Jinja's autoescaping doesn't corrupt the JSON by escaping quotes.
    default=str covers datetime/Decimal, neither of which json.dumps
    handles natively.
    """
    return Markup(json.dumps(value, default=str))

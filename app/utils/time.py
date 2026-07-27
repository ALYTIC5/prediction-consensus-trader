"""Datetime parsing helpers."""

from datetime import UTC, datetime


def parse_iso_datetime(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 string, tolerating a trailing Z. None on bad input.

    A naive result is assumed UTC - Polymarket's timestamps are UTC even
    when the API sends one with no explicit offset.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed

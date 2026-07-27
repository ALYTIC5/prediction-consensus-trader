"""Shared pytest fixtures."""

import pytest
from tenacity import wait_none

from app.collectors.http import PolymarketHTTP


@pytest.fixture(autouse=True)
def _no_tenacity_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero out get_json's retry backoff so retry tests don't actually sleep.

    tenacity attaches its retry controller to the decorated function as
    `.retry` (shared across all instances, since the decorator runs once at
    class-definition time) - swapping its `wait` strategy is the documented
    way to make retries instant in tests without touching the real code.
    """
    monkeypatch.setattr(PolymarketHTTP.get_json.retry, "wait", wait_none())

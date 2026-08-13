"""Integration test for GET /markets, and a regression test for the
OperationalError that took it down in production.

Root cause: app/dashboard/queries.py's _latest_prices_for_assets() built one
`.in_(assets)` query per call, binding one SQL parameter per outcome-token
asset. get_markets() passes every open market's outcome tokens into that one
call - with the market catalog at ~49.8k rows and (so far) zero markets ever
closed, "open" was effectively "every market," pushing well past Postgres's
65535-bind-parameter-per-query limit and raising sqlalchemy.exc.
OperationalError on every GET /markets. Mirrors test_healthz.py's
_load_app + queries-level monkeypatch pattern - no real DB needed.
"""

import importlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.dashboard.queries import _ASSET_QUERY_BATCH_SIZE, MarketOutcomePrice, MarketRow


def _load_app(monkeypatch: pytest.MonkeyPatch):
    defaults = {
        "REDIS_URL": "redis://localhost:6379/0",
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
        "ENVIRONMENT": "development",
    }
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)

    get_settings.cache_clear()

    import app.dashboard.main as dashboard_main

    importlib.reload(dashboard_main)
    return dashboard_main


def _market_row(market_id: int, *, closed: bool = False, priced: bool = True) -> MarketRow:
    return MarketRow(
        id=market_id,
        condition_id=f"0xcond{market_id}",
        question=f"Will thing {market_id} happen?",
        outcome_prices=[
            MarketOutcomePrice(
                outcome="Yes",
                asset=f"asset{market_id}-yes",
                price=Decimal("0.62") if priced else None,
            ),
            MarketOutcomePrice(
                outcome="No",
                asset=f"asset{market_id}-no",
                price=Decimal("0.38") if priced else None,
            ),
        ],
        liquidity=Decimal("15000.50"),
        volume_24h=Decimal("2300.75"),
        spread=Decimal("0.02"),
        end_date=datetime(2026, 12, 1, tzinfo=UTC),
        closed=closed,
    )


def test_markets_page_returns_200_with_representative_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Representative, not empty: a mix of priced/unpriced outcomes and a
    closed market, so the template's None-handling and status branches both
    actually render, not just the happy path.
    """
    dashboard_main = _load_app(monkeypatch)
    rows = [
        _market_row(1, closed=False, priced=True),
        _market_row(2, closed=False, priced=False),
        _market_row(3, closed=True, priced=True),
    ]
    monkeypatch.setattr(dashboard_main.queries, "get_markets", lambda status="open": rows)
    client = TestClient(dashboard_main.app)

    response = client.get("/markets")

    assert response.status_code == 200
    assert "Will thing 1 happen?" in response.text


def test_markets_page_handles_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_main = _load_app(monkeypatch)
    monkeypatch.setattr(dashboard_main.queries, "get_markets", lambda status="open": [])
    client = TestClient(dashboard_main.app)

    response = client.get("/markets")

    assert response.status_code == 200


class _CountingSession:
    """Stands in for a SQLAlchemy Session - counts .execute() calls instead
    of touching a real database. itertools.batched (used by
    _latest_prices_for_assets) guarantees every chunk it yields is at most
    _ASSET_QUERY_BATCH_SIZE elements, so the call count alone proves no
    single query's IN-list can exceed that size.
    """

    def __init__(self) -> None:
        self.call_count = 0

    def execute(self, statement):
        self.call_count += 1

        class _Result:
            def all(self) -> list:
                return []

        return _Result()


def test_latest_prices_for_assets_batches_under_the_bind_parameter_limit() -> None:
    """The actual regression: a large asset set (representative of "every
    open market's outcome tokens," which is what actually broke production)
    must never reach the DB as one giant IN-list - it has to split into
    multiple bounded queries instead of the one unbounded query that
    overflowed Postgres's 65535-bind-parameter limit.
    """
    from app.dashboard.queries import _latest_prices_for_assets

    assets = {f"asset-{i}" for i in range(_ASSET_QUERY_BATCH_SIZE * 3 + 7)}
    session = _CountingSession()

    _latest_prices_for_assets(session, assets)

    assert session.call_count == 4  # 3 full batches of 5000 + 1 remainder of 7


def test_latest_prices_for_assets_makes_one_query_under_the_batch_size() -> None:
    from app.dashboard.queries import _latest_prices_for_assets

    assets = {f"asset-{i}" for i in range(10)}
    session = _CountingSession()

    _latest_prices_for_assets(session, assets)

    assert session.call_count == 1

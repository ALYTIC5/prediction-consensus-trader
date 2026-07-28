"""Integration tests for /healthz's job-staleness check.

Mirrors test_dashboard_auth.py's _load_app pattern (fresh env + reload per
test) rather than a module-level import, so this doesn't depend on whatever
state a previous test file left app.dashboard.main's module cache in.

db/redis and heartbeat data are monkeypatched at the app.dashboard.queries
level - this only exercises the status-code/body logic in the route itself,
not real connectivity (that's what check_db_health/check_redis_health's own
broad excepts are for).
"""

import importlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.dashboard.queries import Heartbeat


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


def _heartbeat(name: str, status: str) -> Heartbeat:
    return Heartbeat(name=name, last_write=datetime.now(UTC), interval_seconds=300, status=status)


def _patch_health(dashboard_main, monkeypatch, *, db_ok: bool, redis_ok: bool, statuses: list[str]):
    monkeypatch.setattr(dashboard_main.queries, "check_db_health", lambda: db_ok)
    monkeypatch.setattr(dashboard_main.queries, "check_redis_health", lambda: redis_ok)
    monkeypatch.setattr(
        dashboard_main.queries,
        "get_collector_heartbeats",
        lambda settings: [_heartbeat(f"job{i}", status) for i, status in enumerate(statuses)],
    )


def test_healthz_ok_when_db_redis_reachable_and_no_job_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard_main = _load_app(monkeypatch)
    _patch_health(
        dashboard_main, monkeypatch, db_ok=True, redis_ok=True, statuses=["fresh", "stale"]
    )
    client = TestClient(dashboard_main.app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["dead_jobs"] == []


def test_healthz_returns_503_when_a_job_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_main = _load_app(monkeypatch)
    _patch_health(
        dashboard_main, monkeypatch, db_ok=True, redis_ok=True, statuses=["fresh", "dead"]
    )
    client = TestClient(dashboard_main.app)

    response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dead_jobs"] == ["job1"]


def test_healthz_returns_503_when_db_unreachable_even_if_jobs_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard_main = _load_app(monkeypatch)
    _patch_health(dashboard_main, monkeypatch, db_ok=False, redis_ok=True, statuses=["fresh"])
    client = TestClient(dashboard_main.app)

    response = client.get("/healthz")

    assert response.status_code == 503

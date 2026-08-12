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
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.dashboard.queries import Heartbeat, JobHealth, _heartbeat_status


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


def _job_health(job_name: str, status: str) -> JobHealth:
    return JobHealth(
        service="scout",
        job_name=job_name,
        interval_seconds=300,
        last_status="ok" if status != "dead" else "failed",
        last_run_at=datetime.now(UTC),
        last_success_at=datetime.now(UTC) if status != "dead" else None,
        last_failure_at=None,
        last_error=None,
        status=status,
    )


def _patch_health(
    dashboard_main,
    monkeypatch,
    *,
    db_ok: bool,
    redis_ok: bool,
    statuses: list[str],
    job_statuses: list[str] | None = None,
):
    monkeypatch.setattr(dashboard_main.queries, "check_db_health", lambda: db_ok)
    monkeypatch.setattr(dashboard_main.queries, "check_redis_health", lambda: redis_ok)
    monkeypatch.setattr(
        dashboard_main.queries,
        "get_collector_heartbeats",
        lambda settings: [_heartbeat(f"job{i}", status) for i, status in enumerate(statuses)],
    )
    job_statuses = job_statuses or []
    monkeypatch.setattr(
        dashboard_main.queries,
        "get_job_health",
        lambda: [_job_health(f"scheduled{i}", status) for i, status in enumerate(job_statuses)],
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


def test_healthz_returns_503_when_a_scheduled_job_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """job_heartbeats covers jobs (e.g. Scout's) that get_collector_heartbeats
    never did - a dead row there must fail /healthz on its own, independent
    of the four data-table-derived heartbeats.
    """
    dashboard_main = _load_app(monkeypatch)
    _patch_health(
        dashboard_main,
        monkeypatch,
        db_ok=True,
        redis_ok=True,
        statuses=["fresh"],
        job_statuses=["fresh", "dead"],
    )
    client = TestClient(dashboard_main.app)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["dead_jobs"] == ["scout/scheduled1"]


def test_healthz_returns_503_when_db_unreachable_even_if_jobs_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard_main = _load_app(monkeypatch)
    _patch_health(dashboard_main, monkeypatch, db_ok=False, redis_ok=True, statuses=["fresh"])
    client = TestClient(dashboard_main.app)

    response = client.get("/healthz")

    assert response.status_code == 503


def test_staleness_uses_each_jobs_own_interval() -> None:
    """The same 2-hour-old success must read fresh for a slow job and dead
    for a fast one - staleness is never a single global constant, it's
    always evaluated against the row's own interval_seconds.
    """
    now = datetime.now(UTC)
    two_hours_ago = now - timedelta(hours=2)

    # positions' real interval (120s): 2h old is far past the 3x-interval
    # dead threshold (6 minutes).
    assert _heartbeat_status(two_hours_ago, 120, now) == "dead"

    # scout_forward_tracking's real interval (3600s/1h): 2h old is past
    # fresh (1h) but still inside the 3x-interval stale window (3h).
    assert _heartbeat_status(two_hours_ago, 3600, now) == "stale"

    # scout_historical_screen's real interval (24h): 2h old is still fresh.
    assert _heartbeat_status(two_hours_ago, 86400, now) == "fresh"

    # A job never having succeeded is dead regardless of its interval.
    assert _heartbeat_status(None, 86400, now) == "dead"

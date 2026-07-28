"""Integration tests for the dashboard's HTTP Basic auth middleware.

app.dashboard.main reads Settings at import time (the production startup
guard) and get_settings() is cached process-wide, so each scenario here
clears that cache and monkeypatches exactly the env vars it needs before
(re)importing the module - never depending on whatever the real .env
happens to contain, so these stay hermetic like every other test here.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings


def _load_app(monkeypatch: pytest.MonkeyPatch, **env: str | None):
    """(Re)import app.dashboard.main with exactly the given env on top of
    sane defaults, against a freshly cleared Settings cache.
    """
    defaults: dict[str, str | None] = {
        "REDIS_URL": "redis://localhost:6379/0",
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
        "ENVIRONMENT": "development",
        "DASHBOARD_USER": None,
        "DASHBOARD_PASSWORD": None,
    }
    defaults.update(env)
    for key, value in defaults.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    get_settings.cache_clear()

    import app.dashboard.main as dashboard_main

    importlib.reload(dashboard_main)
    return dashboard_main


def test_unauthenticated_request_returns_401_when_auth_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard_main = _load_app(monkeypatch, DASHBOARD_USER="admin", DASHBOARD_PASSWORD="secret")
    client = TestClient(dashboard_main.app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_correct_credentials_are_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_main = _load_app(monkeypatch, DASHBOARD_USER="admin", DASHBOARD_PASSWORD="secret")
    client = TestClient(dashboard_main.app, raise_server_exceptions=False)

    response = client.get("/", auth=("admin", "secret"))

    # Not asserting the page itself renders (that needs a real DB) - only
    # that auth let the request reach the route at all, instead of 401ing.
    assert response.status_code != 401


def test_wrong_credentials_still_return_401(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_main = _load_app(monkeypatch, DASHBOARD_USER="admin", DASHBOARD_PASSWORD="secret")
    client = TestClient(dashboard_main.app, raise_server_exceptions=False)

    response = client.get("/", auth=("admin", "wrong-password"))

    assert response.status_code == 401


def test_healthz_stays_open_without_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    dashboard_main = _load_app(monkeypatch, DASHBOARD_USER="admin", DASHBOARD_PASSWORD="secret")
    client = TestClient(dashboard_main.app, raise_server_exceptions=False)

    response = client.get("/healthz")

    assert response.status_code == 200


def test_no_auth_required_when_dashboard_credentials_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matches the original local-dev design: with neither DASHBOARD_USER
    nor DASHBOARD_PASSWORD set, the console behaves exactly as before -
    no 401, ever.
    """
    dashboard_main = _load_app(monkeypatch)
    client = TestClient(dashboard_main.app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code != 401


def test_production_without_dashboard_credentials_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="DASHBOARD_USER and DASHBOARD_PASSWORD"):
        _load_app(monkeypatch, ENVIRONMENT="production")

"""Unit tests for Settings. _env_file=None ignores any local .env so these
tests are hermetic and don't depend on developer machine state.
"""

import pytest

from app.config.settings import Settings


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "postgres_password": "secret",
        "redis_url": "redis://localhost:6379/0",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_database_url_assembled_correctly() -> None:
    settings = _settings(
        postgres_user="polybot",
        postgres_password="secret",
        postgres_db="polybot",
        postgres_host="localhost",
        postgres_port=5432,
    )

    assert settings.database_url == "postgresql+psycopg://polybot:secret@localhost:5432/polybot"


def test_password_special_characters_are_url_encoded() -> None:
    raw_password = "p@ss:w/ord%25"
    settings = _settings(postgres_password=raw_password)

    url = settings.database_url

    assert raw_password not in url
    assert "p%40ss%3Aw%2Ford%2525" in url


def test_defaults() -> None:
    settings = _settings()

    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.postgres_user == "polybot"
    assert settings.postgres_db == "polybot"
    assert settings.postgres_host == "localhost"
    assert settings.postgres_port == 5432


def test_database_url_prefers_database_url_override() -> None:
    """Railway sets DATABASE_URL - when present it wins outright, the
    POSTGRES_* fields (even if also present) are never consulted.
    """
    settings = Settings(
        _env_file=None,
        redis_url="redis://x",
        DATABASE_URL="postgresql://user:pass@host:5432/db",
    )

    assert settings.database_url == "postgresql+psycopg://user:pass@host:5432/db"


def test_database_url_override_does_not_double_up_psycopg() -> None:
    settings = Settings(
        _env_file=None,
        redis_url="redis://x",
        DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db",
    )

    url = settings.database_url
    assert url == "postgresql+psycopg://user:pass@host:5432/db"
    assert url.count("+psycopg") == 1


def test_database_url_raises_when_neither_override_nor_password_set() -> None:
    settings = Settings(_env_file=None, redis_url="redis://x")

    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD is required"):
        _ = settings.database_url

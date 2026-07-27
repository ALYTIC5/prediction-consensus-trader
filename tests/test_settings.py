"""Unit tests for Settings. _env_file=None ignores any local .env so these
tests are hermetic and don't depend on developer machine state.
"""

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

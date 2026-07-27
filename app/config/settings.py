"""Application configuration.

Why centralized here: CLAUDE.md mandates os.environ is read only in this
module, so every other module gets config through get_settings() and stays
testable (no hidden env lookups scattered through the codebase).
"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated application settings loaded from environment/.env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"

    postgres_user: str = "polybot"
    # No default: a missing password must fail fast at startup rather than
    # silently connecting with an empty credential.
    postgres_password: str
    postgres_db: str = "polybot"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    redis_url: str

    @property
    def database_url(self) -> str:
        """Build the SQLAlchemy URL, quoting the password.

        quote_plus prevents special characters (@, :, /, etc.) in the
        password from being misparsed as URL structure.
        """
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so .env is parsed once per process."""
    return Settings()

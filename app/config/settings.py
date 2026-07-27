"""Application configuration.

Why centralized here: CLAUDE.md mandates os.environ is read only in this
module, so every other module gets config through get_settings() and stays
testable (no hidden env lookups scattered through the codebase).
"""

from decimal import Decimal
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import model_validator
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

    gamma_api_base: str = "https://gamma-api.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"

    http_timeout_seconds: float = 15.0
    http_max_concurrency: int = 6

    # Collector polling cadence. Deliberately far below the documented
    # Polymarket rate limits (gamma /markets 300 req/10s, data /trades
    # 200 req/10s, data /positions 150 req/10s - see docs/API_REFERENCE.md) -
    # one request every 120-3600s per collector, single-digit concurrency, is
    # nowhere near those ceilings even with multiple collectors running
    # together.
    leaderboard_time_periods: str = "MONTH,ALL"
    leaderboard_category: str = "OVERALL"
    leaderboard_top_n: int = 100
    tracked_wallets_limit: int = 50
    positions_size_threshold: Decimal = Decimal("1.0")
    leaderboard_interval_seconds: int = 3600
    positions_interval_seconds: int = 120
    markets_interval_seconds: int = 600

    # --- Phase 3: trader scoring, consensus engine, signal generation ---
    # See docs/PHASE3_DESIGN.md. Every threshold here, none hardcoded in
    # app/consensus or app/signals - a hardcoded threshold there is a bug.
    scoring_lookback_days: int = 14
    score_weight_month: Decimal = Decimal("0.45")
    score_weight_all_time: Decimal = Decimal("0.25")
    score_weight_consistency: Decimal = Decimal("0.30")

    consensus_interval_seconds: int = 300
    consensus_freshness_hours: int = 48
    consensus_include_increases: bool = True
    consensus_min_traders: int = 3
    consensus_min_weighted_score: Decimal = Decimal("1.0")
    consensus_min_combined_value_usd: Decimal = Decimal("500")

    signal_min_liquidity_usd: Decimal = Decimal("5000")
    signal_min_volume_24h_usd: Decimal = Decimal("1000")
    signal_price_min: Decimal = Decimal("0.05")
    signal_price_max: Decimal = Decimal("0.95")
    signal_max_spread: Decimal = Decimal("0.05")
    signal_min_hours_to_end: int = 12
    signal_ttl_hours: int = 72

    @model_validator(mode="after")
    def _validate_score_weights(self) -> "Settings":
        """A silent weight mis-sum would quietly under/over-weight every wallet."""
        total = self.score_weight_month + self.score_weight_all_time + self.score_weight_consistency
        if total != Decimal("1.0"):
            raise ValueError(
                "score weights must sum to 1.0, got "
                f"{total} (month={self.score_weight_month}, "
                f"all_time={self.score_weight_all_time}, "
                f"consistency={self.score_weight_consistency})"
            )
        return self

    @property
    def leaderboard_periods(self) -> list[str]:
        """leaderboard_time_periods split into a list, e.g. ["MONTH", "ALL"]."""
        return [p.strip() for p in self.leaderboard_time_periods.split(",") if p.strip()]

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

"""Application configuration.

Why centralized here: CLAUDE.md mandates os.environ is read only in this
module, so every other module gets config through get_settings() and stays
testable (no hidden env lookups scattered through the codebase).
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated application settings loaded from environment/.env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # database_url_override's alias ("DATABASE_URL") doesn't match its
        # field name, so populate_by_name=True is needed for that field to
        # also be constructible/settable by its plain Python name (tests,
        # direct instantiation) - every other field is unaffected since
        # none of them use an alias.
        populate_by_name=True,
    )

    environment: str = "development"
    log_level: str = "INFO"
    # Deliberately NOT under a OneDrive/Dropbox/etc-synced folder by default -
    # this project's own working directory is inside OneDrive, and OneDrive's
    # background file replacement during sync silently orphaned the log
    # handler's file descriptor for 10+ hours with zero error surfaced (see
    # the incident this field was added for). ~/.polybot/logs sits outside
    # whatever's under Desktop/Documents, so it's never touched by that sync.
    log_dir: str = Field(default_factory=lambda: str(Path.home() / ".polybot" / "logs"))
    # Opt-in, not opt-out: Railway runs Linux containers where stdout is the
    # only sink that matters (Railway captures it directly) and there's no
    # OneDrive-style sync process to orphan a file handle - so file logging
    # defaults OFF in production and ON everywhere else, via log_to_file
    # below. None here means "not explicitly set" so that default can apply;
    # explicitly setting LOG_TO_FILE always wins regardless of environment.
    log_to_file_override: bool | None = Field(default=None, alias="LOG_TO_FILE")

    postgres_user: str = "polybot"
    # Optional now: required for local dev (see database_url below, which
    # fails fast if it's actually needed and missing), but Railway supplies
    # a full DATABASE_URL instead and never sets this at all.
    postgres_password: str | None = None
    postgres_db: str = "polybot"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Railway injects the full connection string as DATABASE_URL - the
    # field name deliberately doesn't match so it's never confused with the
    # POSTGRES_*-built URL; only the alias enables it from the environment.
    database_url_override: str | None = Field(default=None, alias="DATABASE_URL")

    redis_url: str

    # Both optional: dashboard auth is off unless both are set (see
    # app/dashboard/main.py), and required only when ENVIRONMENT=production.
    dashboard_user: str | None = None
    dashboard_password: str | None = None

    # None means "not set" - scripts/run_dashboard.py uses that (not just
    # the resolved port number) to tell "Railway set PORT" apart from
    # "nothing set it, use the local-dev default," which decides both the
    # port AND whether to bind 0.0.0.0 or 127.0.0.1.
    port: int | None = None

    gamma_api_base: str = "https://gamma-api.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"

    http_timeout_seconds: float = 15.0
    http_max_concurrency: int = 6

    # TCP connect timeout for the SQLAlchemy engine - see app/db/session.py.
    db_connect_timeout_seconds: int = 10

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
    # A stuck cycle should fail loudly and retry next interval, not hang
    # forever - see docs/PHASE3_DESIGN.md incident: the consensus job hung
    # silently for 10+ hours with no exception and no log line. 600s is
    # generous relative to normal sub-2s cycle times at current data volumes.
    consensus_cycle_timeout_seconds: int = 600
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

    # --- Phase 4: paper trading ---
    # See docs/PHASE4_DESIGN.md. Every threshold here, none hardcoded in
    # app/paper/ - a hardcoded threshold there is a bug, same rule as
    # Phase 3. paper_ prefix keeps this group grep-able alongside
    # consensus_/signal_. A portfolio's params JSONB (app/db/models/paper.py)
    # can override any of these per-portfolio; these are just the defaults
    # used when a portfolio doesn't set its own value for a key.
    paper_interval_seconds: int = 120

    # Fill model (app/paper/fills.py) - see design section 2.
    paper_entry_delay_seconds: int = 30
    paper_slippage_k: Decimal = Decimal("0.5")
    paper_slippage_max: Decimal = Decimal("0.15")
    paper_no_delayed_snapshot_penalty: Decimal = Decimal("0.05")
    paper_max_entry_price_drift: Decimal = Decimal("0.15")
    paper_resolution_price_threshold: Decimal = Decimal("0.02")

    # Sizing (app/paper/sizing.py) - see design section 3.
    paper_sizing_rule: str = "FIXED_FRACTION"
    paper_fixed_fraction_pct: Decimal = Decimal("0.02")
    paper_confidence_base_fraction_pct: Decimal = Decimal("0.02")
    paper_confidence_reference_score: Decimal = Decimal("1.0")
    paper_confidence_min_multiplier: Decimal = Decimal("0.5")
    paper_confidence_max_multiplier: Decimal = Decimal("2.0")
    paper_max_position_notional_pct: Decimal = Decimal("0.10")
    paper_max_total_exposure_pct: Decimal = Decimal("0.60")
    # $1, not $10 - a $10 floor makes every trade dust-reject at realistic
    # small starting bankrolls (e.g. $100 at 2% FIXED_FRACTION = $2/trade).
    # See docs/PHASE4_DESIGN.md section 3's "$4 dust" example: dust is
    # relative to bankroll scale, not a fixed dollar amount independent of it.
    paper_min_position_notional_usd: Decimal = Decimal("1")

    # Portfolio entry filters (app/paper/engine.py) - defaults deliberately
    # mirror the consensus/signal thresholds a signal already had to clear
    # to become ACTIVE, since a portfolio's own filter is a stricter gate on
    # top of that, not an independent one.
    paper_min_traders: int = 3
    paper_min_weighted_score: Decimal = Decimal("1.0")
    paper_min_combined_value_usd: Decimal = Decimal("500")
    paper_min_liquidity_usd: Decimal = Decimal("5000")
    paper_max_spread: Decimal = Decimal("0.05")

    # Exits (app/paper/engine.py) - see design section 4c.
    paper_take_profit_pct: Decimal = Decimal("0.30")
    paper_stop_loss_pct: Decimal = Decimal("0.20")
    paper_exit_on_signal_expiry_hours: int = 72

    # Statistical honesty floor (app/paper/metrics.py) - see design section
    # 7. Deliberately not portfolio-overridable: a sample-size floor is a
    # statement about honesty, not a strategy choice, so every portfolio is
    # held to the same bar regardless of its own params.
    paper_min_trades_for_stats: int = 30

    # --- Phase 5: risk management and dynamic position sizing ---
    # See docs/PHASE5_DESIGN.md. risk_ prefix (flag 11: risk_max_position_pct
    # and risk_max_exposure_pct replace paper_max_position_notional_pct /
    # paper_max_total_exposure_pct, which RiskManager now owns instead of
    # size_position()). Every threshold here, none hardcoded in app/risk/.
    # Portfolio params JSONB can override any risk_/paper_sizer default here,
    # same _get_param mechanism as every other paper_* setting.

    # Stage 1 guardrails (app/risk/manager.py) - see design sections 1-2.
    risk_max_position_pct: Decimal = Decimal("0.10")
    risk_max_exposure_pct: Decimal = Decimal("0.60")
    risk_max_market_exposure_pct: Decimal = Decimal("0.20")
    risk_max_correlated_exposure_pct: Decimal = Decimal("0.30")
    risk_daily_stop_loss_pct: Decimal = Decimal("0.10")
    risk_max_open_positions: int = 10
    risk_min_bankroll_pct: Decimal = Decimal("0.20")

    # Per-rule kill switches - every Stage 1 rule is independently toggleable.
    risk_max_position_size_enabled: bool = True
    risk_max_total_exposure_enabled: bool = True
    risk_max_market_exposure_enabled: bool = True
    risk_max_correlated_exposure_enabled: bool = True
    risk_daily_stop_loss_enabled: bool = True
    risk_max_open_positions_enabled: bool = True
    risk_min_bankroll_halt_enabled: bool = True

    # Global manual kill switch (design flag 9) - lives in the adjustable
    # overrides registry (app/config/adjustable.py) for live, no-redeploy
    # flipping; the field here is just the base/default value (always False)
    # that get_effective_settings() overlays a runtime_overrides row onto.
    paper_emergency_stop: bool = False

    # Stage 2: tiered confidence sizing (app/risk/sizing_tiered.py) - see
    # design section 3. (min_score, max_score, fraction_pct) bands, evaluated
    # in order; a score at or above the last band's min_score uses that
    # band's fraction (an open-ended ">3.0" top band). max_score is a str
    # "inf" for the open-ended top band's upper bound placeholder - never
    # compared against, only min_score and fraction_pct are used at lookup
    # time (see fraction_for() in sizing_tiered.py).
    risk_tiered_score_bands: list[tuple[Decimal, Decimal, Decimal]] = Field(
        default_factory=lambda: [
            (Decimal("1.0"), Decimal("1.5"), Decimal("0.005")),
            (Decimal("1.5"), Decimal("2.0"), Decimal("0.01")),
            (Decimal("2.0"), Decimal("3.0"), Decimal("0.02")),
            (Decimal("3.0"), Decimal("999999"), Decimal("0.03")),
        ]
    )
    # Whether a weighted_score below the lowest band's min_score sizes at
    # that band's floor fraction (True) or is skipped outright (False) -
    # only matters if a portfolio's own entry filter is looser than its own
    # bands, since consensus_min_weighted_score/paper_min_weighted_score
    # already gate signals at >= the lowest band's default min in practice.
    risk_tiered_floor_below_lowest_band: bool = True

    # Which sizer a portfolio uses absent its own paper_sizer param override -
    # same fallback-default/portfolio-override relationship paper_sizing_rule
    # already has (design section 6). Live-tunable via the adjustable
    # overrides registry so an operator can flip the fleet-wide default
    # without a redeploy; a portfolio's own params JSONB always wins over
    # this when set.
    risk_default_sizer: str = "FIXED"

    # Stage 3a: consensus-to-probability calibration (app/risk/calibration.py)
    # - see design section 4. Not portfolio-overridable (flag 7/9's table):
    # calibration is a property of the shared signal-generation process
    # across all portfolios' resolved trades, not a per-portfolio choice.
    calibration_min_samples_per_bucket: int = 30
    calibration_window_days: int = 90
    calibration_trader_bands: list[tuple[Decimal, Decimal]] = Field(
        default_factory=lambda: [
            (Decimal("2"), Decimal("3")),
            (Decimal("3"), Decimal("5")),
            (Decimal("5"), Decimal("999999")),
        ]
    )
    # Mirrors risk_tiered_score_bands' band shape but independently
    # configurable (flag 7) - calibration bucketing and Stage 2's sizing
    # bands are allowed to diverge.
    calibration_score_bands: list[tuple[Decimal, Decimal]] = Field(
        default_factory=lambda: [
            (Decimal("1.0"), Decimal("1.5")),
            (Decimal("1.5"), Decimal("2.0")),
            (Decimal("2.0"), Decimal("3.0")),
            (Decimal("3.0"), Decimal("999999")),
        ]
    )
    calibration_price_bands: list[tuple[Decimal, Decimal]] = Field(
        default_factory=lambda: [
            (Decimal("0"), Decimal("0.3")),
            (Decimal("0.3"), Decimal("0.7")),
            (Decimal("0.7"), Decimal("1")),
        ]
    )

    # Stage 3b: fractional Kelly sizing (app/risk/sizing_kelly.py) - see
    # design section 5. Never above 0.5 (half Kelly) - enforced below, not
    # just documented, same pattern as _validate_paper_sizing_rule.
    risk_kelly_fraction: Decimal = Decimal("0.25")

    @model_validator(mode="after")
    def _validate_paper_fractions(self) -> "Settings":
        """Fields that are a fraction/percentage of a [0, 1]-bounded price
        or of bankroll - a value outside [0, 1] is never meaningful for any
        of these, and would silently produce nonsense sizing/slippage math.
        """
        fields = (
            "paper_slippage_max",
            "paper_no_delayed_snapshot_penalty",
            "paper_max_entry_price_drift",
            "paper_resolution_price_threshold",
            "paper_fixed_fraction_pct",
            "paper_confidence_base_fraction_pct",
            "paper_max_position_notional_pct",
            "paper_max_total_exposure_pct",
            "paper_max_spread",
            "paper_take_profit_pct",
            "paper_stop_loss_pct",
            "risk_max_position_pct",
            "risk_max_exposure_pct",
            "risk_max_market_exposure_pct",
            "risk_max_correlated_exposure_pct",
            "risk_daily_stop_loss_pct",
            "risk_min_bankroll_pct",
            "risk_kelly_fraction",
        )
        for name in fields:
            value = getattr(self, name)
            if not (Decimal("0") <= value <= Decimal("1")):
                raise ValueError(f"{name} must be within [0, 1], got {value}")
        return self

    @model_validator(mode="after")
    def _validate_paper_positive_intervals(self) -> "Settings":
        """Intervals, counts, and non-negative-only amounts - zero or
        negative would either stop the job from ever sleeping or make a
        threshold trivially always-pass.
        """
        positive_fields = (
            "paper_interval_seconds",
            "paper_exit_on_signal_expiry_hours",
            "paper_min_trades_for_stats",
            "paper_confidence_reference_score",
            "risk_max_open_positions",
            "calibration_min_samples_per_bucket",
            "calibration_window_days",
        )
        for name in positive_fields:
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")

        non_negative_fields = (
            "paper_entry_delay_seconds",
            "paper_slippage_k",
            "paper_min_position_notional_usd",
            "paper_min_traders",
            "paper_min_weighted_score",
            "paper_min_combined_value_usd",
            "paper_min_liquidity_usd",
        )
        for name in non_negative_fields:
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value}")
        return self

    @model_validator(mode="after")
    def _validate_paper_confidence_multipliers(self) -> "Settings":
        """The CONFIDENCE_WEIGHTED sizing rule clamps into
        [min_multiplier, max_multiplier] - an inverted or negative range
        would make that clamp meaningless or crash at runtime.
        """
        if self.paper_confidence_min_multiplier <= 0:
            raise ValueError(
                "paper_confidence_min_multiplier must be positive, "
                f"got {self.paper_confidence_min_multiplier}"
            )
        if self.paper_confidence_max_multiplier < self.paper_confidence_min_multiplier:
            raise ValueError(
                "paper_confidence_max_multiplier "
                f"({self.paper_confidence_max_multiplier}) must be >= "
                f"paper_confidence_min_multiplier ({self.paper_confidence_min_multiplier})"
            )
        return self

    @model_validator(mode="after")
    def _validate_paper_sizing_rule(self) -> "Settings":
        valid = {"FIXED_FRACTION", "CONFIDENCE_WEIGHTED"}
        if self.paper_sizing_rule not in valid:
            raise ValueError(
                f"paper_sizing_rule must be one of {sorted(valid)}, got {self.paper_sizing_rule!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_risk_default_sizer(self) -> "Settings":
        valid = {"FIXED", "TIERED", "KELLY"}
        if self.risk_default_sizer not in valid:
            raise ValueError(
                f"risk_default_sizer must be one of {sorted(valid)}, "
                f"got {self.risk_default_sizer!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_risk_kelly_fraction(self) -> "Settings":
        """Never above half Kelly (docs/PHASE5_DESIGN.md section 5) - full
        Kelly carries roughly an f-chance of drawing down to an f-fraction of
        bankroll, and 2x full Kelly has zero expected long-run growth despite
        positive-EV bets, so this is a hard ceiling, not just a default.
        """
        if self.risk_kelly_fraction > Decimal("0.5"):
            raise ValueError(
                f"risk_kelly_fraction must never exceed 0.5 (half Kelly), "
                f"got {self.risk_kelly_fraction}"
            )
        return self

    @model_validator(mode="after")
    def _validate_bands(self) -> "Settings":
        """Every band list must be non-empty and each (min, max) pair must
        have min < max - an inverted or empty band list would make
        fraction_for()/bucket lookups silently fall through to nothing.
        """
        two_tuple_fields = (
            "calibration_trader_bands",
            "calibration_score_bands",
            "calibration_price_bands",
        )
        for name in two_tuple_fields:
            bands = getattr(self, name)
            if not bands:
                raise ValueError(f"{name} must not be empty")
            for low, high in bands:
                if low >= high:
                    raise ValueError(f"{name} band ({low}, {high}) must have min < max")

        if not self.risk_tiered_score_bands:
            raise ValueError("risk_tiered_score_bands must not be empty")
        for low, high, fraction_pct in self.risk_tiered_score_bands:
            if low >= high:
                raise ValueError(
                    f"risk_tiered_score_bands band ({low}, {high}) must have min < max"
                )
            if not (Decimal("0") <= fraction_pct <= Decimal("1")):
                raise ValueError(
                    "risk_tiered_score_bands fraction_pct must be within "
                    f"[0, 1], got {fraction_pct}"
                )
        return self

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
    def log_to_file(self) -> bool:
        """Explicit LOG_TO_FILE wins; otherwise off in production, on elsewhere."""
        if self.log_to_file_override is not None:
            return self.log_to_file_override
        return self.environment != "production"

    @property
    def leaderboard_periods(self) -> list[str]:
        """leaderboard_time_periods split into a list, e.g. ["MONTH", "ALL"]."""
        return [p.strip() for p in self.leaderboard_time_periods.split(",") if p.strip()]

    @property
    def database_url(self) -> str:
        """Railway's DATABASE_URL if set, else built from POSTGRES_* fields.

        Railway's URL uses the plain "postgresql://" scheme, which
        SQLAlchemy would hand to psycopg2 (not installed) rather than our
        psycopg3 driver - rewritten to "postgresql+psycopg://" unless
        that's already there.

        quote_plus on the built-from-parts path prevents special characters
        (@, :, /, etc.) in the password from being misparsed as URL
        structure.
        """
        if self.database_url_override:
            url = self.database_url_override
            if "+psycopg" not in url:
                url = url.replace("postgresql://", "postgresql+psycopg://", 1)
            return url

        if self.postgres_password is None:
            raise RuntimeError(
                "POSTGRES_PASSWORD is required when DATABASE_URL is not set "
                "(local dev needs one or the other, not neither)"
            )
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance so .env is parsed once per process."""
    return Settings()

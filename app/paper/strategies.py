"""Strategy configurations — single source of truth for all paper-trading strategies.

Each strategy is a named set of parameters that override the global defaults.
Adding a strategy here is sufficient to start running it: the seed script
discovers strategies from this module and creates/updates PaperPortfolio rows.

See docs/PHASE4_DESIGN.md sections 1 and 10 for the parameter model and which
fields are portfolio-overridable.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class StrategyConfig:
    """One strategy's full parameter set — clean names, not paper_* prefixes.

    Every field has a default matching the global Settings default, so a
    strategy only sets the values it wants to diverge on.
    """

    name: str

    # --- Entry filters (engine.py EntryFilters) ---
    min_traders: int = 3
    min_score: Decimal = Decimal("1.0")
    min_combined_value: Decimal = Decimal("500")
    min_liquidity: Decimal = Decimal("5000")
    max_spread: Decimal = Decimal("0.05")

    # --- Sizer selection (docs/PHASE5_DESIGN.md section 6) ---
    # FIXED keeps sizing_rule below in play; TIERED/KELLY use app/risk/
    # sizing_tiered.py / sizing_kelly.py instead and ignore sizing_rule.
    sizer: str = "FIXED"

    # --- Adaptive cluster weighting (docs/PHASE6_DESIGN.md workstream 5) ---
    # When True, the entry filter's weighted_score check uses a live,
    # bandit-adjusted recomputation from the signal's own contributors
    # (app/optimization/bandit.py's effective_weighted_score()) instead of
    # the signal's stored weighted_score - see engine.py's _run_entries.
    use_adaptive_weighting: bool = False

    # --- Sizing (sizing.py SizingConfig) - only used when sizer=FIXED ---
    sizing_rule: str = "FIXED_FRACTION"
    bet_size: Decimal = Decimal("0.02")
    confidence_base_fraction: Decimal = Decimal("0.02")
    confidence_reference_score: Decimal = Decimal("1.0")
    confidence_min_multiplier: Decimal = Decimal("0.5")
    confidence_max_multiplier: Decimal = Decimal("2.0")
    max_position_notional_pct: Decimal = Decimal("0.10")
    max_total_exposure_pct: Decimal = Decimal("0.60")
    min_position_notional: Decimal = Decimal("1")

    # --- Fill model (fills.py FillConfig) ---
    entry_delay_seconds: int = 30
    slippage_k: Decimal = Decimal("0.5")
    slippage_max: Decimal = Decimal("0.15")
    no_delayed_snapshot_penalty: Decimal = Decimal("0.05")
    max_entry_price_drift: Decimal = Decimal("0.15")

    # --- Exits (engine.py ExitConfig) ---
    take_profit: Decimal = Decimal("0.30")
    stop_loss: Decimal = Decimal("0.20")
    exit_on_signal_expiry_hours: int = 72

    def to_params(self) -> dict:
        """Convert to JSONB-compatible dict matching engine's paper_* keys.

        Strings for Decimal values so JSON serialisation doesn't lose
        precision — engine's _get_param coerces via Decimal(str(raw)).
        """
        return {
            "paper_sizer": self.sizer,
            "paper_use_adaptive_weighting": self.use_adaptive_weighting,
            "paper_min_traders": self.min_traders,
            "paper_min_weighted_score": str(self.min_score),
            "paper_min_combined_value_usd": str(self.min_combined_value),
            "paper_min_liquidity_usd": str(self.min_liquidity),
            "paper_max_spread": str(self.max_spread),
            "paper_sizing_rule": self.sizing_rule,
            "paper_fixed_fraction_pct": str(self.bet_size),
            "paper_confidence_base_fraction_pct": str(self.confidence_base_fraction),
            "paper_confidence_reference_score": str(self.confidence_reference_score),
            "paper_confidence_min_multiplier": str(self.confidence_min_multiplier),
            "paper_confidence_max_multiplier": str(self.confidence_max_multiplier),
            "paper_max_position_notional_pct": str(self.max_position_notional_pct),
            "paper_max_total_exposure_pct": str(self.max_total_exposure_pct),
            "paper_min_position_notional_usd": str(self.min_position_notional),
            "paper_entry_delay_seconds": self.entry_delay_seconds,
            "paper_slippage_k": str(self.slippage_k),
            "paper_slippage_max": str(self.slippage_max),
            "paper_no_delayed_snapshot_penalty": str(self.no_delayed_snapshot_penalty),
            "paper_max_entry_price_drift": str(self.max_entry_price_drift),
            "paper_take_profit_pct": str(self.take_profit),
            "paper_stop_loss_pct": str(self.stop_loss),
            "paper_exit_on_signal_expiry_hours": self.exit_on_signal_expiry_hours,
        }


STRATEGIES: list[StrategyConfig] = [
    StrategyConfig(
        name="baseline",
        min_traders=3,
        min_score=Decimal("1.0"),
        min_liquidity=Decimal("5000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.30"),
        stop_loss=Decimal("0.20"),
    ),
    StrategyConfig(
        # Identical to baseline in every entry-filter/exit dimension except
        # sizer - the point is a head-to-head comparison against baseline
        # on the identical signal stream, isolating whether calibrated
        # Kelly sizing actually beats fixed sizing (docs/PHASE5_DESIGN.md
        # section 6).
        name="kelly",
        sizer="KELLY",
        min_traders=3,
        min_score=Decimal("1.0"),
        min_liquidity=Decimal("5000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.30"),
        stop_loss=Decimal("0.20"),
    ),
    StrategyConfig(
        # Identical to baseline in every entry-filter/exit/sizer dimension
        # except adaptive weighting - isolates whether the Workstream 5
        # bandit's cluster multipliers actually improve on static consensus
        # weights, the same champion/challenger methodology "kelly" already
        # uses against "baseline" for sizing (docs/PHASE6_DESIGN.md
        # workstream 5).
        name="adaptive",
        use_adaptive_weighting=True,
        min_traders=3,
        min_score=Decimal("1.0"),
        min_liquidity=Decimal("5000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.30"),
        stop_loss=Decimal("0.20"),
    ),
    StrategyConfig(
        name="strict",
        min_traders=5,
        min_score=Decimal("2.0"),
        min_liquidity=Decimal("15000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.30"),
        stop_loss=Decimal("0.20"),
    ),
    StrategyConfig(
        name="conservative",
        min_traders=3,
        min_score=Decimal("1.0"),
        min_liquidity=Decimal("5000"),
        bet_size=Decimal("0.01"),
        take_profit=Decimal("0.15"),
        stop_loss=Decimal("0.10"),
    ),
    StrategyConfig(
        name="aggressive",
        min_traders=2,
        min_score=Decimal("0.8"),
        min_liquidity=Decimal("2000"),
        bet_size=Decimal("0.03"),
        take_profit=Decimal("0.40"),
        stop_loss=Decimal("0.25"),
    ),
    StrategyConfig(
        name="elite_traders",
        min_traders=2,
        min_score=Decimal("3.0"),
        min_liquidity=Decimal("20000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.30"),
        stop_loss=Decimal("0.20"),
    ),
    StrategyConfig(
        name="strong_consensus",
        min_traders=6,
        min_score=Decimal("1.5"),
        min_liquidity=Decimal("10000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.30"),
        stop_loss=Decimal("0.20"),
    ),
    StrategyConfig(
        name="momentum",
        min_traders=3,
        min_score=Decimal("1.0"),
        min_liquidity=Decimal("5000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.50"),
        stop_loss=Decimal("0.20"),
    ),
    StrategyConfig(
        name="scalper",
        min_traders=3,
        min_score=Decimal("1.0"),
        min_liquidity=Decimal("5000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.10"),
        stop_loss=Decimal("0.05"),
    ),
    StrategyConfig(
        name="whale_markets",
        min_traders=3,
        min_score=Decimal("1.0"),
        min_liquidity=Decimal("50000"),
        bet_size=Decimal("0.02"),
        take_profit=Decimal("0.30"),
        stop_loss=Decimal("0.20"),
    ),
    StrategyConfig(
        name="tiny_risk",
        min_traders=3,
        min_score=Decimal("1.0"),
        min_liquidity=Decimal("5000"),
        bet_size=Decimal("0.005"),
        take_profit=Decimal("0.30"),
        stop_loss=Decimal("0.20"),
    ),
]

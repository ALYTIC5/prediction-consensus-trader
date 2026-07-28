"""Position sizing for paper trades - pure, no DB, no network.

See docs/PHASE4_DESIGN.md section 3. Produces a target dollar notional, not
a share count - the fill model (app/paper/fills.py) needs that notional as
an input to its slippage formula, and the actual share count can only be
computed once the fill price is known, so sizing always runs first.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.config.settings import Settings


class SizingSkipReason(StrEnum):
    """Why size_position declined to size a trade at all."""

    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"


@dataclass(frozen=True)
class SizingRequest:
    """current_exposure is the portfolio's open cost basis, read fresh
    (see docs/PHASE4_DESIGN.md flag 6) - the caller is responsible for
    updating it between calls within the same cycle, not this module.
    """

    current_bankroll: Decimal
    current_exposure: Decimal
    weighted_score: Decimal


@dataclass(frozen=True)
class SizingConfig:
    """Every sizing tunable, bundled once - see docs/PHASE4_DESIGN.md
    section 10 for what each one means and its global default.
    """

    sizing_rule: str
    fixed_fraction_pct: Decimal
    confidence_base_fraction_pct: Decimal
    confidence_reference_score: Decimal
    confidence_min_multiplier: Decimal
    confidence_max_multiplier: Decimal
    max_position_notional_pct: Decimal
    max_total_exposure_pct: Decimal
    min_position_notional_usd: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> "SizingConfig":
        return cls(
            sizing_rule=settings.paper_sizing_rule,
            fixed_fraction_pct=settings.paper_fixed_fraction_pct,
            confidence_base_fraction_pct=settings.paper_confidence_base_fraction_pct,
            confidence_reference_score=settings.paper_confidence_reference_score,
            confidence_min_multiplier=settings.paper_confidence_min_multiplier,
            confidence_max_multiplier=settings.paper_confidence_max_multiplier,
            max_position_notional_pct=settings.paper_max_position_notional_pct,
            max_total_exposure_pct=settings.paper_max_total_exposure_pct,
            min_position_notional_usd=settings.paper_min_position_notional_usd,
        )


@dataclass(frozen=True)
class SizingResult:
    target_notional: Decimal | None
    skipped_reason: SizingSkipReason | None


def _base_notional(request: SizingRequest, config: SizingConfig) -> Decimal:
    if config.sizing_rule == "CONFIDENCE_WEIGHTED":
        multiplier = request.weighted_score / config.confidence_reference_score
        multiplier = max(
            config.confidence_min_multiplier, min(multiplier, config.confidence_max_multiplier)
        )
        return request.current_bankroll * config.confidence_base_fraction_pct * multiplier

    # FIXED_FRACTION, and the default for any value that isn't a
    # recognized rule - Settings already validates the global default, but
    # a portfolio's own JSONB override isn't pydantic-validated, so an
    # unrecognized string fails safe to the simpler rule rather than raising.
    return request.current_bankroll * config.fixed_fraction_pct


def size_position(request: SizingRequest, config: SizingConfig) -> SizingResult:
    """Target notional for one candidate trade, after every cap.

    Caps applied in order: the sizing rule's own output, then the
    per-position ceiling, then whatever exposure headroom actually remains.
    A result that survives all three caps but still falls under
    min_position_notional_usd is skipped outright - a dust position isn't a
    meaningful simulation of anything.
    """
    target = _base_notional(request, config)

    target = min(target, request.current_bankroll * config.max_position_notional_pct)

    max_exposure = request.current_bankroll * config.max_total_exposure_pct
    headroom = max(Decimal("0"), max_exposure - request.current_exposure)
    target = min(target, headroom)

    if target < config.min_position_notional_usd:
        return SizingResult(target_notional=None, skipped_reason=SizingSkipReason.BELOW_MIN_NOTIONAL)

    return SizingResult(target_notional=target, skipped_reason=None)

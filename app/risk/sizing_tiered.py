"""Stage 2: tiered confidence sizing - pure, no DB, no network.

See docs/PHASE5_DESIGN.md section 3. An honest heuristic bridge, not
edge-optimal: it scales bankroll fraction with a signal's consensus
strength (weighted_score) via settings-driven bands, which is strictly
better than a flat fraction, but it doesn't measure edge the way Stage 3's
calibrated Kelly sizer will. This is the sizer for any TIERED portfolio, and
the automatic per-signal fallback for a KELLY portfolio whenever a signal's
calibration bucket is too sparse to trust (design section 6).
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class TieredSizingSkipReason(StrEnum):
    """Why size_tiered_position declined to size a trade at all."""

    BELOW_LOWEST_BAND = "BELOW_LOWEST_BAND"


@dataclass(frozen=True)
class TieredSizingRequest:
    current_bankroll: Decimal
    weighted_score: Decimal


@dataclass(frozen=True)
class TieredSizingConfig:
    """bands is (min_score, max_score, fraction_pct) triples, ascending and
    non-overlapping (risk_tiered_score_bands) - see fraction_for() for how a
    score maps to a band. max_position_pct is the same hard ceiling
    RiskManager.apply()'s max_position_size rule enforces (docs/PHASE5_
    DESIGN.md flag 11); capping here too means this sizer's own output is
    never misleading in isolation (e.g. in tests or logs) even before
    RiskManager gets a chance to re-apply the identical cap.
    """

    bands: tuple[tuple[Decimal, Decimal, Decimal], ...]
    max_position_pct: Decimal
    # Whether a score below the lowest band's min_score sizes at that
    # band's floor fraction (True) or is skipped outright (False) - a score
    # this low shouldn't normally reach the paper engine at all (entry
    # filters already gate on a minimum weighted_score), so this only
    # matters if a portfolio sets its own filter looser than its own bands.
    floor_below_lowest_band: bool = True


@dataclass(frozen=True)
class TieredSizingResult:
    target_notional: Decimal | None
    fraction_used: Decimal | None
    skipped_reason: TieredSizingSkipReason | None


def fraction_for(
    weighted_score: Decimal, bands: tuple[tuple[Decimal, Decimal, Decimal], ...]
) -> Decimal | None:
    """Maps a score to its band's fraction, deterministically.

    Bands are half-open on the low end: [min_score, max_score) - a score
    sitting exactly on a shared edge (e.g. 1.5 between "1.0-1.5" and
    "1.5-2.0") resolves to the band it's the *floor* of, not the one it's
    the ceiling of. A score at or above the highest band's max_score uses
    that top band's fraction (bands are configured with an effectively
    unbounded top edge, e.g. 999999, so this only matters if a portfolio
    overrides them with a finite one). None means below the lowest band's
    min_score - the caller decides floor-or-skip.
    """
    if weighted_score < bands[0][0]:
        return None
    for min_score, max_score, fraction_pct in bands:
        if min_score <= weighted_score < max_score:
            return fraction_pct
    return bands[-1][2]


def size_tiered_position(
    request: TieredSizingRequest, config: TieredSizingConfig
) -> TieredSizingResult:
    """Target notional for one candidate trade: bankroll * band fraction,
    capped at the per-position ceiling. No exposure/market/correlated caps
    here - those live in RiskManager.apply() uniformly for every sizer
    (docs/PHASE5_DESIGN.md flag 11).
    """
    fraction = fraction_for(request.weighted_score, config.bands)

    if fraction is None:
        if not config.floor_below_lowest_band:
            return TieredSizingResult(
                target_notional=None,
                fraction_used=None,
                skipped_reason=TieredSizingSkipReason.BELOW_LOWEST_BAND,
            )
        fraction = config.bands[0][2]

    target = request.current_bankroll * fraction
    target = min(target, request.current_bankroll * config.max_position_pct)

    return TieredSizingResult(target_notional=target, fraction_used=fraction, skipped_reason=None)

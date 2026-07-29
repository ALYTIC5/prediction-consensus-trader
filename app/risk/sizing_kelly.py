"""Stage 3b: fractional Kelly sizing - pure, no DB, no network.

See docs/PHASE5_DESIGN.md section 5. Given a calibrated p_hat (never
invented - it comes from app/risk/calibration.py's get_p_hat(), measured
from this project's own resolved trades) and the candidate's price c,
computes the binary-market Kelly fraction, applies the quarter-Kelly
(default) scaling, and never sizes a trade at all when there's no edge.

c is signal.average_entry_price, not paper_trades.entry_price - sizing
runs before the fill model determines the actual slippage-adjusted entry
price (app/paper/sizing.py's own docstring: "sizing always runs first"), so
the realized fill price doesn't exist yet at sizing time. This corrects
docs/PHASE5_DESIGN.md flag 4's assumption that entry_price is available
here; average_entry_price is the only price known before compute_fill runs,
and it's also the exact price calibration.py buckets by, keeping "the price
a signal was measured at" and "the price Kelly sizes against" the same
number.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class KellySizingSkipReason(StrEnum):
    """Why size_kelly_position declined to size a trade at all - Kelly's
    own formula saying there's no profitable edge at this price, not a data
    problem (a sparse calibration bucket is handled entirely by the caller
    falling back to Stage 2 before this function is ever called).
    """

    NO_EDGE = "NO_EDGE"


@dataclass(frozen=True)
class KellySizingRequest:
    current_bankroll: Decimal
    p_hat: Decimal
    fill_price: Decimal


@dataclass(frozen=True)
class KellySizingConfig:
    """kelly_fraction and max_position_pct mirror Settings.risk_kelly_fraction
    (validated <= 0.5) and risk_max_position_pct - the same hard ceiling
    RiskManager.apply()'s max_position_size rule enforces (flag 11); capping
    here too keeps this sizer's own output self-consistent in isolation.
    fee_pct defaults to 0 (docs/PHASE5_DESIGN.md flag 4 - no confirmed
    Polymarket fee schedule) and is the only upward adjustment made to c;
    slippage needs none since c is already slippage-adjusted by construction.
    """

    kelly_fraction: Decimal
    fee_pct: Decimal
    max_position_pct: Decimal


@dataclass(frozen=True)
class KellySizingResult:
    target_notional: Decimal | None
    kelly_fraction_used: Decimal | None
    edge: Decimal | None
    skipped_reason: KellySizingSkipReason | None


def size_kelly_position(
    request: KellySizingRequest, config: KellySizingConfig
) -> KellySizingResult:
    """f = (p_hat - c) / (1 - c), the standard binary-market Kelly fraction
    for a bet that pays $1 per share if the held outcome wins, bought at
    price c. edge is always returned (even on a skip) so "there was no edge
    here" is answerable from the row without recomputing it (docs/PHASE5_
    DESIGN.md section 8).
    """
    c_adjusted = request.fill_price * (1 + config.fee_pct)
    edge = request.p_hat - c_adjusted

    # c_adjusted >= 1 means paying at or above certain-win price - no
    # profitable price for f to be computed against at all.
    if c_adjusted >= 1:
        return KellySizingResult(
            target_notional=None,
            kelly_fraction_used=None,
            edge=edge,
            skipped_reason=KellySizingSkipReason.NO_EDGE,
        )

    f_kelly = edge / (1 - c_adjusted)
    if f_kelly <= 0:
        return KellySizingResult(
            target_notional=None,
            kelly_fraction_used=None,
            edge=edge,
            skipped_reason=KellySizingSkipReason.NO_EDGE,
        )

    kelly_fraction_used = f_kelly * config.kelly_fraction
    raw_notional = request.current_bankroll * kelly_fraction_used
    capped = min(raw_notional, request.current_bankroll * config.max_position_pct)

    return KellySizingResult(
        target_notional=capped,
        kelly_fraction_used=kelly_fraction_used,
        edge=edge,
        skipped_reason=None,
    )

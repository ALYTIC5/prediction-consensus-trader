"""SCALP exit rule - pure, no DB, no network.

The "greed" portfolio's entire reason to exist: same signal stream, same
entry filters as "baseline" (docs/PHASE4_DESIGN.md section 1's
comparability guarantee - every portfolio sees the identical signal
stream), the ONLY difference is this exit rule - a clean A/B test of exit
philosophy (hold for a real move vs. scalp small, frequent wins).

Every price this module compares against a threshold MUST already be
pessimistic (bid-side minus slippage, never mid/last-trade) - scalp
margins are a few cents, so execution cost eats a much larger share of a
scalp's edge than it does baseline's much wider take-profit/stop-loss
bands. The pessimistic price is computed by the caller
(app/paper/engine.py's _pessimistic_exit_price, reusing app/paper/fills.py's
own slippage model on the sell side instead of the buy side) - this module
only ever receives an already-adjusted number, never a raw current_price.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class ScalpExitReason(StrEnum):
    """Why a SCALP-configured trade closed - stored in
    paper_trades.exit_reason, same "first match wins, plain string, no
    migration needed for a new value" convention as
    app/paper/engine.py's ExitReason.
    """

    TAKE_PROFIT = "scalp_take_profit"
    BREAKEVEN = "scalp_breakeven"
    MAX_HOLD = "scalp_max_hold"


@dataclass(frozen=True)
class ScalpExitConfig:
    take_profit: Decimal  # SCALP_TAKE_PROFIT
    breakeven_tolerance: Decimal  # SCALP_BREAKEVEN_TOLERANCE
    max_hold_hours: int  # SCALP_MAX_HOLD_HOURS


def check_scalp_exit(
    entry_price: Decimal,
    pessimistic_price: Decimal | None,
    entered_at: datetime,
    now: datetime,
    config: ScalpExitConfig,
) -> ScalpExitReason | None:
    """First matching condition wins, in the task's own priority order:
    take any small profit the instant it's there, then take a bounce-back-
    to-near-even instead of risking a bigger loss, then the max-hold
    backstop if neither ever fires. None means stay open.

    pessimistic_price is optional: the price-based checks (take-profit,
    breakeven) simply don't fire on a cycle where no pessimistic reference
    is available yet (no market/liquidity data - see
    _pessimistic_exit_price) - but MAX_HOLD_HOURS is a true backstop, time-
    only, and fires regardless, so a trade can never get stuck open forever
    just because price data went missing.
    """
    if pessimistic_price is not None:
        gain_per_share = pessimistic_price - entry_price
        if gain_per_share >= config.take_profit:
            return ScalpExitReason.TAKE_PROFIT

        loss_per_share = -gain_per_share
        if Decimal(0) < loss_per_share <= config.breakeven_tolerance:
            return ScalpExitReason.BREAKEVEN

    if now - entered_at > timedelta(hours=config.max_hold_hours):
        return ScalpExitReason.MAX_HOLD

    return None

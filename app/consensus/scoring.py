"""Trader scoring - pure functions, no DB, no network.

See docs/PHASE3_DESIGN.md section 1 for the full rationale. A caller
elsewhere (app/consensus/scorer.py) loads leaderboard_snapshots and hands
this module plain numbers; nothing here touches the database.
"""

from dataclasses import dataclass
from decimal import Decimal

_LOG_SATURATION_DIVISOR = Decimal(6)  # log10($1,000,000 + 1) / 6 ~= 1.0


@dataclass(frozen=True)
class ScoreInputs:
    """Raw numbers behind one wallet's score, already aggregated from
    leaderboard_snapshots for the lookback window.
    """

    month_pnl: Decimal
    all_pnl: Decimal
    appearances: int
    possible_appearances: int


@dataclass(frozen=True)
class ScoreWeights:
    """How much each component counts toward the final score. Must sum to
    1.0 - Settings validates this at load time (see app/config/settings.py).
    """

    month: Decimal
    all_time: Decimal
    consistency: Decimal


@dataclass(frozen=True)
class ScoreBreakdown:
    """A wallet's score, plus the three components that produced it - kept
    together so callers can log or store the full breakdown, not just the
    final number.
    """

    month_component: Decimal
    all_time_component: Decimal
    consistency_component: Decimal
    score: Decimal


def _log_normalize(pnl: Decimal) -> Decimal:
    """Squash a dollar pnl figure into [0, 1] on a log scale.

    Trading pnl is heavy-tailed: a handful of wallets can post 100x the pnl
    of an otherwise-excellent trader on pure position size, not skill. A
    linear scale would let those outliers dominate consensus weight
    completely. Log-scaling flattens that - it takes real money to move the
    score at all, but a $5M window and a $50M window score the same,
    because past a point more dollars doesn't mean more predictive skill.
    A loss or breakeven window floors at 0 rather than an undefined log.
    """
    floored = max(pnl, Decimal(0))
    normalized = (floored + 1).log10() / _LOG_SATURATION_DIVISOR
    return min(max(normalized, Decimal(0)), Decimal(1))


def _consistency(appearances: int, possible_appearances: int) -> Decimal:
    """Fraction of leaderboard capture cycles this wallet showed up in.

    Catches a wallet that appears once after a single lucky trade and then
    vanishes - a one-week wonder scores low here even with a huge headline
    pnl number, which pulls its blended score down. Capped at 1.0 (a wallet
    seen in every cycle scores full marks regardless of pnl).
    """
    if possible_appearances <= 0:
        return Decimal(0)
    return min(Decimal(appearances) / Decimal(possible_appearances), Decimal(1))


def compute_score(inputs: ScoreInputs, weights: ScoreWeights) -> ScoreBreakdown:
    """Blend recency (month), credibility (all-time), and reliability
    (consistency) into one [0, 1] weight - a wallet's vote strength in the
    consensus engine.
    """
    month_component = _log_normalize(inputs.month_pnl)
    all_time_component = _log_normalize(inputs.all_pnl)
    consistency_component = _consistency(inputs.appearances, inputs.possible_appearances)

    score = (
        weights.month * month_component
        + weights.all_time * all_time_component
        + weights.consistency * consistency_component
    )

    return ScoreBreakdown(
        month_component=month_component,
        all_time_component=all_time_component,
        consistency_component=consistency_component,
        score=score,
    )

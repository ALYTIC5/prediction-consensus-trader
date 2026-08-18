"""Coherence arbitrage detection - pure, no DB, no network.

See docs/COHERENCE_DESIGN.md for the full rationale. Every function here
takes plain order-book levels and fee rates already resolved by the
caller, and returns a RawViolation only when the NET profit (after
walking the book for a common fillable size on every leg, and after fees
on every leg) clears min_edge - a gross top-of-book spread alone is never
enough, per the design doc's "Non-negotiable: fills and fees first".
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.paper.fees import compute_taker_fee
from app.paper.fills import BookLevel


@dataclass(frozen=True)
class LegQuote:
    """One outcome token's current book state - the input every detector
    function below needs for one leg of a candidate violation.
    """

    condition_id: str
    asset: str
    outcome: str
    ask_levels: Sequence[BookLevel]
    best_ask: Decimal | None
    best_bid: Decimal | None
    fee_rate: Decimal


@dataclass(frozen=True)
class RawViolation:
    """One detected violation, sized and net-priced (or not, for a
    detect-only type - see `captured` semantics in
    docs/COHERENCE_DESIGN.md). `legs` is already the JSONB-ready shape.
    `opportunity_key` is computed once here (while the original LegQuote
    objects are still in scope) rather than recomputed downstream from the
    already-flattened `legs` dicts.
    """

    type: str
    opportunity_key: str
    legs: list[dict]
    gross_spread: Decimal
    size: Decimal | None
    net_profit: Decimal | None
    required_capital: Decimal | None


def opportunity_key(violation_type: str, legs: Sequence[LegQuote]) -> str:
    """Stable identity across scan cycles - type plus the sorted
    (condition_id, asset) pairs involved, hashed. Two scans that find the
    same violation (same type, same legs) must produce the identical key
    so app/coherence/scan.py can UPDATE the existing open row instead of
    inserting a duplicate - same content-hash idiom as
    app/optimization/clustering.py's cluster_id_for().
    """
    parts = sorted(f"{leg.condition_id}:{leg.asset}" for leg in legs)
    joined = f"{violation_type}|" + ",".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _max_shares_within_depth_cap(
    book_side: Sequence[BookLevel], max_depth_fraction: Decimal
) -> Decimal | None:
    """Max shares fillable on this side within max_depth_fraction of its
    total visible depth - the size cap one leg alone allows before its own
    price would move too much to trust, same depth-cap semantics as
    app/paper/fills.py's walk_the_book, just expressed as a share count
    instead of stopping once a dollar budget is spent.
    """
    if not book_side:
        return None
    total_depth_usd = sum((lvl.price * lvl.size for lvl in book_side), Decimal(0))
    if total_depth_usd <= 0:
        return None
    budget = max_depth_fraction * total_depth_usd
    shares = Decimal(0)
    spent = Decimal(0)
    for level in book_side:
        level_usd = level.price * level.size
        if spent + level_usd <= budget:
            shares += level.size
            spent += level_usd
            continue
        remaining_budget = budget - spent
        if remaining_budget > 0:
            shares += remaining_budget / level.price
        break
    return shares if shares > 0 else None


def walk_book_for_shares(
    book_side: Sequence[BookLevel], shares: Decimal, max_depth_fraction: Decimal
) -> Decimal | None:
    """Size-weighted average price to acquire an EXACT share count - the
    share-denominated analogue of app/paper/fills.py's walk_the_book
    (which is dollar-denominated). A coherence trade must buy the same
    share count on every leg - one full unit of the hedge - not the same
    dollar amount on each, which would leave share counts mismatched and
    the payoff no longer actually guaranteed. None if shares exceeds what
    this side can supply within the depth cap.
    """
    if shares <= 0 or not book_side:
        return None
    max_shares = _max_shares_within_depth_cap(book_side, max_depth_fraction)
    if max_shares is None or shares > max_shares:
        return None
    remaining = shares
    cost = Decimal(0)
    for level in book_side:
        if remaining <= level.size:
            cost += remaining * level.price
            remaining = Decimal(0)
            break
        cost += level.size * level.price
        remaining -= level.size
    if remaining > 0:
        return None
    return cost / shares


def common_fillable_size(legs: Sequence[LegQuote], max_depth_fraction: Decimal) -> Decimal | None:
    """The binding size across every leg: the smallest max-fillable-shares
    among them, since a partial hedge (fewer shares on one leg than
    another) isn't a guaranteed payoff at all.
    """
    per_leg_max = [_max_shares_within_depth_cap(leg.ask_levels, max_depth_fraction) for leg in legs]
    if any(m is None for m in per_leg_max):
        return None
    return min(per_leg_max)


def detect_outcome_set_violation(
    legs: Sequence[LegQuote],
    min_edge: Decimal,
    max_depth_fraction: Decimal,
    bid_type: str,
    ask_type: str,
) -> list[RawViolation]:
    """The general form of both scan 1 (YES+NO sum, a 2-leg set) and scan
    2 (multi-outcome consistency, an N-leg set) - see
    docs/COHERENCE_DESIGN.md: both are "a mutually exclusive, collectively
    exhaustive set of outcomes must sum to $1.00", just at different set
    sizes, so there is one function, not two.

    Returns 0, 1, or 2 violations: the ask-direction (actionable - net
    profit computed by walking every leg for the common fillable size and
    subtracting fees) and/or the bid-direction (detect-only - no shorting
    infrastructure exists anywhere in this project, so it's reported at
    top-of-book only, never sized or net-priced, and the caller must never
    mark it captured).
    """
    violations: list[RawViolation] = []

    asks = [leg.best_ask for leg in legs]
    if all(a is not None for a in asks):
        ask_sum = sum(asks, Decimal(0))
        gross_spread = Decimal(1) - ask_sum
        if gross_spread > min_edge:
            size = common_fillable_size(legs, max_depth_fraction)
            net_profit: Decimal | None = None
            required_capital: Decimal | None = None
            if size is not None:
                avg_prices = [
                    walk_book_for_shares(leg.ask_levels, size, max_depth_fraction) for leg in legs
                ]
                if all(p is not None for p in avg_prices):
                    fees = [
                        compute_taker_fee(price, size, leg.fee_rate)
                        for price, leg in zip(avg_prices, legs, strict=True)
                    ]
                    required_capital = size * sum(avg_prices, Decimal(0))
                    payout = size  # exactly one leg pays $1/share at resolution
                    net_profit = payout - required_capital - sum(fees, Decimal(0))
                else:
                    size = None
            violations.append(
                RawViolation(
                    type=ask_type,
                    opportunity_key=opportunity_key(ask_type, legs),
                    legs=[
                        {
                            "condition_id": leg.condition_id,
                            "asset": leg.asset,
                            "outcome": leg.outcome,
                            "side": "ask",
                        }
                        for leg in legs
                    ],
                    gross_spread=gross_spread,
                    size=size,
                    net_profit=net_profit,
                    required_capital=required_capital,
                )
            )

    bids = [leg.best_bid for leg in legs]
    if all(b is not None for b in bids):
        bid_sum = sum(bids, Decimal(0))
        gross_spread = bid_sum - Decimal(1)
        if gross_spread > min_edge:
            violations.append(
                RawViolation(
                    type=bid_type,
                    opportunity_key=opportunity_key(bid_type, legs),
                    legs=[
                        {
                            "condition_id": leg.condition_id,
                            "asset": leg.asset,
                            "outcome": leg.outcome,
                            "side": "bid",
                        }
                        for leg in legs
                    ],
                    gross_spread=gross_spread,
                    size=None,
                    net_profit=None,
                    required_capital=None,
                )
            )

    return violations


def detect_nested_logic_violation(
    general_leg: LegQuote, primary_leg: LegQuote, min_edge: Decimal
) -> RawViolation | None:
    """P(wins general) <= P(wins primary) must hold (winning the
    prerequisite is necessary for winning the dependent event). A
    violation is ask_general > ask_primary + min_edge, using best_ask as
    the tradeable probability proxy on both sides (see
    docs/COHERENCE_DESIGN.md's scan 3 for why this is conservative but not
    a true midpoint, and why pair identification here is explicitly weak
    enough that this is detect-only, never auto-traded).
    """
    if general_leg.best_ask is None or primary_leg.best_ask is None:
        return None
    gross_spread = general_leg.best_ask - primary_leg.best_ask
    if gross_spread <= min_edge:
        return None
    return RawViolation(
        type="NESTED_LOGIC",
        opportunity_key=opportunity_key("NESTED_LOGIC", [general_leg, primary_leg]),
        legs=[
            {
                "condition_id": general_leg.condition_id,
                "asset": general_leg.asset,
                "outcome": general_leg.outcome,
                "side": "ask",
            },
            {
                "condition_id": primary_leg.condition_id,
                "asset": primary_leg.asset,
                "outcome": primary_leg.outcome,
                "side": "ask",
            },
        ],
        gross_spread=gross_spread,
        size=None,
        net_profit=None,
        required_capital=None,
    )

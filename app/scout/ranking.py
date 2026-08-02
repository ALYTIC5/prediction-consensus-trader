"""Copy-list ranking: crowdedness folded in as a soft penalty - see
docs/SCOUT_DESIGN.md's Crowdedness section (flag 12).

Mirrors app/optimization/crowdedness.py's hidden_alpha_score() shape
exactly: subtract, clamp, never a hard cutoff - a validated wallet with
heavy following still ranks respectably, never zeroed out on crowdedness
alone. Reads wallet_crowdedness directly (Phase 6 workstream 7 already
ships it in this codebase) - the design doc's fallback proxy (public
leaderboard rank + follower-reaction) is documented but not implemented,
since both are already exactly what crowdedness's own components measure.
The goal, stated in the design doc: a copy list of HIDDEN validated skill,
not FAMOUS validated skill.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import WalletCrowdedness


def scout_rank_score(
    wilson_low: Decimal, avg_forward_clv: Decimal, crowdedness: Decimal, penalty_weight: Decimal
) -> Decimal:
    """Combines the Stage-1 historical Wilson lower bound with current
    forward CLV into one ranking number, penalized by crowdedness.
    wilson_low is a [0,1] probability; avg_forward_clv is a price delta
    (typically small, roughly [-0.2, 0.2]) - summed directly rather than
    weighted, since the design doc names no relative weighting between
    them (documented here, not hidden, exactly as the design doc's Output
    section promises). Floored at 0 - a rank score is never negative, it's
    simply a low rank.
    """
    raw = wilson_low + avg_forward_clv - penalty_weight * crowdedness
    return max(raw, Decimal(0))


def crowdedness_by_wallet_id(session: Session, wallet_ids: set[int]) -> dict[int, Decimal]:
    """wallet_id -> current crowdedness, read straight from
    wallet_crowdedness (Phase 6 workstream 7) - a wallet absent from this
    map (never computed yet) gets 0 crowdedness at the call site, same "no
    penalty without evidence" convention hidden_alpha_weighted_score
    already uses. Keyed by wallet_id, not address (the Scout works in
    wallet_id space throughout, unlike the address-keyed consensus-facing
    code) - a direct query, not app/optimization/crowdedness.py's
    address-keyed crowdedness_by_address().
    """
    if not wallet_ids:
        return {}
    rows = session.execute(
        select(WalletCrowdedness.wallet_id, WalletCrowdedness.crowdedness).where(
            WalletCrowdedness.wallet_id.in_(wallet_ids)
        )
    ).all()
    return dict(rows)

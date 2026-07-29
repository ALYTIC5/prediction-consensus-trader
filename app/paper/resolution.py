"""Market resolution inference - pure, no DB, no network.

Split out of app/paper/engine.py so app/optimization/clv.py (and anything
else that needs "did this market resolve WON/LOST yet") can import it
without creating a circular import back through engine.py, which itself
imports app/optimization/bandit.py -> app/optimization/clv.py for the
Workstream 5 adaptive-weighting wiring (docs/PHASE6_DESIGN.md workstream 5).
"""

from decimal import Decimal
from enum import StrEnum


class ResolutionOutcome(StrEnum):
    """See docs/PHASE4_DESIGN.md section 5 - the fallback price-inference
    rule and its documented failure mode.
    """

    NOT_RESOLVED = "NOT_RESOLVED"
    WON = "WON"
    LOST = "LOST"
    AMBIGUOUS = "AMBIGUOUS"


def detect_resolution(
    closed: bool, latest_price: Decimal | None, threshold: Decimal
) -> ResolutionOutcome:
    """Fallback resolution rule (docs/PHASE4_DESIGN.md section 5, pending
    the doc's own flag 1 - verify whether Gamma exposes an authoritative
    settlement field before this becomes the primary rule instead of a
    fallback). AMBIGUOUS is a real, honest answer, not an error - the
    caller leaves the trade open and logs a warning rather than guess.
    """
    if not closed:
        return ResolutionOutcome.NOT_RESOLVED
    if latest_price is None:
        return ResolutionOutcome.AMBIGUOUS
    if latest_price >= 1 - threshold:
        return ResolutionOutcome.WON
    if latest_price <= threshold:
        return ResolutionOutcome.LOST
    return ResolutionOutcome.AMBIGUOUS

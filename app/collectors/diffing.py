"""Pure diffing logic for wallet position changes - no DB, no network.

Kept free of I/O so it's trivially unit-testable in isolation; a caller
elsewhere loads previous_sizes from the DB and current positions from
PolymarketClient, then hands both to diff_positions.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.collectors.schemas import PositionEntry

# API-reported sizes carry rounding noise (e.g. 6361.352700000001 vs
# 6361.3527) that isn't a real position change - any size delta smaller than
# this is treated as unchanged, not an INCREASED/DECREASED event.
SIZE_EPSILON = Decimal("0.000001")


@dataclass(frozen=True)
class PositionEvent:
    """One detected change to a wallet's stake in one outcome token."""

    event_type: str
    asset: str
    condition_id: str
    size_before: Decimal
    size_after: Decimal
    avg_price: Decimal
    cur_price: Decimal
    outcome: str
    title: str


def diff_positions(
    previous_sizes: dict[str, Decimal], current: list[PositionEntry]
) -> list[PositionEvent]:
    """Compare previously known sizes (by asset) against a fresh snapshot."""
    events: list[PositionEvent] = []
    seen_assets: set[str] = set()

    for position in current:
        seen_assets.add(position.asset)
        before = previous_sizes.get(position.asset)

        if before is None:
            events.append(
                PositionEvent(
                    event_type="OPENED",
                    asset=position.asset,
                    condition_id=position.condition_id,
                    size_before=Decimal("0"),
                    size_after=position.size,
                    avg_price=position.avg_price,
                    cur_price=position.cur_price,
                    outcome=position.outcome,
                    title=position.title,
                )
            )
            continue

        delta = position.size - before
        if delta > SIZE_EPSILON:
            event_type = "INCREASED"
        elif delta < -SIZE_EPSILON:
            event_type = "DECREASED"
        else:
            continue

        events.append(
            PositionEvent(
                event_type=event_type,
                asset=position.asset,
                condition_id=position.condition_id,
                size_before=before,
                size_after=position.size,
                avg_price=position.avg_price,
                cur_price=position.cur_price,
                outcome=position.outcome,
                title=position.title,
            )
        )

    # Assets previous_sizes knew about but that dropped out of the current
    # snapshot entirely - closed positions. previous_sizes only carries
    # size, no condition_id/price/outcome/title, so those fields are empty:
    # this function genuinely doesn't have that data for a closed asset.
    for asset, before in previous_sizes.items():
        if asset in seen_assets:
            continue
        events.append(
            PositionEvent(
                event_type="CLOSED",
                asset=asset,
                condition_id="",
                size_before=before,
                size_after=Decimal("0"),
                avg_price=Decimal("0"),
                cur_price=Decimal("0"),
                outcome="",
                title="",
            )
        )

    return events

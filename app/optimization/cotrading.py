"""Co-trading graph construction - pure graph logic + a thin DB loader.

See docs/PHASE6_DESIGN.md workstream 1. build_cotrading_edges() takes
plain events and returns edges - no DB, no network, fully unit-testable.
load_cotrade_events() is the only piece that touches a session, and does
nothing but shape rows into CoTradeEvent - same split every other
orchestration module in this project uses (app/paper/sizing.py,
app/risk/calibration.py, ...).

Only OPENED events count (design flag 5) - "opened the same outcome" is
the prompt's own wording, and a fresh open is a stronger, less noisy
independence signal than a top-up of an existing position. Bootstrap
events are excluded for a more basic reason: a bootstrap row's detected_at
is when THIS PROJECT first observed the position, not when the wallet
actually opened it - many wallets' pre-existing positions get bootstrapped
in the same collector run, which would manufacture false near-simultaneous
co-occurrences between wallets that have never actually traded together.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PositionHistory, Wallet


@dataclass(frozen=True)
class CoTradeEvent:
    """One wallet's OPENED event on one outcome token - the only fields
    the graph-building logic needs.
    """

    wallet_id: int
    condition_id: str
    asset: str
    detected_at: datetime


@dataclass(frozen=True)
class CoTradingConfig:
    window_minutes: int
    min_shared_markets: int


@dataclass(frozen=True)
class CoTradeEdge:
    """One pair of wallets that co-traded enough to be graph-worthy.

    weight is the total number of qualifying co-occurrence instances
    (can exceed shared_markets if a pair co-traded the same market's
    opposite outcome, or reopened, more than once); shared_markets is the
    distinct condition_id count that actually gates whether this edge
    exists at all (docs/PHASE6_DESIGN.md workstream 1: "coincidentally
    piling into one popular market together" must not be enough on its own).
    """

    wallet_a: int
    wallet_b: int
    weight: int
    shared_markets: int


def build_cotrading_edges(events: list[CoTradeEvent], config: CoTradingConfig) -> list[CoTradeEdge]:
    """Groups events by (condition_id, asset) - the same outcome token -
    and pairs up every two wallets in a group whose OPENED events landed
    within config.window_minutes of each other. An edge is only returned
    once a pair has done this across at least config.min_shared_markets
    distinct markets - not just once, and not just on the same market
    twice (see the module docstring and CoTradeEdge's docstring for why).
    """
    window = timedelta(minutes=config.window_minutes)

    groups: dict[tuple[str, str], list[CoTradeEvent]] = defaultdict(list)
    for event in events:
        groups[(event.condition_id, event.asset)].append(event)

    # pair_key -> {"instances": int, "markets": set[condition_id]}
    pair_stats: dict[tuple[int, int], dict] = {}

    for (condition_id, _asset), group_events in groups.items():
        n = len(group_events)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = group_events[i], group_events[j]
                if a.wallet_id == b.wallet_id:
                    continue
                if abs(a.detected_at - b.detected_at) > window:
                    continue
                pair_key = (min(a.wallet_id, b.wallet_id), max(a.wallet_id, b.wallet_id))
                stats = pair_stats.setdefault(pair_key, {"instances": 0, "markets": set()})
                stats["instances"] += 1
                stats["markets"].add(condition_id)

    edges = []
    for (wallet_a, wallet_b), stats in pair_stats.items():
        shared_markets = len(stats["markets"])
        if shared_markets >= config.min_shared_markets:
            edges.append(
                CoTradeEdge(
                    wallet_a=wallet_a,
                    wallet_b=wallet_b,
                    weight=stats["instances"],
                    shared_markets=shared_markets,
                )
            )
    return edges


def load_cotrade_events(session: Session) -> list[CoTradeEvent]:
    """Every fresh, non-bootstrap OPENED event from a currently-tracked
    wallet. The graph is rebuilt from scratch on every clustering run
    (docs/PHASE6_DESIGN.md workstream 1), so this loads full history, not
    a recent window - PositionHistory is small enough at this project's
    volumes for a full rebuild to stay simple and self-correcting.
    """
    rows = session.execute(
        select(
            PositionHistory.wallet_id,
            PositionHistory.condition_id,
            PositionHistory.asset,
            PositionHistory.detected_at,
        )
        .join(Wallet, PositionHistory.wallet_id == Wallet.id)
        .where(
            PositionHistory.event_type == "OPENED",
            PositionHistory.is_bootstrap.is_(False),
            Wallet.is_tracked.is_(True),
        )
    ).all()
    return [
        CoTradeEvent(
            wallet_id=row.wallet_id,
            condition_id=row.condition_id,
            asset=row.asset,
            detected_at=row.detected_at,
        )
        for row in rows
    ]

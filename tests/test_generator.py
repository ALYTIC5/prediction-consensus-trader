"""Unit tests for app/signals/generator.py's _build_groups - constructs its
inputs as plain in-memory ORM instances (no DB session involved), same as
every other orchestration-layer function this project keeps thin enough to
test without a database.

Covers docs/PHASE6_DESIGN.md workstream 3's consensus impact: a
contributor's weight resolves to that wallet's score IN THE SIGNAL'S OWN
MARKET CATEGORY, not a flat global score.
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.db.models import Market, PositionHistory, Wallet
from app.signals.generator import _build_groups

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _wallet(wallet_id: int, address: str) -> Wallet:
    wallet = Wallet(address=address)
    wallet.id = wallet_id
    return wallet


def _market(condition_id: str, category: str) -> Market:
    market = Market(condition_id=condition_id, category=category, event_slug="evt")
    return market


def _event(wallet_id: int, condition_id: str, asset: str = "asset1") -> PositionHistory:
    return PositionHistory(
        wallet_id=wallet_id,
        asset=asset,
        condition_id=condition_id,
        event_type="OPENED",
        size_before=None,
        size_after=Decimal("100"),
        avg_price=Decimal("0.5"),
        cur_price=Decimal("0.5"),
        outcome="Yes",
        title="Some market",
        detected_at=NOW,
    )


def test_contributor_weight_is_the_category_correct_score():
    """A sports-specialist wallet contributes its HIGH sports score to a
    sports signal and its LOW politics score to a politics signal - the
    consensus engine picks the category-correct score for each.
    """
    wallets = {1: _wallet(1, "0xa")}
    markets = {
        "0xsports": _market("0xsports", "Sports"),
        "0xpolitics": _market("0xpolitics", "Politics"),
    }
    category_scores = {
        (1, "Sports"): Decimal("0.8"),
        (1, "Politics"): Decimal("0.1"),
    }
    events = [_event(1, "0xsports"), _event(1, "0xpolitics")]

    groups = _build_groups(events, wallets, category_scores, markets)
    weight_by_condition = {g.condition_id: g.events[0].weight for g in groups}

    assert weight_by_condition["0xsports"] == Decimal("0.8")
    assert weight_by_condition["0xpolitics"] == Decimal("0.1")


def test_missing_category_score_falls_back_to_low_neutral_not_global():
    """A wallet with no row at all for (wallet, category) - scoring hasn't
    run yet, or this category has no population data - falls back to 0,
    never some other, unrelated global score.
    """
    wallets = {1: _wallet(1, "0xa")}
    markets = {"0xcrypto": _market("0xcrypto", "Crypto")}
    category_scores: dict[tuple[int, str], Decimal] = {}  # nothing computed yet
    events = [_event(1, "0xcrypto")]

    groups = _build_groups(events, wallets, category_scores, markets)
    assert groups[0].events[0].weight == Decimal("0")


def test_unknown_market_defaults_to_other_category():
    """A condition_id with no corresponding Market row (not yet synced)
    still produces a group - category defaults to "Other" rather than
    crashing the whole cycle over one unsynced market.
    """
    wallets = {1: _wallet(1, "0xa")}
    markets: dict[str, Market] = {}
    category_scores = {(1, "Other"): Decimal("0.3")}
    events = [_event(1, "0xunknown")]

    groups = _build_groups(events, wallets, category_scores, markets)
    assert groups[0].events[0].weight == Decimal("0.3")


def test_event_from_an_unknown_wallet_is_dropped():
    wallets: dict[int, Wallet] = {}
    markets = {"0xsports": _market("0xsports", "Sports")}
    events = [_event(1, "0xsports")]

    groups = _build_groups(events, wallets, {}, markets)
    assert groups == []

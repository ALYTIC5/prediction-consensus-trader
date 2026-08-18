"""Tests for app/coherence/detector.py - pure, no DB, no network."""

from decimal import Decimal

from app.coherence.detector import (
    LegQuote,
    detect_nested_logic_violation,
    detect_outcome_set_violation,
    walk_book_for_shares,
)
from app.paper.fills import BookLevel

MIN_EDGE = Decimal("0.01")
MAX_DEPTH = Decimal("0.20")


def _leg(
    condition_id, asset, outcome, best_ask, ask_size=Decimal("1000"), fee_rate=Decimal("0.05")
):
    ask_levels = [BookLevel(price=best_ask, size=ask_size)] if best_ask is not None else []
    return LegQuote(
        condition_id=condition_id,
        asset=asset,
        outcome=outcome,
        ask_levels=ask_levels,
        best_ask=best_ask,
        best_bid=None,
        fee_rate=fee_rate,
    )


# --- walk_book_for_shares ---


def test_walk_book_for_shares_single_level_matches_hand_computation():
    book = [BookLevel(price=Decimal("0.40"), size=Decimal("500"))]
    avg = walk_book_for_shares(book, Decimal("100"), MAX_DEPTH)
    assert avg == Decimal("0.40")


def test_walk_book_for_shares_spans_two_levels():
    book = [
        BookLevel(price=Decimal("0.40"), size=Decimal("50")),
        BookLevel(price=Decimal("0.42"), size=Decimal("500")),
    ]
    avg = walk_book_for_shares(book, Decimal("100"), MAX_DEPTH)
    expected_cost = Decimal("50") * Decimal("0.40") + Decimal("50") * Decimal("0.42")
    assert avg == expected_cost / Decimal("100")


def test_walk_book_for_shares_exceeds_depth_cap_returns_none():
    # Total depth 100 shares * 0.5 = $50; depth cap 20% = $10 = 20 shares.
    book = [BookLevel(price=Decimal("0.50"), size=Decimal("100"))]
    assert walk_book_for_shares(book, Decimal("25"), MAX_DEPTH) is None
    assert walk_book_for_shares(book, Decimal("15"), MAX_DEPTH) is not None


# --- YES+NO sum (2-leg outcome set) ---


def test_book_summing_under_one_minus_fees_is_flagged():
    """0.45 + 0.45 = 0.90, well under 1.00 - min_edge: flagged, net
    profit positive after fees.
    """
    legs = [
        _leg("0xa", "yes-token", "Yes", Decimal("0.45")),
        _leg("0xa", "no-token", "No", Decimal("0.45")),
    ]
    violations = detect_outcome_set_violation(
        legs, MIN_EDGE, MAX_DEPTH, bid_type="YES_NO_SUM_BID", ask_type="YES_NO_SUM_ASK"
    )
    ask_violations = [v for v in violations if v.type == "YES_NO_SUM_ASK"]
    assert len(ask_violations) == 1
    v = ask_violations[0]
    assert v.gross_spread == Decimal("0.10")
    assert v.net_profit is not None
    assert v.net_profit > 0


def test_book_summing_above_one_is_not_flagged_on_ask_side():
    """0.55 + 0.55 = 1.10 - no ask-side arb (buying both costs more than $1)."""
    legs = [
        _leg("0xa", "yes-token", "Yes", Decimal("0.55")),
        _leg("0xa", "no-token", "No", Decimal("0.55")),
    ]
    violations = detect_outcome_set_violation(
        legs, MIN_EDGE, MAX_DEPTH, bid_type="YES_NO_SUM_BID", ask_type="YES_NO_SUM_ASK"
    )
    assert [v for v in violations if v.type == "YES_NO_SUM_ASK"] == []


def test_book_summing_within_min_edge_of_one_is_not_flagged():
    """0.495 + 0.500 = 0.995 - a real but tiny gap, smaller than min_edge (0.01)."""
    legs = [
        _leg("0xa", "yes-token", "Yes", Decimal("0.495")),
        _leg("0xa", "no-token", "No", Decimal("0.500")),
    ]
    violations = detect_outcome_set_violation(
        legs, MIN_EDGE, MAX_DEPTH, bid_type="YES_NO_SUM_BID", ask_type="YES_NO_SUM_ASK"
    )
    assert violations == []


def test_net_profit_subtracts_book_walk_cost_and_fees():
    """Hand-computed: two legs at 0.40 each (ask), depth deep enough that
    the common fillable size is bounded by the depth cap on each leg.
    size = min(max_shares_leg1, max_shares_leg2); with equal books,
    max_shares = max_depth_fraction * total_depth_usd / price =
    0.20 * (0.40*1000) / 0.40 = 200 shares.
    required_capital = size * (0.40 + 0.40) = 200 * 0.80 = 160.
    payout = size = 200. fee per leg = compute_taker_fee(0.40, 200, 0.05)
    = 200 * 0.05 * 0.40 * 0.60 = 2.4; two legs = 4.8.
    net = 200 - 160 - 4.8 = 35.2.
    """
    legs = [
        _leg("0xa", "yes-token", "Yes", Decimal("0.40")),
        _leg("0xa", "no-token", "No", Decimal("0.40")),
    ]
    violations = detect_outcome_set_violation(
        legs, MIN_EDGE, MAX_DEPTH, bid_type="YES_NO_SUM_BID", ask_type="YES_NO_SUM_ASK"
    )
    (v,) = [x for x in violations if x.type == "YES_NO_SUM_ASK"]
    assert v.size == Decimal("200")
    assert v.required_capital == Decimal("160.00")
    assert v.net_profit == Decimal("35.20")


def test_bid_direction_detected_but_never_sized_or_net_priced():
    """Bids summing above 1.00: flagged for the record, but not
    actionable (no shorting infrastructure) - size/net_profit stay None.
    """
    legs = [
        LegQuote("0xa", "yes-token", "Yes", [], None, Decimal("0.55"), Decimal("0.05")),
        LegQuote("0xa", "no-token", "No", [], None, Decimal("0.55"), Decimal("0.05")),
    ]
    violations = detect_outcome_set_violation(
        legs, MIN_EDGE, MAX_DEPTH, bid_type="YES_NO_SUM_BID", ask_type="YES_NO_SUM_ASK"
    )
    (v,) = [x for x in violations if x.type == "YES_NO_SUM_BID"]
    assert v.gross_spread == Decimal("0.10")
    assert v.size is None
    assert v.net_profit is None


# --- multi-outcome (N-leg set), same function, more legs ---


def test_multi_outcome_set_evaluated_as_a_group():
    """4 mutually exclusive outcomes at 0.20 each = 0.80, well under 1.00 -
    flagged as one group violation involving all 4 legs, not per-pair.
    """
    legs = [_leg("0xa", f"token-{i}", f"Candidate{i}", Decimal("0.20")) for i in range(4)]
    violations = detect_outcome_set_violation(
        legs, MIN_EDGE, MAX_DEPTH, bid_type="MULTI_OUTCOME_BID", ask_type="MULTI_OUTCOME_ASK"
    )
    (v,) = [x for x in violations if x.type == "MULTI_OUTCOME_ASK"]
    assert len(v.legs) == 4
    assert v.gross_spread == Decimal("0.20")
    assert v.net_profit is not None
    assert v.net_profit > 0


def test_multi_outcome_set_not_flagged_when_it_sums_to_one():
    legs = [_leg("0xa", f"token-{i}", f"Candidate{i}", Decimal("0.25")) for i in range(4)]
    violations = detect_outcome_set_violation(
        legs, MIN_EDGE, MAX_DEPTH, bid_type="MULTI_OUTCOME_BID", ask_type="MULTI_OUTCOME_ASK"
    )
    assert violations == []


def test_thin_leg_caps_common_size_across_whole_group():
    """One leg has far less depth than the others - the common fillable
    size must be bounded by that thin leg, not the deep ones.
    """
    deep = LegQuote(
        "0xa",
        "deep-token",
        "Deep",
        [BookLevel(Decimal("0.20"), Decimal("10000"))],
        Decimal("0.20"),
        None,
        Decimal("0.05"),
    )
    thin = LegQuote(
        "0xa",
        "thin-token",
        "Thin",
        [BookLevel(Decimal("0.20"), Decimal("10"))],
        Decimal("0.20"),
        None,
        Decimal("0.05"),
    )
    others = [_leg("0xa", f"token-{i}", f"Candidate{i}", Decimal("0.20")) for i in range(2)]
    violations = detect_outcome_set_violation(
        [deep, thin] + others,
        MIN_EDGE,
        MAX_DEPTH,
        bid_type="MULTI_OUTCOME_BID",
        ask_type="MULTI_OUTCOME_ASK",
    )
    (v,) = [x for x in violations if x.type == "MULTI_OUTCOME_ASK"]
    # thin leg's own depth cap: 0.20 * (0.20*10) = 0.4 USD / 0.20 = 2 shares.
    assert v.size == Decimal("2")


# --- nested/conditional logic (detect-only) ---


def test_nested_logic_violation_detected_when_general_priced_above_primary():
    general = _leg("0xgeneral", "yes-token", "Yes", Decimal("0.60"))
    primary = _leg("0xprimary", "yes-token", "Yes", Decimal("0.45"))
    v = detect_nested_logic_violation(general, primary, MIN_EDGE)
    assert v is not None
    assert v.type == "NESTED_LOGIC"
    assert v.gross_spread == Decimal("0.15")
    # Never auto-traded - always unsized, unpriced.
    assert v.size is None
    assert v.net_profit is None


def test_nested_logic_not_flagged_when_constraint_holds():
    general = _leg("0xgeneral", "yes-token", "Yes", Decimal("0.30"))
    primary = _leg("0xprimary", "yes-token", "Yes", Decimal("0.45"))
    assert detect_nested_logic_violation(general, primary, MIN_EDGE) is None

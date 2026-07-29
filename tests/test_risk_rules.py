"""Exhaustive unit tests for app/risk/rules.py and app/risk/manager.py - pure,
no DB, no network.

This is the layer that decides whether a paper-trading portfolio blows up or
survives, so boundaries get dedicated tests, not just a happy-path check:
exactly-at-cap vs. one-cent-over, exactly-zero-headroom vs. exactly-fits, and
the precedence rules the manager applies when multiple guardrails fire at
once (docs/PHASE5_DESIGN.md flags 1-2). Every DENY/RESIZE decision is also
checked for the "exactly one loggable risk_events row" contract (flag 10):
rule/reason are populated so app/paper/engine.py can persist a single row
from the decision alone, and ALLOW decisions carry nothing to log.
"""

from decimal import Decimal

import pytest

from app.risk.manager import RiskManager
from app.risk.rules import (
    OpenPosition,
    PortfolioState,
    ProposedTrade,
    RiskAction,
    RiskRule,
    RiskRuleConfig,
    rule_daily_stop_loss,
    rule_emergency_stop,
    rule_max_correlated_exposure,
    rule_max_market_exposure,
    rule_max_open_positions,
    rule_max_position_size,
    rule_max_total_exposure,
    rule_min_bankroll_halt,
)

DEFAULT_CONFIG = RiskRuleConfig(
    max_position_pct=Decimal("0.10"),
    max_position_size_enabled=True,
    max_exposure_pct=Decimal("0.60"),
    max_total_exposure_enabled=True,
    max_market_exposure_pct=Decimal("0.20"),
    max_market_exposure_enabled=True,
    max_correlated_exposure_pct=Decimal("0.30"),
    max_correlated_exposure_enabled=True,
    daily_stop_loss_pct=Decimal("0.10"),
    daily_stop_loss_enabled=True,
    max_open_positions=10,
    max_open_positions_enabled=True,
    min_bankroll_pct=Decimal("0.20"),
    min_bankroll_halt_enabled=True,
    emergency_stop_active=False,
)


def _config(**overrides) -> RiskRuleConfig:
    fields = DEFAULT_CONFIG.__dict__.copy()
    fields.update(overrides)
    return RiskRuleConfig(**fields)


def _state(
    current_bankroll: Decimal = Decimal("1000"),
    starting_bankroll: Decimal = Decimal("1000"),
    day_start_bankroll: Decimal = Decimal("1000"),
    realized_pnl_today: Decimal = Decimal("0"),
    open_positions: tuple[OpenPosition, ...] = (),
    open_count: int | None = None,
) -> PortfolioState:
    return PortfolioState(
        current_bankroll=current_bankroll,
        starting_bankroll=starting_bankroll,
        day_start_bankroll=day_start_bankroll,
        realized_pnl_today=realized_pnl_today,
        open_positions=open_positions,
        open_count=open_count if open_count is not None else len(open_positions),
    )


def _trade(
    condition_id: str = "0xaaa",
    event_slug: str | None = "event-a",
    notional: Decimal | None = None,
) -> ProposedTrade:
    return ProposedTrade(condition_id=condition_id, event_slug=event_slug, notional=notional)


def _assert_loggable(decision) -> None:
    """The risk_events-row contract (flag 10): non-ALLOW carries a rule and
    a non-empty reason (so exactly one row can be built from the decision
    alone); ALLOW carries neither (so no row is built at all).
    """
    if decision.action == RiskAction.ALLOW:
        assert decision.rule is None
        assert decision.reason == ""
    else:
        assert decision.rule is not None
        assert decision.reason != ""
        assert isinstance(decision.detail, dict)


# --- max_position_size ---


def test_max_position_at_cap_allows():
    state = _state(current_bankroll=Decimal("1000"))
    decision = rule_max_position_size(state, _trade(notional=Decimal("100")), DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW
    _assert_loggable(decision)


def test_max_position_one_cent_over_resizes_to_cap():
    state = _state(current_bankroll=Decimal("1000"))
    decision = rule_max_position_size(state, _trade(notional=Decimal("100.01")), DEFAULT_CONFIG)
    assert decision.action == RiskAction.RESIZE
    assert decision.resized_notional == Decimal("100")
    assert decision.rule == RiskRule.MAX_POSITION_SIZE
    _assert_loggable(decision)


def test_max_position_size_disabled_allows_anything():
    config = _config(max_position_size_enabled=False)
    state = _state(current_bankroll=Decimal("1000"))
    decision = rule_max_position_size(state, _trade(notional=Decimal("999")), config)
    assert decision.action == RiskAction.ALLOW


# --- max_total_exposure ---


def test_max_total_exposure_fits_exactly_allows():
    # cap = 600, used = 400, headroom = 200
    positions = (OpenPosition("0xbbb", "event-b", Decimal("400")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    decision = rule_max_total_exposure(state, _trade(notional=Decimal("200")), DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW
    _assert_loggable(decision)


def test_max_total_exposure_one_over_resizes_down_to_fit():
    positions = (OpenPosition("0xbbb", "event-b", Decimal("400")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    decision = rule_max_total_exposure(state, _trade(notional=Decimal("200.01")), DEFAULT_CONFIG)
    assert decision.action == RiskAction.RESIZE
    assert decision.resized_notional == Decimal("200")
    assert decision.rule == RiskRule.MAX_TOTAL_EXPOSURE
    _assert_loggable(decision)


def test_max_total_exposure_no_room_denies():
    # cap = 600, used = 600, headroom = 0
    positions = (OpenPosition("0xbbb", "event-b", Decimal("600")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    decision = rule_max_total_exposure(state, _trade(notional=Decimal("50")), DEFAULT_CONFIG)
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.MAX_TOTAL_EXPOSURE
    _assert_loggable(decision)


# --- max_market_exposure ---


def test_max_market_exposure_ignores_other_markets():
    # All exposure sits in a different condition_id - shouldn't count.
    positions = (OpenPosition("0xother", "event-x", Decimal("900")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    decision = rule_max_market_exposure(
        state, _trade(condition_id="0xtarget", notional=Decimal("50")), DEFAULT_CONFIG
    )
    assert decision.action == RiskAction.ALLOW


def test_max_market_exposure_concentration_resizes():
    # cap = 200, used in this market = 150, headroom = 50
    positions = (
        OpenPosition("0xtarget", "event-a", Decimal("90")),
        OpenPosition("0xtarget", "event-a", Decimal("60")),
    )
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    decision = rule_max_market_exposure(
        state, _trade(condition_id="0xtarget", notional=Decimal("60")), DEFAULT_CONFIG
    )
    assert decision.action == RiskAction.RESIZE
    assert decision.resized_notional == Decimal("50")
    assert decision.rule == RiskRule.MAX_MARKET_EXPOSURE
    _assert_loggable(decision)


def test_max_market_exposure_no_room_denies():
    # cap = 200, used = 200, headroom = 0
    positions = (OpenPosition("0xtarget", "event-a", Decimal("200")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    decision = rule_max_market_exposure(
        state, _trade(condition_id="0xtarget", notional=Decimal("10")), DEFAULT_CONFIG
    )
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.MAX_MARKET_EXPOSURE
    _assert_loggable(decision)


# --- max_correlated_exposure (shared event_slug) ---


def test_max_correlated_exposure_sums_across_markets_sharing_event_slug():
    # cap = 300, used across two different markets in the same event = 270,
    # headroom = 30
    positions = (
        OpenPosition("0xaaa", "world-cup-2026", Decimal("150")),
        OpenPosition("0xbbb", "world-cup-2026", Decimal("120")),
    )
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    trade = _trade(condition_id="0xccc", event_slug="world-cup-2026", notional=Decimal("30"))
    decision = rule_max_correlated_exposure(state, trade, DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW  # fits exactly


def test_max_correlated_exposure_concentration_resizes():
    positions = (
        OpenPosition("0xaaa", "world-cup-2026", Decimal("150")),
        OpenPosition("0xbbb", "world-cup-2026", Decimal("120")),
    )
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    trade = _trade(condition_id="0xccc", event_slug="world-cup-2026", notional=Decimal("30.01"))
    decision = rule_max_correlated_exposure(state, trade, DEFAULT_CONFIG)
    assert decision.action == RiskAction.RESIZE
    assert decision.resized_notional == Decimal("30")
    assert decision.rule == RiskRule.MAX_CORRELATED_EXPOSURE
    _assert_loggable(decision)


def test_max_correlated_exposure_no_room_denies():
    positions = (OpenPosition("0xaaa", "world-cup-2026", Decimal("300")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    trade = _trade(condition_id="0xbbb", event_slug="world-cup-2026", notional=Decimal("1"))
    decision = rule_max_correlated_exposure(state, trade, DEFAULT_CONFIG)
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.MAX_CORRELATED_EXPOSURE
    _assert_loggable(decision)


def test_max_correlated_exposure_different_event_slug_not_counted():
    positions = (OpenPosition("0xaaa", "world-cup-2026", Decimal("300")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    trade = _trade(condition_id="0xbbb", event_slug="us-election-2026", notional=Decimal("100"))
    decision = rule_max_correlated_exposure(state, trade, DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW


def test_max_correlated_exposure_no_event_slug_never_applies():
    # A trade with no event_slug can't be grouped into any correlated
    # bucket - the rule is a no-op for it regardless of concentration.
    positions = (OpenPosition("0xaaa", "world-cup-2026", Decimal("300")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    trade = _trade(condition_id="0xbbb", event_slug=None, notional=Decimal("9999"))
    decision = rule_max_correlated_exposure(state, trade, DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW


# --- daily_stop_loss ---


def test_daily_stop_loss_at_threshold_denies():
    # threshold = -10% of day_start_bankroll(1000) = -100
    state = _state(day_start_bankroll=Decimal("1000"), realized_pnl_today=Decimal("-100"))
    decision = rule_daily_stop_loss(state, _trade(), DEFAULT_CONFIG)
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.DAILY_STOP_LOSS
    _assert_loggable(decision)


def test_daily_stop_loss_just_above_threshold_allows():
    state = _state(day_start_bankroll=Decimal("1000"), realized_pnl_today=Decimal("-99.99"))
    decision = rule_daily_stop_loss(state, _trade(), DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW


def test_daily_stop_loss_ignores_open_positions_existing_positions_unaffected():
    """The rule only ever looks at realized_pnl_today/day_start_bankroll -
    it has no mechanism to touch already-open positions (those are marked
    and exited by app/paper/engine.py's _run_exits, which never calls
    RiskManager at all). Proven here by showing the decision is identical
    regardless of what's sitting in open_positions/open_count.
    """
    trade = _trade()
    empty = _state(
        day_start_bankroll=Decimal("1000"), realized_pnl_today=Decimal("-500"), open_positions=()
    )
    busy = _state(
        day_start_bankroll=Decimal("1000"),
        realized_pnl_today=Decimal("-500"),
        open_positions=(
            OpenPosition("0xaaa", "event-a", Decimal("300")),
            OpenPosition("0xbbb", "event-b", Decimal("250")),
        ),
    )
    decision_empty = rule_daily_stop_loss(empty, trade, DEFAULT_CONFIG)
    decision_busy = rule_daily_stop_loss(busy, trade, DEFAULT_CONFIG)
    assert decision_empty.action == decision_busy.action == RiskAction.DENY


def test_daily_stop_loss_resets_next_utc_day():
    """No state persists between evaluations - a fresh day's zeroed
    realized_pnl_today (what app/paper/engine.py's day_start filter
    produces once the UTC date rolls over) allows again immediately, even
    though yesterday's catastrophic loss denied.
    """
    trade = _trade()
    yesterday = _state(day_start_bankroll=Decimal("1000"), realized_pnl_today=Decimal("-1000"))
    today = _state(day_start_bankroll=Decimal("1200"), realized_pnl_today=Decimal("0"))
    assert rule_daily_stop_loss(yesterday, trade, DEFAULT_CONFIG).action == RiskAction.DENY
    assert rule_daily_stop_loss(today, trade, DEFAULT_CONFIG).action == RiskAction.ALLOW


def test_daily_stop_loss_disabled_allows():
    config = _config(daily_stop_loss_enabled=False)
    state = _state(day_start_bankroll=Decimal("1000"), realized_pnl_today=Decimal("-1000"))
    assert rule_daily_stop_loss(state, _trade(), config).action == RiskAction.ALLOW


# --- min_bankroll_halt ---


def test_min_bankroll_halt_at_floor_allows():
    # floor = 20% of starting_bankroll(1000) = 200
    state = _state(current_bankroll=Decimal("200"), starting_bankroll=Decimal("1000"))
    decision = rule_min_bankroll_halt(state, _trade(), DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW


def test_min_bankroll_halt_just_below_floor_denies():
    state = _state(current_bankroll=Decimal("199.99"), starting_bankroll=Decimal("1000"))
    decision = rule_min_bankroll_halt(state, _trade(), DEFAULT_CONFIG)
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.MIN_BANKROLL_HALT
    _assert_loggable(decision)


# --- emergency_stop ---


def test_emergency_stop_active_denies_regardless_of_portfolio_health():
    config = _config(emergency_stop_active=True)
    healthy_state = _state(current_bankroll=Decimal("10000"), starting_bankroll=Decimal("1000"))
    decision = rule_emergency_stop(healthy_state, _trade(), config)
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.EMERGENCY_STOP
    _assert_loggable(decision)


def test_emergency_stop_inactive_allows():
    decision = rule_emergency_stop(_state(), _trade(), DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW


# --- max_open_positions ---


def test_max_open_positions_at_cap_denies():
    state = _state(open_count=10)
    decision = rule_max_open_positions(state, _trade(), DEFAULT_CONFIG)
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.MAX_OPEN_POSITIONS
    _assert_loggable(decision)


def test_max_open_positions_one_below_cap_allows():
    state = _state(open_count=9)
    decision = rule_max_open_positions(state, _trade(), DEFAULT_CONFIG)
    assert decision.action == RiskAction.ALLOW


# --- RiskManager.pre_check: first-failure-wins, emergency_stop checked first ---


def test_pre_check_allows_when_everything_clear():
    manager = RiskManager(DEFAULT_CONFIG)
    decision = manager.pre_check(_state(), _trade())
    assert decision.action == RiskAction.ALLOW
    assert decision.rule is None


def test_pre_check_emergency_stop_wins_over_other_deny_causes():
    config = _config(emergency_stop_active=True)
    manager = RiskManager(config)
    # Also breaches max_open_positions - emergency_stop must still be the
    # one reported (docs/PHASE5_DESIGN.md section 2: checked first).
    state = _state(open_count=999)
    decision = manager.pre_check(state, _trade())
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.EMERGENCY_STOP


def test_pre_check_max_open_positions_denies_alone():
    manager = RiskManager(DEFAULT_CONFIG)
    decision = manager.pre_check(_state(open_count=10), _trade())
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.MAX_OPEN_POSITIONS


def test_pre_check_daily_stop_loss_denies_alone():
    manager = RiskManager(DEFAULT_CONFIG)
    state = _state(day_start_bankroll=Decimal("1000"), realized_pnl_today=Decimal("-100"))
    decision = manager.pre_check(state, _trade())
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.DAILY_STOP_LOSS


def test_pre_check_min_bankroll_halt_denies_alone():
    manager = RiskManager(DEFAULT_CONFIG)
    state = _state(current_bankroll=Decimal("100"), starting_bankroll=Decimal("1000"))
    decision = manager.pre_check(state, _trade())
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.MIN_BANKROLL_HALT


# --- RiskManager.apply: deny beats resize, minimum of resizes wins ---


def test_apply_allows_when_everything_clear():
    manager = RiskManager(DEFAULT_CONFIG)
    decision = manager.apply(_state(), _trade(notional=Decimal("50")))
    assert decision.action == RiskAction.ALLOW
    assert decision.rule is None


def test_apply_requires_notional():
    manager = RiskManager(DEFAULT_CONFIG)
    with pytest.raises(ValueError):
        manager.apply(_state(), _trade(notional=None))


def test_apply_deny_beats_resize():
    """max_position_size alone would only resize (cap=100), but
    max_total_exposure has zero headroom (used=600=cap) and denies - the
    deny must win outright over the resize (flag 2)."""
    manager = RiskManager(DEFAULT_CONFIG)
    positions = (OpenPosition("0xbbb", "event-b", Decimal("600")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    decision = manager.apply(state, _trade(condition_id="0xnew", notional=Decimal("500")))
    assert decision.action == RiskAction.DENY
    assert decision.rule == RiskRule.MAX_TOTAL_EXPOSURE
    _assert_loggable(decision)


def test_apply_takes_minimum_of_all_resizes():
    """max_position_size resizes to 100 (10% cap); a tighter per-market cap
    (custom config) resizes further down to 80 - the tighter one wins, not
    whichever rule happens to run first (flag 2: minimum of all proposed
    resizes)."""
    config = _config(max_market_exposure_pct=Decimal("0.09"))  # cap = 90
    manager = RiskManager(config)
    # 10 already used in this market -> headroom = 90 - 10 = 80
    positions = (OpenPosition("0xtarget", "event-a", Decimal("10")),)
    state = _state(current_bankroll=Decimal("1000"), open_positions=positions)
    decision = manager.apply(
        state, _trade(condition_id="0xtarget", event_slug=None, notional=Decimal("150"))
    )
    assert decision.action == RiskAction.RESIZE
    assert decision.resized_notional == Decimal("80")
    assert decision.rule == RiskRule.MAX_MARKET_EXPOSURE
    _assert_loggable(decision)


def test_apply_disabled_rules_never_fire():
    config = _config(
        max_position_size_enabled=False,
        max_total_exposure_enabled=False,
        max_market_exposure_enabled=False,
        max_correlated_exposure_enabled=False,
    )
    manager = RiskManager(config)
    decision = manager.apply(_state(), _trade(notional=Decimal("100000")))
    assert decision.action == RiskAction.ALLOW

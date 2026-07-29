"""Pure Stage 1 risk-guardrail rules - no DB, no network.

See docs/PHASE5_DESIGN.md sections 1-2. Every rule takes the same three
arguments (the portfolio's current state, the candidate trade, and the
resolved config) and returns a RiskDecision - deny-capable rules only ever
return ALLOW/DENY, resize-capable rules can also return RESIZE.
RiskManager (app/risk/manager.py) decides which rules run in which phase and
how their outputs combine; these functions know nothing about that ordering,
which is what keeps each one independently unit-testable.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class RiskAction(StrEnum):
    ALLOW = "ALLOW"
    RESIZE = "RESIZE"
    DENY = "DENY"


class RiskRule(StrEnum):
    """Machine-readable rule identifiers - stored in risk_events.rule and,
    on a denial, a MISSED paper_trades row's exit_reason (docs/PHASE5_
    DESIGN.md section 7).
    """

    EMERGENCY_STOP = "EMERGENCY_STOP"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    DAILY_STOP_LOSS = "DAILY_STOP_LOSS"
    MIN_BANKROLL_HALT = "MIN_BANKROLL_HALT"
    MAX_POSITION_SIZE = "MAX_POSITION_SIZE"
    MAX_TOTAL_EXPOSURE = "MAX_TOTAL_EXPOSURE"
    MAX_MARKET_EXPOSURE = "MAX_MARKET_EXPOSURE"
    MAX_CORRELATED_EXPOSURE = "MAX_CORRELATED_EXPOSURE"


@dataclass(frozen=True)
class OpenPosition:
    """One open position's contribution to the exposure totals below."""

    condition_id: str
    event_slug: str | None
    notional: Decimal


@dataclass(frozen=True)
class PortfolioState:
    """Everything a rule might need to know about a portfolio's current
    state - plain values the caller already loaded, no DB session here.
    day_start_bankroll/realized_pnl_today are derived by the caller (docs/
    PHASE5_DESIGN.md flag 3), not recomputed inside this package.
    """

    current_bankroll: Decimal
    starting_bankroll: Decimal
    day_start_bankroll: Decimal
    realized_pnl_today: Decimal
    open_positions: tuple[OpenPosition, ...]
    open_count: int


@dataclass(frozen=True)
class ProposedTrade:
    """The candidate being evaluated. notional is None during pre_check
    (before a sizer has proposed one) - deny-capable rules never look at
    it; resize-capable rules require it.
    """

    condition_id: str
    event_slug: str | None
    notional: Decimal | None = None


@dataclass(frozen=True)
class RiskRuleConfig:
    """Every Stage 1 threshold/toggle, resolved against portfolio params
    the same way app/paper/engine.py's PortfolioConfig is (docs/PHASE5_
    DESIGN.md section 9) - none hardcoded in app/risk/.
    """

    max_position_pct: Decimal
    max_position_size_enabled: bool
    max_exposure_pct: Decimal
    max_total_exposure_enabled: bool
    max_market_exposure_pct: Decimal
    max_market_exposure_enabled: bool
    max_correlated_exposure_pct: Decimal
    max_correlated_exposure_enabled: bool
    daily_stop_loss_pct: Decimal
    daily_stop_loss_enabled: bool
    max_open_positions: int
    max_open_positions_enabled: bool
    min_bankroll_pct: Decimal
    min_bankroll_halt_enabled: bool
    # Global-only (flag 9) - never portfolio-overridable, always the
    # fleet-wide adjustable-overrides value.
    emergency_stop_active: bool


@dataclass(frozen=True)
class RiskDecision:
    action: RiskAction
    resized_notional: Decimal | None
    rule: str | None
    reason: str
    detail: dict


def _allow() -> RiskDecision:
    return RiskDecision(
        action=RiskAction.ALLOW, resized_notional=None, rule=None, reason="", detail={}
    )


# --- Deny-capable rules (pre_check phase - no candidate size needed) ---


def rule_emergency_stop(
    state: PortfolioState, trade: ProposedTrade, config: RiskRuleConfig
) -> RiskDecision:
    """Global manual kill switch - checked first among deny-capable rules,
    since it's the one an operator reaches for when something's actively
    wrong (docs/PHASE5_DESIGN.md section 2).
    """
    if not config.emergency_stop_active:
        return _allow()
    return RiskDecision(
        action=RiskAction.DENY,
        resized_notional=None,
        rule=RiskRule.EMERGENCY_STOP,
        reason="Emergency stop is active - all new entries denied fleet-wide",
        detail={},
    )


def rule_max_open_positions(
    state: PortfolioState, trade: ProposedTrade, config: RiskRuleConfig
) -> RiskDecision:
    if not config.max_open_positions_enabled:
        return _allow()
    if state.open_count >= config.max_open_positions:
        return RiskDecision(
            action=RiskAction.DENY,
            resized_notional=None,
            rule=RiskRule.MAX_OPEN_POSITIONS,
            reason=(
                f"Open position count {state.open_count} at or above cap "
                f"{config.max_open_positions}"
            ),
            detail={"open_count": state.open_count, "cap": config.max_open_positions},
        )
    return _allow()


def rule_daily_stop_loss(
    state: PortfolioState, trade: ProposedTrade, config: RiskRuleConfig
) -> RiskDecision:
    if not config.daily_stop_loss_enabled:
        return _allow()
    threshold = -config.daily_stop_loss_pct * state.day_start_bankroll
    if state.realized_pnl_today <= threshold:
        return RiskDecision(
            action=RiskAction.DENY,
            resized_notional=None,
            rule=RiskRule.DAILY_STOP_LOSS,
            reason=(
                f"Today's realized PnL {state.realized_pnl_today} at or below stop-loss "
                f"threshold {threshold} (day-start bankroll {state.day_start_bankroll})"
            ),
            detail={
                "realized_pnl_today": str(state.realized_pnl_today),
                "threshold": str(threshold),
                "day_start_bankroll": str(state.day_start_bankroll),
                "daily_stop_loss_pct": str(config.daily_stop_loss_pct),
            },
        )
    return _allow()


def rule_min_bankroll_halt(
    state: PortfolioState, trade: ProposedTrade, config: RiskRuleConfig
) -> RiskDecision:
    if not config.min_bankroll_halt_enabled:
        return _allow()
    floor = config.min_bankroll_pct * state.starting_bankroll
    if state.current_bankroll < floor:
        return RiskDecision(
            action=RiskAction.DENY,
            resized_notional=None,
            rule=RiskRule.MIN_BANKROLL_HALT,
            reason=(
                f"Current bankroll {state.current_bankroll} below halt floor {floor} "
                f"(starting bankroll {state.starting_bankroll})"
            ),
            detail={
                "current_bankroll": str(state.current_bankroll),
                "floor": str(floor),
                "starting_bankroll": str(state.starting_bankroll),
                "min_bankroll_pct": str(config.min_bankroll_pct),
            },
        )
    return _allow()


PRE_CHECK_RULES = (
    rule_emergency_stop,
    rule_max_open_positions,
    rule_daily_stop_loss,
    rule_min_bankroll_halt,
)


# --- Resize-capable rules (apply phase - candidate size required) ---


def rule_max_position_size(
    state: PortfolioState, trade: ProposedTrade, config: RiskRuleConfig
) -> RiskDecision:
    if not config.max_position_size_enabled:
        return _allow()
    cap = config.max_position_pct * state.current_bankroll
    if trade.notional <= cap:
        return _allow()
    return RiskDecision(
        action=RiskAction.RESIZE,
        resized_notional=cap,
        rule=RiskRule.MAX_POSITION_SIZE,
        reason=f"Requested notional {trade.notional} exceeds per-position cap {cap}",
        detail={
            "requested": str(trade.notional),
            "cap": str(cap),
            "cap_pct": str(config.max_position_pct),
        },
    )


def _scoped_exposure_rule(
    trade: ProposedTrade,
    *,
    cap_pct: Decimal,
    current_bankroll: Decimal,
    enabled: bool,
    rule: RiskRule,
    used_notional: Decimal,
    label: str,
) -> RiskDecision:
    if not enabled:
        return _allow()
    cap = cap_pct * current_bankroll
    headroom = cap - used_notional
    if headroom <= 0:
        return RiskDecision(
            action=RiskAction.DENY,
            resized_notional=None,
            rule=rule,
            reason=(
                f"No {label} exposure headroom remaining (cap {cap}, already used {used_notional})"
            ),
            detail={"cap": str(cap), "used": str(used_notional), "requested": str(trade.notional)},
        )
    if trade.notional > headroom:
        return RiskDecision(
            action=RiskAction.RESIZE,
            resized_notional=headroom,
            rule=rule,
            reason=(
                f"Requested notional {trade.notional} exceeds remaining {label} exposure "
                f"headroom {headroom} (cap {cap}, already used {used_notional})"
            ),
            detail={
                "cap": str(cap),
                "used": str(used_notional),
                "requested": str(trade.notional),
                "headroom": str(headroom),
            },
        )
    return _allow()


def rule_max_total_exposure(
    state: PortfolioState, trade: ProposedTrade, config: RiskRuleConfig
) -> RiskDecision:
    used = sum((p.notional for p in state.open_positions), Decimal("0"))
    return _scoped_exposure_rule(
        trade,
        cap_pct=config.max_exposure_pct,
        current_bankroll=state.current_bankroll,
        enabled=config.max_total_exposure_enabled,
        rule=RiskRule.MAX_TOTAL_EXPOSURE,
        used_notional=used,
        label="total",
    )


def rule_max_market_exposure(
    state: PortfolioState, trade: ProposedTrade, config: RiskRuleConfig
) -> RiskDecision:
    used = sum(
        (p.notional for p in state.open_positions if p.condition_id == trade.condition_id),
        Decimal("0"),
    )
    return _scoped_exposure_rule(
        trade,
        cap_pct=config.max_market_exposure_pct,
        current_bankroll=state.current_bankroll,
        enabled=config.max_market_exposure_enabled,
        rule=RiskRule.MAX_MARKET_EXPOSURE,
        used_notional=used,
        label="market",
    )


def rule_max_correlated_exposure(
    state: PortfolioState, trade: ProposedTrade, config: RiskRuleConfig
) -> RiskDecision:
    if trade.event_slug is None:
        return _allow()
    used = sum(
        (p.notional for p in state.open_positions if p.event_slug == trade.event_slug),
        Decimal("0"),
    )
    return _scoped_exposure_rule(
        trade,
        cap_pct=config.max_correlated_exposure_pct,
        current_bankroll=state.current_bankroll,
        enabled=config.max_correlated_exposure_enabled,
        rule=RiskRule.MAX_CORRELATED_EXPOSURE,
        used_notional=used,
        label="correlated-event",
    )


APPLY_RULES = (
    rule_max_position_size,
    rule_max_total_exposure,
    rule_max_market_exposure,
    rule_max_correlated_exposure,
)

"""RiskManager: Stage 1 guardrail orchestration - pure, no DB, no network.

See docs/PHASE5_DESIGN.md sections 1-2 (flag 1). Takes plain PortfolioState/
ProposedTrade/RiskRuleConfig values in and returns a RiskDecision out, same
shape as app/paper/engine.py's passes_entry_filters/check_exit. Runs twice
per candidate: pre_check before a sizer has proposed a notional (deny-only,
first-failure-wins), apply after (resize-capable, evaluates every applicable
rule and takes the tightest). The caller (app/paper/engine.py) persists a
risk_events row for every non-ALLOW decision, mirroring how it already
persists a MISSED paper_trades row from other pure decision functions'
output - this module never touches a session.
"""

from app.risk.rules import (
    APPLY_RULES,
    PRE_CHECK_RULES,
    PortfolioState,
    ProposedTrade,
    RiskAction,
    RiskDecision,
    RiskRuleConfig,
)


def _allow_decision() -> RiskDecision:
    return RiskDecision(
        action=RiskAction.ALLOW, resized_notional=None, rule=None, reason="", detail={}
    )


class RiskManager:
    """Evaluates Stage 1 rules against one resolved per-portfolio config."""

    def __init__(self, config: RiskRuleConfig) -> None:
        self.config = config

    def pre_check(self, state: PortfolioState, trade: ProposedTrade) -> RiskDecision:
        """Deny-only gate, no candidate size needed - first-failure-wins,
        in rule order (docs/PHASE5_DESIGN.md flag 2).
        """
        for rule_fn in PRE_CHECK_RULES:
            decision = rule_fn(state, trade, self.config)
            if decision.action != RiskAction.ALLOW:
                return decision
        return _allow_decision()

    def apply(self, state: PortfolioState, trade: ProposedTrade) -> RiskDecision:
        """Resize-capable gate, candidate size required. Every applicable
        rule is evaluated; any DENY wins outright regardless of what any
        resize would have allowed, otherwise the minimum of all proposed
        resizes is the final decision (flag 2).
        """
        if trade.notional is None:
            raise ValueError("apply() requires trade.notional - pre_check runs before sizing")

        decisions = [rule_fn(state, trade, self.config) for rule_fn in APPLY_RULES]

        for decision in decisions:
            if decision.action == RiskAction.DENY:
                return decision

        resizing = [d for d in decisions if d.action == RiskAction.RESIZE]
        if not resizing:
            return _allow_decision()

        return min(resizing, key=lambda d: d.resized_notional)

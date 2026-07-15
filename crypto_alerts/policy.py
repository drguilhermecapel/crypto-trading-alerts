"""Fail-closed advisory policy; this module never submits or authorizes orders."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import RiskConfig

HARD_MAX_HOLDINGS = 5
HARD_MAX_ASSET_WEIGHT = 0.40
HARD_MAX_RISK_PER_TRADE = 0.01
HARD_WEEKLY_LOSS_CAP = -0.06


class PolicyError(ValueError):
    """Raised when policy configuration itself is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Projected portfolio state for a non-executable advisory decision.

    Weights, risk, and weekly P&L are fractions: 0.40 is 40% and -0.06 is -6%.
    ``active_holdings`` is the projected count after following the advisory.
    """

    active_holdings: int
    asset_weight: float
    risk_per_trade: float
    weekly_pnl: float
    instrument_type: str = "SPOT"
    leverage: float = 1.0
    autonomous_execution_requested: bool = False


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A recommendation gate, never an order authorization."""

    allowed: bool
    advisory_only: bool
    violations: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return not self.allowed


class AdvisoryPolicy:
    """Apply configured limits without ever weakening the hard safety ceilings."""

    def __init__(self, config: RiskConfig) -> None:
        if config.spot_only is not True:
            raise PolicyError("policy configuration must enforce spot-only instruments")
        if config.autonomous_trading is not False:
            raise PolicyError("autonomous trading must remain disabled")
        numeric_values = (
            config.max_asset_weight,
            config.risk_per_trade,
            config.weekly_loss_cap,
        )
        if not all(_is_finite_number(value) for value in numeric_values):
            raise PolicyError("policy limits must be finite numbers")
        if (
            isinstance(config.max_holdings, bool)
            or not isinstance(config.max_holdings, int)
            or config.max_holdings < 1
        ):
            raise PolicyError("max holdings must be a positive integer")
        if config.max_asset_weight <= 0 or config.risk_per_trade <= 0:
            raise PolicyError("weight and risk limits must be positive")
        if config.weekly_loss_cap >= 0:
            raise PolicyError("weekly loss cap must be negative")

        self.max_holdings = min(config.max_holdings, HARD_MAX_HOLDINGS)
        self.max_asset_weight = min(config.max_asset_weight, HARD_MAX_ASSET_WEIGHT)
        self.max_risk_per_trade = min(config.risk_per_trade, HARD_MAX_RISK_PER_TRADE)
        # A threshold closer to zero is stricter for a loss represented as a negative number.
        self.weekly_loss_cap = max(config.weekly_loss_cap, HARD_WEEKLY_LOSS_CAP)

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """Return every violation in deterministic order and fail closed on bad input."""

        violations: list[str] = []

        if (
            isinstance(context.active_holdings, bool)
            or not isinstance(context.active_holdings, int)
            or context.active_holdings < 0
        ):
            violations.append("invalid_active_holdings")
        elif context.active_holdings > self.max_holdings:
            violations.append("max_holdings_exceeded")

        if not _is_finite_number(context.asset_weight) or context.asset_weight < 0:
            violations.append("invalid_asset_weight")
        elif context.asset_weight > self.max_asset_weight:
            violations.append("max_asset_weight_exceeded")

        if not _is_finite_number(context.risk_per_trade) or context.risk_per_trade < 0:
            violations.append("invalid_risk_per_trade")
        elif context.risk_per_trade > self.max_risk_per_trade:
            violations.append("max_risk_per_trade_exceeded")

        if not _is_finite_number(context.weekly_pnl):
            violations.append("invalid_weekly_pnl")
        elif context.weekly_pnl <= self.weekly_loss_cap:
            violations.append("weekly_loss_cap_reached")

        if not isinstance(context.instrument_type, str):
            violations.append("invalid_instrument_type")
        elif context.instrument_type.strip().upper() != "SPOT":
            violations.append("spot_only")

        if not _is_finite_number(context.leverage) or context.leverage != 1.0:
            violations.append("leverage_forbidden")

        if not isinstance(context.autonomous_execution_requested, bool):
            violations.append("invalid_execution_flag")
        elif context.autonomous_execution_requested:
            violations.append("autonomous_execution_forbidden")

        return PolicyDecision(
            allowed=not violations,
            advisory_only=True,
            violations=tuple(violations),
        )


def evaluate_policy(context: PolicyContext, config: RiskConfig) -> PolicyDecision:
    """Convenience wrapper for one advisory evaluation."""

    return AdvisoryPolicy(config).evaluate(context)


def _is_finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


__all__ = [
    "AdvisoryPolicy",
    "HARD_MAX_ASSET_WEIGHT",
    "HARD_MAX_HOLDINGS",
    "HARD_MAX_RISK_PER_TRADE",
    "HARD_WEEKLY_LOSS_CAP",
    "PolicyContext",
    "PolicyDecision",
    "PolicyError",
    "evaluate_policy",
]

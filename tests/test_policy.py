from __future__ import annotations

import unittest

from crypto_alerts.config import RiskConfig
from crypto_alerts.policy import AdvisoryPolicy, PolicyContext, PolicyError, evaluate_policy


def risk_config(**overrides: object) -> RiskConfig:
    values = {
        "spot_only": True,
        "autonomous_trading": False,
        "max_holdings": 5,
        "max_asset_weight": 0.40,
        "risk_per_trade": 0.01,
        "weekly_loss_cap": -0.06,
    }
    values.update(overrides)
    return RiskConfig(**values)


class AdvisoryPolicyTests(unittest.TestCase):
    def test_safe_boundaries_are_inclusive_and_advisory_only(self) -> None:
        context = PolicyContext(
            active_holdings=5,
            asset_weight=0.40,
            risk_per_trade=0.01,
            weekly_pnl=-0.059999,
            instrument_type="spot",
            leverage=1,
        )
        decision = evaluate_policy(context, risk_config())
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.advisory_only)
        self.assertEqual(decision.violations, ())

    def test_weekly_loss_boundary_is_blocked_inclusively(self) -> None:
        policy = AdvisoryPolicy(risk_config())
        for weekly_pnl in (-0.06, -0.060001, -1.0):
            with self.subTest(weekly_pnl=weekly_pnl):
                decision = policy.evaluate(PolicyContext(1, 0.10, 0.005, weekly_pnl))
                self.assertTrue(decision.blocked)
                self.assertIn("weekly_loss_cap_reached", decision.violations)

    def test_boundary_matrix_rejects_each_unsafe_dimension(self) -> None:
        cases = (
            (PolicyContext(6, 0.10, 0.005, 0.0), "max_holdings_exceeded"),
            (PolicyContext(1, 0.400001, 0.005, 0.0), "max_asset_weight_exceeded"),
            (PolicyContext(1, 0.10, 0.010001, 0.0), "max_risk_per_trade_exceeded"),
            (PolicyContext(1, 0.10, 0.005, 0.0, "SWAP"), "spot_only"),
            (PolicyContext(1, 0.10, 0.005, 0.0, leverage=2.0), "leverage_forbidden"),
            (
                PolicyContext(1, 0.10, 0.005, 0.0, autonomous_execution_requested=True),
                "autonomous_execution_forbidden",
            ),
        )
        policy = AdvisoryPolicy(risk_config())
        for context, violation in cases:
            with self.subTest(violation=violation):
                decision = policy.evaluate(context)
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.advisory_only)
                self.assertIn(violation, decision.violations)

    def test_all_safety_rejections_are_reported_deterministically(self) -> None:
        decision = AdvisoryPolicy(risk_config()).evaluate(
            PolicyContext(
                active_holdings=6,
                asset_weight=0.50,
                risk_per_trade=0.02,
                weekly_pnl=-0.06,
                instrument_type="FUTURES",
                leverage=3.0,
                autonomous_execution_requested=True,
            )
        )
        self.assertEqual(
            decision.violations,
            (
                "max_holdings_exceeded",
                "max_asset_weight_exceeded",
                "max_risk_per_trade_exceeded",
                "weekly_loss_cap_reached",
                "spot_only",
                "leverage_forbidden",
                "autonomous_execution_forbidden",
            ),
        )

    def test_invalid_numbers_fail_closed(self) -> None:
        contexts = (
            PolicyContext(-1, 0.1, 0.005, 0.0),
            PolicyContext(1, float("nan"), 0.005, 0.0),
            PolicyContext(1, 0.1, float("inf"), 0.0),
            PolicyContext(1, 0.1, 0.005, float("nan")),
            PolicyContext(1, 0.1, 0.005, 0.0, leverage=float("nan")),
        )
        policy = AdvisoryPolicy(risk_config())
        for context in contexts:
            with self.subTest(context=context):
                self.assertTrue(policy.evaluate(context).blocked)

    def test_stricter_configuration_is_honored(self) -> None:
        policy = AdvisoryPolicy(
            risk_config(
                max_holdings=3,
                max_asset_weight=0.25,
                risk_per_trade=0.005,
                weekly_loss_cap=-0.03,
            )
        )
        decision = policy.evaluate(PolicyContext(4, 0.30, 0.006, -0.03))
        self.assertEqual(
            decision.violations,
            (
                "max_holdings_exceeded",
                "max_asset_weight_exceeded",
                "max_risk_per_trade_exceeded",
                "weekly_loss_cap_reached",
            ),
        )

    def test_unsafe_policy_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyError, "spot-only"):
            AdvisoryPolicy(risk_config(spot_only=False))
        with self.assertRaisesRegex(PolicyError, "autonomous"):
            AdvisoryPolicy(risk_config(autonomous_trading=True))

    def test_policy_has_no_execution_surface(self) -> None:
        policy = AdvisoryPolicy(risk_config())
        self.assertFalse(hasattr(policy, "create_order"))
        self.assertFalse(hasattr(policy, "place_order"))
        self.assertFalse(hasattr(policy, "execute"))


if __name__ == "__main__":
    unittest.main()

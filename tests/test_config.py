from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crypto_alerts.config import EXPECTED_SYMBOLS, ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = json.loads(Path("config.example.json").read_text(encoding="utf-8"))

    def load(self, value: dict) -> object:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return load_config(path)

    def test_default_config_has_exact_fixed_universe_and_safety_mode(self) -> None:
        config = self.load(self.base)
        self.assertEqual(tuple(asset.symbol for asset in config.assets), EXPECTED_SYMBOLS)
        self.assertEqual(config.mode, "alert_only")
        self.assertTrue(config.risk.spot_only)
        self.assertFalse(config.risk.autonomous_trading)
        self.assertEqual(config.analysis.engine, "fuzzy_expert")
        self.assertEqual(config.analysis.openai_model, "gpt-5.6")

    def test_unknown_keys_and_changed_universe_are_rejected(self) -> None:
        self.base["unexpected"] = True
        with self.assertRaises(ConfigError):
            self.load(self.base)
        self.base.pop("unexpected")
        self.base["assets"] = self.base["assets"][:-1]
        with self.assertRaises(ConfigError):
            self.load(self.base)

    def test_materiality_thresholds_cannot_drift_from_contract(self) -> None:
        for field, value in (
            ("price_move_pct", 4.99),
            ("volume_ratio_min", 1.49),
            ("volume_baseline_days", 6),
        ):
            candidate = json.loads(json.dumps(self.base))
            candidate["market"][field] = value
            with self.subTest(field=field), self.assertRaises(ConfigError):
                self.load(candidate)

    def test_risk_limits_can_only_be_equal_or_stricter(self) -> None:
        unsafe_values = {
            "max_holdings": 6,
            "max_asset_weight": 0.41,
            "risk_per_trade": 0.011,
            "weekly_loss_cap": -0.061,
        }
        for field, value in unsafe_values.items():
            candidate = json.loads(json.dumps(self.base))
            candidate["risk"][field] = value
            with self.subTest(field=field), self.assertRaises(ConfigError):
                self.load(candidate)

    def test_non_https_feed_and_autonomous_trading_are_rejected(self) -> None:
        candidate = json.loads(json.dumps(self.base))
        candidate["news"]["feeds"][0]["url"] = "http://example.com/feed"
        with self.assertRaises(ConfigError):
            self.load(candidate)
        candidate = json.loads(json.dumps(self.base))
        candidate["risk"]["autonomous_trading"] = True
        with self.assertRaises(ConfigError):
            self.load(candidate)

    def test_state_path_must_stay_inside_the_working_directory(self) -> None:
        for value in (str(Path.cwd() / "state.json"), "../state.json", "."):
            candidate = json.loads(json.dumps(self.base))
            candidate["state"]["path"] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.load(candidate)

    def test_analysis_contract_is_strict_and_cannot_enable_execution(self) -> None:
        for field, value in (
            ("engine", "black_box_trader"),
            ("openai_model", "latest"),
            ("openai_enabled", "yes"),
            ("openai_timeout_seconds", 31),
        ):
            candidate = json.loads(json.dumps(self.base))
            candidate["analysis"][field] = value
            with self.subTest(field=field), self.assertRaises(ConfigError):
                self.load(candidate)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from crypto_alerts import cli
from crypto_alerts.models import (
    AlertEvent,
    AnalysisType,
    EventCategory,
    SourceQuality,
)

NOW = datetime(2026, 7, 15, 10, tzinfo=UTC)


def event() -> AlertEvent:
    return AlertEvent(
        event_id="test-event",
        asset="BTC",
        category=EventCategory.PRICE_VOLUME,
        catalyst="BTC moved +5.00% with 1.50x quote volume",
        evidence_urls=("https://www.okx.com/api/v5/market/candles?instId=BTC-USDT",),
        source_quality=SourceQuality.HIGH,
        probable_market_impact="Short-term pressure is plausible.",
        main_risk="The move can reverse.",
        technical_vs_fundamental=AnalysisType.TECHNICAL,
        observed_at=NOW,
        metrics={"change_24h_pct": 5.0, "volume_ratio": 1.5},
    )


class CliTests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_end_to_end_artifact_state_and_same_day_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            output = Path(directory) / "artifacts"
            arguments = [
                "run",
                "--config",
                "config.example.json",
                "--state",
                str(state),
                "--output-dir",
                str(output),
                "--no-notify",
                "--now",
                NOW.isoformat(),
            ]
            with patch.object(cli, "_collect_events", return_value=([event()], [])):
                first, first_stdout, first_stderr = self.invoke(arguments)
                second, second_stdout, second_stderr = self.invoke(arguments)

            self.assertEqual((first, second), (0, 0))
            self.assertEqual((first_stderr, second_stderr), ("", ""))
            self.assertEqual(json.loads(first_stdout)["emitted_events"], 1)
            self.assertEqual(json.loads(second_stdout)["emitted_events"], 0)
            payload = json.loads((output / "digest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["event_count"], 0)
            self.assertTrue(payload["suppressed"]["daily_digest_already_sent"])
            self.assertTrue(state.exists())

    def test_source_failure_is_nonzero_and_does_not_advance_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with patch.object(cli, "_collect_events", side_effect=cli.RunError("fixture outage")):
                code, stdout, stderr = self.invoke(
                    [
                        "run",
                        "--config",
                        "config.example.json",
                        "--state",
                        str(state),
                        "--output-dir",
                        str(Path(directory) / "artifacts"),
                        "--no-notify",
                        "--now",
                        NOW.isoformat(),
                    ]
                )
            self.assertEqual(code, cli.EXIT_SOURCE)
            self.assertEqual(stdout, "")
            self.assertIn("fixture outage", stderr)
            self.assertFalse(state.exists())

    def test_empty_digest_setting_cannot_bypass_same_day_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
            config["delivery"]["send_empty_digest"] = True
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            state = Path(directory) / "state.json"
            arguments = [
                "run",
                "--config",
                str(config_path),
                "--state",
                str(state),
                "--output-dir",
                str(Path(directory) / "artifacts"),
                "--no-notify",
                "--now",
                NOW.isoformat(),
            ]
            with patch.object(cli, "_collect_events", return_value=([event()], [])):
                first, _, _ = self.invoke(arguments)
                first_generation = json.loads(state.read_text(encoding="utf-8"))["generation"]
                second, second_stdout, _ = self.invoke(arguments)
                second_generation = json.loads(state.read_text(encoding="utf-8"))["generation"]

            self.assertEqual((first, second), (cli.EXIT_OK, cli.EXIT_OK))
            self.assertEqual(first_generation, second_generation)
            self.assertEqual(json.loads(second_stdout)["emitted_events"], 0)

    def test_no_enabled_channel_requires_explicit_artifact_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
            config["delivery"]["telegram_enabled"] = False
            config["delivery"]["email_enabled"] = False
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            state = Path(directory) / "state.json"
            with patch.object(cli, "_collect_events", return_value=([event()], [])):
                code, _, stderr = self.invoke(
                    [
                        "run",
                        "--config",
                        str(config_path),
                        "--state",
                        str(state),
                        "--output-dir",
                        str(Path(directory) / "artifacts"),
                        "--now",
                        NOW.isoformat(),
                    ]
                )
            self.assertEqual(code, cli.EXIT_INPUT)
            self.assertIn("no notification channel", stderr)
            self.assertFalse(state.exists())

    def test_sixth_holding_is_blocked_and_execution_flag_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            portfolio = {
                "market_type": "spot",
                "leverage": 1,
                "risk_per_trade": 0.01,
                "weekly_return": 0,
                "positions": [
                    {"symbol": symbol, "weight": 0.1}
                    for symbol in ("BTC", "ETH", "SOL", "XRP", "ADA", "SEI")
                ],
            }
            path = Path(directory) / "portfolio.json"
            path.write_text(json.dumps(portfolio), encoding="utf-8")
            code, stdout, _ = self.invoke(
                [
                    "check-portfolio",
                    "--config",
                    "config.example.json",
                    "--portfolio",
                    str(path),
                ]
            )
            self.assertEqual(code, cli.EXIT_POLICY_BLOCKED)
            self.assertIn("max_holdings_exceeded", json.loads(stdout)["violations"])

        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli._parser().parse_args(["run", "--place_orders"])
        package_source = "\n".join(
            path.read_text(encoding="utf-8") for path in Path("crypto_alerts").glob("*.py")
        )
        self.assertNotIn("create_order", package_source)


if __name__ == "__main__":
    unittest.main()

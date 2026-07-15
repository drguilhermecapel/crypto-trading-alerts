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
from crypto_alerts.config import EXPECTED_SYMBOLS, load_config
from crypto_alerts.market import MarketAssessment
from crypto_alerts.models import (
    AlertEvent,
    AnalysisType,
    EventCategory,
    MarketSnapshot,
    RecommendationAction,
    RecommendationSource,
    SourceQuality,
)
from crypto_alerts.openai_advisor import AIReviewResult, AISecondOpinion

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


def collection(events: tuple[AlertEvent, ...] | None = None) -> cli.CollectedData:
    assessments = tuple(
        MarketAssessment(
            snapshot=MarketSnapshot(
                asset=symbol,
                instrument=f"{symbol}-USDT",
                observed_at=NOW,
                last_price=100.0,
                change_24h_pct=0.0,
                quote_volume_24h=100.0,
                baseline_quote_volume=100.0,
                volume_ratio=1.0,
                change_72h_pct=0.0,
                rsi_14h=50.0,
                ema_24h=100.0,
                ema_72h=100.0,
                trend_spread_pct=0.0,
                realized_volatility_24h_pct=2.0,
                drawdown_7d_pct=-3.0,
            ),
            price_threshold_met=False,
            volume_threshold_met=False,
            material=False,
            evidence_url=(
                f"https://www.okx.com/api/v5/market/candles?instId={symbol}-USDT&bar=1H&limit=193"
            ),
        )
        for symbol in EXPECTED_SYMBOLS
    )
    selected_events = (event(),) if events is None else events
    return cli.CollectedData(events=selected_events, warnings=(), assessments=assessments)


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
            with patch.object(cli, "_collect_events", return_value=collection()) as collector:
                first, first_stdout, first_stderr = self.invoke(arguments)
                second, second_stdout, second_stderr = self.invoke(arguments)

            self.assertEqual((first, second), (0, 0))
            self.assertEqual((first_stderr, second_stderr), ("", ""))
            self.assertEqual(json.loads(first_stdout)["emitted_events"], 1)
            self.assertEqual(json.loads(second_stdout)["emitted_events"], 0)
            self.assertEqual(json.loads(second_stdout)["status"], "daily_suppressed")
            self.assertEqual(collector.call_count, 1)
            payload = json.loads((output / "digest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["event_count"], 1)
            self.assertEqual(payload["recommendation_count"], 8)
            self.assertFalse(payload["suppressed"]["daily_digest_already_sent"])
            self.assertTrue((output / "suppressed-run.json").exists())
            self.assertTrue(state.exists())

    def test_stricter_configured_risk_caps_reach_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
            config["risk"]["risk_per_trade"] = 0.005
            config["risk"]["max_asset_weight"] = 0.25
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = Path(directory) / "artifacts"
            with patch.object(cli, "_collect_events", return_value=collection(())):
                code, _, stderr = self.invoke(
                    [
                        "run",
                        "--config",
                        str(config_path),
                        "--state",
                        str(Path(directory) / "state.json"),
                        "--output-dir",
                        str(output),
                        "--no-notify",
                        "--now",
                        NOW.isoformat(),
                    ]
                )

            self.assertEqual((code, stderr), (cli.EXIT_OK, ""))
            payload = json.loads((output / "digest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    (item["risk_per_trade_cap_pct"], item["max_asset_weight_pct"])
                    for item in payload["recommendations"]
                },
                {(0.5, 25.0)},
            )

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

    def test_quiet_day_still_commits_eight_recommendations_once(self) -> None:
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
            with patch.object(cli, "_collect_events", return_value=collection(())):
                first, first_stdout, first_stderr = self.invoke(arguments)
                generation = json.loads(state.read_text(encoding="utf-8"))["generation"]
                second, second_stdout, second_stderr = self.invoke(arguments)

            self.assertEqual((first, second), (cli.EXIT_OK, cli.EXIT_OK))
            self.assertEqual((first_stderr, second_stderr), ("", ""))
            first_status = json.loads(first_stdout)
            self.assertEqual(first_status["material_events"], 0)
            self.assertEqual(first_status["recommendations"], 8)
            self.assertEqual(json.loads(second_stdout)["emitted_events"], 0)
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["generation"], generation
            )
            payload = json.loads((output / "digest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["recommendation_count"], 8)
            self.assertEqual(payload["event_count"], 0)

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
            with patch.object(cli, "_collect_events", return_value=collection()):
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
            with patch.object(cli, "_collect_events", return_value=collection()):
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

    def test_model_disagreement_is_visible_but_cannot_change_effective_action(self) -> None:
        config = load_config("config.example.json")
        collected = collection()
        local = cli.build_recommendations(
            collected.assessments,
            collected.events,
            generated_at=NOW,
            expected_symbols=EXPECTED_SYMBOLS,
            max_buy_candidates=5,
            risk_per_trade_cap_pct=1.0,
            max_asset_weight_pct=40.0,
        )
        opinion = AISecondOpinion(
            asset="BTC",
            action=RecommendationAction.BUY,
            signal_strength=99,
            rationale="The second opinion is bullish.",
            primary_risk="The model may be wrong.",
            evidence_event_ids=("test-event",),
        )
        review = AIReviewResult(
            opinions={"BTC": opinion},
            status="completed",
            warning=None,
            input_hash="a" * 64,
            prompt_version="test-v1",
            model="gpt-5.6",
        )
        with patch("crypto_alerts.openai_advisor.review_recommendations", return_value=review):
            enriched, warning = cli._apply_optional_ai_review(
                local, collected.assessments, collected.events, config
            )

        btc = enriched[0]
        self.assertIs(btc.action, RecommendationAction.HOLD)
        self.assertIs(btc.model_action, RecommendationAction.BUY)
        self.assertIs(btc.model_source, RecommendationSource.FUZZY_EXPERT)
        self.assertEqual(btc.model_status, "reviewed_disagreement")
        self.assertEqual(btc.model_name, "gpt-5.6")
        self.assertEqual(btc.model_evidence_event_ids, ("test-event",))
        self.assertIn("test-event", cli.render_markdown([], NOW, recommendations=enriched))
        self.assertIsNone(warning)

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

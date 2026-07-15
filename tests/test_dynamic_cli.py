from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from crypto_alerts import cli
from crypto_alerts.config import EXPECTED_SYMBOLS, load_config
from crypto_alerts.http import PublicSourceError
from crypto_alerts.market import MarketAssessment, MarketDataError
from crypto_alerts.models import (
    MarketSnapshot,
    RecommendationAction,
    RecommendationSource,
    TokenRecommendation,
)
from crypto_alerts.openai_advisor import AIReviewResult, AISecondOpinion
from crypto_alerts.universe import UniverseAsset, Venue, VenueInstrument, build_universe

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def okx_row(symbol: str) -> dict[str, object]:
    return {
        "instType": "SPOT",
        "instId": f"{symbol}-USDT",
        "baseCcy": symbol,
        "quoteCcy": "USDT",
        "state": "live",
    }


def binance_row(symbol: str) -> dict[str, object]:
    return {
        "symbol": f"{symbol}USDT",
        "status": "TRADING",
        "baseAsset": symbol,
        "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "permissions": ["SPOT"],
    }


def make_assessment(
    symbol: str,
    *,
    exchange: str = "okx",
    available_exchanges: tuple[str, ...] = (),
) -> MarketAssessment:
    instrument = f"{symbol}-USDT"
    return MarketAssessment(
        snapshot=MarketSnapshot(
            asset=symbol,
            instrument=instrument,
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
            exchange=exchange,
            available_exchanges=available_exchanges,
        ),
        price_threshold_met=False,
        volume_threshold_met=False,
        material=False,
        evidence_url=(
            f"https://www.okx.com/market/{symbol}"
            if exchange == "okx"
            else f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT"
        ),
    )


def make_recommendation(
    symbol: str,
    *,
    score: float = 0.0,
    action: RecommendationAction = RecommendationAction.HOLD,
    analysis_status: str = "analyzed",
    market_exchange: str | None = "okx",
) -> TokenRecommendation:
    return TokenRecommendation(
        asset=symbol,
        action=action,
        signal_strength=0.6,
        score=score,
        technical_score=score,
        fundamental_score=0.0,
        model_source=RecommendationSource.FUZZY_EXPERT,
        rationale="Deterministic test recommendation.",
        primary_risk="Market conditions can change abruptly.",
        evidence_urls=(),
        evidence_event_ids=(),
        technical_metrics={"rsi_14h": 50.0},
        generated_at=NOW,
        analysis_status=analysis_status,
        analysis_reason=None if analysis_status == "analyzed" else "market data unavailable",
        market_exchange=market_exchange,
        available_exchanges=(market_exchange,) if market_exchange else (),
    )


def dynamic_universe() -> tuple[UniverseAsset, ...]:
    okx = tuple(
        VenueInstrument(symbol, Venue.OKX, f"{symbol}-USDT")
        for symbol in (*EXPECTED_SYMBOLS, "DOGE")
    )
    binance = tuple(
        VenueInstrument(symbol, Venue.BINANCE, f"{symbol}USDT") for symbol in ("BTC", "DOGE")
    )
    return build_universe(okx, binance, max_assets=20)


class DynamicDiscoveryTests(unittest.TestCase):
    def test_discovery_unions_both_exchanges_and_requires_every_core_asset(self) -> None:
        config = load_config("config.example.json")
        okx_symbols = ("BTC", "ETH", "SOL", "XRP")
        binance_symbols = ("BTC", "ADA", "SEI", "APT", "AVAX", "DOGE")
        valid_payloads = (
            {"code": "0", "msg": "", "data": [okx_row(item) for item in okx_symbols]},
            {"symbols": [binance_row(item) for item in binance_symbols]},
        )

        with patch.object(cli, "fetch_json", side_effect=valid_payloads) as fetch:
            universe, warnings = cli._discover_universe(config)

        self.assertEqual(warnings, [])
        self.assertEqual(
            tuple(item.symbol for item in universe),
            ("ADA", "APT", "AVAX", "BTC", "DOGE", "ETH", "SEI", "SOL", "XRP"),
        )
        btc = next(item for item in universe if item.symbol == "BTC")
        self.assertEqual(
            tuple(item.venue for item in btc.instruments),
            (Venue.OKX, Venue.BINANCE),
        )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            fetch.call_args_list[0].kwargs["allowed_hosts"],
            frozenset({"www.okx.com", "my.okx.com"}),
        )
        self.assertEqual(
            fetch.call_args_list[1].kwargs["allowed_hosts"],
            frozenset({"api.binance.com", "data-api.binance.vision"}),
        )

        missing_avax = (
            valid_payloads[0],
            {"symbols": [binance_row(item) for item in binance_symbols if item != "AVAX"]},
        )
        with patch.object(cli, "fetch_json", side_effect=missing_avax):
            with self.assertRaisesRegex(cli.RunError, "core assets.*AVAX"):
                cli._discover_universe(config)

        with patch.object(
            cli,
            "fetch_json",
            side_effect=(PublicSourceError("offline"), valid_payloads[1]),
        ):
            with self.assertRaisesRegex(cli.RunError, "complete OKX and Binance"):
                cli._discover_universe(config)

    def test_collection_falls_back_to_binance_and_run_renders_noncore_placeholder(self) -> None:
        config = load_config("config.example.json")
        config = replace(
            config,
            universe=replace(
                config.universe,
                minimum_coverage_ratio=0.8,
                max_workers=1,
            ),
            analysis=replace(config.analysis, openai_enabled=False),
        )
        universe = dynamic_universe()
        okx_client = MagicMock()
        binance_client = MagicMock()

        def assess_okx(asset: object) -> MarketAssessment:
            symbol = asset.symbol
            if symbol in {"BTC", "DOGE"}:
                raise MarketDataError("okx fixture unavailable")
            return make_assessment(symbol, exchange="okx")

        def assess_binance(asset: object) -> MarketAssessment:
            symbol = asset.symbol
            if symbol == "DOGE":
                raise MarketDataError("binance fixture unavailable")
            if symbol != "BTC":
                raise AssertionError(f"unexpected Binance fallback for {symbol}")
            return make_assessment(symbol, exchange="binance")

        okx_client.assess.side_effect = assess_okx
        binance_client.assess.side_effect = assess_binance
        with (
            patch.object(cli, "_discover_universe", return_value=(universe, [])),
            patch.object(cli, "OkxPublicMarketClient", return_value=okx_client),
            patch.object(cli, "BinancePublicMarketClient", return_value=binance_client),
            patch.object(cli._RateLimiter, "wait", return_value=None),
            patch.object(cli, "fetch_bytes", return_value=b""),
            patch.object(cli, "parse_feed", return_value=[]),
            patch.object(cli, "news_items_to_events", return_value=[]),
        ):
            collected = cli._collect_events(config, NOW)

        btc = next(item for item in collected.assessments if item.snapshot.asset == "BTC")
        self.assertEqual(btc.snapshot.exchange, "binance")
        self.assertEqual(btc.snapshot.available_exchanges, ("okx", "binance"))
        self.assertEqual(
            collected.market_failures,
            (("DOGE", "okx: okx fixture unavailable; binance: binance fixture unavailable"),),
        )
        self.assertTrue(any("DOGE" in warning for warning in collected.warnings))
        self.assertEqual(
            {item.snapshot.asset for item in collected.assessments},
            set(EXPECTED_SYMBOLS),
        )

        def build_fixture(
            assessment_values: object,
            _events: object,
            **_kwargs: object,
        ) -> list[TokenRecommendation]:
            return [
                make_recommendation(
                    item.snapshot.asset,
                    market_exchange=item.snapshot.exchange,
                )
                for item in assessment_values
            ]

        store = MagicMock()
        store.load.return_value = object()
        store.digest_sent_today.return_value = False
        store.new_event_ids.return_value = []
        args = argparse.Namespace(
            force=False,
            output_dir=None,
            no_notify=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            args.output_dir = directory
            with (
                patch.object(cli, "_collect_events", return_value=collected),
                patch.object(cli, "build_recommendations", side_effect=build_fixture),
                patch("sys.stdout", new=io.StringIO()),
            ):
                code = cli._run_monitor_locked(args, config, NOW, store)

            payload = json.loads((Path(directory) / "digest.json").read_text(encoding="utf-8"))

        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(payload["recommendation_count"], len(universe))
        doge = next(item for item in payload["recommendations"] if item["asset"] == "DOGE")
        self.assertEqual(doge["action"], "NOT_RATED")
        self.assertEqual(doge["analysis_status"], "unavailable")
        self.assertIn("binance fixture unavailable", doge["analysis_reason"])
        self.assertEqual(
            payload["universe"]["market_failures"],
            [
                {
                    "asset": "DOGE",
                    "reason": (
                        "okx: okx fixture unavailable; binance: binance fixture unavailable"
                    ),
                }
            ],
        )


class DynamicBudgetAndRenderingTests(unittest.TestCase):
    def test_ai_shortlist_respects_configured_max_and_marks_rest_as_budget(self) -> None:
        config = load_config("config.example.json")
        config = replace(
            config,
            analysis=replace(
                config.analysis,
                openai_enabled=True,
                openai_max_assets=3,
            ),
        )
        analyzed_symbols = tuple(f"A{index:02d}" for index in range(1, 8))
        recommendations = [
            make_recommendation(symbol, score=float(index))
            for index, symbol in enumerate(analyzed_symbols, start=1)
        ]
        recommendations.append(
            make_recommendation(
                "A00",
                action=RecommendationAction.NOT_RATED,
                analysis_status="unavailable",
                market_exchange=None,
            )
        )
        assessments = [make_assessment(symbol) for symbol in analyzed_symbols]
        selected = ("A07", "A06", "A05")
        review = AIReviewResult(
            opinions={
                symbol: AISecondOpinion(
                    asset=symbol,
                    action=RecommendationAction.HOLD,
                    signal_strength=60,
                    rationale="The local HOLD assessment is reasonable.",
                    primary_risk="Conditions can change quickly.",
                    evidence_event_ids=(),
                )
                for symbol in selected
            },
            status="completed",
            warning=None,
            input_hash="a" * 64,
            prompt_version="test-v1",
            model="gpt-5.6",
        )

        with (
            patch(
                "crypto_alerts.openai_advisor.review_recommendations",
                return_value=review,
            ) as reviewer,
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-testtoken123"}),
        ):
            enriched, warning = cli._apply_optional_ai_review(
                recommendations,
                assessments,
                [],
                config,
            )

        self.assertIsNone(warning)
        sent_recommendations = reviewer.call_args.args[0]
        self.assertEqual(tuple(item.asset for item in sent_recommendations), selected)
        self.assertEqual(len(sent_recommendations), config.analysis.openai_max_assets)
        sent_assessments = reviewer.call_args.args[1]
        self.assertEqual({item.snapshot.asset for item in sent_assessments}, set(selected))
        by_asset = {item.asset: item for item in enriched}
        self.assertTrue(
            all(by_asset[symbol].model_status == "reviewed_agreement" for symbol in selected)
        )
        self.assertTrue(
            all(
                by_asset[symbol].model_status == "not_selected_budget"
                for symbol in ("A01", "A02", "A03", "A04")
            )
        )
        self.assertEqual(by_asset["A00"].model_status, "not_selected_no_market_data")

    def test_markdown_is_bounded_for_300_recommendations_but_json_keeps_all(self) -> None:
        recommendations = [
            make_recommendation(symbol=f"T{index:03d}", score=float(index % 100))
            for index in range(300)
        ]
        collected = cli.CollectedData(events=(), warnings=(), assessments=())
        with tempfile.TemporaryDirectory() as directory:
            _, json_path, markdown = cli._write_digest(
                directory,
                [],
                NOW,
                [],
                {
                    "duplicate_events": 0,
                    "daily_digest_already_sent": False,
                    "material_events_before_suppression": 0,
                },
                recommendations,
                collected,
                12,
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["recommendation_count"], 300)
        self.assertEqual(len(payload["recommendations"]), 300)
        self.assertEqual(
            {item["asset"] for item in payload["recommendations"]},
            {f"T{index:03d}" for index in range(300)},
        )
        self.assertEqual(markdown.count("\n### "), 12)
        self.assertIn("Details omitted here for 288 tokens", markdown)
        self.assertNotIn("\n### T000", markdown)
        self.assertLess(len(markdown), 40_000)


if __name__ == "__main__":
    unittest.main()

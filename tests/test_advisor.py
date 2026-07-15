from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import crypto_alerts.advisor as advisor_module
from crypto_alerts.advisor import build_recommendations
from crypto_alerts.config import EXPECTED_SYMBOLS
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

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "ADA", "SEI", "APT", "AVAX")


def make_assessment(
    symbol: str,
    *,
    change_24h_pct: float = 0.0,
    change_72h_pct: float = 0.0,
    volume_ratio: float = 1.0,
    rsi_14h: float = 50.0,
    trend_spread_pct: float = 0.0,
    realized_volatility_24h_pct: float = 4.0,
    drawdown_7d_pct: float = -5.0,
) -> MarketAssessment:
    price_met = abs(change_24h_pct) >= 5.0
    volume_met = volume_ratio >= 1.5
    return MarketAssessment(
        snapshot=MarketSnapshot(
            asset=symbol,
            instrument=f"{symbol}-USDT",
            observed_at=NOW,
            last_price=100.0,
            change_24h_pct=change_24h_pct,
            quote_volume_24h=100_000.0 * volume_ratio,
            baseline_quote_volume=100_000.0,
            volume_ratio=volume_ratio,
            change_72h_pct=change_72h_pct,
            rsi_14h=rsi_14h,
            ema_24h=100.0 * (1.0 + trend_spread_pct / 100.0),
            ema_72h=100.0,
            trend_spread_pct=trend_spread_pct,
            realized_volatility_24h_pct=realized_volatility_24h_pct,
            drawdown_7d_pct=drawdown_7d_pct,
        ),
        price_threshold_met=price_met,
        volume_threshold_met=volume_met,
        material=price_met and volume_met,
        evidence_url=(
            "https://www.okx.com/api/v5/market/candles?"
            f"instId={symbol}-USDT&bar=1H&limit=193"
        ),
    )


def universe(**overrides: MarketAssessment) -> list[MarketAssessment]:
    return [overrides.get(symbol, make_assessment(symbol)) for symbol in SYMBOLS]


def build(
    assessments: list[MarketAssessment] | None = None,
    events: list[AlertEvent] | None = None,
    *,
    max_buy_candidates: int = 5,
    risk_per_trade_cap_pct: float = 1.0,
    max_asset_weight_pct: float = 40.0,
):
    return build_recommendations(
        universe() if assessments is None else assessments,
        [] if events is None else events,
        generated_at=NOW,
        expected_symbols=SYMBOLS,
        max_buy_candidates=max_buy_candidates,
        risk_per_trade_cap_pct=risk_per_trade_cap_pct,
        max_asset_weight_pct=max_asset_weight_pct,
    )


def make_event(
    event_id: str,
    asset: str,
    category: EventCategory,
    catalyst: str,
    *,
    quality: SourceQuality = SourceQuality.HIGH,
    observed_at: datetime = NOW,
) -> AlertEvent:
    domain = {
        SourceQuality.HIGH: "bitcoin.org",
        SourceQuality.MEDIUM: "reuters.com",
        SourceQuality.LOW: "example.org",
    }[quality]
    return AlertEvent(
        event_id=event_id,
        asset=asset,
        category=category,
        catalyst=catalyst,
        evidence_urls=(f"https://{domain}/{event_id}",),
        source_quality=quality,
        probable_market_impact="Scenario-dependent market impact.",
        main_risk="The catalyst may be misread or already priced in.",
        technical_vs_fundamental=AnalysisType.FUNDAMENTAL,
        observed_at=observed_at,
    )


class AdvisorInputTests(unittest.TestCase):
    def test_requires_exactly_one_assessment_for_each_of_the_eight_assets(self) -> None:
        self.assertEqual(EXPECTED_SYMBOLS, SYMBOLS)

        recommendations = build()

        self.assertEqual(len(recommendations), 8)
        self.assertEqual(tuple(item.asset for item in recommendations), SYMBOLS)

    def test_rejects_missing_and_duplicate_assessments(self) -> None:
        complete = universe()
        with self.subTest(case="missing"):
            with self.assertRaisesRegex(ValueError, "exact(?:ly)? (?:cover|expected)"):
                build(complete[:-1])

        duplicated = [*complete[:-1], complete[0]]
        with self.subTest(case="duplicate"):
            with self.assertRaisesRegex(ValueError, "(?:mismatch|duplicat)"):
                build(duplicated)

    def test_rejects_non_finite_and_zero_default_technical_metrics(self) -> None:
        btc = make_assessment("BTC")
        nan_snapshot = replace(btc.snapshot, change_72h_pct=float("nan"))
        with self.subTest(case="nan"):
            with self.assertRaisesRegex(ValueError, "change_72h_pct must be finite"):
                build(universe(BTC=replace(btc, snapshot=nan_snapshot)))

        default_zero_snapshot = MarketSnapshot(
            asset="BTC",
            instrument="BTC-USDT",
            observed_at=NOW,
            last_price=100.0,
            change_24h_pct=0.0,
            quote_volume_24h=100_000.0,
            baseline_quote_volume=100_000.0,
            volume_ratio=1.0,
        )
        with self.subTest(case="zero-default-ema"):
            with self.assertRaisesRegex(ValueError, "(?:EMA|positive)"):
                build(universe(BTC=replace(btc, snapshot=default_zero_snapshot)))

    def test_rejects_stale_or_untrusted_inputs_and_a_changed_universe(self) -> None:
        stale = replace(
            make_assessment("BTC").snapshot,
            observed_at=NOW - timedelta(hours=3),
        )
        with self.subTest(case="stale-market"):
            with self.assertRaisesRegex(ValueError, "market snapshots must be current"):
                build(universe(BTC=replace(make_assessment("BTC"), snapshot=stale)))

        forged = replace(
            make_event(
                "forged-high",
                "BTC",
                EventCategory.REGULATORY_LEGAL,
                "SEC approves an application",
            ),
            evidence_urls=("https://example.org/forged-high",),
        )
        with self.subTest(case="forged-source-quality"):
            with self.assertRaisesRegex(ValueError, "source quality"):
                build(events=[forged])

        with self.subTest(case="stale-event"):
            old = make_event(
                "stale",
                "BTC",
                EventCategory.REGULATORY_LEGAL,
                "SEC approves an application",
                observed_at=NOW - timedelta(hours=73),
            )
            with self.assertRaisesRegex(ValueError, "72 hours"):
                build(events=[old])

        with self.subTest(case="changed-universe"):
            with self.assertRaisesRegex(ValueError, "fixed eight-token universe"):
                build_recommendations(
                    universe(),
                    [],
                    generated_at=NOW,
                    expected_symbols=(*SYMBOLS[:-1], "DOGE"),
                    max_buy_candidates=5,
                    risk_per_trade_cap_pct=1.0,
                    max_asset_weight_pct=40.0,
                )


class AdvisorDecisionTests(unittest.TestCase):
    def test_action_boundaries_are_inclusive_and_strength_gated(self) -> None:
        cases = (
            (60.0, 0.70, 0.1, 0.1, RecommendationAction.BUY),
            (60.0, 0.6999, 0.1, 0.1, RecommendationAction.HOLD),
            (-60.0, 0.70, -0.1, -0.1, RecommendationAction.SELL),
            (-60.0, 0.6999, -0.1, -0.1, RecommendationAction.REDUCE),
            (-25.0, 0.40, -0.1, -0.1, RecommendationAction.REDUCE),
            (-24.999, 0.40, -0.1, -0.1, RecommendationAction.HOLD),
        )
        for score, strength, s24, trend, expected in cases:
            with self.subTest(score=score, strength=strength):
                self.assertIs(
                    advisor_module._classify(score, strength, s24, trend),
                    expected,
                )

    def test_strong_up_down_and_flat_scenarios_are_buy_sell_and_hold(self) -> None:
        assessments = universe(
            BTC=make_assessment(
                "BTC",
                change_24h_pct=8.0,
                change_72h_pct=18.0,
                volume_ratio=2.0,
                rsi_14h=65.0,
                trend_spread_pct=4.0,
                drawdown_7d_pct=-1.0,
            ),
            ETH=make_assessment(
                "ETH",
                change_24h_pct=-8.0,
                change_72h_pct=-18.0,
                volume_ratio=2.0,
                rsi_14h=25.0,
                trend_spread_pct=-4.0,
                drawdown_7d_pct=-25.0,
            ),
        )

        by_asset = {item.asset: item for item in build(assessments)}

        self.assertIs(by_asset["BTC"].action, RecommendationAction.BUY)
        self.assertIs(by_asset["ETH"].action, RecommendationAction.SELL)
        self.assertIs(by_asset["SOL"].action, RecommendationAction.HOLD)
        self.assertGreater(by_asset["BTC"].score, 0.0)
        self.assertLess(by_asset["ETH"].score, 0.0)

    def test_buy_capacity_keeps_first_five_tied_candidates_deterministically(self) -> None:
        bullish = [
            make_assessment(
                symbol,
                change_24h_pct=8.0,
                change_72h_pct=18.0,
                volume_ratio=2.0,
                rsi_14h=65.0,
                trend_spread_pct=4.0,
                drawdown_7d_pct=-1.0,
            )
            for symbol in SYMBOLS
        ]

        first = build(bullish)
        second = build(bullish)

        expected_actions = [RecommendationAction.BUY] * 5 + [RecommendationAction.HOLD] * 3
        self.assertEqual([item.action for item in first], expected_actions)
        self.assertEqual([item.action for item in second], expected_actions)
        self.assertEqual(
            [item.asset for item in first if item.action is RecommendationAction.BUY],
            list(SYMBOLS[:5]),
        )

    def test_positive_fundamental_event_cannot_promote_hold_to_buy(self) -> None:
        event = make_event(
            "btc-upgrade",
            "BTC",
            EventCategory.NETWORK_UPGRADE,
            "Upgrade completed and adoption growth announced",
        )

        result = build(events=[event])[0]

        self.assertIs(result.action, RecommendationAction.HOLD)
        self.assertGreater(result.fundamental_score, 0.0)
        self.assertGreater(result.score, result.technical_score)

    def test_positive_fundamental_event_can_soften_a_defensive_action(self) -> None:
        weak_downtrend = make_assessment(
            "BTC",
            change_24h_pct=-1.0,
            change_72h_pct=-2.0,
            volume_ratio=1.5,
            rsi_14h=40.0,
            trend_spread_pct=-1.0,
            drawdown_7d_pct=-10.0,
        )
        approval = make_event(
            "btc-approval",
            "BTC",
            EventCategory.REGULATORY_LEGAL,
            "SEC approves ETF inflows",
        )

        before = build(universe(BTC=weak_downtrend))[0]
        after = build(universe(BTC=weak_downtrend), [approval])[0]

        self.assertIs(before.action, RecommendationAction.REDUCE)
        self.assertIs(after.action, RecommendationAction.HOLD)
        self.assertGreater(after.score, before.score)

    def test_high_quality_unresolved_outage_vetoes_an_otherwise_valid_buy(self) -> None:
        bullish_btc = make_assessment(
            "BTC",
            change_24h_pct=8.0,
            change_72h_pct=18.0,
            volume_ratio=2.0,
            rsi_14h=65.0,
            trend_spread_pct=4.0,
            drawdown_7d_pct=-1.0,
        )
        outage = make_event(
            "btc-outage",
            "BTC",
            EventCategory.OUTAGE_EXPLOIT,
            "Critical network outage and exploit remain active",
        )

        result = build(universe(BTC=bullish_btc), [outage])[0]

        self.assertIs(result.action, RecommendationAction.HOLD)
        self.assertEqual(result.fundamental_score, -100.0)
        self.assertEqual(result.evidence_event_ids, ("btc-outage",))

    def test_resolved_outage_and_common_flexions_have_expected_polarity(self) -> None:
        bullish_btc = make_assessment(
            "BTC",
            change_24h_pct=8.0,
            change_72h_pct=18.0,
            volume_ratio=2.0,
            rsi_14h=65.0,
            trend_spread_pct=4.0,
            drawdown_7d_pct=-1.0,
        )
        events = [
            make_event(
                "btc-resolved",
                "BTC",
                EventCategory.OUTAGE_EXPLOIT,
                "Network outage resolved and service restored",
            ),
            make_event(
                "eth-lawsuit",
                "ETH",
                EventCategory.REGULATORY_LEGAL,
                "SEC sues issuer while an exchange delists the token",
            ),
        ]

        by_asset = {item.asset: item for item in build(universe(BTC=bullish_btc), events)}

        self.assertIs(by_asset["BTC"].action, RecommendationAction.BUY)
        self.assertGreater(by_asset["BTC"].fundamental_score, 0.0)
        self.assertLess(by_asset["ETH"].fundamental_score, 0.0)

    def test_effective_risk_caps_are_propagated_to_every_recommendation(self) -> None:
        recommendations = build(
            risk_per_trade_cap_pct=0.5,
            max_asset_weight_pct=25.0,
        )

        self.assertEqual(
            {(item.risk_per_trade_cap_pct, item.max_asset_weight_pct) for item in recommendations},
            {(0.5, 25.0)},
        )

    def test_output_is_auditable_advice_and_has_no_execution_surface(self) -> None:
        recommendation = build()[0]
        payload = recommendation.to_dict()
        required_fields = {
            "asset",
            "action",
            "signal_strength",
            "signal_strength_is_probability",
            "score",
            "technical_score",
            "fundamental_score",
            "model_source",
            "rationale",
            "primary_risk",
            "evidence_urls",
            "evidence_event_ids",
            "technical_metrics",
            "generated_at",
            "risk_per_trade_cap_pct",
            "max_asset_weight_pct",
            "model_name",
            "model_evidence_event_ids",
            "advisory_only",
            "execution_allowed",
        }

        self.assertTrue(required_fields <= payload.keys())
        self.assertIs(recommendation.model_source, RecommendationSource.FUZZY_EXPERT)
        self.assertTrue(payload["advisory_only"])
        self.assertFalse(payload["execution_allowed"])
        self.assertFalse(payload["signal_strength_is_probability"])
        self.assertEqual(payload["risk_per_trade_cap_pct"], 1.0)
        self.assertEqual(payload["max_asset_weight_pct"], 40.0)
        self.assertTrue(0.0 <= payload["signal_strength"] <= 1.0)
        self.assertTrue(-100.0 <= payload["score"] <= 100.0)
        for forbidden in ("create_order", "place_order", "execute_order", "submit_order"):
            self.assertFalse(hasattr(advisor_module, forbidden))


if __name__ == "__main__":
    unittest.main()

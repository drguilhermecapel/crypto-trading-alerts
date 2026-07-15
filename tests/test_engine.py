from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from crypto_alerts.engine import combine_events, deduplicate_events
from crypto_alerts.models import AlertEvent, AnalysisType, EventCategory, SourceQuality
from crypto_alerts.report import build_payload, render_json, render_markdown

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def make_event(
    event_id: str,
    *,
    asset: str = "BTC",
    category: EventCategory = EventCategory.ETF_INSTITUTIONAL,
    url: str = "https://www.sec.gov/evidence/1",
    analysis: AnalysisType = AnalysisType.FUNDAMENTAL,
    observed_at: datetime = NOW,
    quality: SourceQuality = SourceQuality.HIGH,
    catalyst: str = "Primary-source catalyst",
    metrics: dict[str, object] | None = None,
) -> AlertEvent:
    return AlertEvent(
        event_id=event_id,
        asset=asset,
        category=category,
        catalyst=catalyst,
        evidence_urls=(url,),
        source_quality=quality,
        probable_market_impact="Recorded probable impact.",
        main_risk="Recorded main risk.",
        technical_vs_fundamental=analysis,
        observed_at=observed_at,
        metrics=metrics or {},
    )


class EventEngineTests(unittest.TestCase):
    def test_duplicate_canonical_urls_merge_deterministically(self) -> None:
        first = make_event(
            "event-b",
            url="http://www.sec.gov/evidence/1/?utm_source=feed&id=3#fragment",
            observed_at=NOW - timedelta(minutes=10),
            quality=SourceQuality.HIGH,
            metrics={"first": 1},
        )
        second = make_event(
            "event-a",
            url="https://www.sec.gov/evidence/1?id=3",
            observed_at=NOW,
            quality=SourceQuality.MEDIUM,
            metrics={"second": 2},
        )

        merged = deduplicate_events([first, second])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].event_id, "event-a")
        self.assertEqual(merged[0].evidence_urls, ("https://www.sec.gov/evidence/1?id=3",))
        self.assertEqual(merged[0].source_quality, SourceQuality.HIGH)
        self.assertEqual(merged[0].observed_at, NOW)
        self.assertEqual(merged[0].metrics["first"], 1)
        self.assertEqual(merged[0].metrics["second"], 2)
        self.assertEqual(merged[0].metrics["deduplicated_event_ids"], ["event-a", "event-b"])

    def test_same_url_for_different_assets_or_categories_is_not_collapsed(self) -> None:
        url = "https://www.sec.gov/evidence/shared"
        values = [
            make_event("btc-etf", url=url),
            make_event("eth-etf", asset="ETH", url=url),
            make_event("btc-legal", category=EventCategory.REGULATORY_LEGAL, url=url),
        ]
        self.assertEqual(len(deduplicate_events(values)), 3)

    def test_matching_technical_and_fundamental_events_are_enriched_not_rewritten(self) -> None:
        technical = make_event(
            "price-btc",
            category=EventCategory.PRICE_VOLUME,
            url="https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT",
            analysis=AnalysisType.TECHNICAL,
            observed_at=NOW,
            catalyst="BTC moved 5.4% with 1.8x volume",
            metrics={"change_24h_pct": 5.4, "volume_ratio": 1.8},
        )
        fundamental = make_event(
            "etf-btc",
            observed_at=NOW - timedelta(hours=2),
            catalyst="Official Bitcoin ETF notice",
            metrics={"source_count": 1},
        )
        unrelated = make_event(
            "upgrade-eth",
            asset="ETH",
            category=EventCategory.NETWORK_UPGRADE,
            url="https://ethereum.org/en/roadmap/example",
            observed_at=NOW,
            catalyst="Official Ethereum protocol upgrade",
        )

        combined = combine_events([fundamental], [technical, unrelated])
        by_id = {event.event_id: event for event in combined}

        self.assertEqual(len(combined), 3)
        self.assertEqual(by_id["price-btc"].technical_vs_fundamental, AnalysisType.MIXED)
        self.assertEqual(by_id["etf-btc"].technical_vs_fundamental, AnalysisType.MIXED)
        self.assertEqual(by_id["upgrade-eth"].technical_vs_fundamental, AnalysisType.FUNDAMENTAL)
        self.assertEqual(by_id["etf-btc"].catalyst, "Official Bitcoin ETF notice")
        self.assertEqual(by_id["etf-btc"].probable_market_impact, "Recorded probable impact.")
        self.assertEqual(by_id["etf-btc"].main_risk, "Recorded main risk.")
        technical_context = by_id["etf-btc"].metrics["technical_context"]
        self.assertEqual(technical_context[0]["event_id"], "price-btc")
        self.assertEqual(technical_context[0]["metrics"]["change_24h_pct"], 5.4)
        fundamental_context = by_id["price-btc"].metrics["fundamental_context"]
        self.assertEqual(fundamental_context[0]["event_id"], "etf-btc")

    def test_events_outside_correlation_window_are_not_enriched(self) -> None:
        technical = make_event(
            "price",
            category=EventCategory.PRICE_VOLUME,
            analysis=AnalysisType.TECHNICAL,
            observed_at=NOW,
        )
        old = make_event("old-news", observed_at=NOW - timedelta(hours=25))
        result = {event.event_id: event for event in combine_events([technical], [old])}
        self.assertEqual(result["price"].technical_vs_fundamental, AnalysisType.TECHNICAL)
        self.assertEqual(result["old-news"].technical_vs_fundamental, AnalysisType.FUNDAMENTAL)


class ReportTests(unittest.TestCase):
    def test_payload_and_markdown_include_every_required_field_and_url(self) -> None:
        event = make_event(
            "event-1",
            url="https://www.sec.gov/evidence/1",
            metrics={"when": NOW, "non_finite": float("nan")},
        )
        payload = build_payload([event], NOW)
        alert = payload["alerts"][0]
        expected_fields = {
            "event_id",
            "asset",
            "category",
            "catalyst",
            "evidence_urls",
            "source_quality",
            "probable_market_impact",
            "main_risk",
            "technical_vs_fundamental",
            "observed_at",
            "metrics",
        }

        self.assertEqual(set(alert), expected_fields)
        self.assertEqual(alert["evidence_urls"], ["https://www.sec.gov/evidence/1"])
        self.assertIsNone(alert["metrics"]["non_finite"])
        json.dumps(payload, allow_nan=False)
        json.loads(render_json([event], NOW))

        markdown = render_markdown([event], NOW)
        for required in (
            "Event ID",
            "Category",
            "Source quality",
            "Probable market impact",
            "Main risk",
            "Technical vs fundamental",
            "Observed at",
            "Evidence URLs",
            "Metrics",
            "https://www.sec.gov/evidence/1",
        ):
            with self.subTest(required=required):
                self.assertIn(required, markdown)


if __name__ == "__main__":
    unittest.main()

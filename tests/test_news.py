from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from crypto_alerts.config import DEFAULT_ALIASES
from crypto_alerts.models import AnalysisType, Asset, EventCategory, NewsItem, SourceQuality
from crypto_alerts.news import (
    NEWS_CATEGORIES,
    FeedParseError,
    canonicalize_url,
    classify_category,
    news_items_to_events,
    parse_feed,
    source_quality_for_url,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
ASSETS = tuple(
    Asset(symbol=symbol, instrument=f"{symbol}-USDT", aliases=aliases)
    for symbol, aliases in DEFAULT_ALIASES.items()
)


class FeedParsingTests(unittest.TestCase):
    def test_rss_and_atom_are_parsed_and_urls_are_canonical_https(self) -> None:
        rss = """<?xml version="1.0"?>
        <rss version="2.0"><channel><title>SEC feed</title><item>
          <title>SEC announces crypto regulatory framework for Bitcoin</title>
          <link>http://www.sec.gov/news/item/?utm_source=test&amp;b=2&amp;a=1#part</link>
          <description>Digital asset regulation update.</description>
          <pubDate>Wed, 15 Jul 2026 11:00:00 GMT</pubDate>
        </item></channel></rss>"""
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom"><title>CoinDesk</title><entry>
          <title>Solana network upgrade ships</title>
          <link rel="alternate" href="https://www.coindesk.com/tech/story/?utm_medium=rss" />
          <summary>Solana protocol upgrade coverage.</summary>
          <published>2026-07-15T10:30:00Z</published>
        </entry></feed>"""

        rss_items = parse_feed(rss, now=NOW, lookback_hours=30)
        atom_items = parse_feed(atom, now=NOW, lookback_hours=30)

        self.assertEqual(len(rss_items), 1)
        self.assertEqual(rss_items[0].source_name, "SEC feed")
        self.assertEqual(rss_items[0].url, "https://www.sec.gov/news/item?a=1&b=2")
        self.assertEqual(rss_items[0].source_quality, SourceQuality.HIGH)
        self.assertEqual(len(atom_items), 1)
        self.assertEqual(atom_items[0].url, "https://www.coindesk.com/tech/story")
        self.assertEqual(atom_items[0].source_quality, SourceQuality.MEDIUM)

    def test_stale_entry_is_excluded_at_parse_boundary(self) -> None:
        rss = """<rss><channel><item>
          <title>Bitcoin ETF filing</title><link>https://www.sec.gov/old</link>
          <pubDate>Mon, 13 Jul 2026 00:00:00 GMT</pubDate>
        </item></channel></rss>"""
        self.assertEqual(parse_feed(rss, now=NOW, lookback_hours=30), [])

    def test_source_quality_is_domain_based(self) -> None:
        cases = {
            "https://www.sec.gov/news": SourceQuality.HIGH,
            "https://status.solana.com/incidents/1": SourceQuality.HIGH,
            "https://www.reuters.com/technology/example": SourceQuality.MEDIUM,
            "https://blog.example.invalid/claim": SourceQuality.LOW,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(source_quality_for_url(url), expected)

    def test_url_canonicalization_rejects_non_web_schemes(self) -> None:
        self.assertEqual(
            canonicalize_url("HTTP://Example.com/a//b/?utm_campaign=x&z=9#fragment"),
            "https://example.com/a/b?z=9",
        )
        with self.assertRaises(ValueError):
            canonicalize_url("javascript:alert(1)")

    def test_dtd_and_entity_payloads_are_rejected(self) -> None:
        payload = """<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        <rss><channel><item><title>&xxe;</title></item></channel></rss>"""
        with self.assertRaises(FeedParseError):
            parse_feed(payload)

    def test_non_feed_xml_is_not_counted_as_a_valid_feed(self) -> None:
        with self.assertRaises(FeedParseError):
            parse_feed("<html><body>Access challenge</body></html>")


class CatalystClassificationTests(unittest.TestCase):
    def _item(
        self,
        title: str,
        index: int,
        *,
        quality: SourceQuality = SourceQuality.HIGH,
        age_hours: int = 1,
        domain: str = "sec.gov",
    ) -> NewsItem:
        return NewsItem(
            title=title,
            url=f"https://{domain}/evidence/{index}",
            summary="Confirmed crypto and digital asset update.",
            published_at=NOW - timedelta(hours=age_hours),
            source_name="Fixture source",
            source_domain=domain,
            source_quality=quality,
        )

    def test_exact_six_news_categories_are_classified(self) -> None:
        fixtures = {
            EventCategory.ONCHAIN_ECOSYSTEM: "Solana on-chain active addresses accelerate",
            EventCategory.EXCHANGE_LIQUIDITY: "OKX exchange listing adds XRP liquidity",
            EventCategory.NETWORK_UPGRADE: "Ethereum protocol upgrade is released",
            EventCategory.OUTAGE_EXPLOIT: "Avalanche network outage affects validators",
            EventCategory.ETF_INSTITUTIONAL: "Bitcoin ETF records institutional demand",
            EventCategory.REGULATORY_LEGAL: "SEC announces crypto regulation for Cardano",
        }
        self.assertEqual(set(NEWS_CATEGORIES), set(fixtures))
        for expected, text in fixtures.items():
            with self.subTest(category=expected):
                self.assertEqual(classify_category(text), expected)

        events = news_items_to_events(
            [self._item(title, index) for index, title in enumerate(fixtures.values())],
            ASSETS,
            NOW,
            30,
        )
        self.assertEqual({event.category for event in events}, set(fixtures))
        self.assertEqual(len(events), 6)

    def test_event_contains_exact_contract_fields_and_canonical_evidence(self) -> None:
        item = NewsItem(
            title="Bitcoin ETF records institutional demand",
            url="http://www.sec.gov/etf/notice/?utm_source=feed&id=7#top",
            summary="Crypto fund update.",
            published_at=NOW - timedelta(hours=1),
            source_name="SEC",
            source_domain="www.sec.gov",
            source_quality=SourceQuality.HIGH,
        )
        event = news_items_to_events([item], ASSETS, NOW, 30)[0]
        payload = event.to_dict()

        self.assertEqual(
            set(payload),
            {
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
            },
        )
        self.assertEqual(payload["asset"], "BTC")
        self.assertEqual(payload["category"], "etf_institutional")
        self.assertEqual(payload["evidence_urls"], ["https://www.sec.gov/etf/notice?id=7"])
        self.assertEqual(payload["source_quality"], "HIGH")
        self.assertEqual(payload["technical_vs_fundamental"], AnalysisType.FUNDAMENTAL.value)
        self.assertTrue(payload["probable_market_impact"])
        self.assertTrue(payload["main_risk"])

    def test_stale_and_low_only_items_do_not_generate_events(self) -> None:
        low = self._item(
            "Bitcoin ETF records institutional demand",
            1,
            quality=SourceQuality.LOW,
            domain="unknown.example",
        )
        stale = self._item("Ethereum network upgrade is released", 2, age_hours=31)
        spoofed = self._item(
            "Solana on-chain active addresses accelerate",
            3,
            quality=SourceQuality.HIGH,
            domain="unknown.example",
        )
        self.assertEqual(news_items_to_events([low, stale, spoofed], ASSETS, NOW, 30), [])

    def test_global_crypto_catalyst_maps_only_to_the_fixed_universe(self) -> None:
        events = news_items_to_events(
            [self._item("Court issues regulatory ruling for cryptocurrency markets", 3)],
            ASSETS,
            NOW,
            30,
        )
        self.assertEqual({event.asset for event in events}, set(DEFAULT_ALIASES))
        self.assertEqual({event.category for event in events}, {EventCategory.REGULATORY_LEGAL})

    def test_lowercase_apt_word_does_not_match_the_apt_ticker(self) -> None:
        item = NewsItem(
            title="SEC charges apt company in legal action",
            url="https://www.sec.gov/evidence/unrelated",
            summary="Unrelated corporate matter.",
            published_at=NOW - timedelta(hours=1),
            source_name="SEC",
            source_domain="sec.gov",
            source_quality=SourceQuality.HIGH,
        )
        self.assertEqual(news_items_to_events([item], ASSETS, NOW, 30), [])
        explicit = self._item("SEC charges APT issuer in legal action", 5)
        self.assertEqual(news_items_to_events([explicit], ASSETS, NOW, 30)[0].asset, "APT")

    def test_ambiguous_sec_and_charges_words_are_not_regulatory_events(self) -> None:
        self.assertIsNone(classify_category("Solana finality under 1 sec"))
        self.assertIsNone(classify_category("Solana network charges fall"))

    def test_event_id_is_stable_when_same_canonical_url_is_republished(self) -> None:
        original = self._item("Bitcoin ETF records institutional demand", 9)
        updated = NewsItem(
            title="Updated: Bitcoin ETF records new institutional demand",
            url=f"{original.url}?utm_source=republished",
            summary=original.summary,
            published_at=NOW,
            source_name=original.source_name,
            source_domain=original.source_domain,
            source_quality=original.source_quality,
        )
        original_event = news_items_to_events([original], ASSETS, NOW, 30)[0]
        updated_event = news_items_to_events([updated], ASSETS, NOW, 30)[0]
        self.assertEqual(original_event.event_id, updated_event.event_id)

    def test_event_id_is_stable_when_higher_quality_corroboration_arrives(self) -> None:
        editorial = self._item(
            "Bitcoin ETF records institutional demand",
            10,
            quality=SourceQuality.MEDIUM,
            domain="coindesk.com",
        )
        official = self._item("Bitcoin ETF records institutional demand", 11)
        initial = news_items_to_events([editorial], ASSETS, NOW, 30)[0]
        corroborated = news_items_to_events([editorial, official], ASSETS, NOW, 30)[0]
        self.assertEqual(initial.event_id, corroborated.event_id)
        self.assertEqual(corroborated.source_quality, SourceQuality.HIGH)

    def test_editorial_only_titles_fall_back_to_distinct_urls(self) -> None:
        first = self._item("Breaking", 12)
        second = self._item("Latest", 13)
        first = replace(first, summary="Bitcoin ETF records institutional crypto demand.")
        second = replace(second, summary="Bitcoin ETF records institutional crypto demand.")
        events = news_items_to_events([first, second], ASSETS, NOW, 30)
        self.assertEqual(len(events), 2)
        self.assertEqual(len({event.event_id for event in events}), 2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from crypto_alerts.config import MarketConfig
from crypto_alerts.market import BinancePublicMarketClient, MarketDataError
from crypto_alerts.models import Asset

NOW = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)
ASSET = Asset("BTC", "BTC-USDT", ("BTC", "Bitcoin"))


def market_config(*, binance_base_url: str = "https://api.binance.com") -> MarketConfig:
    return MarketConfig(
        base_url="https://www.okx.com",
        binance_base_url=binance_base_url,
        price_move_pct=5.0,
        volume_ratio_min=1.5,
        lookback_hours=24,
        volume_baseline_days=7,
        request_timeout_seconds=3.0,
    )


def kline_rows(
    *,
    change_pct: float = 5.0,
    volume_ratio: float = 1.5,
    include_unclosed: bool = True,
) -> list[list[object]]:
    last_open = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    first_open = last_open - timedelta(hours=191)
    baseline_daily_volumes = (80.0, 100.0, 120.0, 90.0, 110.0, 100.0, 100.0)
    rows: list[list[object]] = []
    for index in range(192):
        opened_at = first_open + timedelta(hours=index)
        if index < 168:
            quote_volume = baseline_daily_volumes[index // 24] / 24.0
            open_price = close = 100.0
        else:
            quote_volume = 100.0 * volume_ratio / 24.0
            position = index - 168
            open_price = 100.0 if position == 0 else 100.0 + change_pct * position / 24.0
            close = 100.0 + change_pct * (position + 1) / 24.0
        high = max(open_price, close) + 1.0
        low = min(open_price, close) - 1.0
        opened_ms = int(opened_at.timestamp() * 1000)
        rows.append(
            [
                opened_ms,
                str(open_price),
                str(high),
                str(low),
                str(close),
                "999999999",  # Base volume is intentionally ignored.
                opened_ms + 3_600_000 - 1,
                str(quote_volume),
                10,
                "1",
                "1",
                "0",
            ]
        )
    if include_unclosed:
        current_open = last_open + timedelta(hours=1)
        current_ms = int(current_open.timestamp() * 1000)
        rows.append(
            [
                current_ms,
                "999",
                "1000",
                "998",
                "999",
                "999999999",
                current_ms + 3_600_000 - 1,
                "999999999",
                10,
                "1",
                "1",
                "0",
            ]
        )
    return rows


class FakeResponse:
    def __init__(self, payload: object, url: str, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self._url = url
        self.status = status

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeOpener:
    def __init__(self, payload: object, *, redirect_url: str | None = None) -> None:
        self.payload = payload
        self.redirect_url = redirect_url
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request: object, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        return FakeResponse(self.payload, self.redirect_url or request.full_url)


class BinanceMarketClientTests(unittest.TestCase):
    def test_closed_eight_day_snapshot_uses_quote_volume_and_is_read_only(self) -> None:
        opener = FakeOpener(kline_rows())
        client = BinancePublicMarketClient(market_config(), opener=opener, clock=lambda: NOW)

        assessment = client.assess(ASSET)

        self.assertTrue(assessment.material)
        self.assertAlmostEqual(assessment.snapshot.change_24h_pct, 5.0)
        self.assertAlmostEqual(assessment.snapshot.quote_volume_24h, 150.0)
        self.assertAlmostEqual(assessment.snapshot.baseline_quote_volume, 100.0)
        self.assertAlmostEqual(assessment.snapshot.volume_ratio, 1.5)
        self.assertEqual(assessment.snapshot.last_price, 105.0)
        self.assertEqual(assessment.snapshot.exchange, "binance")
        self.assertEqual(
            assessment.snapshot.observed_at,
            NOW.replace(minute=0, second=0, microsecond=0),
        )

        request, timeout = opener.calls[0]
        parsed = urlparse(request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 3.0)
        self.assertEqual(parsed.hostname, "api.binance.com")
        self.assertEqual(parsed.path, "/api/v3/klines")
        self.assertEqual(parse_qs(parsed.query)["symbol"], ["BTCUSDT"])
        self.assertEqual(parse_qs(parsed.query)["interval"], ["1h"])
        self.assertEqual(parse_qs(parsed.query)["limit"], ["193"])
        self.assertNotIn("authorization", {key.lower() for key in request.headers})
        self.assertEqual(assessment.evidence_url, request.full_url)
        self.assertFalse(hasattr(client, "create_order"))
        self.assertFalse(hasattr(client, "place_order"))

    def test_current_unclosed_kline_is_excluded(self) -> None:
        client = BinancePublicMarketClient(
            market_config(),
            opener=FakeOpener(kline_rows()),
            clock=lambda: NOW,
        )

        candles = client.fetch_candles(ASSET)

        self.assertEqual(len(candles), 192)
        self.assertEqual(candles[-1].close, 105.0)
        self.assertLess(candles[-1].quote_volume, 999999999.0)

    def test_dynamic_canonical_symbol_is_supported(self) -> None:
        asset = Asset("1000SATS", "1000SATS-USDT", ("1000SATS",))
        opener = FakeOpener(kline_rows())
        client = BinancePublicMarketClient(market_config(), opener=opener, clock=lambda: NOW)

        snapshot = client.fetch_snapshot(asset)

        self.assertEqual(snapshot.asset, "1000SATS")
        query = parse_qs(urlparse(opener.calls[0][0].full_url).query)
        self.assertEqual(query["symbol"], ["1000SATSUSDT"])

    def test_noncanonical_assets_are_rejected_before_io(self) -> None:
        opener = FakeOpener(kline_rows())
        client = BinancePublicMarketClient(market_config(), opener=opener, clock=lambda: NOW)
        invalid_assets = (
            Asset("BTC", "BTC-USDT-SWAP", ("BTC",)),
            Asset("btc", "btc-USDT", ("btc",)),
            Asset("BT-C", "BT-C-USDT", ("BT-C",)),
            Asset("BTC", "ETH-USDT", ("BTC",)),
        )
        for asset in invalid_assets:
            with self.subTest(asset=asset):
                with self.assertRaises(ValueError):
                    client.fetch_snapshot(asset)
        self.assertEqual(opener.calls, [])

    def test_too_few_closed_klines_are_rejected(self) -> None:
        rows = kline_rows(include_unclosed=False)[:-1]
        client = BinancePublicMarketClient(
            market_config(), opener=FakeOpener(rows), clock=lambda: NOW
        )

        with self.assertRaisesRegex(MarketDataError, "expected at least 192"):
            client.fetch_candles(ASSET)

    def test_gap_in_closed_history_is_rejected(self) -> None:
        rows = kline_rows(include_unclosed=False)
        oldest = list(rows[0])
        oldest[0] = int(oldest[0]) - 3_600_000
        oldest[6] = int(oldest[6]) - 3_600_000
        rows.insert(0, oldest)
        del rows[80]
        client = BinancePublicMarketClient(
            market_config(), opener=FakeOpener(rows), clock=lambda: NOW
        )

        with self.assertRaisesRegex(MarketDataError, "gap"):
            client.fetch_candles(ASSET)

    def test_invalid_close_time_is_rejected(self) -> None:
        rows = kline_rows()
        rows[10][6] = int(rows[10][6]) + 1
        client = BinancePublicMarketClient(
            market_config(), opener=FakeOpener(rows), clock=lambda: NOW
        )

        with self.assertRaisesRegex(MarketDataError, "invalid close time"):
            client.fetch_candles(ASSET)

    def test_stale_history_is_rejected(self) -> None:
        client = BinancePublicMarketClient(
            market_config(),
            opener=FakeOpener(kline_rows(include_unclosed=False)),
            clock=lambda: NOW + timedelta(hours=1, minutes=1),
        )

        with self.assertRaisesRegex(MarketDataError, "stale"):
            client.fetch_snapshot(ASSET)

    def test_redirect_to_nonofficial_host_is_rejected(self) -> None:
        client = BinancePublicMarketClient(
            market_config(),
            opener=FakeOpener(kline_rows(), redirect_url="https://example.com/klines"),
            clock=lambda: NOW,
        )

        with self.assertRaisesRegex(MarketDataError, "non-official"):
            client.fetch_snapshot(ASSET)

    def test_error_object_is_not_accepted_as_kline_data(self) -> None:
        client = BinancePublicMarketClient(
            market_config(),
            opener=FakeOpener({"code": -1121, "msg": "Invalid symbol."}),
            clock=lambda: NOW,
        )

        with self.assertRaisesRegex(MarketDataError, "root must be an array"):
            client.fetch_snapshot(ASSET)


if __name__ == "__main__":
    unittest.main()

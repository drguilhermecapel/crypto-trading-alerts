from __future__ import annotations

import json
import math
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from crypto_alerts.config import MarketConfig
from crypto_alerts.market import (
    MarketDataError,
    OkxPublicMarketClient,
    _realized_volatility_pct,
    meets_material_thresholds,
)
from crypto_alerts.models import Asset, MarketSnapshot

NOW = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)
ASSET = Asset("BTC", "BTC-USDT", ("BTC", "Bitcoin"))


def market_config() -> MarketConfig:
    return MarketConfig(
        base_url="https://www.okx.com",
        price_move_pct=5.0,
        volume_ratio_min=1.5,
        lookback_hours=24,
        volume_baseline_days=7,
        request_timeout_seconds=3.0,
    )


def candle_rows(
    *,
    change_pct: float = 5.0,
    volume_ratio: float = 1.5,
    include_unconfirmed: bool = True,
) -> list[list[str]]:
    last_open = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    first_open = last_open - timedelta(hours=191)
    baseline_daily_volumes = (80.0, 100.0, 120.0, 90.0, 110.0, 100.0, 100.0)
    rows: list[list[str]] = []
    for index in range(192):
        opened_at = first_open + timedelta(hours=index)
        if index < 168:
            daily_total = baseline_daily_volumes[index // 24]
            quote_volume = daily_total / 24.0
            open_price = close = 100.0
        else:
            quote_volume = 100.0 * volume_ratio / 24.0
            position = index - 168
            open_price = 100.0 if position == 0 else 100.0 + change_pct * position / 24.0
            close = 100.0 + change_pct * (position + 1) / 24.0
        high = max(open_price, close) + 1.0
        low = min(open_price, close) - 1.0
        rows.append(
            [
                str(int(opened_at.timestamp() * 1000)),
                str(open_price),
                str(high),
                str(low),
                str(close),
                "1",
                "1",
                str(quote_volume),
                "1",
            ]
        )
    if include_unconfirmed:
        current_open = last_open + timedelta(hours=1)
        rows.append(
            [
                str(int(current_open.timestamp() * 1000)),
                "999",
                "1000",
                "998",
                "999",
                "1",
                "1",
                "999999999",
                "0",
            ]
        )
    return list(reversed(rows))


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
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request: object, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        return FakeResponse({"code": "0", "msg": "", "data": self.rows}, request.full_url)


class MarketClientTests(unittest.TestCase):
    def test_realized_volatility_counts_monotonic_log_returns(self) -> None:
        closes = [100.0 * (1.01**index) for index in range(25)]

        result = _realized_volatility_pct(closes)

        self.assertAlmostEqual(result, math.sqrt(24.0) * math.log(1.01) * 100.0)
        self.assertGreater(result, 4.0)

    def test_extreme_timestamp_is_reported_as_market_data_error(self) -> None:
        row = [str(10**100), "1", "1", "1", "1", "0", "0", "1", "1"]
        with self.assertRaises(MarketDataError):
            OkxPublicMarketClient._row_timestamp(row, 0)

    def test_rounding_noise_at_exact_boundary_is_inclusive(self) -> None:
        snapshot = MarketSnapshot(
            "BTC",
            "BTC-USDT",
            NOW,
            105.0,
            5.0 - 5e-13,
            150.0,
            100.0,
            1.5 - 5e-13,
        )
        self.assertTrue(
            meets_material_thresholds(snapshot, price_move_pct=5.0, volume_ratio_min=1.5)
        )

    def test_confirmed_eight_day_snapshot_and_inclusive_boundary(self) -> None:
        opener = FakeOpener(candle_rows())
        client = OkxPublicMarketClient(market_config(), opener=opener, clock=lambda: NOW)

        assessment = client.assess(ASSET)

        self.assertTrue(assessment.material)
        self.assertTrue(assessment.price_threshold_met)
        self.assertTrue(assessment.volume_threshold_met)
        self.assertAlmostEqual(assessment.snapshot.change_24h_pct, 5.0)
        self.assertAlmostEqual(assessment.snapshot.quote_volume_24h, 150.0)
        self.assertAlmostEqual(assessment.snapshot.baseline_quote_volume, 100.0)
        self.assertAlmostEqual(assessment.snapshot.volume_ratio, 1.5)
        self.assertEqual(assessment.snapshot.last_price, 105.0)
        self.assertAlmostEqual(assessment.snapshot.change_72h_pct, 5.0)
        self.assertEqual(assessment.snapshot.rsi_14h, 100.0)
        self.assertGreater(assessment.snapshot.ema_24h, assessment.snapshot.ema_72h)
        self.assertGreater(assessment.snapshot.trend_spread_pct, 0.0)
        self.assertGreaterEqual(assessment.snapshot.realized_volatility_24h_pct, 0.0)
        self.assertLessEqual(assessment.snapshot.drawdown_7d_pct, 0.0)
        expected_observed_at = NOW.replace(minute=0, second=0, microsecond=0)
        self.assertEqual(assessment.snapshot.observed_at, expected_observed_at)

        request, timeout = opener.calls[0]
        parsed = urlparse(request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 3.0)
        self.assertEqual(parsed.hostname, "www.okx.com")
        self.assertEqual(parsed.path, "/api/v5/market/candles")
        self.assertEqual(parse_qs(parsed.query)["instId"], ["BTC-USDT"])
        self.assertEqual(parse_qs(parsed.query)["bar"], ["1H"])
        self.assertEqual(parse_qs(parsed.query)["limit"], ["193"])
        self.assertNotIn("authorization", {key.lower() for key in request.headers})
        self.assertEqual(assessment.evidence_url, request.full_url)
        self.assertFalse(hasattr(client, "create_order"))
        self.assertFalse(hasattr(client, "place_order"))

    def test_threshold_matrix_is_inclusive_and_requires_both_conditions(self) -> None:
        cases = (
            (5.0, 1.5, True),
            (-5.0, 1.5, True),
            (5.0, 1.499999, False),
            (4.999999, 2.0, False),
            (-4.999999, 2.0, False),
        )
        for change, ratio, expected in cases:
            with self.subTest(change=change, ratio=ratio):
                snapshot = MarketSnapshot(
                    asset="BTC",
                    instrument="BTC-USDT",
                    observed_at=NOW,
                    last_price=100.0,
                    change_24h_pct=change,
                    quote_volume_24h=150.0,
                    baseline_quote_volume=100.0,
                    volume_ratio=ratio,
                )
                self.assertEqual(
                    meets_material_thresholds(
                        snapshot,
                        price_move_pct=5.0,
                        volume_ratio_min=1.5,
                    ),
                    expected,
                )

    def test_gap_in_confirmed_history_is_rejected(self) -> None:
        rows = list(reversed(candle_rows(include_unconfirmed=False)))
        oldest = rows[0]
        older_timestamp = int(oldest[0]) - 3_600_000
        rows.insert(0, [str(older_timestamp), *oldest[1:]])
        del rows[80]
        opener = FakeOpener(list(reversed(rows)))
        client = OkxPublicMarketClient(market_config(), opener=opener, clock=lambda: NOW)

        with self.assertRaisesRegex(MarketDataError, "gap"):
            client.fetch_candles(ASSET)

    def test_stale_history_is_rejected(self) -> None:
        stale_now = NOW + timedelta(hours=1, minutes=1)
        client = OkxPublicMarketClient(
            market_config(),
            opener=FakeOpener(candle_rows()),
            clock=lambda: stale_now,
        )
        with self.assertRaisesRegex(MarketDataError, "stale"):
            client.fetch_snapshot(ASSET)

    def test_non_spot_or_non_allowlisted_instrument_is_rejected_before_io(self) -> None:
        opener = FakeOpener(candle_rows())
        client = OkxPublicMarketClient(market_config(), opener=opener, clock=lambda: NOW)
        invalid_assets = (
            Asset("BTC", "BTC-USDT-SWAP", ("BTC",)),
            Asset("DOGE", "DOGE-USDT", ("DOGE",)),
            Asset("BTC", "ETH-USDT", ("BTC",)),
        )
        for asset in invalid_assets:
            with self.subTest(asset=asset):
                with self.assertRaisesRegex(ValueError, "allowlisted"):
                    client.fetch_snapshot(asset)
        self.assertEqual(opener.calls, [])

    def test_non_finite_confirmed_value_is_rejected(self) -> None:
        rows = candle_rows()
        rows[10][4] = "NaN"
        client = OkxPublicMarketClient(
            market_config(),
            opener=FakeOpener(rows),
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(MarketDataError, "finite"):
            client.fetch_snapshot(ASSET)


if __name__ == "__main__":
    unittest.main()

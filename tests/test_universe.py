from __future__ import annotations

import unittest

from crypto_alerts.universe import (
    MAX_UNIVERSE_ASSETS,
    UniversePayloadError,
    Venue,
    build_universe,
    parse_binance_instruments,
    parse_okx_instruments,
)


def okx_row(
    base: str,
    *,
    quote: str = "USDT",
    state: str = "live",
    instrument_type: str = "SPOT",
    instrument: str | None = None,
) -> dict[str, object]:
    return {
        "instType": instrument_type,
        "instId": instrument or f"{base}-{quote}",
        "baseCcy": base,
        "quoteCcy": quote,
        "state": state,
    }


def okx_payload(*rows: dict[str, object]) -> dict[str, object]:
    return {"code": "0", "msg": "", "data": list(rows)}


def binance_row(
    base: str,
    *,
    quote: str = "USDT",
    status: str = "TRADING",
    spot_allowed: bool = True,
    instrument: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "symbol": instrument or f"{base}{quote}",
        "status": status,
        "baseAsset": base,
        "quoteAsset": quote,
        "isSpotTradingAllowed": spot_allowed,
        "permissions": ["SPOT"] if permissions is None else permissions,
    }


def binance_payload(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "timezone": "UTC",
        "serverTime": 1_784_110_400_000,
        "symbols": list(rows),
    }


def discovered_symbols(values: object) -> tuple[str, ...]:
    return tuple(item.symbol for item in values)


class ExchangeDiscoveryTests(unittest.TestCase):
    def test_okx_discovers_only_live_spot_usdt_instruments(self) -> None:
        payload = okx_payload(
            okx_row("BTC"),
            okx_row("ETH", state="suspend"),
            okx_row("SOL", instrument_type="SWAP", instrument="SOL-USDT-SWAP"),
            okx_row("XRP", quote="USDC"),
            okx_row("ADA"),
        )

        result = parse_okx_instruments(payload)

        self.assertEqual(discovered_symbols(result), ("ADA", "BTC"))
        self.assertEqual(tuple(item.instrument for item in result), ("ADA-USDT", "BTC-USDT"))
        self.assertTrue(all(item.venue is Venue.OKX for item in result))

    def test_binance_discovers_only_trading_spot_usdt_instruments(self) -> None:
        payload = binance_payload(
            binance_row("BTC"),
            binance_row("ETH", status="BREAK"),
            binance_row("SOL", spot_allowed=False),
            binance_row("XRP", quote="FDUSD"),
            binance_row("ADA", permissions=["MARGIN"]),
            binance_row("AVAX"),
        )

        result = parse_binance_instruments(payload)

        self.assertEqual(discovered_symbols(result), ("AVAX", "BTC"))
        self.assertEqual(tuple(item.instrument for item in result), ("AVAXUSDT", "BTCUSDT"))
        self.assertTrue(all(item.venue is Venue.BINANCE for item in result))

    def test_binance_accepts_official_permission_sets_shape(self) -> None:
        row = binance_row("BTC", permissions=[])
        row["permissionSets"] = [["SPOT", "MARGIN"]]

        result = parse_binance_instruments(binance_payload(row))

        self.assertEqual(discovered_symbols(result), ("BTC",))

    def test_rejects_unsuccessful_malformed_or_internally_inconsistent_payloads(self) -> None:
        cases = (
            ("okx-error", parse_okx_instruments, {"code": "50011", "data": []}),
            ("okx-root", parse_okx_instruments, []),
            (
                "okx-symbol-mismatch",
                parse_okx_instruments,
                okx_payload(okx_row("BTC", instrument="ETH-USDT")),
            ),
            ("binance-error", parse_binance_instruments, {"code": -1000, "msg": "error"}),
            ("binance-symbols", parse_binance_instruments, {"symbols": {}}),
            (
                "binance-symbol-mismatch",
                parse_binance_instruments,
                binance_payload(binance_row("BTC", instrument="ETHUSDT")),
            ),
        )
        for name, parser, payload in cases:
            with self.subTest(name=name):
                with self.assertRaises(UniversePayloadError):
                    parser(payload)

    def test_duplicate_instrument_inside_one_exchange_is_rejected(self) -> None:
        with self.subTest(venue="okx"):
            with self.assertRaises(UniversePayloadError):
                parse_okx_instruments(okx_payload(okx_row("BTC"), okx_row("BTC")))

        with self.subTest(venue="binance"):
            with self.assertRaises(UniversePayloadError):
                parse_binance_instruments(binance_payload(binance_row("BTC"), binance_row("BTC")))

    def test_filters_stablecoins_and_leveraged_tokens_without_dropping_jup(self) -> None:
        okx = parse_okx_instruments(
            okx_payload(
                okx_row("BTC"),
                okx_row("JUP"),
                okx_row("USDC"),
                okx_row("FDUSD"),
                okx_row("BTC3L"),
                okx_row("ETH3S"),
            )
        )
        binance = parse_binance_instruments(
            binance_payload(
                binance_row("SOL"),
                binance_row("JUP"),
                binance_row("TUSD"),
                binance_row("DAI"),
                binance_row("BTCUP"),
                binance_row("BTCDOWN"),
                binance_row("ETHBULL"),
                binance_row("ETHBEAR"),
            )
        )

        self.assertEqual(discovered_symbols(okx), ("BTC", "JUP"))
        self.assertEqual(discovered_symbols(binance), ("JUP", "SOL"))


class UniverseAssemblyTests(unittest.TestCase):
    def test_union_deduplicates_cross_exchange_asset_and_preserves_provenance(self) -> None:
        okx = parse_okx_instruments(okx_payload(okx_row("ETH"), okx_row("BTC")))
        binance = parse_binance_instruments(binance_payload(binance_row("SOL"), binance_row("BTC")))

        result = build_universe(okx, binance, max_assets=10)

        self.assertEqual(discovered_symbols(result), ("BTC", "ETH", "SOL"))
        btc = result[0]
        self.assertEqual(
            tuple((item.venue, item.instrument) for item in btc.instruments),
            ((Venue.OKX, "BTC-USDT"), (Venue.BINANCE, "BTCUSDT")),
        )
        self.assertEqual(len(result[1].instruments), 1)
        self.assertEqual(len(result[2].instruments), 1)

    def test_payload_and_exchange_order_do_not_change_canonical_output(self) -> None:
        okx_forward = parse_okx_instruments(
            okx_payload(okx_row("SOL"), okx_row("BTC"), okx_row("ETH"))
        )
        okx_reverse = parse_okx_instruments(
            okx_payload(okx_row("ETH"), okx_row("BTC"), okx_row("SOL"))
        )
        binance_forward = parse_binance_instruments(
            binance_payload(binance_row("AVAX"), binance_row("BTC"), binance_row("ADA"))
        )
        binance_reverse = parse_binance_instruments(
            binance_payload(binance_row("ADA"), binance_row("BTC"), binance_row("AVAX"))
        )

        first = build_universe(okx_forward, binance_forward, max_assets=10)
        second = build_universe(okx_reverse, binance_reverse, max_assets=10)

        self.assertEqual(first, second)
        self.assertEqual(discovered_symbols(first), ("ADA", "AVAX", "BTC", "ETH", "SOL"))

    def test_cap_is_checked_after_union_and_deduplication_without_truncation(self) -> None:
        okx = parse_okx_instruments(
            okx_payload(okx_row("ZEC"), okx_row("BTC"), okx_row("SOL"), okx_row("ADA"))
        )
        binance = parse_binance_instruments(
            binance_payload(
                binance_row("XRP"),
                binance_row("BTC"),
                binance_row("ETH"),
                binance_row("AVAX"),
            )
        )

        with self.assertRaisesRegex(UniversePayloadError, "above max_assets"):
            build_universe(okx, binance, max_assets=3)

    def test_cap_must_be_a_non_boolean_integer_within_the_hard_limit(self) -> None:
        okx = parse_okx_instruments(okx_payload(okx_row("BTC")))
        invalid_values = (True, 0, -1, 1.5, MAX_UNIVERSE_ASSETS + 1)
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    build_universe(okx, (), max_assets=value)


if __name__ == "__main__":
    unittest.main()

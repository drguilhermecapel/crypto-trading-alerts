"""Read-only OKX spot market data with fail-closed validation.

Only the public candlestick endpoint is exposed.  This module intentionally has
no credential fields and no order-related methods.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .config import EXPECTED_SYMBOLS, MarketConfig
from .models import Asset, Candle, MarketSnapshot

ONE_HOUR = timedelta(hours=1)
MINIMUM_HISTORY_HOURS = 8 * 24
MAX_OKX_CANDLES = 300
MAX_RESPONSE_BYTES = 2_000_000
OKX_PUBLIC_HOSTS = frozenset({"www.okx.com", "my.okx.com"})
ALLOWED_INSTRUMENTS = frozenset(f"{symbol}-USDT" for symbol in EXPECTED_SYMBOLS)


class MarketDataError(RuntimeError):
    """Raised when public market data is unavailable, stale, or invalid."""


class _Response(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def geturl(self) -> str: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


OpenUrl = Callable[..., _Response]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class MarketAssessment:
    """A snapshot plus the exact, inclusive materiality decision."""

    snapshot: MarketSnapshot
    price_threshold_met: bool
    volume_threshold_met: bool
    material: bool
    evidence_url: str


def meets_material_thresholds(
    snapshot: MarketSnapshot,
    *,
    price_move_pct: float,
    volume_ratio_min: float,
) -> bool:
    """Return true only when both configured thresholds are met inclusively."""

    values = (
        snapshot.change_24h_pct,
        snapshot.volume_ratio,
        price_move_pct,
        volume_ratio_min,
    )
    if not all(math.isfinite(value) for value in values):
        return False
    return _at_least(abs(snapshot.change_24h_pct), price_move_pct) and _at_least(
        snapshot.volume_ratio, volume_ratio_min
    )


def _at_least(value: float, threshold: float) -> bool:
    return value >= threshold or math.isclose(value, threshold, rel_tol=1e-12, abs_tol=1e-12)


def _ema(values: list[float], period: int) -> float:
    """Return a conventional exponentially weighted moving average."""

    if not values or period < 1:
        raise MarketDataError("EMA requires prices and a positive period")
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi(values: list[float], period: int = 14) -> float:
    """Calculate bounded RSI from the latest closed-candle changes."""

    if len(values) < period + 1:
        raise MarketDataError("RSI history is incomplete")
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    recent = changes[-period:]
    gains = math.fsum(max(change, 0.0) for change in recent) / period
    losses = math.fsum(max(-change, 0.0) for change in recent) / period
    if gains == 0 and losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def _realized_volatility_pct(values: list[float], hours: int = 24) -> float:
    """Return square-root-of-summed-squares realized volatility, in percent."""

    if len(values) < hours + 1:
        raise MarketDataError("volatility history is incomplete")
    recent = values[-(hours + 1) :]
    returns = [
        math.log(current / previous)
        for previous, current in zip(recent[:-1], recent[1:], strict=True)
    ]
    return math.sqrt(math.fsum(value**2 for value in returns)) * 100.0


class OkxPublicMarketClient:
    """Injectable, standard-library-only client for confirmed OKX 1H candles."""

    def __init__(
        self,
        config: MarketConfig,
        *,
        opener: OpenUrl = urlopen,
        clock: Clock | None = None,
    ) -> None:
        parsed = urlparse(config.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in OKX_PUBLIC_HOSTS
            or parsed.username
            or parsed.password
        ):
            raise ValueError("OKX base URL must be an official credential-free HTTPS URL")
        if config.lookback_hours != 24:
            raise ValueError("market lookback must be exactly 24 hours")
        if config.volume_baseline_days != 7:
            raise ValueError("volume baseline must contain exactly seven daily blocks")
        requested_hours = max(
            MINIMUM_HISTORY_HOURS,
            config.lookback_hours * (config.volume_baseline_days + 1),
        )
        if requested_hours + 1 > MAX_OKX_CANDLES:
            raise ValueError("requested history exceeds the OKX public endpoint limit")

        self._config = config
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(UTC))
        self._history_hours = requested_hours
        self._request_limit = requested_hours + 1

    def evidence_url(self, asset: Asset) -> str:
        """Return the exact public OKX API URL used as primary evidence."""

        self._validate_asset(asset)
        query = urlencode(
            {
                "instId": asset.instrument,
                "bar": "1H",
                "limit": str(self._request_limit),
            }
        )
        return f"{self._config.base_url}/api/v5/market/candles?{query}"

    def fetch_candles(self, asset: Asset) -> tuple[Candle, ...]:
        """Fetch exactly the latest validated history ending in a closed candle."""

        url = self.evidence_url(asset)
        payload = self._get_json(url)
        rows = payload.get("data")
        if payload.get("code") != "0" or not isinstance(rows, list):
            message = payload.get("msg")
            suffix = f": {message}" if isinstance(message, str) and message else ""
            raise MarketDataError(f"OKX returned an unsuccessful candle response{suffix}")

        confirmed: list[Candle] = []
        seen_timestamps: set[datetime] = set()
        for index, row in enumerate(rows):
            candle = self._parse_row(row, index)
            opened_at = self._row_timestamp(row, index)
            if opened_at in seen_timestamps:
                raise MarketDataError(f"duplicate candle timestamp at row {index}")
            seen_timestamps.add(opened_at)
            if candle is not None:
                confirmed.append(candle)

        confirmed.sort(key=lambda item: item.opened_at)
        if len(confirmed) < self._history_hours:
            raise MarketDataError(
                f"expected at least {self._history_hours} confirmed 1H candles, "
                f"received {len(confirmed)}"
            )
        candles = confirmed[-self._history_hours :]
        self._validate_series(candles)
        return tuple(candles)

    def fetch_snapshot(self, asset: Asset) -> MarketSnapshot:
        """Calculate explainable technical features from confirmed closed candles."""

        candles = self.fetch_candles(asset)
        lookback = self._config.lookback_hours
        baseline_count = lookback * self._config.volume_baseline_days
        calculation = candles[-(baseline_count + lookback) :]
        baseline = calculation[:baseline_count]
        current = calculation[baseline_count:]

        daily_volumes = [
            math.fsum(candle.quote_volume for candle in baseline[offset : offset + lookback])
            for offset in range(0, baseline_count, lookback)
        ]
        baseline_volume = float(median(daily_volumes))
        if not math.isfinite(baseline_volume) or baseline_volume <= 0:
            raise MarketDataError("baseline quote volume must be positive and finite")

        current_volume = math.fsum(candle.quote_volume for candle in current)
        starting_price = current[0].open
        last_price = current[-1].close
        change_pct = ((last_price / starting_price) - 1.0) * 100.0
        volume_ratio = current_volume / baseline_volume
        closes = [candle.close for candle in candles]
        change_72h_pct = ((last_price / candles[-72].open) - 1.0) * 100.0
        ema_24h = _ema(closes, 24)
        ema_72h = _ema(closes, 72)
        trend_spread_pct = ((ema_24h / ema_72h) - 1.0) * 100.0
        rsi_14h = _rsi(closes)
        realized_volatility = _realized_volatility_pct(closes)
        seven_day_high = max(candle.high for candle in candles[-168:])
        drawdown_7d_pct = ((last_price / seven_day_high) - 1.0) * 100.0
        metrics = (
            current_volume,
            starting_price,
            last_price,
            change_pct,
            volume_ratio,
            change_72h_pct,
            ema_24h,
            ema_72h,
            trend_spread_pct,
            rsi_14h,
            realized_volatility,
            drawdown_7d_pct,
        )
        if not all(math.isfinite(value) for value in metrics):
            raise MarketDataError("calculated market metrics must be finite")

        return MarketSnapshot(
            asset=asset.symbol,
            instrument=asset.instrument,
            observed_at=current[-1].opened_at + ONE_HOUR,
            last_price=last_price,
            change_24h_pct=change_pct,
            quote_volume_24h=current_volume,
            baseline_quote_volume=baseline_volume,
            volume_ratio=volume_ratio,
            change_72h_pct=change_72h_pct,
            rsi_14h=rsi_14h,
            ema_24h=ema_24h,
            ema_72h=ema_72h,
            trend_spread_pct=trend_spread_pct,
            realized_volatility_24h_pct=realized_volatility,
            drawdown_7d_pct=drawdown_7d_pct,
        )

    def assess(self, asset: Asset) -> MarketAssessment:
        """Fetch a snapshot and apply the configured inclusive thresholds."""

        snapshot = self.fetch_snapshot(asset)
        price_met = _at_least(abs(snapshot.change_24h_pct), self._config.price_move_pct)
        volume_met = _at_least(snapshot.volume_ratio, self._config.volume_ratio_min)
        return MarketAssessment(
            snapshot=snapshot,
            price_threshold_met=price_met,
            volume_threshold_met=volume_met,
            material=price_met and volume_met,
            evidence_url=self.evidence_url(asset),
        )

    def _get_json(self, url: str) -> dict[str, Any]:
        request = Request(  # noqa: S310 - evidence_url validates the official HTTPS host.
            url,
            headers={"Accept": "application/json", "User-Agent": "crypto-alerts/2.0"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._config.request_timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise MarketDataError(f"OKX returned HTTP {status}")
                final_url = getattr(response, "geturl", lambda: url)()
                final_host = urlparse(final_url).hostname
                if final_host not in OKX_PUBLIC_HOSTS:
                    raise MarketDataError("OKX request redirected to a non-official host")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise MarketDataError(f"cannot fetch OKX public candles: {exc}") from exc

        if len(raw) > MAX_RESPONSE_BYTES:
            raise MarketDataError("OKX response exceeds the size limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataError("OKX returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise MarketDataError("OKX response root must be an object")
        return decoded

    @staticmethod
    def _validate_asset(asset: Asset) -> None:
        expected = f"{asset.symbol}-USDT"
        if (
            asset.symbol not in EXPECTED_SYMBOLS
            or asset.instrument not in ALLOWED_INSTRUMENTS
            or asset.instrument != expected
        ):
            raise ValueError("only allowlisted SYMBOL-USDT spot instruments are supported")

    @staticmethod
    def _row_timestamp(row: object, index: int) -> datetime:
        if not isinstance(row, list) or len(row) < 9:
            raise MarketDataError(f"candle row {index} must contain at least 9 fields")
        raw_timestamp = row[0]
        if isinstance(raw_timestamp, bool):
            raise MarketDataError(f"candle row {index} has an invalid timestamp")
        try:
            timestamp_ms = int(raw_timestamp)
        except (TypeError, ValueError) as exc:
            raise MarketDataError(f"candle row {index} has an invalid timestamp") from exc
        if str(timestamp_ms) != str(raw_timestamp):
            raise MarketDataError(f"candle row {index} timestamp must be integer milliseconds")
        if timestamp_ms <= 0 or timestamp_ms % 3_600_000 != 0:
            raise MarketDataError(f"candle row {index} is not aligned to a UTC hour")
        try:
            return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise MarketDataError(
                f"candle row {index} timestamp is outside the supported range"
            ) from exc

    @classmethod
    def _parse_row(cls, row: object, index: int) -> Candle | None:
        opened_at = cls._row_timestamp(row, index)
        row_values = cast(list[object], row)
        confirmation = row_values[8]
        if confirmation not in {"0", "1"}:
            raise MarketDataError(f"candle row {index} has an invalid confirmation flag")
        if confirmation == "0":
            return None

        values = [
            cls._number(row_values[position], index, position) for position in (1, 2, 3, 4, 7)
        ]
        open_price, high, low, close, quote_volume = values
        if min(open_price, high, low, close) <= 0:
            raise MarketDataError(f"candle row {index} prices must be positive")
        if quote_volume < 0:
            raise MarketDataError(f"candle row {index} quote volume cannot be negative")
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise MarketDataError(f"candle row {index} has inconsistent OHLC bounds")
        return Candle(opened_at, open_price, high, low, close, quote_volume)

    @staticmethod
    def _number(value: object, row: int, position: int) -> float:
        if isinstance(value, bool):
            raise MarketDataError(f"candle row {row} field {position} must be numeric")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketDataError(f"candle row {row} field {position} must be numeric") from exc
        if not math.isfinite(result):
            raise MarketDataError(f"candle row {row} field {position} must be finite")
        return result

    def _validate_series(self, candles: list[Candle]) -> None:
        for previous, current in zip(candles, candles[1:], strict=False):
            if current.opened_at - previous.opened_at != ONE_HOUR:
                raise MarketDataError("confirmed candle history contains a gap")

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise MarketDataError("market clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        latest_close = candles[-1].opened_at + ONE_HOUR
        if latest_close > now:
            raise MarketDataError("latest confirmed candle has not closed yet")
        if now - latest_close > ONE_HOUR:
            raise MarketDataError("latest confirmed candle is stale")


# Conventional initialism spelling for callers that prefer it.
OKXPublicMarketClient = OkxPublicMarketClient


__all__ = [
    "ALLOWED_INSTRUMENTS",
    "MarketAssessment",
    "MarketDataError",
    "OKXPublicMarketClient",
    "OkxPublicMarketClient",
    "meets_material_thresholds",
]

"""Strict configuration loader for the alert-only monitor."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Asset

EXPECTED_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "ADA", "SEI", "APT", "AVAX")
DEFAULT_ALIASES: Mapping[str, tuple[str, ...]] = {
    "BTC": ("BTC", "Bitcoin"),
    "ETH": ("ETH", "Ethereum", "Ether"),
    "SOL": ("SOL", "Solana"),
    "XRP": ("XRP", "Ripple"),
    "ADA": ("ADA", "Cardano"),
    "SEI": ("SEI", "Sei Network"),
    "APT": ("APT", "Aptos"),
    "AVAX": ("AVAX", "Avalanche"),
}


class ConfigError(ValueError):
    """Raised when configuration is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class MarketConfig:
    base_url: str
    price_move_pct: float
    volume_ratio_min: float
    lookback_hours: int
    volume_baseline_days: int
    request_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class FeedConfig:
    name: str
    url: str
    required: bool


@dataclass(frozen=True, slots=True)
class NewsConfig:
    lookback_hours: int
    minimum_successful_feeds: int
    feeds: tuple[FeedConfig, ...]


@dataclass(frozen=True, slots=True)
class RiskConfig:
    spot_only: bool
    autonomous_trading: bool
    max_holdings: int
    max_asset_weight: float
    risk_per_trade: float
    weekly_loss_cap: float


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    """Heuristic engine plus an optional, non-authoritative model review."""

    engine: str
    openai_enabled: bool
    openai_model: str
    openai_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    send_empty_digest: bool
    telegram_enabled: bool
    email_enabled: bool


@dataclass(frozen=True, slots=True)
class StateConfig:
    path: str
    dedupe_hours: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    version: int
    mode: str
    timezone: str
    assets: tuple[Asset, ...]
    market: MarketConfig
    news: NewsConfig
    risk: RiskConfig
    analysis: AnalysisConfig
    delivery: DeliveryConfig
    state: StateConfig


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be an object")
    return value


def _keys(data: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"{field} has unknown keys: {', '.join(sorted(unknown))}")


def _https_url(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be a string")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigError(f"{field} must be an HTTPS URL without embedded credentials")
    return value.rstrip("/")


def _number(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{field} must be numeric")
    result = float(value)
    if not low <= result <= high:
        raise ConfigError(f"{field} must be between {low} and {high}")
    return result


def _integer(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{field} must be an integer between {low} and {high}")
    return value


def load_config(path: str | Path) -> AppConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read configuration: {exc}") from exc
    data = _object(raw, "root")
    _keys(
        data,
        {
            "version",
            "mode",
            "timezone",
            "assets",
            "market",
            "news",
            "risk",
            "analysis",
            "delivery",
            "state",
        },
        "root",
    )

    if data.get("version") != 1:
        raise ConfigError("version must be 1")
    if data.get("mode") != "alert_only":
        raise ConfigError("mode must be alert_only; order execution is not supported")
    timezone_name = data.get("timezone")
    if not isinstance(timezone_name, str):
        raise ConfigError("timezone must be a string")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown timezone: {timezone_name}") from exc

    asset_values = data.get("assets")
    if not isinstance(asset_values, list) or not all(
        isinstance(item, str) for item in asset_values
    ):
        raise ConfigError("assets must be a list of symbols")
    symbols = tuple(item.upper() for item in asset_values)
    if symbols != EXPECTED_SYMBOLS:
        raise ConfigError(f"assets must exactly equal {list(EXPECTED_SYMBOLS)}")
    assets = tuple(
        Asset(symbol=s, instrument=f"{s}-USDT", aliases=DEFAULT_ALIASES[s]) for s in symbols
    )

    market_raw = _object(data.get("market"), "market")
    _keys(
        market_raw,
        {
            "base_url",
            "price_move_pct",
            "volume_ratio_min",
            "lookback_hours",
            "volume_baseline_days",
            "request_timeout_seconds",
        },
        "market",
    )
    price_move_pct = _number(market_raw.get("price_move_pct"), "market.price_move_pct", 5.0, 5.0)
    volume_ratio_min = _number(
        market_raw.get("volume_ratio_min"), "market.volume_ratio_min", 1.5, 1.5
    )
    market = MarketConfig(
        base_url=_https_url(market_raw.get("base_url"), "market.base_url"),
        price_move_pct=price_move_pct,
        volume_ratio_min=volume_ratio_min,
        lookback_hours=_integer(market_raw.get("lookback_hours"), "market.lookback_hours", 24, 24),
        volume_baseline_days=_integer(
            market_raw.get("volume_baseline_days"), "market.volume_baseline_days", 7, 7
        ),
        request_timeout_seconds=_number(
            market_raw.get("request_timeout_seconds"), "market.request_timeout_seconds", 1.0, 30.0
        ),
    )
    if urlparse(market.base_url).hostname not in {"www.okx.com", "my.okx.com"}:
        raise ConfigError("market.base_url must be an official OKX host")

    news_raw = _object(data.get("news"), "news")
    _keys(news_raw, {"lookback_hours", "minimum_successful_feeds", "feeds"}, "news")
    feeds_raw = news_raw.get("feeds")
    if not isinstance(feeds_raw, list) or not feeds_raw:
        raise ConfigError("news.feeds must be a non-empty list")
    feeds: list[FeedConfig] = []
    for index, value in enumerate(feeds_raw):
        feed = _object(value, f"news.feeds[{index}]")
        _keys(feed, {"name", "url", "required"}, f"news.feeds[{index}]")
        if not isinstance(feed.get("name"), str) or not feed["name"].strip():
            raise ConfigError(f"news.feeds[{index}].name must be non-empty")
        if not isinstance(feed.get("required"), bool):
            raise ConfigError(f"news.feeds[{index}].required must be boolean")
        feeds.append(
            FeedConfig(
                feed["name"].strip(),
                _https_url(feed.get("url"), f"news.feeds[{index}].url"),
                feed["required"],
            )
        )
    news = NewsConfig(
        lookback_hours=_integer(news_raw.get("lookback_hours"), "news.lookback_hours", 1, 72),
        minimum_successful_feeds=_integer(
            news_raw.get("minimum_successful_feeds"), "news.minimum_successful_feeds", 1, len(feeds)
        ),
        feeds=tuple(feeds),
    )

    risk_raw = _object(data.get("risk"), "risk")
    _keys(
        risk_raw,
        {
            "spot_only",
            "autonomous_trading",
            "max_holdings",
            "max_asset_weight",
            "risk_per_trade",
            "weekly_loss_cap",
        },
        "risk",
    )
    risk = RiskConfig(
        spot_only=risk_raw.get("spot_only"),
        autonomous_trading=risk_raw.get("autonomous_trading"),
        max_holdings=_integer(risk_raw.get("max_holdings"), "risk.max_holdings", 1, 5),
        max_asset_weight=_number(
            risk_raw.get("max_asset_weight"), "risk.max_asset_weight", 0.01, 0.40
        ),
        risk_per_trade=_number(risk_raw.get("risk_per_trade"), "risk.risk_per_trade", 0.0001, 0.01),
        weekly_loss_cap=_number(
            risk_raw.get("weekly_loss_cap"), "risk.weekly_loss_cap", -0.06, -0.001
        ),
    )
    if risk.spot_only is not True or risk.autonomous_trading is not False:
        raise ConfigError("risk must enforce spot_only=true and autonomous_trading=false")

    analysis_raw = _object(data.get("analysis"), "analysis")
    _keys(
        analysis_raw,
        {"engine", "openai_enabled", "openai_model", "openai_timeout_seconds"},
        "analysis",
    )
    if analysis_raw.get("engine") != "fuzzy_expert":
        raise ConfigError("analysis.engine must be fuzzy_expert")
    if not isinstance(analysis_raw.get("openai_enabled"), bool):
        raise ConfigError("analysis.openai_enabled must be boolean")
    if analysis_raw.get("openai_model") != "gpt-5.6":
        raise ConfigError("analysis.openai_model must be gpt-5.6")
    analysis = AnalysisConfig(
        engine="fuzzy_expert",
        openai_enabled=analysis_raw["openai_enabled"],
        openai_model="gpt-5.6",
        openai_timeout_seconds=_number(
            analysis_raw.get("openai_timeout_seconds"),
            "analysis.openai_timeout_seconds",
            1.0,
            30.0,
        ),
    )

    delivery_raw = _object(data.get("delivery"), "delivery")
    _keys(delivery_raw, {"send_empty_digest", "telegram_enabled", "email_enabled"}, "delivery")
    if not all(
        isinstance(delivery_raw.get(key), bool)
        for key in ("send_empty_digest", "telegram_enabled", "email_enabled")
    ):
        raise ConfigError("delivery flags must be boolean")
    delivery = DeliveryConfig(**delivery_raw)

    state_raw = _object(data.get("state"), "state")
    _keys(state_raw, {"path", "dedupe_hours"}, "state")
    if not isinstance(state_raw.get("path"), str) or not state_raw["path"].strip():
        raise ConfigError("state.path must be non-empty")
    state_path = Path(state_raw["path"])
    if state_path.is_absolute() or ".." in state_path.parts or state_path.name in {"", "."}:
        raise ConfigError("state.path must be a relative file path without parent traversal")
    state = StateConfig(
        state_raw["path"], _integer(state_raw.get("dedupe_hours"), "state.dedupe_hours", 24, 720)
    )

    return AppConfig(
        1,
        "alert_only",
        timezone_name,
        assets,
        market,
        news,
        risk,
        analysis,
        delivery,
        state,
    )

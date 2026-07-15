"""Domain models shared by the monitor, reporters, and tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventCategory(StrEnum):
    PRICE_VOLUME = "price_volume"
    ONCHAIN_ECOSYSTEM = "onchain_ecosystem"
    EXCHANGE_LIQUIDITY = "exchange_liquidity"
    NETWORK_UPGRADE = "network_upgrade"
    OUTAGE_EXPLOIT = "outage_exploit"
    ETF_INSTITUTIONAL = "etf_institutional"
    REGULATORY_LEGAL = "regulatory_legal"


class SourceQuality(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AnalysisType(StrEnum):
    TECHNICAL = "technical"
    FUNDAMENTAL = "fundamental"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class Asset:
    symbol: str
    instrument: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Candle:
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    asset: str
    instrument: str
    observed_at: datetime
    last_price: float
    change_24h_pct: float
    quote_volume_24h: float
    baseline_quote_volume: float
    volume_ratio: float


@dataclass(frozen=True, slots=True)
class NewsItem:
    title: str
    url: str
    summary: str
    published_at: datetime
    source_name: str
    source_domain: str
    source_quality: SourceQuality


@dataclass(frozen=True, slots=True)
class AlertEvent:
    event_id: str
    asset: str
    category: EventCategory
    catalyst: str
    evidence_urls: tuple[str, ...]
    source_quality: SourceQuality
    probable_market_impact: str
    main_risk: str
    technical_vs_fundamental: AnalysisType
    observed_at: datetime
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        observed = self.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        return {
            "event_id": self.event_id,
            "asset": self.asset,
            "category": self.category.value,
            "catalyst": self.catalyst,
            "evidence_urls": list(self.evidence_urls),
            "source_quality": self.source_quality.value,
            "probable_market_impact": self.probable_market_impact,
            "main_risk": self.main_risk,
            "technical_vs_fundamental": self.technical_vs_fundamental.value,
            "observed_at": observed.astimezone(UTC).isoformat(),
            "metrics": dict(self.metrics),
        }

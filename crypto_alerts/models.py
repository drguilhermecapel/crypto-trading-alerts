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


class RecommendationAction(StrEnum):
    """Non-executable position guidance produced for each allowlisted asset."""

    BUY = "BUY"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"
    NOT_RATED = "NOT_RATED"


class RecommendationSource(StrEnum):
    """Auditable origin of a recommendation."""

    FUZZY_EXPERT = "fuzzy_expert"
    HYBRID_CONSENSUS = "hybrid_consensus"


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
    change_72h_pct: float = 0.0
    rsi_14h: float = 50.0
    ema_24h: float = 0.0
    ema_72h: float = 0.0
    trend_spread_pct: float = 0.0
    realized_volatility_24h_pct: float = 0.0
    drawdown_7d_pct: float = 0.0
    exchange: str = "okx"
    available_exchanges: tuple[str, ...] = ()
    venue_instrument: str | None = None


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


@dataclass(frozen=True, slots=True)
class TokenRecommendation:
    """Explainable advice only; never an instruction accepted by an exchange."""

    asset: str
    action: RecommendationAction
    signal_strength: float
    score: float
    technical_score: float
    fundamental_score: float
    model_source: RecommendationSource
    rationale: str
    primary_risk: str
    evidence_urls: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    technical_metrics: Mapping[str, float]
    generated_at: datetime
    model_action: RecommendationAction | None = None
    model_signal_strength: float | None = None
    model_rationale: str | None = None
    model_primary_risk: str | None = None
    model_status: str = "not_requested"
    model_input_hash: str | None = None
    prompt_version: str | None = None
    model_name: str | None = None
    model_evidence_event_ids: tuple[str, ...] = ()
    risk_per_trade_cap_pct: float = 1.0
    max_asset_weight_pct: float = 40.0
    advisory_only: bool = True
    execution_allowed: bool = False
    analysis_status: str = "analyzed"
    analysis_reason: str | None = None
    market_exchange: str | None = None
    market_instrument: str | None = None
    available_exchanges: tuple[str, ...] = ()
    universe_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        generated = self.generated_at
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=UTC)
        return {
            "asset": self.asset,
            "action": self.action.value,
            "signal_strength": self.signal_strength,
            "signal_strength_is_probability": False,
            "score": self.score,
            "technical_score": self.technical_score,
            "fundamental_score": self.fundamental_score,
            "model_source": self.model_source.value,
            "rationale": self.rationale,
            "primary_risk": self.primary_risk,
            "evidence_urls": list(self.evidence_urls),
            "evidence_event_ids": list(self.evidence_event_ids),
            "technical_metrics": dict(self.technical_metrics),
            "generated_at": generated.astimezone(UTC).isoformat(),
            "model_action": self.model_action.value if self.model_action else None,
            "model_signal_strength": self.model_signal_strength,
            "model_rationale": self.model_rationale,
            "model_primary_risk": self.model_primary_risk,
            "model_status": self.model_status,
            "model_input_hash": self.model_input_hash,
            "prompt_version": self.prompt_version,
            "model_name": self.model_name,
            "model_evidence_event_ids": list(self.model_evidence_event_ids),
            "risk_per_trade_cap_pct": self.risk_per_trade_cap_pct,
            "max_asset_weight_pct": self.max_asset_weight_pct,
            "advisory_only": self.advisory_only,
            "execution_allowed": self.execution_allowed,
            "analysis_status": self.analysis_status,
            "analysis_reason": self.analysis_reason,
            "market_exchange": self.market_exchange,
            "market_instrument": self.market_instrument,
            "available_exchanges": list(self.available_exchanges),
            "universe_hash": self.universe_hash,
        }

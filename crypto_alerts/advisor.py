"""Deterministic, explainable token recommendations.

This module implements an uncalibrated fuzzy expert heuristic.  Its scores and
``signal_strength`` are rulebook diagnostics, not probabilities, forecasts, or
instructions that can be submitted to an exchange.  The implementation is pure:
it performs no I/O and only uses the evidence supplied by the caller.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import urlparse

from .config import EXPECTED_SYMBOLS
from .market import MarketAssessment
from .models import (
    AlertEvent,
    EventCategory,
    RecommendationAction,
    RecommendationSource,
    SourceQuality,
    TokenRecommendation,
)
from .news import source_quality_for_url

_TECHNICAL_WEIGHTS = (0.30, 0.20, 0.25, 0.10, 0.15)
_QUALITY_WEIGHT = {
    SourceQuality.HIGH: 1.0,
    SourceQuality.MEDIUM: 0.65,
    SourceQuality.LOW: 0.0,
}
_CATEGORY_PRIOR = {
    EventCategory.ONCHAIN_ECOSYSTEM: 0.0,
    EventCategory.EXCHANGE_LIQUIDITY: 0.0,
    EventCategory.NETWORK_UPGRADE: 0.0,
    EventCategory.OUTAGE_EXPLOIT: -85.0,
    EventCategory.ETF_INSTITUTIONAL: 0.0,
    EventCategory.REGULATORY_LEGAL: 0.0,
}
_POSITIVE_TERMS = (
    "approval",
    "approved",
    "adoption",
    "adopts",
    "upgrade",
    "launch",
    "launched",
    "partnership",
    "integrates",
    "integration",
    "inflow",
    "record growth",
    "recovery",
    "restored",
    "resolved",
    "listing",
    "aprovado",
    "adoção",
    "atualização",
    "lançamento",
)
_NEGATIVE_TERMS = (
    "exploit",
    "hack",
    "breach",
    "outage",
    "attack",
    "vulnerability",
    "ban",
    "banned",
    "lawsuit",
    "investigation",
    "delisting",
    "delisted",
    "insolvency",
    "liquidation",
    "outflow",
    "delay",
    "decline",
    "crash",
    "fraud",
    "fraude",
    "ataque",
    "falha",
    "proibição",
    "sues",
    "sued",
    "delist",
    "delists",
    "outflows",
)
_INFLECTED_POSITIVE_TERMS = (
    "approve",
    "approves",
    "lists",
    "inflows",
    "patched",
    "fixed",
    "contained",
)
_RESOLUTION_TERMS = ("resolved", "restored", "patched", "fixed", "contained", "recovered")
_ACTIVE_INCIDENT_TERMS = ("active", "ongoing", "unresolved", "remain", "remains", "continuing")


@dataclass(frozen=True, slots=True)
class _TechnicalResult:
    score: float
    signal_strength: float
    action: RecommendationAction
    s24: float
    s72: float
    trend: float
    rsi: float
    drawdown: float
    volume_confirmation: float
    agreement: float
    high_volatility: float


def _clip(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("analysis timestamps must include a timezone")
    return value.astimezone(UTC)


def _validate_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _validate_assessment(assessment: MarketAssessment, expected_symbol: str) -> None:
    if not isinstance(assessment, MarketAssessment):
        raise ValueError("assessments must contain MarketAssessment values")
    snapshot = assessment.snapshot
    if snapshot.asset != expected_symbol:
        raise ValueError(
            f"assessment order/coverage mismatch: expected {expected_symbol}, got {snapshot.asset}"
        )
    if snapshot.instrument != f"{expected_symbol}-USDT":
        raise ValueError(f"unexpected instrument for {expected_symbol}")
    parsed_evidence = (
        urlparse(assessment.evidence_url)
        if isinstance(assessment.evidence_url, str)
        else None
    )
    if (
        parsed_evidence is None
        or parsed_evidence.scheme != "https"
        or parsed_evidence.hostname not in {"www.okx.com", "my.okx.com"}
    ):
        raise ValueError(f"{expected_symbol} assessment must have an official evidence URL")
    _utc(snapshot.observed_at)
    flags = (
        assessment.price_threshold_met,
        assessment.volume_threshold_met,
        assessment.material,
    )
    if not all(isinstance(value, bool) for value in flags):
        raise ValueError(f"{expected_symbol} materiality flags must be boolean")
    if assessment.material != (
        assessment.price_threshold_met and assessment.volume_threshold_met
    ):
        raise ValueError(f"{expected_symbol} materiality flags are inconsistent")

    positive = {
        "last_price": snapshot.last_price,
        "baseline_quote_volume": snapshot.baseline_quote_volume,
        "ema_24h": snapshot.ema_24h,
        "ema_72h": snapshot.ema_72h,
    }
    checked = {name: _validate_number(name, value) for name, value in positive.items()}
    for name, value in checked.items():
        if value <= 0.0:
            raise ValueError(f"{name} must be positive")

    quote_volume = _validate_number("quote_volume_24h", snapshot.quote_volume_24h)
    volume_ratio = _validate_number("volume_ratio", snapshot.volume_ratio)
    if quote_volume < 0.0 or volume_ratio < 0.0:
        raise ValueError(f"{expected_symbol} volumes and ratios cannot be negative")

    change_24h = _validate_number("change_24h_pct", snapshot.change_24h_pct)
    change_72h = _validate_number("change_72h_pct", snapshot.change_72h_pct)
    trend = _validate_number("trend_spread_pct", snapshot.trend_spread_pct)
    rsi = _validate_number("rsi_14h", snapshot.rsi_14h)
    volatility = _validate_number(
        "realized_volatility_24h_pct", snapshot.realized_volatility_24h_pct
    )
    drawdown = _validate_number("drawdown_7d_pct", snapshot.drawdown_7d_pct)
    if change_24h <= -100.0 or change_72h <= -100.0 or trend <= -100.0:
        raise ValueError(f"{expected_symbol} percentage changes must be greater than -100")
    if not 0.0 <= rsi <= 100.0:
        raise ValueError(f"{expected_symbol} RSI must be between 0 and 100")
    if volatility < 0.0:
        raise ValueError(f"{expected_symbol} realized volatility cannot be negative")
    if not -100.0 <= drawdown <= 0.0:
        raise ValueError(f"{expected_symbol} drawdown must be between -100 and 0")
    calculated_ratio = quote_volume / checked["baseline_quote_volume"]
    if not math.isclose(volume_ratio, calculated_ratio, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError(f"{expected_symbol} volume ratio is inconsistent")
    calculated_trend = ((checked["ema_24h"] / checked["ema_72h"]) - 1.0) * 100.0
    if not math.isclose(trend, calculated_trend, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(f"{expected_symbol} EMA trend is inconsistent")


def _classify(
    score: float,
    signal_strength: float,
    s24: float,
    trend: float,
) -> RecommendationAction:
    if score >= 60.0 and signal_strength >= 0.70 and s24 > 0.0 and trend > 0.0:
        return RecommendationAction.BUY
    if score <= -60.0 and signal_strength >= 0.70 and s24 < 0.0 and trend < 0.0:
        return RecommendationAction.SELL
    if score <= -25.0:
        return RecommendationAction.REDUCE
    return RecommendationAction.HOLD


def _technical_result(assessment: MarketAssessment) -> _TechnicalResult:
    snapshot = assessment.snapshot
    s24 = _clip(snapshot.change_24h_pct / 5.0, -1.0, 1.0)
    s72 = _clip(snapshot.change_72h_pct / 12.0, -1.0, 1.0)
    trend = _clip(snapshot.trend_spread_pct / 2.5, -1.0, 1.0)
    base_rsi = _clip((snapshot.rsi_14h - 50.0) / 20.0, -1.0, 1.0)
    if snapshot.rsi_14h > 70.0:
        srsi = _clip((85.0 - snapshot.rsi_14h) / 15.0, 0.0, 1.0)
    else:
        srsi = base_rsi
    sdd = 2.0 * _clip((snapshot.drawdown_7d_pct + 15.0) / 12.0, 0.0, 1.0) - 1.0
    vconf = _clip((snapshot.volume_ratio - 0.8) / 0.7, 0.0, 1.0)
    highvol = _clip((snapshot.realized_volatility_24h_pct - 8.0) / 12.0, 0.0, 1.0)
    components = (s24, s72, trend, srsi, sdd)
    weighted_sum = math.fsum(
        weight * component
        for weight, component in zip(_TECHNICAL_WEIGHTS, components, strict=True)
    )
    denominator = math.fsum(
        abs(weight * component)
        for weight, component in zip(_TECHNICAL_WEIGHTS, components, strict=True)
    )
    agreement = abs(weighted_sum) / denominator if denominator else 0.0
    score = 100.0 * weighted_sum * (0.65 + 0.35 * vconf)
    signal_strength = _clip(
        0.35 + 0.35 * vconf + 0.30 * agreement - 0.25 * highvol,
        0.0,
        1.0,
    )
    return _TechnicalResult(
        score=score,
        signal_strength=signal_strength,
        action=_classify(score, signal_strength, s24, trend),
        s24=s24,
        s72=s72,
        trend=trend,
        rsi=srsi,
        drawdown=sdd,
        volume_confirmation=vconf,
        agreement=agreement,
        high_volatility=highvol,
    )


def _contains(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def _recency_weight(observed_at: datetime, generated_at: datetime) -> float:
    age_hours = (_utc(generated_at) - _utc(observed_at)).total_seconds() / 3600.0
    if age_hours < -5.0 / 60.0:
        return 0.0
    return _clip(1.0 - max(age_hours, 0.0) / 72.0, 0.0, 1.0)


def _event_score(event: AlertEvent) -> float:
    prior = _CATEGORY_PRIOR.get(event.category, 0.0)
    # Score the observed catalyst only. ``probable_market_impact`` is generated
    # category boilerplate and must not become circular evidence for polarity.
    text = event.catalyst.casefold()
    positives = sum(
        _contains(text, term) for term in (*_POSITIVE_TERMS, *_INFLECTED_POSITIVE_TERMS)
    )
    negatives = sum(_contains(text, term) for term in _NEGATIVE_TERMS)
    if event.category is EventCategory.OUTAGE_EXPLOIT:
        return -100.0 if _is_active_outage(event) else 25.0
    if negatives:
        return _clip(-60.0 - 10.0 * min(negatives - 1, 3) + 10.0 * positives, -100.0, 0.0)
    if positives:
        return _clip(35.0 + 5.0 * min(positives, 2), 0.0, 45.0)
    return prior


def _is_active_outage(event: AlertEvent) -> bool:
    if event.category is not EventCategory.OUTAGE_EXPLOIT:
        return False
    text = event.catalyst.casefold()
    resolved = any(_contains(text, term) for term in _RESOLUTION_TERMS)
    explicitly_active = any(_contains(text, term) for term in _ACTIVE_INCIDENT_TERMS)
    return explicitly_active or not resolved


def _root_domain(value: str) -> str:
    labels = value.rstrip(".").lower().split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else value.lower()


def _event_domains(event: AlertEvent) -> set[str]:
    domains = {
        (urlparse(url).hostname or "").lower()
        for url in event.evidence_urls
        if isinstance(url, str)
    }
    metric_domains = event.metrics.get("source_domains")
    if isinstance(metric_domains, list | tuple):
        domains.update(value.lower() for value in metric_domains if isinstance(value, str))
    return {_root_domain(domain) for domain in domains if domain}


def _direction_supported(
    events: Sequence[AlertEvent], positive: bool, generated_at: datetime
) -> bool:
    relevant = [
        event
        for event in events
        if _recency_weight(event.observed_at, generated_at) > 0.0
        and (_event_score(event) > 0.0 if positive else _event_score(event) < 0.0)
    ]
    if any(event.source_quality is SourceQuality.HIGH for event in relevant):
        return True
    medium_domains = {
        domain
        for event in relevant
        if event.source_quality is SourceQuality.MEDIUM
        for domain in _event_domains(event)
    }
    return len(medium_domains) >= 2


def _fundamental_score(events: Sequence[AlertEvent], generated_at: datetime) -> float:
    weighted_sum = 0.0
    total_weight = 0.0
    for event in events:
        if event.category is EventCategory.PRICE_VOLUME:
            continue
        recency = _recency_weight(event.observed_at, generated_at)
        weight = _QUALITY_WEIGHT[event.source_quality] * recency
        weighted_sum += weight * _event_score(event)
        total_weight += weight
    if total_weight == 0.0:
        return 0.0
    score = _clip(weighted_sum / total_weight, -100.0, 45.0)
    if score > 0.0 and not _direction_supported(events, True, generated_at):
        return 0.0
    if score < 0.0 and not _direction_supported(events, False, generated_at):
        return 0.0
    return score


def _fused_action(
    technical: _TechnicalResult,
    fundamental_score: float,
    *,
    high_quality_outage: bool,
) -> tuple[float, RecommendationAction]:
    delta = (
        min(15.0, 0.20 * fundamental_score)
        if fundamental_score >= 0.0
        else max(-35.0, 0.35 * fundamental_score)
    )
    score = _clip(technical.score + delta, -100.0, 100.0)
    action = _classify(score, technical.signal_strength, technical.s24, technical.trend)
    if fundamental_score > 0.0 and action is RecommendationAction.BUY:
        # Positive headlines may soften a defensive class but cannot create a
        # BUY without the independent technical BUY gate.
        action = (
            RecommendationAction.BUY
            if technical.action is RecommendationAction.BUY
            else RecommendationAction.HOLD
        )
    if high_quality_outage and action is RecommendationAction.BUY:
        action = RecommendationAction.HOLD
    return score, action


def _technical_metrics(
    assessment: MarketAssessment, technical: _TechnicalResult
) -> dict[str, float]:
    snapshot = assessment.snapshot
    return {
        "last_price": float(snapshot.last_price),
        "change_24h_pct": float(snapshot.change_24h_pct),
        "change_72h_pct": float(snapshot.change_72h_pct),
        "rsi_14h": float(snapshot.rsi_14h),
        "ema_24h": float(snapshot.ema_24h),
        "ema_72h": float(snapshot.ema_72h),
        "trend_spread_pct": float(snapshot.trend_spread_pct),
        "volume_ratio": float(snapshot.volume_ratio),
        "realized_volatility_24h_pct": float(snapshot.realized_volatility_24h_pct),
        "drawdown_7d_pct": float(snapshot.drawdown_7d_pct),
        "membership_change_24h": technical.s24,
        "membership_change_72h": technical.s72,
        "membership_trend": technical.trend,
        "membership_rsi": technical.rsi,
        "membership_drawdown": technical.drawdown,
        "volume_confirmation": technical.volume_confirmation,
        "rule_agreement": technical.agreement,
        "high_volatility_penalty": technical.high_volatility,
    }


def _rationale(
    assessment: MarketAssessment,
    technical: _TechnicalResult,
    fundamental_score: float,
    action: RecommendationAction,
    event_count: int,
    outage_veto: bool,
) -> str:
    snapshot = assessment.snapshot
    veto = " Veto de segurança por outage/exploit HIGH aplicado." if outage_veto else ""
    return (
        "Heurística fuzzy não calibrada: "
        f"a regra técnica indicou {technical.action.value} (T={technical.score:.1f}; "
        f"força={technical.signal_strength:.2f}) e {event_count} catalisador(es) "
        f"resultaram em F={fundamental_score:.1f}; decisão final {action.value}. "
        f"24h={snapshot.change_24h_pct:+.2f}%, 72h={snapshot.change_72h_pct:+.2f}%, "
        f"tendência={snapshot.trend_spread_pct:+.2f}%, RSI={snapshot.rsi_14h:.1f}."
        f"{veto}"
    )


def _primary_risk(
    assessment: MarketAssessment,
    action: RecommendationAction,
    events: Sequence[AlertEvent],
    generated_at: datetime,
) -> str:
    severe = [
        event
        for event in events
        if event.category is EventCategory.OUTAGE_EXPLOIT
        and event.source_quality is SourceQuality.HIGH
        and _is_active_outage(event)
        and _recency_weight(event.observed_at, generated_at) > 0.0
    ]
    if severe:
        event = sorted(
            severe,
            key=lambda item: (-_utc(item.observed_at).timestamp(), item.event_id),
        )[0]
        return event.main_risk
    volatility = assessment.snapshot.realized_volatility_24h_pct
    if action is RecommendationAction.BUY:
        return f"O momentum pode reverter; volatilidade realizada em 24h de {volatility:.2f}%."
    if action is RecommendationAction.SELL:
        return "A venda pode cristalizar perdas antes de uma reversão abrupta."
    if action is RecommendationAction.REDUCE:
        return "Reduzir exposição pode limitar a participação em uma recuperação."
    return "HOLD não elimina risco de queda nem custo de oportunidade."


def _deduplicated_evidence(
    assessment: MarketAssessment, events: Sequence[AlertEvent]
) -> tuple[str, ...]:
    primary = assessment.evidence_url.strip()
    secondary = sorted(
        {
            url.strip()
            for event in events
            for url in event.evidence_urls
            if isinstance(url, str) and url.strip() and url.strip() != primary
        }
    )
    return (primary, *secondary)


def build_recommendations(
    assessments: Sequence[MarketAssessment],
    events: Sequence[AlertEvent],
    *,
    generated_at: datetime,
    expected_symbols: Sequence[str],
    max_buy_candidates: int,
    risk_per_trade_cap_pct: float,
    max_asset_weight_pct: float,
) -> list[TokenRecommendation]:
    """Build one deterministic advisory recommendation per expected symbol.

    Input coverage is fail-closed: assessments must contain exactly one snapshot
    for each expected symbol.  Output follows ``expected_symbols`` order. The fuzzy heuristic is
    intentionally uncalibrated and ``signal_strength`` is not a probability.
    """

    symbols = tuple(expected_symbols)
    supplied = tuple(assessments)
    if symbols != EXPECTED_SYMBOLS:
        raise ValueError("expected_symbols must equal the fixed eight-token universe")
    if not symbols or any(
        not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper()
        for symbol in symbols
    ):
        raise ValueError("expected_symbols must contain canonical uppercase symbols")
    if len(set(symbols)) != len(symbols):
        raise ValueError("expected_symbols must not contain duplicates")
    if len(supplied) != len(symbols):
        raise ValueError("assessments must match the exact expected universe")
    if any(not isinstance(assessment, MarketAssessment) for assessment in supplied):
        raise ValueError("assessments must contain MarketAssessment values")
    supplied_symbols = tuple(assessment.snapshot.asset for assessment in supplied)
    if len(set(supplied_symbols)) != len(supplied_symbols) or set(supplied_symbols) != set(symbols):
        raise ValueError("assessments contain missing, duplicated, or unexpected assets")
    by_symbol = {assessment.snapshot.asset: assessment for assessment in supplied}
    values = tuple(by_symbol[symbol] for symbol in symbols)
    if isinstance(max_buy_candidates, bool) or not isinstance(max_buy_candidates, int):
        raise ValueError("max_buy_candidates must be an integer")
    if not 0 <= max_buy_candidates <= len(symbols):
        raise ValueError("max_buy_candidates must be between zero and the asset count")
    risk_cap = _validate_number("risk_per_trade_cap_pct", risk_per_trade_cap_pct)
    weight_cap = _validate_number("max_asset_weight_pct", max_asset_weight_pct)
    if not 0.0 < risk_cap <= 1.0:
        raise ValueError("risk_per_trade_cap_pct must be in (0, 1]")
    if not 0.0 < weight_cap <= 40.0:
        raise ValueError("max_asset_weight_pct must be in (0, 40]")
    generated = _utc(generated_at)

    event_values = tuple(events)
    seen_event_ids: set[str] = set()
    for event in event_values:
        if not isinstance(event, AlertEvent) or event.asset not in symbols:
            raise ValueError("events must contain allowlisted AlertEvent values")
        if not isinstance(event.category, EventCategory) or not isinstance(
            event.source_quality, SourceQuality
        ):
            raise ValueError("event category or source quality is invalid")
        if (
            not isinstance(event.event_id, str)
            or not event.event_id
            or len(event.event_id) > 127
            or any(ord(character) < 32 for character in event.event_id)
            or event.event_id in seen_event_ids
        ):
            raise ValueError("event IDs must be unique, bounded, non-empty strings")
        _utc(event.observed_at)
        age_hours = (generated - _utc(event.observed_at)).total_seconds() / 3600.0
        if age_hours < -5.0 / 60.0 or age_hours > 72.0:
            raise ValueError("events must be no more than 72 hours old and not future-dated")
        if not event.evidence_urls:
            raise ValueError("events must include evidence URLs")
        for url in event.evidence_urls:
            parsed = urlparse(url) if isinstance(url, str) else None
            if parsed is None or parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("event evidence URLs must use HTTPS")
        evidence_quality = max(
            (source_quality_for_url(url) for url in event.evidence_urls),
            key=lambda value: _QUALITY_WEIGHT[value],
        )
        if event.source_quality is not evidence_quality:
            raise ValueError("event source quality does not match its evidence domains")
        seen_event_ids.add(event.event_id)

    recommendations: list[TokenRecommendation] = []
    for assessment, symbol in zip(values, symbols, strict=True):
        _validate_assessment(assessment, symbol)
        snapshot_age_hours = (
            generated - _utc(assessment.snapshot.observed_at)
        ).total_seconds() / 3600.0
        if snapshot_age_hours < -5.0 / 60.0 or snapshot_age_hours > 2.0:
            raise ValueError("market snapshots must be current and not future-dated")
        relevant = tuple(event for event in event_values if event.asset == symbol)
        technical = _technical_result(assessment)
        fundamental = _fundamental_score(relevant, generated)
        outage_veto = any(
            event.category is EventCategory.OUTAGE_EXPLOIT
            and event.source_quality is SourceQuality.HIGH
            and _is_active_outage(event)
            and _recency_weight(event.observed_at, generated) > 0.0
            for event in relevant
        )
        score, action = _fused_action(
            technical,
            fundamental,
            high_quality_outage=outage_veto,
        )
        recommendations.append(
            TokenRecommendation(
                asset=symbol,
                action=action,
                signal_strength=round(technical.signal_strength, 4),
                score=round(score, 2),
                technical_score=round(technical.score, 2),
                fundamental_score=round(fundamental, 2),
                model_source=RecommendationSource.FUZZY_EXPERT,
                rationale=_rationale(
                    assessment,
                    technical,
                    fundamental,
                    action,
                    sum(event.category is not EventCategory.PRICE_VOLUME for event in relevant),
                    outage_veto,
                ),
                primary_risk=_primary_risk(assessment, action, relevant, generated),
                evidence_urls=_deduplicated_evidence(assessment, relevant),
                evidence_event_ids=tuple(sorted({event.event_id for event in relevant})),
                technical_metrics=_technical_metrics(assessment, technical),
                generated_at=generated,
                risk_per_trade_cap_pct=round(risk_cap, 4),
                max_asset_weight_pct=round(weight_cap, 4),
            )
        )

    buy_indexes = [
        index
        for index, recommendation in enumerate(recommendations)
        if recommendation.action is RecommendationAction.BUY
    ]
    ranked = sorted(
        buy_indexes,
        key=lambda index: (
            -recommendations[index].score,
            -recommendations[index].signal_strength,
            index,
        ),
    )
    for index in ranked[max_buy_candidates:]:
        recommendation = recommendations[index]
        recommendations[index] = replace(
            recommendation,
            action=RecommendationAction.HOLD,
            rationale=(
                f"{recommendation.rationale} BUY rebaixado para HOLD pelo limite de "
                f"{max_buy_candidates} candidato(s) simultâneo(s)."
            ),
            primary_risk="Limite de diversificação: não ampliar o conjunto de posições.",
        )
    return recommendations


__all__ = ["build_recommendations"]

"""JSON and Markdown reporting for recommendations and material alert events."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .config import EXPECTED_SYMBOLS
from .engine import sort_events
from .models import AlertEvent, TokenRecommendation


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_value(value: Any) -> Any:
    """Recursively normalize domain metrics to strict JSON-compatible values."""

    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        normalized = [_json_value(item) for item in value]
        if isinstance(value, set | frozenset):
            return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
        return normalized
    return str(value)


def event_payload(event: AlertEvent) -> dict[str, Any]:
    """Return one event with every field required by the alert contract."""

    payload = event.to_dict()
    payload["evidence_urls"] = list(event.evidence_urls)
    payload["metrics"] = _json_value(event.metrics)
    return _json_value(payload)


def recommendation_payload(recommendation: TokenRecommendation) -> dict[str, Any]:
    """Return one recommendation with finite JSON-normalized values."""

    return _json_value(recommendation.to_dict())


def build_payload(
    events: Iterable[AlertEvent],
    generated_at: datetime | None = None,
    *,
    recommendations: Iterable[TokenRecommendation] = (),
) -> dict[str, Any]:
    """Build a deterministic, directly JSON-serializable digest payload."""

    generated = _utc(generated_at or datetime.now(UTC))
    alerts = [event_payload(event) for event in sort_events(events)]
    asset_order = {symbol: index for index, symbol in enumerate(EXPECTED_SYMBOLS)}
    advice = [
        recommendation_payload(item)
        for item in sorted(
            recommendations,
            key=lambda item: (asset_order.get(item.asset, len(asset_order)), item.asset),
        )
    ]
    return {
        "schema_version": 2,
        "generated_at": generated.isoformat(),
        "recommendation_count": len(advice),
        "recommendations": advice,
        "event_count": len(alerts),
        "alerts": alerts,
    }


def render_json(
    events: Iterable[AlertEvent],
    generated_at: datetime | None = None,
    *,
    indent: int = 2,
    recommendations: Iterable[TokenRecommendation] = (),
) -> str:
    """Render the digest as strict UTF-8 JSON text."""

    return json.dumps(
        build_payload(events, generated_at, recommendations=recommendations),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
        allow_nan=False,
    )


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def render_markdown(
    events: Iterable[AlertEvent],
    generated_at: datetime | None = None,
    *,
    recommendations: Iterable[TokenRecommendation] = (),
) -> str:
    """Render a readable digest while retaining every contractual field."""

    payload = build_payload(events, generated_at, recommendations=recommendations)
    lines = [
        "# Daily crypto analysis and material alerts",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Recommendation count: **{payload['recommendation_count']}**",
        f"Event count: **{payload['event_count']}**",
    ]

    action_labels = {
        "BUY": "BUY / COMPRAR",
        "HOLD": "HOLD / MANTER",
        "REDUCE": "REDUCE / REDUZIR",
        "SELL": "SELL / VENDER",
    }
    lines.extend(
        (
            "",
            "## Advisory recommendations",
            "",
            "Heuristic decision support only. Signal strength is rule agreement, not a "
            "probability of profit. BUY is a non-actionable candidate until portfolio "
            "limits are checked; REDUCE/SELL applies only to an existing position and "
            "never means opening a short.",
        )
    )
    if payload["recommendations"]:
        lines.extend(
            (
                "",
                "| Token | Suggestion | Rule strength | Integrated score | Source |",
                "|---|---|---:|---:|---|",
            )
        )
        for item in payload["recommendations"]:
            label = action_labels.get(item["action"], item["action"])
            lines.append(
                f"| {item['asset']} | {label} | {item['signal_strength']:.0%} | "
                f"{item['score']:+.1f} | {item['model_source']} |"
            )

        for item in payload["recommendations"]:
            label = action_labels.get(item["action"], item["action"])
            lines.extend(
                (
                    "",
                    f"### {item['asset']} — {label}",
                    "",
                    f"- Rationale: {_one_line(item['rationale'])}",
                    f"- Primary risk: {_one_line(item['primary_risk'])}",
                    f"- Technical / fundamental scores: "
                    f"`{item['technical_score']:+.1f}` / `{item['fundamental_score']:+.1f}`",
                    f"- Effective / model action: `{item['action']}` / "
                    f"`{item['model_action'] or 'unavailable'}`",
                    f"- AI review status: `{item['model_status']}`",
                    f"- AI model / prompt: `{item['model_name'] or 'unavailable'}` / "
                    f"`{item['prompt_version'] or 'unavailable'}`",
                    f"- AI input hash: `{item['model_input_hash'] or 'unavailable'}`",
                    "- Local evidence event IDs: "
                    + (
                        ", ".join(f"`{value}`" for value in item["evidence_event_ids"])
                        if item["evidence_event_ids"]
                        else "none"
                    ),
                    "- AI-cited event IDs: "
                    + (
                        ", ".join(
                            f"`{value}`" for value in item["model_evidence_event_ids"]
                        )
                        if item["model_evidence_event_ids"]
                        else "none"
                    ),
                    f"- Risk caps: {item['risk_per_trade_cap_pct']:.1f}% per trade; "
                    f"{item['max_asset_weight_pct']:.1f}% per asset",
                    "- Evidence URLs:",
                )
            )
            if item["evidence_urls"]:
                lines.extend(f"  - {url}" for url in item["evidence_urls"])
            else:
                lines.append("  - None supplied")
            metrics = json.dumps(
                item["technical_metrics"], ensure_ascii=False, sort_keys=True, allow_nan=False
            )
            lines.append(f"- Technical metrics: `{metrics}`")
            if item["model_rationale"]:
                lines.append(f"- Optional model review: {_one_line(item['model_rationale'])}")
            if item["model_primary_risk"]:
                lines.append(f"- Optional model risk: {_one_line(item['model_primary_risk'])}")
    else:
        lines.extend(("", "No recommendation set was supplied."))

    lines.extend(("", "## Material alerts"))
    if not payload["alerts"]:
        lines.extend(("", "No qualifying material events were found."))
        return "\n".join(lines) + "\n"

    for alert in payload["alerts"]:
        lines.extend(
            (
                "",
                f"## {alert['asset']} — {_one_line(alert['catalyst'])}",
                "",
                f"- Event ID: `{alert['event_id']}`",
                f"- Category: `{alert['category']}`",
                f"- Source quality: `{alert['source_quality']}`",
                f"- Probable market impact: {_one_line(alert['probable_market_impact'])}",
                f"- Main risk: {_one_line(alert['main_risk'])}",
                f"- Technical vs fundamental: `{alert['technical_vs_fundamental']}`",
                f"- Observed at: `{alert['observed_at']}`",
                "- Evidence URLs:",
            )
        )
        if alert["evidence_urls"]:
            lines.extend(f"  - {url}" for url in alert["evidence_urls"])
        else:
            lines.append("  - None supplied")
        metrics = json.dumps(alert["metrics"], ensure_ascii=False, sort_keys=True, allow_nan=False)
        lines.append(f"- Metrics: `{metrics}`")
    return "\n".join(lines) + "\n"


to_payload = build_payload


__all__ = [
    "build_payload",
    "event_payload",
    "recommendation_payload",
    "render_json",
    "render_markdown",
    "to_payload",
]

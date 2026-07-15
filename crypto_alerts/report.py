"""JSON and Markdown reporting for material alert events."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .engine import sort_events
from .models import AlertEvent


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


def build_payload(
    events: Iterable[AlertEvent],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a deterministic, directly JSON-serializable digest payload."""

    generated = _utc(generated_at or datetime.now(UTC))
    alerts = [event_payload(event) for event in sort_events(events)]
    return {
        "schema_version": 1,
        "generated_at": generated.isoformat(),
        "event_count": len(alerts),
        "alerts": alerts,
    }


def render_json(
    events: Iterable[AlertEvent],
    generated_at: datetime | None = None,
    *,
    indent: int = 2,
) -> str:
    """Render the digest as strict UTF-8 JSON text."""

    return json.dumps(
        build_payload(events, generated_at),
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
) -> str:
    """Render a readable digest while retaining every contractual field."""

    payload = build_payload(events, generated_at)
    lines = [
        "# Crypto material-development alerts",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Event count: **{payload['event_count']}**",
    ]
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
    "render_json",
    "render_markdown",
    "to_payload",
]

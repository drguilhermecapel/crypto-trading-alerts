"""Pure orchestration for combining market and catalyst alert events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import AlertEvent, AnalysisType, EventCategory, SourceQuality
from .news import canonicalize_url

_QUALITY_RANK = {
    SourceQuality.LOW: 0,
    SourceQuality.MEDIUM: 1,
    SourceQuality.HIGH: 2,
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_urls(event: AlertEvent) -> tuple[str, ...]:
    urls: set[str] = set()
    for url in event.evidence_urls:
        try:
            urls.add(canonicalize_url(url))
        except ValueError:
            # Invalid links are never promoted into a merged evidence set. The
            # original event can still be reported and inspected by its ID.
            continue
    return tuple(sorted(urls))


def sort_events(events: Iterable[AlertEvent]) -> list[AlertEvent]:
    """Sort newest first with stable deterministic tie-breakers."""

    return sorted(
        events,
        key=lambda event: (
            -_utc(event.observed_at).timestamp(),
            event.asset,
            event.category.value,
            event.event_id,
        ),
    )


def _analysis_type(events: Iterable[AlertEvent]) -> AnalysisType:
    kinds = {event.technical_vs_fundamental for event in events}
    if AnalysisType.MIXED in kinds or {
        AnalysisType.TECHNICAL,
        AnalysisType.FUNDAMENTAL,
    }.issubset(kinds):
        return AnalysisType.MIXED
    if AnalysisType.TECHNICAL in kinds:
        return AnalysisType.TECHNICAL
    return AnalysisType.FUNDAMENTAL


def _representative(events: Iterable[AlertEvent]) -> AlertEvent:
    return sorted(
        events,
        key=lambda event: (
            -_QUALITY_RANK[event.source_quality],
            -_utc(event.observed_at).timestamp(),
            event.event_id,
        ),
    )[0]


def _merge_metrics(representative: AlertEvent, events: list[AlertEvent]) -> dict[str, Any]:
    """Keep non-conflicting source metrics and disclose every merged event ID."""

    metrics = dict(representative.metrics)
    for event in sorted(events, key=lambda item: item.event_id):
        for key, value in event.metrics.items():
            if key not in metrics:
                metrics[key] = value
    ids = sorted({event.event_id for event in events})
    if len(ids) > 1:
        metrics["deduplicated_event_ids"] = ids
    return metrics


def _merge_cluster(events: list[AlertEvent]) -> AlertEvent:
    representative = _representative(events)
    evidence_urls = tuple(sorted({url for event in events for url in _canonical_urls(event)}))
    source_quality = max(
        (event.source_quality for event in events),
        key=_QUALITY_RANK.__getitem__,
    )
    return replace(
        representative,
        event_id=min(event.event_id for event in events),
        evidence_urls=evidence_urls or representative.evidence_urls,
        source_quality=source_quality,
        technical_vs_fundamental=_analysis_type(events),
        observed_at=max(_utc(event.observed_at) for event in events),
        metrics=_merge_metrics(representative, events),
    )


def deduplicate_events(events: Iterable[AlertEvent]) -> list[AlertEvent]:
    """Deduplicate transitive same-asset/category events by ID or evidence URL."""

    remaining = list(events)
    clusters: list[list[AlertEvent]] = []
    while remaining:
        cluster = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            cluster_ids = {event.event_id for event in cluster}
            cluster_urls = {url for event in cluster for url in _canonical_urls(event)}
            cluster_asset_categories = {(event.asset, event.category) for event in cluster}
            retained: list[AlertEvent] = []
            for candidate in remaining:
                candidate_urls = set(_canonical_urls(candidate))
                same_kind = (candidate.asset, candidate.category) in cluster_asset_categories
                if candidate.event_id in cluster_ids or (
                    same_kind and cluster_urls & candidate_urls
                ):
                    cluster.append(candidate)
                    changed = True
                else:
                    retained.append(candidate)
            remaining = retained
        clusters.append(cluster)
    return sort_events(_merge_cluster(cluster) for cluster in clusters)


def _technical_context(event: AlertEvent) -> Mapping[str, Any]:
    return {
        "event_id": event.event_id,
        "observed_at": _utc(event.observed_at).isoformat(),
        "metrics": dict(event.metrics),
        "evidence_urls": list(_canonical_urls(event)),
    }


def _fundamental_context(event: AlertEvent) -> Mapping[str, Any]:
    return {
        "event_id": event.event_id,
        "category": event.category.value,
        "catalyst": event.catalyst,
        "observed_at": _utc(event.observed_at).isoformat(),
        "source_quality": event.source_quality.value,
        "evidence_urls": list(_canonical_urls(event)),
    }


def enrich_matching_events(
    events: Iterable[AlertEvent],
    correlation_window_hours: int = 24,
) -> list[AlertEvent]:
    """Attach observed cross-signal context without inventing causal claims.

    A price/volume event and a catalyst event match only when they name the same
    asset and were observed within the configured window. Both original events
    remain in the result, and copied context is explicitly namespaced.
    """

    if correlation_window_hours < 0:
        raise ValueError("correlation_window_hours must be non-negative")
    values = list(events)
    maximum_gap = timedelta(hours=correlation_window_hours)
    technical = [event for event in values if event.category is EventCategory.PRICE_VOLUME]
    fundamental = [event for event in values if event.category is not EventCategory.PRICE_VOLUME]
    result: list[AlertEvent] = []

    for event in values:
        if event.category is EventCategory.PRICE_VOLUME:
            matches = [
                candidate
                for candidate in fundamental
                if candidate.asset == event.asset
                and abs(_utc(candidate.observed_at) - _utc(event.observed_at)) <= maximum_gap
            ]
            context_key = "fundamental_context"
            contexts = [_fundamental_context(candidate) for candidate in sort_events(matches)]
        else:
            matches = [
                candidate
                for candidate in technical
                if candidate.asset == event.asset
                and abs(_utc(candidate.observed_at) - _utc(event.observed_at)) <= maximum_gap
            ]
            context_key = "technical_context"
            contexts = [_technical_context(candidate) for candidate in sort_events(matches)]

        if not contexts:
            result.append(event)
            continue
        metrics = dict(event.metrics)
        metrics[context_key] = contexts
        result.append(
            replace(
                event,
                technical_vs_fundamental=AnalysisType.MIXED,
                metrics=metrics,
            )
        )
    return sort_events(result)


def combine_events(
    *event_groups: Iterable[AlertEvent],
    correlation_window_hours: int = 24,
) -> list[AlertEvent]:
    """Flatten, canonical-deduplicate, enrich, and deterministically sort events."""

    flattened = [event for group in event_groups for event in group]
    deduplicated = deduplicate_events(flattened)
    return enrich_matching_events(deduplicated, correlation_window_hours)


combine_and_enrich = combine_events


__all__ = [
    "combine_and_enrich",
    "combine_events",
    "deduplicate_events",
    "enrich_matching_events",
    "sort_events",
]

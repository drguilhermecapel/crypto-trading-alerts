"""Command-line interface for the read-only daily monitor."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .advisor import build_recommendations
from .config import DEFAULT_ALIASES, EXPECTED_SYMBOLS, AppConfig, ConfigError, load_config
from .engine import combine_events
from .http import PublicSourceError, fetch_bytes, fetch_json
from .market import (
    BinancePublicMarketClient,
    MarketAssessment,
    MarketDataError,
    OkxPublicMarketClient,
)
from .models import (
    AlertEvent,
    AnalysisType,
    Asset,
    EventCategory,
    RecommendationAction,
    RecommendationSource,
    SourceQuality,
    TokenRecommendation,
)
from .news import FeedParseError, news_items_to_events, parse_feed
from .notify import NotificationConfigError, Notifier
from .policy import AdvisoryPolicy, PolicyContext
from .report import build_payload, render_markdown
from .state import StateError, StateStore
from .universe import (
    UniverseAsset,
    UniversePayloadError,
    Venue,
    build_universe,
    parse_binance_instruments,
    parse_okx_instruments,
)

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_SOURCE = 3
EXIT_DELIVERY = 4
EXIT_STATE = 5
EXIT_POLICY_BLOCKED = 6
COLLECTION_DEADLINE_SECONDS = 8 * 60
MAX_HUMAN_DIGEST_CHARS = 90_000


class RunError(RuntimeError):
    """A source or orchestration invariant prevented a trustworthy run."""


class DeliveryError(RuntimeError):
    """Configured external delivery failed after artifacts were written."""


@dataclass(frozen=True, slots=True)
class CollectedData:
    """Validated inputs retained for both alerts and all-token analysis."""

    events: tuple[AlertEvent, ...]
    warnings: tuple[str, ...]
    assessments: tuple[MarketAssessment, ...]
    universe: tuple[UniverseAsset, ...] = ()
    universe_hash: str | None = None
    market_failures: tuple[tuple[str, str], ...] = ()
    exchange_counts: tuple[tuple[str, int], ...] = ()


class _RateLimiter:
    """Small thread-safe request pacer for public venue limits."""

    def __init__(self, requests_per_second: float) -> None:
        self._interval = 1.0 / requests_per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if delay:
            time.sleep(delay)


def _aware_utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise ValueError("time must include a timezone")
    return result.astimezone(UTC)


def _parse_time(value: str | None) -> datetime:
    if value is None:
        return _aware_utc()
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return _aware_utc(datetime.fromisoformat(candidate))
    except ValueError as exc:
        raise ConfigError("--now must be an ISO-8601 timestamp with timezone") from exc


def _event_id(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _market_event(assessment: MarketAssessment, timezone_name: str) -> AlertEvent:
    snapshot = assessment.snapshot
    local_day = snapshot.observed_at.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    change = snapshot.change_24h_pct
    direction = "bullish" if change > 0 else "bearish"
    signed_change = f"{change:+.2f}%"
    catalyst = (
        f"{snapshot.asset} moved {signed_change} over 24 hours with "
        f"{snapshot.volume_ratio:.2f}x quote volume"
    )
    impact = f"Short-term {direction} pressure is plausible while quote volume remains elevated."
    risk = (
        "A high-volume move can reverse; this technical detector does not identify "
        "or verify a fundamental cause."
    )
    identity = f"market|{snapshot.asset}|{EventCategory.PRICE_VOLUME.value}|{local_day}"
    return AlertEvent(
        event_id=f"market-{_event_id(identity)}",
        asset=snapshot.asset,
        category=EventCategory.PRICE_VOLUME,
        catalyst=catalyst,
        evidence_urls=(assessment.evidence_url,),
        source_quality=SourceQuality.HIGH,
        probable_market_impact=impact,
        main_risk=risk,
        technical_vs_fundamental=AnalysisType.TECHNICAL,
        observed_at=snapshot.observed_at,
        metrics={
            "instrument": snapshot.instrument,
            "venue_instrument": snapshot.venue_instrument,
            "exchange": snapshot.exchange,
            "last_price": snapshot.last_price,
            "change_24h_pct": snapshot.change_24h_pct,
            "quote_volume_24h": snapshot.quote_volume_24h,
            "baseline_quote_volume": snapshot.baseline_quote_volume,
            "volume_ratio": snapshot.volume_ratio,
            "change_72h_pct": snapshot.change_72h_pct,
            "rsi_14h": snapshot.rsi_14h,
            "ema_24h": snapshot.ema_24h,
            "ema_72h": snapshot.ema_72h,
            "trend_spread_pct": snapshot.trend_spread_pct,
            "realized_volatility_24h_pct": snapshot.realized_volatility_24h_pct,
            "drawdown_7d_pct": snapshot.drawdown_7d_pct,
            "price_threshold_met": assessment.price_threshold_met,
            "volume_threshold_met": assessment.volume_threshold_met,
        },
    )


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_digest(
    output_dir: str | Path,
    events: Sequence[AlertEvent],
    generated_at: datetime,
    warnings: Sequence[str],
    suppressed: dict[str, int | bool],
    recommendations: Sequence[TokenRecommendation],
    collected: CollectedData,
    max_markdown_recommendations: int,
) -> tuple[Path, Path, str]:
    directory = Path(output_dir)
    markdown = render_markdown(
        events,
        generated_at,
        recommendations=recommendations,
        max_recommendations=max_markdown_recommendations,
    )
    payload = build_payload(events, generated_at, recommendations=recommendations)
    payload["warnings"] = list(warnings)
    payload["suppressed"] = suppressed
    total = len(collected.universe) or len(recommendations)
    analyzed = sum(item.analysis_status == "analyzed" for item in recommendations)
    coverage_ratio = analyzed / total if total else 0.0
    payload["universe"] = {
        "mode": "exchange_union" if collected.universe else "fixed_fixture",
        "hash": collected.universe_hash,
        "discovered_assets": total,
        "analyzed_assets": analyzed,
        "not_rated_assets": total - analyzed,
        "coverage_ratio": round(coverage_ratio, 6),
        "complete": bool(collected.universe),
        "identity_basis": "canonical_ticker_assumption",
        "exchange_asset_counts": dict(collected.exchange_counts),
        "market_failures": [
            {"asset": symbol, "reason": reason} for symbol, reason in collected.market_failures
        ],
    }
    if collected.universe:
        coverage_block = (
            "## Universe coverage\n\n"
            f"Analyzed **{analyzed}/{total}** discovered eligible tokens "
            f"(**{coverage_ratio:.1%}**); NOT_RATED: **{total - analyzed}**. "
            "Sources: OKX and Binance.\n\n"
        )
        markdown = markdown.replace(
            "## Advisory recommendations\n",
            coverage_block + "## Advisory recommendations\n",
            1,
        )
    visible_warnings = [" ".join(item.split())[:500] for item in warnings[:20]]
    if visible_warnings:
        markdown += "\n## Source warnings\n\n" + "\n".join(f"- {item}" for item in visible_warnings)
        omitted_warnings = len(warnings) - len(visible_warnings)
        if omitted_warnings:
            markdown += f"\n- {omitted_warnings} additional warnings are in digest.json\n"
    if len(markdown) > MAX_HUMAN_DIGEST_CHARS:
        markdown = (
            "# Daily crypto analysis and material alerts\n\n"
            f"Generated at: `{generated_at.isoformat()}`\n\n"
            f"Recommendation count: **{len(recommendations)}**\n\n"
            f"Event count: **{len(events)}**\n\n"
            "The human digest exceeded its safe delivery budget. The complete, "
            "validated analysis is available in digest.json.\n"
        )
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown_path = directory / "digest.md"
    json_path = directory / "digest.json"
    _atomic_text(markdown_path, markdown)
    _atomic_text(json_path, json_text)
    return markdown_path, json_path, markdown


def _universe_hash(universe: Sequence[UniverseAsset]) -> str:
    canonical = [
        {
            "symbol": item.symbol,
            "instruments": [
                {"venue": instrument.venue.value, "instrument": instrument.instrument}
                for instrument in item.instruments
            ],
        }
        for item in universe
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_asset(item: UniverseAsset) -> Asset:
    aliases = DEFAULT_ALIASES.get(item.symbol, (item.symbol,))
    return Asset(item.symbol, f"{item.symbol}-USDT", aliases)


def _discover_universe(config: AppConfig) -> tuple[tuple[UniverseAsset, ...], list[str]]:
    """Discover a frozen union, tolerating one failed venue but never both."""

    warnings: list[str] = []
    okx = ()
    binance = ()
    successes = 0
    try:
        okx_payload = fetch_json(
            f"{config.market.base_url}/api/v5/public/instruments?instType=SPOT",
            timeout_seconds=config.market.request_timeout_seconds,
            allowed_hosts=frozenset({"www.okx.com", "my.okx.com"}),
        )
        okx = parse_okx_instruments(okx_payload)
        successes += 1
    except (PublicSourceError, UniversePayloadError) as exc:
        warnings.append(f"OKX universe discovery unavailable: {exc}")

    try:
        binance_payload = fetch_json(
            f"{config.market.binance_base_url}/api/v3/exchangeInfo?"
            "permissions=SPOT&symbolStatus=TRADING&showPermissionSets=true",
            timeout_seconds=config.market.request_timeout_seconds,
            max_bytes=10 * 1024 * 1024,
            allowed_hosts=frozenset({"api.binance.com", "data-api.binance.vision"}),
        )
        binance = parse_binance_instruments(binance_payload)
        successes += 1
    except (PublicSourceError, UniversePayloadError) as exc:
        warnings.append(f"Binance universe discovery unavailable: {exc}")

    if successes != 2:
        raise RunError(
            "complete OKX and Binance universe discovery is required: " + "; ".join(warnings)
        )
    try:
        universe = build_universe(okx, binance, max_assets=config.universe.max_assets)
    except UniversePayloadError as exc:
        raise RunError(f"cannot assemble exchange universe: {exc}") from exc
    if not universe:
        raise RunError("exchange discovery returned an empty eligible universe")
    missing_core = sorted(set(EXPECTED_SYMBOLS) - {item.symbol for item in universe})
    if missing_core:
        raise RunError(
            "required core assets missing from discovered universe: " + ", ".join(missing_core)
        )
    return universe, warnings


def _assess_universe_asset(
    item: UniverseAsset,
    *,
    okx_client: OkxPublicMarketClient,
    binance_client: BinancePublicMarketClient,
    limiters: dict[Venue, _RateLimiter],
) -> MarketAssessment:
    asset = _runtime_asset(item)
    available = tuple(instrument.venue.value for instrument in item.instruments)
    failures: list[str] = []
    for instrument in item.instruments:
        client = okx_client if instrument.venue is Venue.OKX else binance_client
        try:
            limiters[instrument.venue].wait()
            assessment = client.assess(asset)
        except MarketDataError as exc:
            failures.append(f"{instrument.venue.value}: {exc}")
            continue
        return replace(
            assessment,
            snapshot=replace(assessment.snapshot, available_exchanges=available),
        )
    raise MarketDataError("; ".join(failures) or "no supported listing")


def _collect_events(config: AppConfig, now: datetime) -> CollectedData:
    universe, discovery_warnings = _discover_universe(config)
    okx_client = OkxPublicMarketClient(config.market, clock=lambda: now)
    binance_client = BinancePublicMarketClient(config.market, clock=lambda: now)
    limiters = {
        Venue.OKX: _RateLimiter(12.0),
        Venue.BINANCE: _RateLimiter(20.0),
    }
    market_events: list[AlertEvent] = []
    assessments_by_symbol: dict[str, MarketAssessment] = {}
    market_errors: dict[str, str] = {}
    ordered_universe = sorted(
        universe,
        key=lambda item: (item.symbol not in EXPECTED_SYMBOLS, item.symbol),
    )
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config.universe.max_workers,
        thread_name_prefix="public-market",
    )
    try:
        futures = {
            executor.submit(
                _assess_universe_asset,
                item,
                okx_client=okx_client,
                binance_client=binance_client,
                limiters=limiters,
            ): item.symbol
            for item in ordered_universe
        }
        try:
            completed = concurrent.futures.as_completed(
                futures,
                timeout=COLLECTION_DEADLINE_SECONDS,
            )
            for future in completed:
                symbol = futures[future]
                try:
                    assessments_by_symbol[symbol] = future.result()
                except (MarketDataError, ValueError) as exc:
                    market_errors[symbol] = str(exc)
        except TimeoutError:
            for future, symbol in futures.items():
                if not future.done():
                    future.cancel()
                    market_errors[symbol] = "collection deadline exceeded"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    for item in universe:
        if item.symbol not in assessments_by_symbol:
            market_errors.setdefault(item.symbol, "collection did not complete")

    core_failures = [symbol for symbol in EXPECTED_SYMBOLS if symbol in market_errors]
    if core_failures:
        raise RunError(
            "required core market data failed: "
            + "; ".join(f"{symbol}: {market_errors[symbol]}" for symbol in core_failures)
        )
    coverage = len(assessments_by_symbol) / len(universe)
    if coverage < config.universe.minimum_coverage_ratio:
        raise RunError(
            "market coverage below configured minimum: "
            f"{len(assessments_by_symbol)}/{len(universe)} ({coverage:.1%})"
        )

    assessments = [
        assessments_by_symbol[item.symbol]
        for item in universe
        if item.symbol in assessments_by_symbol
    ]
    for assessment in assessments:
        if assessment.material:
            market_events.append(_market_event(assessment, config.timezone))

    news_items = []
    warnings: list[str] = list(discovery_warnings)
    warnings.extend(
        f"{symbol}: market analysis unavailable ({reason})"
        for symbol, reason in sorted(market_errors.items())
    )
    successful_feeds = 0
    required_failures: list[str] = []
    for feed in config.news.feeds:
        try:
            content = fetch_bytes(
                feed.url,
                timeout_seconds=config.market.request_timeout_seconds,
            )
            news_items.extend(
                parse_feed(
                    content,
                    source_name=feed.name,
                    now=now,
                    lookback_hours=config.news.lookback_hours,
                )
            )
            successful_feeds += 1
        except (PublicSourceError, FeedParseError) as exc:
            warning = f"{feed.name}: {exc}"
            warnings.append(warning)
            if feed.required:
                required_failures.append(warning)
    if required_failures:
        raise RunError("required news feed failed: " + "; ".join(required_failures))
    if successful_feeds < config.news.minimum_successful_feeds:
        raise RunError(
            "successful news feeds below minimum: "
            f"{successful_feeds}/{config.news.minimum_successful_feeds}"
        )
    news_events = news_items_to_events(
        news_items,
        (_runtime_asset(item) for item in universe),
        now=now,
        lookback_hours=config.news.lookback_hours,
    )
    exchange_counts = tuple(
        (
            venue.value,
            sum(
                any(instrument.venue is venue for instrument in item.instruments)
                for item in universe
            ),
        )
        for venue in Venue
    )
    return CollectedData(
        events=tuple(combine_events(market_events, news_events)),
        warnings=tuple(warnings),
        assessments=tuple(assessments),
        universe=universe,
        universe_hash=_universe_hash(universe),
        market_failures=tuple(sorted(market_errors.items())),
        exchange_counts=exchange_counts,
    )


def _apply_optional_ai_review(
    recommendations: Sequence[TokenRecommendation],
    assessments: Sequence[MarketAssessment],
    events: Sequence[AlertEvent],
    config: AppConfig,
) -> tuple[list[TokenRecommendation], str | None]:
    """Attach a model's second opinion without changing any effective action or score."""

    if not config.analysis.openai_enabled:
        return [replace(item, model_status="disabled") for item in recommendations], None

    reviewable = [
        item
        for item in recommendations
        if item.analysis_status == "analyzed" and item.action is not RecommendationAction.NOT_RATED
    ]
    shortlist = sorted(
        reviewable,
        key=lambda item: (
            item.action is RecommendationAction.HOLD,
            -abs(item.score),
            -item.signal_strength,
            item.asset,
        ),
    )[: config.analysis.openai_max_assets]
    shortlist_symbols = {item.asset for item in shortlist}
    if not shortlist:
        unavailable = [
            replace(item, model_status="not_selected_no_market_data") for item in recommendations
        ]
        return unavailable, None

    # Imported lazily so the deterministic engine remains independently usable.
    from .openai_advisor import review_recommendations

    shortlisted_events: list[AlertEvent] = []
    for symbol in sorted(shortlist_symbols):
        matching = sorted(
            (item for item in events if item.asset == symbol),
            key=lambda item: (-item.observed_at.timestamp(), item.event_id),
        )
        shortlisted_events.extend(matching[:4])

    result = review_recommendations(
        shortlist,
        [item for item in assessments if item.snapshot.asset in shortlist_symbols],
        shortlisted_events,
        api_key=os.environ.get("OPENAI_API_KEY"),
        model=config.analysis.openai_model,
        timeout_seconds=config.analysis.openai_timeout_seconds,
    )
    enriched: list[TokenRecommendation] = []
    for item in recommendations:
        if item.asset not in shortlist_symbols:
            status = (
                "not_selected_no_market_data"
                if item.analysis_status != "analyzed"
                else "not_selected_budget"
            )
            enriched.append(replace(item, model_status=status))
            continue
        opinion = result.opinions.get(item.asset)
        if opinion is None:
            enriched.append(
                replace(
                    item,
                    model_status=result.status,
                    model_input_hash=result.input_hash,
                    prompt_version=result.prompt_version,
                    model_name=result.model,
                )
            )
            continue
        agrees = opinion.action is item.action
        enriched.append(
            replace(
                item,
                model_source=(
                    RecommendationSource.HYBRID_CONSENSUS
                    if agrees
                    else RecommendationSource.FUZZY_EXPERT
                ),
                model_action=opinion.action,
                model_signal_strength=opinion.signal_strength / 100.0,
                model_rationale=opinion.rationale,
                model_primary_risk=opinion.primary_risk,
                model_status="reviewed_agreement" if agrees else "reviewed_disagreement",
                model_input_hash=result.input_hash,
                prompt_version=result.prompt_version,
                model_name=result.model,
                model_evidence_event_ids=opinion.evidence_event_ids,
            )
        )
    warning = f"AI review unavailable ({result.warning})" if result.warning else None
    return enriched, warning


def _unavailable_recommendation(
    item: UniverseAsset,
    *,
    reason: str,
    generated_at: datetime,
    config: AppConfig,
    universe_hash: str | None,
) -> TokenRecommendation:
    venues = tuple(instrument.venue.value for instrument in item.instruments)
    return TokenRecommendation(
        asset=item.symbol,
        action=RecommendationAction.NOT_RATED,
        signal_strength=0.0,
        score=0.0,
        technical_score=0.0,
        fundamental_score=0.0,
        model_source=RecommendationSource.FUZZY_EXPERT,
        rationale="Dados públicos validados insuficientes para emitir uma sugestão responsável.",
        primary_risk=(
            "Tratar ausência de dados como HOLD poderia ocultar risco ou uma nova listagem."
        ),
        evidence_urls=(),
        evidence_event_ids=(),
        technical_metrics={},
        generated_at=generated_at,
        model_status="not_selected_no_market_data",
        risk_per_trade_cap_pct=round(config.risk.risk_per_trade * 100.0, 4),
        max_asset_weight_pct=round(config.risk.max_asset_weight * 100.0, 4),
        analysis_status="unavailable",
        analysis_reason=reason,
        available_exchanges=venues,
        universe_hash=universe_hash,
    )


def _run_monitor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    now = _parse_time(args.now)
    state_path = args.state or config.state.path
    store = StateStore(state_path, config.state.dedupe_hours, config.timezone)
    with store.run_lock():
        return _run_monitor_locked(args, config, now, store)


def _run_monitor_locked(
    args: argparse.Namespace,
    config: AppConfig,
    now: datetime,
    store: StateStore,
) -> int:
    """Run one serialized daily transaction from state check through commit."""

    snapshot = store.load()
    already_sent_today = store.digest_sent_today(snapshot, now=now)
    if already_sent_today and not args.force:
        directory = Path(args.output_dir)
        receipt_path = directory / "suppressed-run.json"
        receipt = {
            "status": "daily_suppressed",
            "reason": "daily_digest_already_sent",
            "generated_at": now.isoformat(),
            "emitted_events": 0,
            "recommendations": 0,
            "notified": False,
            "markdown": str(directory / "digest.md"),
            "json": str(directory / "digest.json"),
        }
        _atomic_text(
            receipt_path,
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        print(json.dumps({**receipt, "suppression_receipt": str(receipt_path)}, sort_keys=True))
        return EXIT_OK

    try:
        collected = _collect_events(config, now)
    except RunError as exc:
        failure_path = Path(args.output_dir) / "collection-failure.json"
        _atomic_text(
            failure_path,
            json.dumps(
                {
                    "status": "collection_failed",
                    "generated_at": now.isoformat(),
                    "reason": " ".join(str(exc).split())[:1_000],
                    "state_advanced": False,
                    "notified": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        raise
    events = list(collected.events)
    warnings = list(collected.warnings)
    analyzed_symbols = tuple(item.snapshot.asset for item in collected.assessments)
    analyzed_symbol_set = set(analyzed_symbols)
    analysis_events = [event for event in events if event.asset in analyzed_symbol_set]
    try:
        analyzed_recommendations = build_recommendations(
            collected.assessments,
            analysis_events,
            generated_at=now,
            expected_symbols=analyzed_symbols,
            max_buy_candidates=config.risk.max_holdings,
            risk_per_trade_cap_pct=config.risk.risk_per_trade * 100.0,
            max_asset_weight_pct=config.risk.max_asset_weight * 100.0,
        )
    except ValueError as exc:
        raise RunError(f"cannot build complete recommendations: {exc}") from exc
    by_symbol = {
        item.asset: replace(item, universe_hash=collected.universe_hash)
        for item in analyzed_recommendations
    }
    if collected.universe:
        failures = dict(collected.market_failures)
        recommendations = [
            by_symbol.get(item.symbol)
            or _unavailable_recommendation(
                item,
                reason=failures.get(item.symbol, "market data unavailable"),
                generated_at=now,
                config=config,
                universe_hash=collected.universe_hash,
            )
            for item in collected.universe
        ]
    else:
        # Test fixtures and direct library callers may supply assessments
        # without a discovery manifest.
        recommendations = [by_symbol[symbol] for symbol in analyzed_symbols]
    recommendations, ai_warning = _apply_optional_ai_review(
        recommendations,
        collected.assessments,
        events,
        config,
    )
    if ai_warning:
        warnings.append(ai_warning)

    all_ids = [event.event_id for event in events]
    new_ids = set(store.new_event_ids(snapshot, all_ids, now=now))
    if args.force:
        selected = events
    elif already_sent_today:
        selected = []
    else:
        selected = [event for event in events if event.event_id in new_ids]

    should_emit = args.force or not already_sent_today
    suppressed = {
        "duplicate_events": len(events)
        - len([event for event in events if event.event_id in new_ids]),
        "daily_digest_already_sent": already_sent_today and not args.force,
        "material_events_before_suppression": len(events),
    }
    markdown_path, json_path, markdown = _write_digest(
        args.output_dir,
        selected,
        now,
        warnings,
        suppressed,
        recommendations,
        collected,
        config.delivery.max_markdown_recommendations,
    )

    notified = False
    if should_emit and not args.no_notify:
        notifier = Notifier.from_environment(
            config.delivery,
            timeout_seconds=config.market.request_timeout_seconds,
        )
        local_day = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
        result = notifier.send(f"Daily crypto analysis — {local_day}", markdown)
        if not result.success:
            failures = [
                channel.error_code
                for channel in (result.telegram, result.email)
                if channel.enabled and not channel.success
            ]
            raise DeliveryError(
                "notification delivery failed: " + ", ".join(item or "unknown" for item in failures)
            )
        notified = result.telegram.enabled or result.email.enabled
        if not notified:
            raise NotificationConfigError(
                "no notification channel is enabled; use --no-notify for artifact-only mode"
            )

    if should_emit:
        store.commit(
            snapshot,
            event_ids=(event.event_id for event in selected),
            mark_digest_sent=not already_sent_today,
            now=now,
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "material_events": len(events),
                "emitted_events": len(selected),
                "recommendations": len(recommendations),
                "ai_review_status": sorted({item.model_status for item in recommendations}),
                "notified": notified,
                "markdown": str(markdown_path),
                "json": str(json_path),
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


def _read_portfolio(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read portfolio: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("portfolio root must be an object")
    allowed = {"market_type", "leverage", "risk_per_trade", "weekly_return", "positions"}
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"portfolio has unknown keys: {', '.join(sorted(unknown))}")
    return value


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ConfigError(f"{field} must be a finite number")
    return float(value)


def _check_portfolio(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    value = _read_portfolio(args.portfolio)
    positions = value.get("positions")
    if not isinstance(positions, list):
        raise ConfigError("portfolio.positions must be a list")
    weights: dict[str, float] = {}
    for index, item in enumerate(positions):
        if not isinstance(item, dict) or set(item) != {"symbol", "weight"}:
            raise ConfigError(f"portfolio.positions[{index}] must contain symbol and weight")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,32}", symbol.upper()):
            raise ConfigError(f"portfolio.positions[{index}].symbol is not canonical")
        symbol = symbol.upper()
        if symbol in weights:
            raise ConfigError(f"duplicate portfolio symbol: {symbol}")
        weight = _finite(item.get("weight"), f"portfolio.positions[{index}].weight")
        if not 0 < weight <= 1:
            raise ConfigError(f"portfolio.positions[{index}].weight must be in (0, 1]")
        weights[symbol] = weight

    context = PolicyContext(
        active_holdings=len(weights),
        asset_weight=max(weights.values(), default=0.0),
        risk_per_trade=_finite(value.get("risk_per_trade"), "portfolio.risk_per_trade"),
        weekly_pnl=_finite(value.get("weekly_return"), "portfolio.weekly_return"),
        instrument_type=value.get("market_type"),
        leverage=_finite(value.get("leverage"), "portfolio.leverage"),
    )
    decision = AdvisoryPolicy(config.risk).evaluate(context)
    violations = list(decision.violations)
    if sum(weights.values()) > 1.0 + 1e-12:
        violations.append("total_weight_exceeds_one")
    allowed = not violations
    print(
        json.dumps(
            {
                "allowed": allowed,
                "advisory_only": True,
                "holdings": len(weights),
                "total_weight": sum(weights.values()),
                "violations": violations,
            },
            sort_keys=True,
        )
    )
    return EXIT_OK if allowed else EXIT_POLICY_BLOCKED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-alerts",
        description="Read-only daily monitor for material crypto developments.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="validate configuration and exit")
    validate.add_argument("--config", required=True)

    run = subparsers.add_parser("run", help="collect, classify, report, and optionally notify")
    run.add_argument("--config", required=True)
    run.add_argument("--state", help="override the configured state path")
    run.add_argument("--output-dir", default="artifacts")
    run.add_argument("--force", action="store_true", help="bypass daily and event deduplication")
    run.add_argument(
        "--no-notify", action="store_true", help="write artifacts without external delivery"
    )
    run.add_argument("--now", help=argparse.SUPPRESS)

    portfolio = subparsers.add_parser("check-portfolio", help="evaluate advisory spot-risk limits")
    portfolio.add_argument("--config", required=True)
    portfolio.add_argument("--portfolio", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-config":
            config = load_config(args.config)
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "mode": config.mode,
                        "core_assets": [asset.symbol for asset in config.assets],
                        "universe_mode": config.universe.mode,
                        "exchanges": list(config.universe.exchanges),
                        "max_assets": config.universe.max_assets,
                    },
                    sort_keys=True,
                )
            )
            return EXIT_OK
        if args.command == "check-portfolio":
            return _check_portfolio(args)
        return _run_monitor(args)
    except (ConfigError, NotificationConfigError, ValueError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except (RunError, PublicSourceError, MarketDataError, FeedParseError) as exc:
        print(f"source error: {exc}", file=sys.stderr)
        return EXIT_SOURCE
    except DeliveryError as exc:
        print(f"delivery error: {exc}", file=sys.stderr)
        return EXIT_DELIVERY
    except StateError as exc:
        print(f"state error: {exc}", file=sys.stderr)
        return EXIT_STATE


if __name__ == "__main__":
    raise SystemExit(main())

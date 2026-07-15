"""Command-line interface for the read-only daily monitor."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .advisor import build_recommendations
from .config import EXPECTED_SYMBOLS, AppConfig, ConfigError, load_config
from .engine import combine_events
from .http import PublicSourceError, fetch_bytes
from .market import MarketAssessment, MarketDataError, OkxPublicMarketClient
from .models import (
    AlertEvent,
    AnalysisType,
    EventCategory,
    RecommendationSource,
    SourceQuality,
    TokenRecommendation,
)
from .news import FeedParseError, news_items_to_events, parse_feed
from .notify import NotificationConfigError, Notifier
from .policy import AdvisoryPolicy, PolicyContext
from .report import build_payload, render_markdown
from .state import StateError, StateStore

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_SOURCE = 3
EXIT_DELIVERY = 4
EXIT_STATE = 5
EXIT_POLICY_BLOCKED = 6


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
) -> tuple[Path, Path, str]:
    directory = Path(output_dir)
    markdown = render_markdown(events, generated_at, recommendations=recommendations)
    if warnings:
        markdown += "\n## Source warnings\n\n" + "\n".join(f"- {item}" for item in warnings) + "\n"
    payload = build_payload(events, generated_at, recommendations=recommendations)
    payload["warnings"] = list(warnings)
    payload["suppressed"] = suppressed
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    markdown_path = directory / "digest.md"
    json_path = directory / "digest.json"
    _atomic_text(markdown_path, markdown)
    _atomic_text(json_path, json_text)
    return markdown_path, json_path, markdown


def _collect_events(config: AppConfig, now: datetime) -> CollectedData:
    market_client = OkxPublicMarketClient(config.market, clock=lambda: now)
    market_events: list[AlertEvent] = []
    assessments: list[MarketAssessment] = []
    market_errors: list[str] = []
    for asset in config.assets:
        try:
            assessment = market_client.assess(asset)
        except MarketDataError as exc:
            market_errors.append(f"{asset.symbol}: {exc}")
            continue
        assessments.append(assessment)
        if assessment.material:
            market_events.append(_market_event(assessment, config.timezone))
    if market_errors:
        raise RunError("required OKX market data failed: " + "; ".join(market_errors))

    news_items = []
    warnings: list[str] = []
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
        config.assets,
        now=now,
        lookback_hours=config.news.lookback_hours,
    )
    if tuple(item.snapshot.asset for item in assessments) != EXPECTED_SYMBOLS:
        raise RunError("market assessment set does not match the fixed asset universe")
    return CollectedData(
        events=tuple(combine_events(market_events, news_events)),
        warnings=tuple(warnings),
        assessments=tuple(assessments),
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

    # Imported lazily so the deterministic engine remains independently usable.
    from .openai_advisor import review_recommendations

    result = review_recommendations(
        recommendations,
        assessments,
        events,
        api_key=os.environ.get("OPENAI_API_KEY"),
        model=config.analysis.openai_model,
        timeout_seconds=config.analysis.openai_timeout_seconds,
    )
    enriched: list[TokenRecommendation] = []
    for item in recommendations:
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

    collected = _collect_events(config, now)
    events = list(collected.events)
    warnings = list(collected.warnings)
    try:
        recommendations = build_recommendations(
            collected.assessments,
            events,
            generated_at=now,
            expected_symbols=EXPECTED_SYMBOLS,
            max_buy_candidates=config.risk.max_holdings,
            risk_per_trade_cap_pct=config.risk.risk_per_trade * 100.0,
            max_asset_weight_pct=config.risk.max_asset_weight * 100.0,
        )
    except ValueError as exc:
        raise RunError(f"cannot build complete recommendations: {exc}") from exc
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
        if not isinstance(symbol, str) or symbol.upper() not in EXPECTED_SYMBOLS:
            raise ConfigError(f"portfolio.positions[{index}].symbol is not allowlisted")
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
                        "assets": [asset.symbol for asset in config.assets],
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

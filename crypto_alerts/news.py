"""Deterministic parsing and classification of catalyst feeds.

The monitor deliberately treats feeds as evidence, not as instructions to trade.
Only items that are recent, crypto-relevant, classifiable, and supported by at
least one medium- or high-quality source become :class:`AlertEvent` objects.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .models import (
    AlertEvent,
    AnalysisType,
    Asset,
    EventCategory,
    NewsItem,
    SourceQuality,
)


class FeedParseError(ValueError):
    """Raised when an RSS or Atom document cannot be parsed safely."""


NEWS_CATEGORIES = (
    EventCategory.ONCHAIN_ECOSYSTEM,
    EventCategory.EXCHANGE_LIQUIDITY,
    EventCategory.NETWORK_UPGRADE,
    EventCategory.OUTAGE_EXPLOIT,
    EventCategory.ETF_INSTITUTIONAL,
    EventCategory.REGULATORY_LEGAL,
)

_QUALITY_RANK = {
    SourceQuality.LOW: 0,
    SourceQuality.MEDIUM: 1,
    SourceQuality.HIGH: 2,
}

# Government, protocol, exchange, and identified institutional domains are
# primary evidence for announcements made by those organizations. A HIGH grade
# means "primary/official source", not that every claim on the site is true.
_HIGH_QUALITY_DOMAINS = frozenset(
    {
        "sec.gov",
        "cftc.gov",
        "justice.gov",
        "treasury.gov",
        "federalreserve.gov",
        "congress.gov",
        "europa.eu",
        "esma.europa.eu",
        "bitcoin.org",
        "ethereum.org",
        "ethereum.foundation",
        "solana.com",
        "solana.org",
        "ripple.com",
        "cardano.org",
        "cardanofoundation.org",
        "sei.io",
        "aptosfoundation.org",
        "aptoslabs.com",
        "avax.network",
        "avalanche.foundation",
        "okx.com",
        "binance.com",
        "coinbase.com",
        "kraken.com",
        "bybit.com",
        "cmegroup.com",
        "nasdaq.com",
        "blackrock.com",
        "fidelity.com",
        "grayscale.com",
    }
)

_MEDIUM_QUALITY_DOMAINS = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "bloomberg.com",
        "ft.com",
        "wsj.com",
        "coindesk.com",
        "theblock.co",
        "cointelegraph.com",
    }
)

_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref_src",
    }
)

_GLOBAL_CRYPTO_PATTERNS = (
    r"\bcrypto(?:currency|currencies)?\b",
    r"\bdigital assets?\b",
    r"\bblockchain\b",
    r"\btoken(?:s|ized)?\b",
    r"\bstablecoins?\b",
    r"\bweb3\b",
)

_EDITORIAL_IDENTITY_WORDS = frozenset(
    {"breaking", "exclusive", "latest", "new", "update", "updated"}
)
_AMBIGUOUS_SYMBOLS = frozenset({"ADA", "APT", "SEI", "SOL"})
_AMBIGUOUS_NAME_ALIASES = frozenset({"Avalanche", "Ether", "Ripple"})

# Scored patterns reduce accidental classification from one broad word. The
# ordered category list below is the deterministic tie-breaker.
_CATEGORY_PATTERNS: dict[EventCategory, tuple[str, ...]] = {
    EventCategory.ONCHAIN_ECOSYSTEM: (
        r"\bon[ -]?chain\b",
        r"\bactive addresses?\b",
        r"\btransaction (?:count|volume|activity)\b",
        r"\btotal value locked\b",
        r"\bTVL\b",
        r"\becosystem (?:growth|activity|adoption|fund|accelerat\w*)\b",
        r"\bdeveloper activity\b",
        r"\bstaking (?:activity|deposits?|participation)\b",
        r"\bvalidator(?:s| activity)?\b",
    ),
    EventCategory.EXCHANGE_LIQUIDITY: (
        r"\bexchange (?:listing|delisting|liquidity|outflows?|inflows?)\b",
        r"\b(?:lists?|delists?|listing|delisting)\b",
        r"\bliquidity\b",
        r"\bmarket makers?\b",
        r"\btrading pairs?\b",
        r"\border books?\b",
        r"\bdeposit(?:s)? and withdrawal(?:s)?\b",
    ),
    EventCategory.NETWORK_UPGRADE: (
        r"\bnetwork upgrade\b",
        r"\bprotocol upgrade\b",
        r"\bhard forks?\b",
        r"\bsoft forks?\b",
        r"\bmainnet (?:launch|upgrade|release)\b",
        r"\btestnet (?:launch|upgrade|release)\b",
        r"\bsoftware (?:upgrade|release|version)\b",
        r"\bgovernance proposals?\b",
    ),
    EventCategory.OUTAGE_EXPLOIT: (
        r"\bexploits?\b",
        r"\bhack(?:ed|s|ing)?\b",
        r"\bsecurity breach\b",
        r"\bvulnerabilit(?:y|ies)\b",
        r"\boutages?\b",
        r"\bdowntime\b",
        r"\bnetwork halt(?:ed|s)?\b",
        r"\bchain halt(?:ed|s)?\b",
        r"\bdrain(?:ed|s|ing)\b",
        r"\b51% attack\b",
    ),
    EventCategory.ETF_INSTITUTIONAL: (
        r"\bETFs?\b",
        r"\bETPs?\b",
        r"\bexchange-traded funds?\b",
        r"\binstitutional (?:adoption|demand|flows?|investors?|allocation)\b",
        r"\bfund (?:flows?|inflows?|outflows?)\b",
        r"\btreasury (?:allocation|purchase|holdings?)\b",
        r"\bBlackRock\b",
        r"\bFidelity\b",
        r"\bGrayscale\b",
    ),
    EventCategory.REGULATORY_LEGAL: (
        r"\bregulat(?:ion|ions|or|ors|ory)\b",
        r"\blegislat(?:ion|ive)\b",
        r"\blawsuits?\b",
        r"\bcourt (?:rules?|ruling|decision|filing)\b",
        r"\blegal (?:action|challenge|framework|ruling)\b",
        r"\benforcement (?:action|case)\b",
        r"\b(?-i:SEC|CFTC) (?:charges?|sues?|files?|settles?|announces?|approves?|rejects?)\b",
        r"\b(?:legal|regulatory|court|enforcement) settlement\b",
        r"\bMiCA\b",
    ),
}

_CATEGORY_PRIORITY = (
    EventCategory.OUTAGE_EXPLOIT,
    EventCategory.ETF_INSTITUTIONAL,
    EventCategory.REGULATORY_LEGAL,
    EventCategory.NETWORK_UPGRADE,
    EventCategory.EXCHANGE_LIQUIDITY,
    EventCategory.ONCHAIN_ECOSYSTEM,
)

_IMPACT_AND_RISK: dict[EventCategory, tuple[str, str]] = {
    EventCategory.ONCHAIN_ECOSYSTEM: (
        "May affect network demand and valuation if the reported activity persists.",
        "The activity may be temporary, incentive-driven, or measured inconsistently.",
    ),
    EventCategory.EXCHANGE_LIQUIDITY: (
        "May change near-term access, liquidity, spreads, and price discovery.",
        "Venue-specific liquidity may not translate into durable market-wide demand.",
    ),
    EventCategory.NETWORK_UPGRADE: (
        "May affect network capability, reliability, or adoption after implementation.",
        "Execution defects, delays, or limited adoption may offset the intended benefit.",
    ),
    EventCategory.OUTAGE_EXPLOIT: (
        "May increase short-term downside volatility and operational risk until resolved.",
        "Initial loss, exposure, and recovery estimates may change as facts are confirmed.",
    ),
    EventCategory.ETF_INSTITUTIONAL: (
        "May affect institutional access and capital flows if confirmed demand is material.",
        "Announcements or gross flows may not produce persistent net buying pressure.",
    ),
    EventCategory.REGULATORY_LEGAL: (
        "May reprice legal, compliance, and market-access risk in affected jurisdictions.",
        "Scope, appeals, implementation timing, and cross-border applicability may differ.",
    ),
}


def _domain_matches(domain: str, candidates: frozenset[str]) -> bool:
    return any(domain == candidate or domain.endswith(f".{candidate}") for candidate in candidates)


def canonicalize_url(url: str) -> str:
    """Return a stable HTTPS evidence URL with common tracking data removed.

    HTTP links are upgraded to HTTPS. Other schemes, embedded credentials, and
    hostless values are rejected because they are unsuitable as evidence links.
    """

    if not isinstance(url, str) or not url.strip():
        raise ValueError("evidence URL must be non-empty")
    parsed = urlsplit(unescape(url.strip()))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("evidence URL must use HTTP(S) and include a host")
    if parsed.username or parsed.password:
        raise ValueError("evidence URL must not contain credentials")

    host = parsed.hostname.rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("evidence URL has an invalid port") from exc
    if port and port not in {80, 443}:
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_parts = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        query_parts.append((key, value))
    query = urlencode(sorted(query_parts))
    return urlunsplit(("https", netloc, path, query, ""))


def source_quality_for_url(url: str) -> SourceQuality:
    """Grade a source by its registrable official/reputable domain allowlist."""

    try:
        domain = (urlsplit(canonicalize_url(url)).hostname or "").lower()
    except ValueError:
        return SourceQuality.LOW
    if _domain_matches(domain, _HIGH_QUALITY_DOMAINS):
        return SourceQuality.HIGH
    if _domain_matches(domain, _MEDIUM_QUALITY_DOMAINS):
        return SourceQuality.MEDIUM
    return SourceQuality.LOW


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first_text(element: Any, names: set[str]) -> str:
    for child in element.iter():
        if _local_name(child.tag) in names and child.text:
            value = " ".join(child.itertext()).strip()
            if value:
                return _clean_text(value)
    return ""


def _entry_url(element: Any) -> str:
    for child in element.iter():
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        relation = child.attrib.get("rel", "alternate").lower()
        if href and relation in {"alternate", ""}:
            return href
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _clean_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", unescape(value))
    return re.sub(r"\s+", " ", without_tags).strip()


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_feed(
    content: bytes | str,
    source_name: str = "",
    now: datetime | None = None,
    lookback_hours: int | None = None,
) -> list[NewsItem]:
    """Parse RSS 2.x or Atom into normalized :class:`NewsItem` values.

    Entries without a valid HTTPS-canonicalizable link or publication time are
    skipped. Supplying ``now`` and ``lookback_hours`` applies an inclusive age
    filter at the parsing boundary.
    """

    try:
        raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        root = ElementTree.fromstring(
            raw,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (ElementTree.ParseError, DefusedXmlException, TypeError, ValueError) as exc:
        raise FeedParseError(f"invalid RSS/Atom document: {exc}") from exc

    root_name = _local_name(root.tag)
    if root_name not in {"feed", "rdf", "rss"}:
        raise FeedParseError("document root is not RSS or Atom")
    if root_name == "rss" and not any(_local_name(node.tag) == "channel" for node in root):
        raise FeedParseError("RSS document is missing its channel")

    entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    feed_name = source_name.strip() or _first_text(root, {"title"}) or "Unknown feed"
    current = _utc(now or datetime.now(UTC))
    cutoff = current - timedelta(hours=lookback_hours) if lookback_hours is not None else None

    result: list[NewsItem] = []
    for entry in entries:
        title = _first_text(entry, {"title"})
        summary = _first_text(entry, {"description", "summary", "content", "content:encoded"})
        published_raw = _first_text(entry, {"pubdate", "published", "updated", "date"})
        published_at = _parse_datetime(published_raw)
        if not title or published_at is None:
            continue
        if cutoff is not None and not cutoff <= published_at <= current + timedelta(minutes=5):
            continue
        try:
            url = canonicalize_url(_entry_url(entry))
        except ValueError:
            continue
        domain = urlsplit(url).hostname or ""
        result.append(
            NewsItem(
                title=title,
                url=url,
                summary=summary,
                published_at=published_at,
                source_name=feed_name,
                source_domain=domain,
                source_quality=source_quality_for_url(url),
            )
        )
    return sorted(result, key=lambda item: (-item.published_at.timestamp(), item.url, item.title))


def _contains_pattern(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _matched_assets(text: str, assets: Iterable[Asset]) -> tuple[str, ...]:
    matches: list[str] = []
    has_crypto_context = _is_globally_crypto_relevant(text)
    for asset in assets:
        if asset.aliases == (asset.symbol,):
            explicit_dynamic_reference = re.search(
                rf"(?:\${re.escape(asset.symbol)}\b|"
                rf"\b{re.escape(asset.symbol)}(?:/|-)USDT\b)",
                text,
            )
            if explicit_dynamic_reference is None:
                continue
        matched = False
        for alias in asset.aliases:
            alias_match = re.search(
                rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                text,
                0 if alias == asset.symbol else re.IGNORECASE,
            )
            if alias_match is None:
                continue
            if alias == asset.symbol and asset.symbol in _AMBIGUOUS_SYMBOLS:
                if not has_crypto_context and not re.search(rf"\${re.escape(alias)}\b", text):
                    continue
            if alias in _AMBIGUOUS_NAME_ALIASES and not has_crypto_context:
                continue
            matched = True
            break
        if matched:
            matches.append(asset.symbol)
    return tuple(matches)


def classify_category(text: str) -> EventCategory | None:
    """Classify text into exactly one of the six supported catalyst classes."""

    scores = {
        category: sum(_contains_pattern(text, pattern) for pattern in patterns)
        for category, patterns in _CATEGORY_PATTERNS.items()
    }
    best_score = max(scores.values(), default=0)
    if best_score == 0:
        return None
    return next(category for category in _CATEGORY_PRIORITY if scores[category] == best_score)


def _is_globally_crypto_relevant(text: str) -> bool:
    return any(_contains_pattern(text, pattern) for pattern in _GLOBAL_CRYPTO_PATTERNS)


def _fingerprint_text(value: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", value.casefold()).split()
    return " ".join(word for word in words if word not in _EDITORIAL_IDENTITY_WORDS)


def _event_id(asset: str, category: EventCategory, headline_key: str) -> str:
    # Content identity stays stable when corroborating sources arrive or a feed
    # adds common editorial labels such as "updated" to the same headline.
    identity = "|".join((asset, category.value, headline_key))
    return f"news-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def news_items_to_events(
    items: Iterable[NewsItem],
    assets: Iterable[Asset],
    now: datetime | None = None,
    lookback_hours: int = 30,
) -> list[AlertEvent]:
    """Convert recent, corroborated catalyst items into deterministic alerts.

    Items with the same normalized headline, asset, and category are treated as
    corroboration. A group supported only by LOW-quality sources is excluded.
    """

    current = _utc(now or datetime.now(UTC))
    cutoff = current - timedelta(hours=lookback_hours)
    asset_values = tuple(assets)
    grouped: dict[tuple[str, EventCategory, str], list[NewsItem]] = defaultdict(list)

    for item in items:
        published_at = _utc(item.published_at)
        if not cutoff <= published_at <= current + timedelta(minutes=5):
            continue
        try:
            canonical_url = canonicalize_url(item.url)
        except ValueError:
            continue
        domain = urlsplit(canonical_url).hostname or ""
        normalized_item = replace(
            item,
            url=canonical_url,
            source_domain=domain,
            source_quality=source_quality_for_url(canonical_url),
        )
        text = f"{normalized_item.title} {normalized_item.summary}".strip()
        category = classify_category(text)
        if category not in NEWS_CATEGORIES:
            continue
        matched = _matched_assets(text, asset_values)
        # Generic crypto headlines are global context, not token-specific
        # evidence.  Replicating one headline to every discovered market would
        # manufacture fundamental signals and grow quadratically at scale.
        if not matched:
            continue
        headline_key = _fingerprint_text(normalized_item.title) or f"url:{canonical_url}"
        for symbol in matched:
            grouped[(symbol, category, headline_key)].append(normalized_item)

    events: list[AlertEvent] = []
    for (symbol, category, headline_key), evidence_items in grouped.items():
        highest_quality = max(
            (item.source_quality for item in evidence_items),
            key=_QUALITY_RANK.__getitem__,
        )
        if highest_quality is SourceQuality.LOW:
            continue
        representative = sorted(
            evidence_items,
            key=lambda item: (
                -_QUALITY_RANK[item.source_quality],
                -_utc(item.published_at).timestamp(),
                item.url,
                item.title,
            ),
        )[0]
        evidence_urls = tuple(sorted({canonicalize_url(item.url) for item in evidence_items}))
        impact, risk = _IMPACT_AND_RISK[category]
        observed_at = max(_utc(item.published_at) for item in evidence_items)
        events.append(
            AlertEvent(
                event_id=_event_id(symbol, category, headline_key),
                asset=symbol,
                category=category,
                catalyst=representative.title,
                evidence_urls=evidence_urls,
                source_quality=highest_quality,
                probable_market_impact=impact,
                main_risk=risk,
                technical_vs_fundamental=AnalysisType.FUNDAMENTAL,
                observed_at=observed_at,
                metrics={
                    "source_count": len(evidence_urls),
                    "source_domains": sorted({item.source_domain for item in evidence_items}),
                    "published_at": _utc(representative.published_at).isoformat(),
                },
            )
        )
    return sorted(
        events,
        key=lambda event: (-event.observed_at.timestamp(), event.asset, event.event_id),
    )


# Explicit aliases keep the integration surface readable in the CLI and in
# downstream tests without introducing separate behavior.
parse_news_feed = parse_feed
build_news_events = news_items_to_events


__all__ = [
    "FeedParseError",
    "NEWS_CATEGORIES",
    "build_news_events",
    "canonicalize_url",
    "classify_category",
    "news_items_to_events",
    "parse_feed",
    "parse_news_feed",
    "source_quality_for_url",
]

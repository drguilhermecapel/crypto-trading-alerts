"""Small bounded HTTP helpers for public read-only sources."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from typing import Any
from urllib import error, parse, request

USER_AGENT = "CryptoTradingAlerts/2.0 (+https://github.com/drguilhermecapel/crypto-trading-alerts)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class PublicSourceError(RuntimeError):
    """A public source could not be read or returned an unsafe response."""


UrlOpen = Callable[..., Any]


def validate_public_https_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise PublicSourceError("source URL must be non-empty")
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PublicSourceError("source URL must be credential-free HTTPS")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise PublicSourceError("local source hosts are prohibited")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise PublicSourceError("non-public source addresses are prohibited")
    return url


def fetch_bytes(
    url: str,
    *,
    timeout_seconds: float = 12.0,
    max_bytes: int = MAX_RESPONSE_BYTES,
    urlopen: UrlOpen = request.urlopen,
) -> bytes:
    """Fetch one bounded HTTPS document without exposing response bodies in errors."""

    validate_public_https_url(url)
    if isinstance(timeout_seconds, bool) or not 1 <= float(timeout_seconds) <= 60:
        raise ValueError("timeout_seconds must be between 1 and 60")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= 10 * 1024 * 1024
    ):
        raise ValueError("max_bytes must be between 1 and 10485760")
    outgoing = request.Request(  # noqa: S310 - URL is validated as public HTTPS above.
        url,
        headers={
            "Accept": (
                "application/json, application/rss+xml, application/atom+xml, "
                "application/xml, text/xml;q=0.9"
            ),
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urlopen(outgoing, timeout=float(timeout_seconds)) as response:
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            final_url = response.geturl() if hasattr(response, "geturl") else url
            validate_public_https_url(final_url)
            payload = response.read(max_bytes + 1)
    except error.HTTPError as exc:
        raise PublicSourceError(f"source returned HTTP {exc.code}") from None
    except TimeoutError:
        raise PublicSourceError("source request timed out") from None
    except (error.URLError, OSError):
        raise PublicSourceError("source network request failed") from None
    if status != 200:
        raise PublicSourceError("source returned a non-success status")
    if len(payload) > max_bytes:
        raise PublicSourceError("source response exceeded the safe size limit")
    return payload


def fetch_json(url: str, **kwargs: Any) -> dict[str, Any]:
    payload = fetch_bytes(url, **kwargs)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise PublicSourceError("source did not return valid UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise PublicSourceError("source JSON root must be an object")
    return document

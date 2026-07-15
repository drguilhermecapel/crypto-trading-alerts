"""Strict, deterministic discovery of active spot-USDT instruments.

This module only normalizes exchange metadata supplied by callers.  It performs
no network I/O, accepts no credentials, and deliberately derives asset identity
from the exchanges' explicit base/quote fields instead of splitting instrument
names.  The resulting universe is suitable for later read-only market-data
collection.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_UNIVERSE_ASSETS = 2_000
"""Hard upper bound for a materialized, canonical asset universe."""

_MAX_PAYLOAD_INSTRUMENTS = 10_000
_ASSET_PATTERN = re.compile(r"[A-Z0-9]{1,24}\Z")

# Quote-backed assets are useful market plumbing, but they should not receive
# directional token recommendations from the momentum advisor.  Keep this list
# explicit: broad rules such as ``startswith('USD')`` risk hiding unrelated
# assets and make additions difficult to audit.
_STABLECOINS = frozenset(
    {
        "BUSD",
        "CRVUSD",
        "DAI",
        "DOLA",
        "EURC",
        "EURS",
        "EURT",
        "FDUSD",
        "FRAX",
        "GUSD",
        "LUSD",
        "PYUSD",
        "RLUSD",
        "SUSD",
        "TUSD",
        "USD0",
        "USD1",
        "USDC",
        "USDD",
        "USDE",
        "USDJ",
        "USDP",
        "USDS",
        "USDT",
        "USDX",
        "USTC",
    }
)

# Exchange-issued leveraged tokens commonly encode their direction in the base
# asset.  Requiring at least two characters before the direction suffix avoids
# classifying the ordinary JUP token as a leveraged ``...UP`` product.
_BINANCE_LEVERAGED = re.compile(r"[A-Z0-9]{2,}(?:UP|DOWN|BULL|BEAR)\Z")
_OKX_LEVERAGED = re.compile(r"[A-Z0-9]{2,}\d+[LS]\Z")


class Venue(StrEnum):
    """Supported public spot venues."""

    OKX = "okx"
    BINANCE = "binance"


class UniversePayloadError(ValueError):
    """An exchange discovery payload is malformed or internally inconsistent."""


class ExclusionReason(StrEnum):
    """Auditable reasons why a syntactically valid listing is not eligible."""

    INACTIVE = "inactive"
    NOT_SPOT = "not_spot"
    NOT_USDT_QUOTED = "not_usdt_quoted"
    STABLECOIN = "stablecoin"
    LEVERAGED_PRODUCT = "leveraged_product"


@dataclass(frozen=True, slots=True, order=True)
class VenueInstrument:
    """One validated exchange instrument for a canonical base asset."""

    symbol: str
    venue: Venue
    instrument: str
    quote: str = "USDT"


@dataclass(frozen=True, slots=True)
class ExcludedInstrument:
    """Optional caller-collected record for an otherwise valid filtered row."""

    venue: Venue
    instrument: str
    symbol: str
    reason: ExclusionReason


@dataclass(frozen=True, slots=True)
class UniverseAsset:
    """A deduplicated asset with deterministic exchange provenance."""

    symbol: str
    instruments: tuple[VenueInstrument, ...]


def _root(payload: Any, venue: Venue) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UniversePayloadError(f"{venue.value} payload root must be an object")
    return payload


def _rows(value: Any, venue: Venue) -> list[Any]:
    if not isinstance(value, list):
        raise UniversePayloadError(f"{venue.value} instruments must be an array")
    if len(value) > _MAX_PAYLOAD_INSTRUMENTS:
        raise UniversePayloadError(f"{venue.value} payload exceeds the safe instrument limit")
    return value


def _text(row: dict[str, Any], field: str, venue: Venue, index: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise UniversePayloadError(f"{venue.value} instrument row {index} has invalid {field}")
    if value != value.strip() or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise UniversePayloadError(f"{venue.value} instrument row {index} has invalid {field}")
    return value


def _asset_code(value: str, field: str, venue: Venue, index: int) -> str:
    if _ASSET_PATTERN.fullmatch(value) is None:
        raise UniversePayloadError(f"{venue.value} instrument row {index} has invalid {field}")
    return value


def _record_exclusion(
    records: list[ExcludedInstrument] | None,
    *,
    venue: Venue,
    instrument: str,
    symbol: str,
    reason: ExclusionReason,
) -> None:
    if records is not None:
        records.append(ExcludedInstrument(venue, instrument, symbol, reason))


def _is_stablecoin(symbol: str) -> bool:
    return symbol in _STABLECOINS


def _is_leveraged(symbol: str, venue: Venue) -> bool:
    pattern = _OKX_LEVERAGED if venue is Venue.OKX else _BINANCE_LEVERAGED
    return pattern.fullmatch(symbol) is not None


def _deduplicated_sorted(
    values: list[VenueInstrument], venue: Venue
) -> tuple[VenueInstrument, ...]:
    seen_instruments: set[str] = set()
    seen_symbols: set[str] = set()
    for value in values:
        if value.instrument in seen_instruments or value.symbol in seen_symbols:
            raise UniversePayloadError(
                f"{venue.value} payload contains a duplicate eligible instrument"
            )
        seen_instruments.add(value.instrument)
        seen_symbols.add(value.symbol)
    return tuple(sorted(values, key=lambda item: (item.symbol, item.instrument)))


def parse_okx_instruments(
    payload: Any,
    *,
    exclusions: list[ExcludedInstrument] | None = None,
) -> tuple[VenueInstrument, ...]:
    """Parse a public OKX instruments response into live spot-USDT listings."""

    root = _root(payload, Venue.OKX)
    if root.get("code") != "0":
        raise UniversePayloadError("okx returned an unsuccessful instruments response")
    rows = _rows(root.get("data"), Venue.OKX)
    result: list[VenueInstrument] = []
    seen_payload_instruments: set[str] = set()

    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            raise UniversePayloadError(f"okx instrument row {index} must be an object")
        instrument_type = _text(raw_row, "instType", Venue.OKX, index)
        instrument = _text(raw_row, "instId", Venue.OKX, index)
        base = _asset_code(_text(raw_row, "baseCcy", Venue.OKX, index), "baseCcy", Venue.OKX, index)
        quote = _asset_code(
            _text(raw_row, "quoteCcy", Venue.OKX, index), "quoteCcy", Venue.OKX, index
        )
        state = _text(raw_row, "state", Venue.OKX, index)
        if instrument in seen_payload_instruments:
            raise UniversePayloadError("okx payload contains a duplicate instrument")
        seen_payload_instruments.add(instrument)

        if instrument_type != "SPOT":
            _record_exclusion(
                exclusions,
                venue=Venue.OKX,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.NOT_SPOT,
            )
            continue
        if instrument != f"{base}-{quote}":
            raise UniversePayloadError("okx instrument is inconsistent with base/quote fields")
        if state != "live":
            _record_exclusion(
                exclusions,
                venue=Venue.OKX,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.INACTIVE,
            )
            continue
        if quote != "USDT":
            _record_exclusion(
                exclusions,
                venue=Venue.OKX,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.NOT_USDT_QUOTED,
            )
            continue
        if _is_stablecoin(base):
            _record_exclusion(
                exclusions,
                venue=Venue.OKX,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.STABLECOIN,
            )
            continue
        if _is_leveraged(base, Venue.OKX):
            _record_exclusion(
                exclusions,
                venue=Venue.OKX,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.LEVERAGED_PRODUCT,
            )
            continue
        result.append(VenueInstrument(base, Venue.OKX, instrument, quote))

    return _deduplicated_sorted(result, Venue.OKX)


def parse_binance_instruments(
    payload: Any,
    *,
    exclusions: list[ExcludedInstrument] | None = None,
) -> tuple[VenueInstrument, ...]:
    """Parse Binance exchangeInfo into trading spot-USDT listings."""

    root = _root(payload, Venue.BINANCE)
    if "code" in root:
        raise UniversePayloadError("binance returned an unsuccessful exchangeInfo response")
    rows = _rows(root.get("symbols"), Venue.BINANCE)
    result: list[VenueInstrument] = []
    seen_payload_instruments: set[str] = set()

    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            raise UniversePayloadError(f"binance instrument row {index} must be an object")
        instrument = _text(raw_row, "symbol", Venue.BINANCE, index)
        base = _asset_code(
            _text(raw_row, "baseAsset", Venue.BINANCE, index),
            "baseAsset",
            Venue.BINANCE,
            index,
        )
        quote = _asset_code(
            _text(raw_row, "quoteAsset", Venue.BINANCE, index),
            "quoteAsset",
            Venue.BINANCE,
            index,
        )
        status = _text(raw_row, "status", Venue.BINANCE, index)
        spot_allowed = raw_row.get("isSpotTradingAllowed")
        permissions = raw_row.get("permissions", [])
        permission_sets = raw_row.get("permissionSets", [])
        if not isinstance(spot_allowed, bool):
            raise UniversePayloadError(
                f"binance instrument row {index} has invalid isSpotTradingAllowed"
            )
        if not isinstance(permissions, list) or any(
            not isinstance(permission, str) or not permission for permission in permissions
        ):
            raise UniversePayloadError(f"binance instrument row {index} has invalid permissions")
        if not isinstance(permission_sets, list) or any(
            not isinstance(permission_set, list)
            or any(
                not isinstance(permission, str) or not permission for permission in permission_set
            )
            for permission_set in permission_sets
        ):
            raise UniversePayloadError(f"binance instrument row {index} has invalid permissionSets")
        if instrument in seen_payload_instruments:
            raise UniversePayloadError("binance payload contains a duplicate instrument")
        seen_payload_instruments.add(instrument)
        if instrument != f"{base}{quote}":
            raise UniversePayloadError("binance instrument is inconsistent with base/quote fields")

        has_spot_permission = "SPOT" in permissions or any(
            "SPOT" in permission_set for permission_set in permission_sets
        )
        if not spot_allowed or not has_spot_permission:
            _record_exclusion(
                exclusions,
                venue=Venue.BINANCE,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.NOT_SPOT,
            )
            continue
        if status != "TRADING":
            _record_exclusion(
                exclusions,
                venue=Venue.BINANCE,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.INACTIVE,
            )
            continue
        if quote != "USDT":
            _record_exclusion(
                exclusions,
                venue=Venue.BINANCE,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.NOT_USDT_QUOTED,
            )
            continue
        if _is_stablecoin(base):
            _record_exclusion(
                exclusions,
                venue=Venue.BINANCE,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.STABLECOIN,
            )
            continue
        if _is_leveraged(base, Venue.BINANCE):
            _record_exclusion(
                exclusions,
                venue=Venue.BINANCE,
                instrument=instrument,
                symbol=base,
                reason=ExclusionReason.LEVERAGED_PRODUCT,
            )
            continue
        result.append(VenueInstrument(base, Venue.BINANCE, instrument, quote))

    return _deduplicated_sorted(result, Venue.BINANCE)


def build_universe(
    okx_instruments: Sequence[VenueInstrument],
    binance_instruments: Sequence[VenueInstrument],
    *,
    max_assets: int = MAX_UNIVERSE_ASSETS,
) -> tuple[UniverseAsset, ...]:
    """Union both venues and fail closed if the bounded universe is exceeded."""

    if (
        isinstance(max_assets, bool)
        or not isinstance(max_assets, int)
        or not 1 <= max_assets <= MAX_UNIVERSE_ASSETS
    ):
        raise ValueError(f"max_assets must be an integer between 1 and {MAX_UNIVERSE_ASSETS}")

    grouped: dict[str, dict[Venue, VenueInstrument]] = {}
    for expected_venue, values in (
        (Venue.OKX, okx_instruments),
        (Venue.BINANCE, binance_instruments),
    ):
        if isinstance(values, str | bytes) or not isinstance(values, Sequence):
            raise TypeError("exchange instruments must be sequences")
        for value in values:
            if not isinstance(value, VenueInstrument) or value.venue is not expected_venue:
                raise UniversePayloadError("instrument provenance does not match its exchange")
            if (
                _ASSET_PATTERN.fullmatch(value.symbol) is None
                or value.quote != "USDT"
                or not value.instrument
            ):
                raise UniversePayloadError("instrument contains invalid canonical metadata")
            expected_instrument = (
                f"{value.symbol}-{value.quote}"
                if value.venue is Venue.OKX
                else f"{value.symbol}{value.quote}"
            )
            if value.instrument != expected_instrument:
                raise UniversePayloadError("instrument is inconsistent with canonical metadata")
            by_venue = grouped.setdefault(value.symbol, {})
            if expected_venue in by_venue:
                raise UniversePayloadError("universe input contains duplicate venue provenance")
            by_venue[expected_venue] = value

    venue_order = {Venue.OKX: 0, Venue.BINANCE: 1}
    universe = [
        UniverseAsset(
            symbol=symbol,
            instruments=tuple(
                sorted(grouped[symbol].values(), key=lambda item: venue_order[item.venue])
            ),
        )
        for symbol in sorted(grouped)
    ]
    if len(universe) > max_assets:
        raise UniversePayloadError(
            f"discovered universe contains {len(universe)} assets, above max_assets={max_assets}"
        )
    return tuple(universe)


__all__ = [
    "ExcludedInstrument",
    "ExclusionReason",
    "MAX_UNIVERSE_ASSETS",
    "UniverseAsset",
    "UniversePayloadError",
    "Venue",
    "VenueInstrument",
    "build_universe",
    "parse_binance_instruments",
    "parse_okx_instruments",
]

"""Durable, fail-closed state for event de-duplication and daily digests.

Reading and querying a :class:`StateStore` never writes to disk.  Callers must
explicitly call :meth:`StateStore.commit` after delivery has succeeded.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if os.name == "nt":
    import msvcrt
else:
    import fcntl

STATE_VERSION = 1
_MAX_STATE_BYTES = 5 * 1024 * 1024
_MAX_EVENT_IDS = 100_000
_MAX_DIGEST_DAYS = 20_000


class StateError(RuntimeError):
    """Base class for state failures that must stop notification delivery."""


class StateCorruptionError(StateError):
    """Raised when existing state cannot be trusted."""


class StateConflictError(StateError):
    """Raised when committing a stale snapshot or a duplicate daily digest."""


@dataclass(frozen=True, slots=True, order=True)
class SeenEvent:
    """An event identifier and the UTC time at which it was committed."""

    event_id: str
    seen_at: datetime


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Immutable in-memory representation of the state file."""

    generation: int = 0
    seen_events: tuple[SeenEvent, ...] = ()
    digest_days: frozenset[date] = frozenset()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateCorruptionError(f"state contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise StateCorruptionError(f"state contains non-finite JSON number: {value}")


def _aware_utc(value: datetime | None) -> datetime:
    result = value if value is not None else datetime.now(UTC)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return result.astimezone(UTC)


def _event_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("event_id must be a string")
    if not value or value != value.strip() or len(value) > 512:
        raise ValueError("event_id must be non-empty, trimmed, and at most 512 characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError("event_id must not contain control characters")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise StateCorruptionError("seen event timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateCorruptionError("seen event timestamp is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise StateCorruptionError("seen event timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _digest_day(value: Any) -> date:
    if not isinstance(value, str):
        raise StateCorruptionError("digest day must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise StateCorruptionError("digest day is not a valid ISO date") from exc
    if parsed.isoformat() != value:
        raise StateCorruptionError("digest day must use YYYY-MM-DD format")
    return parsed


class StateStore:
    """Strict JSON state store with explicit, atomic commits."""

    def __init__(self, path: str | Path, dedupe_hours: int, timezone_name: str) -> None:
        if isinstance(dedupe_hours, bool) or not isinstance(dedupe_hours, int):
            raise ValueError("dedupe_hours must be an integer")
        if not 1 <= dedupe_hours <= 24 * 365:
            raise ValueError("dedupe_hours must be between 1 and 8760")
        try:
            local_timezone = ZoneInfo(timezone_name)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise ValueError(f"unknown timezone: {timezone_name}") from exc

        self.path = Path(path)
        self.dedupe_window = timedelta(hours=dedupe_hours)
        self.local_timezone = local_timezone

    def load(self) -> StateSnapshot:
        """Load state without creating or modifying any filesystem object."""

        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return StateSnapshot()
        except OSError as exc:
            raise StateError("cannot inspect state file") from exc
        if size > _MAX_STATE_BYTES:
            raise StateCorruptionError("state file exceeds the safe size limit")

        try:
            payload = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StateError("cannot read state file") from exc
        try:
            raw = json.loads(
                payload,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
        except StateCorruptionError:
            raise
        except json.JSONDecodeError as exc:
            raise StateCorruptionError("state file is not valid JSON") from exc

        if not isinstance(raw, dict):
            raise StateCorruptionError("state root must be an object")
        expected_keys = {"version", "generation", "seen_events", "digest_days"}
        if set(raw) != expected_keys:
            raise StateCorruptionError("state root has missing or unknown keys")
        if (
            not isinstance(raw["version"], int)
            or isinstance(raw["version"], bool)
            or raw["version"] != STATE_VERSION
        ):
            raise StateCorruptionError(f"state version must be {STATE_VERSION}")
        generation = raw["generation"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise StateCorruptionError("state generation must be a non-negative integer")

        seen_raw = raw["seen_events"]
        if not isinstance(seen_raw, dict) or len(seen_raw) > _MAX_EVENT_IDS:
            raise StateCorruptionError("seen_events must be an object within the safe limit")
        seen_events: list[SeenEvent] = []
        for raw_event_id, raw_timestamp in seen_raw.items():
            try:
                valid_event_id = _event_id(raw_event_id)
            except ValueError as exc:
                raise StateCorruptionError("state contains an invalid event_id") from exc
            seen_events.append(SeenEvent(valid_event_id, _timestamp(raw_timestamp)))

        days_raw = raw["digest_days"]
        if not isinstance(days_raw, list) or len(days_raw) > _MAX_DIGEST_DAYS:
            raise StateCorruptionError("digest_days must be a list within the safe limit")
        digest_days = tuple(_digest_day(value) for value in days_raw)
        if len(set(digest_days)) != len(digest_days):
            raise StateCorruptionError("digest_days must not contain duplicates")

        return StateSnapshot(
            generation=generation,
            seen_events=tuple(sorted(seen_events)),
            digest_days=frozenset(digest_days),
        )

    def is_duplicate(
        self,
        snapshot: StateSnapshot,
        event_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether an event was committed within the de-duplication window."""

        valid_event_id = _event_id(event_id)
        cutoff = _aware_utc(now) - self.dedupe_window
        return any(
            event.event_id == valid_event_id and event.seen_at >= cutoff
            for event in snapshot.seen_events
        )

    def new_event_ids(
        self,
        snapshot: StateSnapshot,
        event_ids: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Filter IDs without mutating the snapshot or the state file."""

        if isinstance(event_ids, str | bytes):
            raise ValueError("event_ids must be an iterable of strings, not a string")
        result: list[str] = []
        supplied: set[str] = set()
        for value in event_ids:
            valid_event_id = _event_id(value)
            if valid_event_id in supplied:
                continue
            supplied.add(valid_event_id)
            if not self.is_duplicate(snapshot, valid_event_id, now=now):
                result.append(valid_event_id)
        return tuple(result)

    def local_day(self, *, now: datetime | None = None) -> date:
        """Resolve the configured local calendar day for an instant."""

        return _aware_utc(now).astimezone(self.local_timezone).date()

    def digest_sent_today(
        self,
        snapshot: StateSnapshot,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether today's local digest was already committed."""

        return self.local_day(now=now) in snapshot.digest_days

    def commit(
        self,
        snapshot: StateSnapshot,
        *,
        event_ids: Iterable[str] = (),
        mark_digest_sent: bool = False,
        now: datetime | None = None,
    ) -> StateSnapshot:
        """Atomically persist a new generation based on ``snapshot``.

        Old event IDs are pruned here, never during a read or query.  A stale
        snapshot is rejected so one run cannot silently overwrite another.
        """

        if not isinstance(snapshot, StateSnapshot):
            raise TypeError("snapshot must be a StateSnapshot")
        if not isinstance(mark_digest_sent, bool):
            raise ValueError("mark_digest_sent must be boolean")
        if isinstance(event_ids, str | bytes):
            raise ValueError("event_ids must be an iterable of strings, not a string")
        valid_event_ids = tuple(_event_id(value) for value in event_ids)
        committed_at = _aware_utc(now)
        today = self.local_day(now=committed_at)
        if mark_digest_sent and today in snapshot.digest_days:
            raise StateConflictError("digest was already committed for this local day")

        with self._commit_lock():
            current = self.load()
            if current != snapshot:
                raise StateConflictError("state changed after the snapshot was loaded")

            cutoff = committed_at - self.dedupe_window
            retained = {
                event.event_id: event.seen_at
                for event in snapshot.seen_events
                if event.seen_at >= cutoff
            }
            for value in valid_event_ids:
                retained[value] = committed_at
            if len(retained) > _MAX_EVENT_IDS:
                raise StateError("committed state would exceed the safe event limit")

            days = set(snapshot.digest_days)
            if mark_digest_sent:
                days.add(today)
            if len(days) > _MAX_DIGEST_DAYS:
                raise StateError("committed state would exceed the safe digest-day limit")

            result = StateSnapshot(
                generation=snapshot.generation + 1,
                seen_events=tuple(
                    sorted(SeenEvent(event_id, seen_at) for event_id, seen_at in retained.items())
                ),
                digest_days=frozenset(days),
            )
            self._atomic_write(result)
        return result

    @contextmanager
    def _commit_lock(self) -> Iterator[None]:
        """Hold a non-blocking process lock across compare-and-replace."""

        parent = self.path.parent
        lock_path = parent / f".{self.path.name}.lock"
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise StateError("cannot prepare state commit lock") from exc
        try:
            try:
                self._lock_descriptor(descriptor)
            except OSError as exc:
                raise StateConflictError("another state commit is already in progress") from exc
            yield
        finally:
            with suppress(OSError):
                self._unlock_descriptor(descriptor)
            with suppress(OSError):
                os.close(descriptor)

    @staticmethod
    def _lock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _atomic_write(self, snapshot: StateSnapshot) -> None:
        seen = {
            event.event_id: event.seen_at.astimezone(UTC).isoformat()
            for event in snapshot.seen_events
        }
        document = {
            "version": STATE_VERSION,
            "generation": snapshot.generation,
            "seen_events": seen,
            "digest_days": sorted(day.isoformat() for day in snapshot.digest_days),
        }
        encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > _MAX_STATE_BYTES:
            raise StateError("committed state would exceed the safe file size")

        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=parent
            )
        except OSError as exc:
            raise StateError("cannot prepare atomic state commit") from exc

        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            try:
                directory_descriptor = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                # The file replacement is already durable on common local filesystems;
                # not every platform permits fsync on a directory.
                return
        except OSError as exc:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
            raise StateError("cannot atomically commit state") from exc

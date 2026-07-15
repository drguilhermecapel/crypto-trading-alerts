from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crypto_alerts.state import (
    StateConflictError,
    StateCorruptionError,
    StateSnapshot,
    StateStore,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "nested" / "state.json"
        self.store = StateStore(self.path, dedupe_hours=48, timezone_name="America/Sao_Paulo")
        self.now = datetime(2026, 7, 15, 12, tzinfo=UTC)

    def test_queries_do_not_mutate_disk_until_explicit_commit(self) -> None:
        snapshot = self.store.load()

        self.assertEqual(snapshot, StateSnapshot())
        self.assertEqual(
            self.store.new_event_ids(snapshot, ["event-a", "event-a", "event-b"], now=self.now),
            ("event-a", "event-b"),
        )
        self.assertFalse(self.store.digest_sent_today(snapshot, now=self.now))
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

        committed = self.store.commit(
            snapshot,
            event_ids=("event-a",),
            mark_digest_sent=True,
            now=self.now,
        )

        self.assertTrue(self.path.exists())
        self.assertEqual(self.store.load(), committed)
        self.assertTrue(self.store.is_duplicate(committed, "event-a", now=self.now))
        self.assertTrue(self.store.digest_sent_today(committed, now=self.now))

    def test_expired_event_ids_are_pruned_only_during_commit(self) -> None:
        initial = self.store.commit(StateSnapshot(), event_ids=("old",), now=self.now)
        after_window = self.now + timedelta(hours=49)

        self.assertFalse(self.store.is_duplicate(initial, "old", now=after_window))
        self.assertIn("old", {event.event_id for event in initial.seen_events})
        self.assertIn("old", json.loads(self.path.read_text())["seen_events"])

        committed = self.store.commit(initial, event_ids=("new",), now=after_window)

        self.assertEqual({event.event_id for event in committed.seen_events}, {"new"})
        self.assertNotIn("old", json.loads(self.path.read_text())["seen_events"])

    def test_digest_day_uses_configured_local_calendar(self) -> None:
        before_midnight_local = datetime(2026, 7, 15, 2, 30, tzinfo=UTC)
        after_midnight_local = datetime(2026, 7, 15, 3, 30, tzinfo=UTC)
        committed = self.store.commit(
            StateSnapshot(),
            mark_digest_sent=True,
            now=before_midnight_local,
        )

        self.assertTrue(
            self.store.digest_sent_today(
                committed,
                now=before_midnight_local + timedelta(minutes=20),
            )
        )
        self.assertFalse(self.store.digest_sent_today(committed, now=after_midnight_local))

        next_day = self.store.commit(
            committed,
            mark_digest_sent=True,
            now=after_midnight_local,
        )
        self.assertEqual(len(next_day.digest_days), 2)
        with self.assertRaises(StateConflictError):
            self.store.commit(
                next_day,
                mark_digest_sent=True,
                now=after_midnight_local,
            )

    def test_corrupt_or_ambiguous_state_fails_closed(self) -> None:
        self.path.parent.mkdir(parents=True)
        corrupt_documents = (
            "not-json",
            '{"version":1,"version":1,"generation":0,"seen_events":{},"digest_days":[]}',
            '{"version":1,"generation":0,"seen_events":{},"digest_days":[],"extra":true}',
            '{"version":1,"generation":0,"seen_events":{"x":"not-a-date"},"digest_days":[]}',
            '{"version":1,"generation":0,"seen_events":{},"digest_days":["2026-7-1"]}',
        )
        for document in corrupt_documents:
            with self.subTest(document=document):
                self.path.write_text(document, encoding="utf-8")
                with self.assertRaises(StateCorruptionError):
                    self.store.load()

    def test_stale_snapshot_cannot_overwrite_newer_state(self) -> None:
        first_reader = self.store.load()
        second_reader = self.store.load()
        self.store.commit(first_reader, event_ids=("first",), now=self.now)

        with self.assertRaises(StateConflictError):
            self.store.commit(second_reader, event_ids=("second",), now=self.now)

        loaded = self.store.load()
        self.assertEqual({event.event_id for event in loaded.seen_events}, {"first"})

    def test_concurrent_commit_fails_instead_of_overwriting_state(self) -> None:
        snapshot = self.store.load()
        with self.store._commit_lock(), self.assertRaises(StateConflictError):
            self.store.commit(snapshot, event_ids=("second",), now=self.now)

    def test_commit_is_atomic_and_leaves_no_temporary_file(self) -> None:
        self.store.commit(StateSnapshot(), event_ids=("event-a",), now=self.now)
        self.store.commit(self.store.load(), event_ids=("event-b",), now=self.now)

        self.assertEqual(
            {event.event_id for event in self.store.load().seen_events},
            {"event-a", "event-b"},
        )
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_invalid_identifiers_and_naive_times_are_rejected_without_writing(self) -> None:
        snapshot = self.store.load()
        with self.assertRaises(ValueError):
            self.store.commit(snapshot, event_ids=(" bad ",), now=self.now)
        with self.assertRaises(ValueError):
            self.store.commit(snapshot, event_ids=("event",), now=self.now.replace(tzinfo=None))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

"""Regression tests for timestamp normalization and local-day boundaries.

Run with: python3 -m unittest discover tests
No third-party deps — models.py only needs the stdlib.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

# Point the app at a throwaway DB and a fixed timezone before config loads.
_tmpdir = tempfile.mkdtemp()
os.environ["CHRONICLE_DB"] = os.path.join(_tmpdir, "test.db")
os.environ["USER_TIMEZONE"] = "America/New_York"

import config  # noqa: E402
import models  # noqa: E402

ET = ZoneInfo("America/New_York")


def _event(source_id: str, start: str, end: str, source: str = "google", **kw) -> dict:
    return {
        "id": f"{source}_{source_id}",
        "source": source,
        "source_id": source_id,
        "calendar_id": "primary",
        "title": kw.get("title", "Test Event"),
        "description": kw.get("description", ""),
        "location": kw.get("location", ""),
        "start_time": start,
        "end_time": end,
        "all_day": kw.get("all_day", 0),
        "status": kw.get("status", "confirmed"),
        "raw_json": "{}",
    }


class ToUtcStorage(unittest.TestCase):
    def test_offset_string_converts(self):
        # Google-style: 3 PM ET == 19:00 UTC in July (EDT)
        self.assertEqual(models.to_utc_storage("2026-07-21T15:00:00-04:00"), "2026-07-21T19:00:00")

    def test_z_suffix_converts(self):
        self.assertEqual(models.to_utc_storage("2026-07-21T19:00:00Z"), "2026-07-21T19:00:00")

    def test_naive_assumed_utc(self):
        self.assertEqual(models.to_utc_storage("2026-07-21T19:00:00"), "2026-07-21T19:00:00")

    def test_same_instant_from_both_providers_is_identical(self):
        google = models.to_utc_storage("2026-07-21T15:00:00-04:00")
        outlook = models.to_utc_storage("2026-07-21T19:00:00Z")
        self.assertEqual(google, outlook)

    def test_all_day_bare_date_is_local_midnight(self):
        # Midnight ET on July 21 == 04:00 UTC (EDT)
        self.assertEqual(models.to_utc_storage("2026-07-21", all_day=True), "2026-07-21T04:00:00")

    def test_microseconds_stripped(self):
        self.assertEqual(models.to_utc_storage("2026-07-21T19:00:00.123456Z"), "2026-07-21T19:00:00")

    def test_empty_and_garbage(self):
        self.assertEqual(models.to_utc_storage(""), "")
        self.assertEqual(models.to_utc_storage(None), "")
        self.assertEqual(models.to_utc_storage("not-a-date"), "not-a-date")

    def test_future_event_sorts_after_now_cutoff(self):
        """The original bug: a Google event 1h out, stored with -04:00 offset,
        compared lexicographically below a naive-UTC 'now' — so it vanished
        from !upcoming. Canonical storage must sort correctly."""
        now_utc = datetime(2026, 7, 21, 18, 0, 0)  # 2 PM ET
        event_local = now_utc.astimezone(ET) if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc).astimezone(ET)
        one_hour_out = (event_local + timedelta(hours=1)).isoformat()  # 3 PM ET, offset string
        stored = models.to_utc_storage(one_hour_out)
        cutoff = now_utc.isoformat(timespec="seconds")
        # Raw provider string fails this; canonical form must pass.
        self.assertGreater(stored, cutoff)


class LocalDayRange(unittest.TestCase):
    def test_today_starts_at_local_midnight(self):
        start, end = models.local_day_range()
        start_local = models.to_local(start)
        self.assertEqual((start_local.hour, start_local.minute, start_local.second), (0, 0, 0))
        self.assertEqual(start_local.date(), datetime.now(ET).date())

    def test_span_is_exact_days(self):
        start, end = models.local_day_range(span_days=7)
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        # 7 days, allowing for a DST transition inside the span (+/- 1h)
        self.assertIn(e - s, (timedelta(days=7), timedelta(days=7, hours=1), timedelta(days=6, hours=23)))

    def test_tomorrow_follows_today(self):
        _, today_end = models.local_day_range()
        tom_start, _ = models.local_day_range(offset_days=1)
        self.assertEqual(today_end, tom_start)


class UpsertAndMigration(unittest.TestCase):
    def setUp(self):
        models.init_db()
        self.conn = models.get_db()
        self.conn.execute("DELETE FROM events")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_upsert_change_detection(self):
        e = _event("ev1", "2026-07-21T19:00:00", "2026-07-21T20:00:00")
        self.assertTrue(models.upsert_event(self.conn, e))       # new -> changed
        self.assertFalse(models.upsert_event(self.conn, e))      # identical -> unchanged
        e["title"] = "Renamed"
        self.assertTrue(models.upsert_event(self.conn, e))       # modified -> changed

    def test_updated_at_sorts_against_python_cutoff(self):
        e = _event("ev2", "2026-07-21T19:00:00", "2026-07-21T20:00:00")
        models.upsert_event(self.conn, e)
        cutoff = (datetime.utcnow() - timedelta(minutes=1)).isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT * FROM events WHERE source_id='ev2' AND updated_at >= ?", (cutoff,)
        ).fetchone()
        self.assertIsNotNone(row)

    def test_migration_normalizes_legacy_rows(self):
        # Simulate pre-migration rows: Google offset string + Outlook Z string
        self.conn.execute(
            "INSERT INTO events (id, source, source_id, calendar_id, title, start_time, end_time, all_day, status, synced_at) "
            "VALUES ('google_old', 'google', 'old', 'primary', 'Legacy G', '2026-07-21T15:00:00-04:00', '2026-07-21T16:00:00-04:00', 0, 'confirmed', '2026-07-19T00:00:00')"
        )
        self.conn.execute(
            "INSERT INTO events (id, source, source_id, calendar_id, title, start_time, end_time, all_day, status, synced_at) "
            "VALUES ('outlook_old', 'outlook', 'old2', 'default', 'Legacy O', '2026-07-21T19:00:00Z', '2026-07-21T20:00:00Z', 0, 'confirmed', '2026-07-19T00:00:00')"
        )
        self.conn.execute("PRAGMA user_version = 0")
        self.conn.commit()
        models._migrate_timestamps_v1(self.conn)
        rows = {r["id"]: r for r in self.conn.execute("SELECT * FROM events").fetchall()}
        self.assertEqual(rows["google_old"]["start_time"], "2026-07-21T19:00:00")
        self.assertEqual(rows["outlook_old"]["start_time"], "2026-07-21T19:00:00")
        # Idempotent: second run is a no-op
        models._migrate_timestamps_v1(self.conn)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config


def to_local(iso_str: str | None) -> datetime | None:
    """Parse an ISO 8601 string and return a tz-aware datetime in USER_TIMEZONE.
    Strings with no offset and no Z are treated as UTC — Outlook's sync stores
    times that way (Microsoft Graph drops the timeZone field separately)."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(config.USER_TIMEZONE)


def get_db() -> sqlite3.Connection:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,              -- 'google' or 'outlook'
            source_id TEXT NOT NULL,           -- original event ID from provider
            calendar_id TEXT NOT NULL,         -- which calendar it belongs to
            title TEXT NOT NULL DEFAULT '',
            description TEXT DEFAULT '',
            location TEXT DEFAULT '',
            start_time TEXT NOT NULL,          -- ISO 8601
            end_time TEXT NOT NULL,            -- ISO 8601
            all_day INTEGER DEFAULT 0,
            status TEXT DEFAULT 'confirmed',   -- confirmed, tentative, cancelled
            raw_json TEXT,                     -- full event JSON for debugging
            synced_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source, source_id)
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            calendar_id TEXT NOT NULL,
            sync_token TEXT,                   -- incremental sync token
            channel_id TEXT,                   -- webhook channel ID
            channel_expiry TEXT,               -- webhook expiration
            resource_id TEXT,                  -- Google resource ID (needed to stop channels)
            last_sync TEXT,
            UNIQUE(source, calendar_id)
        );

        CREATE TABLE IF NOT EXISTS analysis_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_ids TEXT,                    -- comma-separated event IDs analyzed
            prompt TEXT,
            response TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time);
        CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, source_id);
    """)
    conn.close()


def upsert_event(conn: sqlite3.Connection, event: dict) -> bool:
    """Upsert an event. Returns True if the event was actually changed (new or modified)."""
    now = datetime.utcnow().isoformat()

    # Check if event exists and has changed
    existing = conn.execute(
        "SELECT title, description, location, start_time, end_time, all_day, status FROM events WHERE source=:source AND source_id=:source_id",
        event
    ).fetchone()

    if existing:
        changed = (
            existing["title"] != event.get("title", "") or
            existing["start_time"] != event.get("start_time", "") or
            existing["end_time"] != event.get("end_time", "") or
            existing["location"] != event.get("location", "") or
            existing["status"] != event.get("status", "confirmed")
        )
        if not changed:
            return False

    conn.execute("""
        INSERT INTO events (id, source, source_id, calendar_id, title, description,
                           location, start_time, end_time, all_day, status, raw_json, synced_at)
        VALUES (:id, :source, :source_id, :calendar_id, :title, :description,
                :location, :start_time, :end_time, :all_day, :status, :raw_json, :synced_at)
        ON CONFLICT(source, source_id) DO UPDATE SET
            title=:title, description=:description, location=:location,
            start_time=:start_time, end_time=:end_time, all_day=:all_day,
            status=:status, raw_json=:raw_json, synced_at=:synced_at,
            updated_at=datetime('now')
    """, {**event, "synced_at": now})
    return True


def get_upcoming_events(conn: sqlite3.Connection, hours: int = 24) -> list[dict]:
    now = datetime.utcnow()
    horizon = (now + timedelta(hours=hours)).isoformat()
    rows = conn.execute("""
        SELECT * FROM events
        WHERE start_time >= ? AND start_time < ? AND status != 'cancelled'
        ORDER BY start_time
        LIMIT 50
    """, (now.isoformat(), horizon)).fetchall()
    return [dict(r) for r in rows]


def get_events_range(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = conn.execute("""
        SELECT * FROM events
        WHERE start_time >= ? AND start_time < ? AND status != 'cancelled'
        ORDER BY start_time
    """, (start, end)).fetchall()
    return [dict(r) for r in rows]


def mark_orphans_cancelled(conn: sqlite3.Connection, source: str, calendar_id: str,
                            time_min: str, time_max: str, observed_ids: set) -> int:
    """Mark confirmed events in the [time_min, time_max) window as cancelled if their
    source_id wasn't in the latest fetch. Used after a tokenless full resync — the source
    only returns currently-existing events, so anything in our DB but absent from the
    response was deleted upstream."""
    candidates = conn.execute("""
        SELECT source_id FROM events
        WHERE source=? AND calendar_id=? AND status='confirmed'
          AND start_time >= ? AND start_time < ?
    """, (source, calendar_id, time_min, time_max)).fetchall()
    reaped = 0
    for r in candidates:
        if r["source_id"] not in observed_ids:
            conn.execute(
                "UPDATE events SET status='cancelled', updated_at=datetime('now') "
                "WHERE source=? AND source_id=?",
                (source, r["source_id"])
            )
            reaped += 1
    return reaped

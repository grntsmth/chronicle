import json
import uuid
import logging
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import config
from models import get_db, upsert_event, mark_orphans_cancelled, to_utc_storage

log = logging.getLogger("chronicle.google")


def get_oauth_flow() -> Flow:
    flow = Flow.from_client_secrets_file(
        config.GOOGLE_CREDS_FILE,
        scopes=config.GOOGLE_SCOPES,
        redirect_uri=f"{config.WEBHOOK_BASE_URL}/oauth/callback",
    )
    return flow


def get_credentials() -> Credentials | None:
    try:
        with open(config.GOOGLE_TOKEN_FILE) as f:
            token_data = json.load(f)
        creds = Credentials.from_authorized_user_info(token_data, config.GOOGLE_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_credentials(creds)
        return creds
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        log.warning(f"No valid Google credentials: {e}")
        return None


def save_credentials(creds: Credentials):
    from pathlib import Path
    Path(config.GOOGLE_TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(config.GOOGLE_TOKEN_FILE, "w") as f:
        json.dump(json.loads(creds.to_json()), f)


def get_service():
    creds = get_credentials()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def parse_event(event: dict, calendar_id: str) -> dict:
    start = event.get("start", {})
    end = event.get("end", {})

    if "dateTime" in start:
        start_time = start["dateTime"]
        end_time = end.get("dateTime", start_time)
        all_day = False
    else:
        start_time = start.get("date", "")
        end_time = end.get("date", start_time)
        all_day = True

    return {
        "id": f"google_{event['id']}",
        "source": "google",
        "source_id": event["id"],
        "calendar_id": calendar_id,
        "title": event.get("summary", "(No title)"),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "start_time": to_utc_storage(start_time, all_day),
        "end_time": to_utc_storage(end_time, all_day),
        "all_day": 1 if all_day else 0,
        "status": event.get("status", "confirmed"),
        "raw_json": json.dumps(event),
    }


def sync_calendar(calendar_id: str = "primary") -> int:
    service = get_service()
    if not service:
        log.error("Google Calendar not authenticated")
        return 0

    conn = get_db()

    # Check for existing sync token
    row = conn.execute(
        "SELECT sync_token FROM sync_state WHERE source='google' AND calendar_id=?",
        (calendar_id,)
    ).fetchone()

    sync_token = row["sync_token"] if row else None
    tokenless = sync_token is None
    observed_ids: set[str] = set()
    time_min = time_max = None
    count = 0

    try:
        kwargs = {
            "calendarId": calendar_id,
            "singleEvents": True,
            "maxResults": 250,
        }

        if sync_token:
            kwargs["syncToken"] = sync_token
        else:
            # First sync: get events from 30 days ago to 90 days ahead
            time_min = (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"
            time_max = (datetime.utcnow() + timedelta(days=90)).isoformat() + "Z"
            kwargs["timeMin"] = time_min
            kwargs["timeMax"] = time_max

        while True:
            result = service.events().list(**kwargs).execute()

            for event in result.get("items", []):
                if event.get("status") == "cancelled":
                    conn.execute(
                        "UPDATE events SET status='cancelled' WHERE source='google' AND source_id=?",
                        (event["id"],)
                    )
                else:
                    parsed = parse_event(event, calendar_id)
                    upsert_event(conn, parsed)
                    if tokenless:
                        observed_ids.add(event["id"])
                count += 1

            page_token = result.get("nextPageToken")
            if page_token:
                kwargs["pageToken"] = page_token
                kwargs.pop("syncToken", None)
            else:
                break

        if tokenless:
            reaped = mark_orphans_cancelled(conn, "google", calendar_id, time_min, time_max, observed_ids)
            if reaped:
                log.warning(f"Google: reaped {reaped} orphan events absent from full resync")

        # Save new sync token
        new_sync_token = result.get("nextSyncToken", "")
        conn.execute("""
            INSERT INTO sync_state (source, calendar_id, sync_token, last_sync)
            VALUES ('google', ?, ?, datetime('now'))
            ON CONFLICT(source, calendar_id) DO UPDATE SET
                sync_token=?, last_sync=datetime('now')
        """, (calendar_id, new_sync_token, new_sync_token))
        conn.commit()

    except Exception as e:
        if "Sync token" in str(e) and "invalid" in str(e):
            log.warning("Sync token expired, doing full sync")
            conn.execute("DELETE FROM sync_state WHERE source='google' AND calendar_id=?", (calendar_id,))
            conn.commit()
            conn.close()
            return sync_calendar(calendar_id)
        log.error(f"Google sync error: {e}")
        conn.rollback()
    finally:
        conn.close()

    log.info(f"Google sync complete: {count} events processed")
    return count


def create_event(summary: str, start: str, end: str, description: str = "", location: str = "",
                 calendar_id: str = "primary") -> dict | None:
    service = get_service()
    if not service:
        return None

    body = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": str(config.USER_TIMEZONE)},
        "end": {"dateTime": end, "timeZone": str(config.USER_TIMEZONE)},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    log.info(f"Created Google event: {event['id']} - {summary}")

    # Sync it into our DB
    conn = get_db()
    upsert_event(conn, parse_event(event, calendar_id))
    conn.commit()
    conn.close()

    return event


def delete_event(source_id: str, calendar_id: str = "primary") -> bool:
    service = get_service()
    if not service:
        return False
    try:
        service.events().delete(calendarId=calendar_id, eventId=source_id).execute()
        conn = get_db()
        conn.execute("UPDATE events SET status='cancelled' WHERE source='google' AND source_id=?", (source_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error(f"Failed to delete event {source_id}: {e}")
        return False


def stop_webhook(channel_id: str, resource_id: str) -> bool:
    """Stop an existing Google push notification channel."""
    service = get_service()
    if not service:
        return False
    try:
        service.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()
        log.info(f"Stopped Google webhook channel: {channel_id}")
        return True
    except Exception as e:
        log.warning(f"Failed to stop Google webhook channel {channel_id}: {e}")
        return False


def setup_webhook(calendar_id: str = "primary") -> dict | None:
    """Register a push notification channel for real-time updates.
    Stops the previous channel first to avoid duplicate notifications."""
    service = get_service()
    if not service:
        return None

    # Stop existing channel before creating a new one
    conn = get_db()
    row = conn.execute(
        "SELECT channel_id, resource_id FROM sync_state WHERE source='google' AND calendar_id=?",
        (calendar_id,)
    ).fetchone()
    if row and row["channel_id"] and row["resource_id"]:
        stop_webhook(row["channel_id"], row["resource_id"])
    conn.close()

    channel_id = str(uuid.uuid4())
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": f"{config.WEBHOOK_BASE_URL}/webhook/google",
        "params": {"ttl": "604800"},  # 7 days
    }

    try:
        result = service.events().watch(calendarId=calendar_id, body=body).execute()
        expiry = datetime.utcfromtimestamp(int(result["expiration"]) / 1000).isoformat()
        resource_id = result.get("resourceId", "")

        conn = get_db()
        conn.execute("""
            INSERT INTO sync_state (source, calendar_id, channel_id, channel_expiry, resource_id, last_sync)
            VALUES ('google', ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(source, calendar_id) DO UPDATE SET
                channel_id=?, channel_expiry=?, resource_id=?
        """, (calendar_id, channel_id, expiry, resource_id, channel_id, expiry, resource_id))
        conn.commit()
        conn.close()

        log.info(f"Google webhook registered: channel={channel_id}, expires={expiry}")
        return result
    except Exception as e:
        log.error(f"Failed to set up Google webhook: {e}")
        return None

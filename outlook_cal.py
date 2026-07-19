import json
import logging
from datetime import datetime, timedelta

import msal
import httpx

import config
from models import get_db, upsert_event, mark_orphans_cancelled, to_utc_storage

log = logging.getLogger("chronicle.outlook")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def get_msal_app() -> msal.ConfidentialClientApplication | None:
    if not config.AZURE_CLIENT_ID:
        return None
    return msal.ConfidentialClientApplication(
        config.AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{config.AZURE_TENANT_ID}",
        client_credential=config.AZURE_CLIENT_SECRET,
    )


def get_auth_url(state: str | None = None) -> str | None:
    app = get_msal_app()
    if not app:
        return None
    return app.get_authorization_request_url(
        scopes=["Calendars.ReadWrite"],
        redirect_uri=config.AZURE_REDIRECT_URI,
        state=state,
    )


def exchange_code(code: str) -> dict | None:
    app = get_msal_app()
    if not app:
        return None
    result = app.acquire_token_by_authorization_code(
        code,
        scopes=["Calendars.ReadWrite"],
        redirect_uri=config.AZURE_REDIRECT_URI,
    )
    if "access_token" in result:
        save_token(result)
        return result
    log.error(f"Outlook token exchange failed: {result.get('error_description', result)}")
    return None


def save_token(token_data: dict):
    from pathlib import Path
    Path(config.OUTLOOK_TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(config.OUTLOOK_TOKEN_FILE, "w") as f:
        json.dump(token_data, f)


def load_token() -> dict | None:
    try:
        with open(config.OUTLOOK_TOKEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_access_token() -> str | None:
    token = load_token()
    if not token:
        return None

    app = get_msal_app()
    if not app:
        return None

    # Try to use refresh token
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(["Calendars.ReadWrite"], account=accounts[0])
        if result and "access_token" in result:
            save_token(result)
            return result["access_token"]

    # Try refresh token directly
    if "refresh_token" in token:
        result = app.acquire_token_by_refresh_token(
            token["refresh_token"], scopes=["Calendars.ReadWrite"]
        )
        if result and "access_token" in result:
            save_token(result)
            return result["access_token"]
        if result and "error" in result:
            log.error(f"Outlook refresh failed: {result.get('error')} - {result.get('error_description', '')[:200]}")

    return token.get("access_token")


def _iso_with_tz(dt_obj: dict) -> str:
    """Microsoft Graph returns {dateTime, timeZone} pairs. The dateTime field
    carries no tz suffix; if we store it as-is, downstream parsers treat it as
    naive local time and the clock-time displays in UTC. Tag UTC responses with
    Z so to_local() can correctly convert them."""
    dt = dt_obj.get("dateTime", "")
    if dt and dt_obj.get("timeZone") == "UTC" and not dt.endswith("Z"):
        return dt + "Z"
    return dt


def parse_event(event: dict) -> dict:
    start = event.get("start", {})
    end = event.get("end", {})
    all_day = event.get("isAllDay", False)

    return {
        "id": f"outlook_{event['id']}",
        "source": "outlook",
        "source_id": event["id"],
        "calendar_id": "default",
        "title": event.get("subject", "(No title)"),
        "description": event.get("bodyPreview", ""),
        "location": event.get("location", {}).get("displayName", ""),
        "start_time": to_utc_storage(_iso_with_tz(start), all_day),
        "end_time": to_utc_storage(_iso_with_tz(end), all_day),
        "all_day": 1 if all_day else 0,
        "status": "cancelled" if event.get("isCancelled") else "confirmed",
        "raw_json": json.dumps(event),
    }


def sync_calendar() -> int:
    access_token = get_access_token()
    if not access_token:
        log.warning("Outlook not authenticated")
        return 0

    conn = get_db()
    count = 0
    observed_ids: set[str] = set()
    now = datetime.utcnow()
    time_min = (now - timedelta(days=30)).isoformat() + "Z"
    time_max = (now + timedelta(days=90)).isoformat() + "Z"

    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": 'outlook.timezone="UTC"',
        }
        params = {
            "$select": "id,subject,bodyPreview,start,end,location,isAllDay,isCancelled",
            "$orderby": "start/dateTime",
            "$top": 250,
            "$filter": f"start/dateTime ge '{time_min}' and start/dateTime le '{time_max}'",
        }

        url = f"{GRAPH_BASE}/me/calendar/events"
        while url:
            resp = httpx.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

            for event in data.get("value", []):
                parsed = parse_event(event)
                upsert_event(conn, parsed)
                if not event.get("isCancelled"):
                    observed_ids.add(event["id"])
                count += 1

            url = data.get("@odata.nextLink")
            params = {}  # nextLink includes params

        reaped = mark_orphans_cancelled(conn, "outlook", "default", time_min, time_max, observed_ids)
        if reaped:
            log.warning(f"Outlook: reaped {reaped} orphan events absent from sync window")

        conn.execute("""
            INSERT INTO sync_state (source, calendar_id, last_sync)
            VALUES ('outlook', 'default', datetime('now'))
            ON CONFLICT(source, calendar_id) DO UPDATE SET last_sync=datetime('now')
        """)
        conn.commit()

    except Exception as e:
        log.error(f"Outlook sync error: {e}")
        conn.rollback()
    finally:
        conn.close()

    log.info(f"Outlook sync complete: {count} events processed")
    return count


def create_event(subject: str, start: str, end: str, description: str = "",
                 location: str = "") -> dict | None:
    access_token = get_access_token()
    if not access_token:
        return None

    body = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": str(config.USER_TIMEZONE)},
        "end": {"dateTime": end, "timeZone": str(config.USER_TIMEZONE)},
    }
    if description:
        body["body"] = {"contentType": "Text", "content": description}
    if location:
        body["location"] = {"displayName": location}

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    resp = httpx.post(f"{GRAPH_BASE}/me/calendar/events", headers=headers, json=body)

    if resp.status_code == 201:
        event = resp.json()
        conn = get_db()
        upsert_event(conn, parse_event(event))
        conn.commit()
        conn.close()
        log.info(f"Created Outlook event: {event['id'][:32]} - {subject}")
        return event

    log.error(f"Failed to create Outlook event: {resp.status_code} {resp.text}")
    return None


def list_subscriptions(access_token: str) -> list[dict]:
    """List all Graph subscriptions for this account."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.get(f"{GRAPH_BASE}/subscriptions", headers=headers)
    if resp.status_code == 200:
        return resp.json().get("value", [])
    log.warning(f"Failed to list Outlook subscriptions: {resp.status_code} {resp.text}")
    return []


def delete_subscription(access_token: str, sub_id: str) -> bool:
    """Delete a Graph subscription by ID."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.delete(f"{GRAPH_BASE}/subscriptions/{sub_id}", headers=headers)
    if resp.status_code in (204, 404):
        return True
    log.warning(f"Failed to delete subscription {sub_id}: {resp.status_code} {resp.text}")
    return False


def setup_webhook() -> dict | None:
    """Register a Graph subscription, deleting any prior ones pointing at our URL.
    Graph does not enforce uniqueness — renewing without deleting stacks duplicates."""
    access_token = get_access_token()
    if not access_token:
        return None

    our_url = f"{config.WEBHOOK_BASE_URL}/webhook/outlook"

    purged = 0
    for sub in list_subscriptions(access_token):
        if sub.get("notificationUrl") == our_url:
            if delete_subscription(access_token, sub["id"]):
                purged += 1
    if purged:
        log.info(f"Outlook webhook: purged {purged} existing subscription(s)")

    expiry = (datetime.utcnow() + timedelta(days=2)).isoformat() + "Z"
    body = {
        "changeType": "created,updated,deleted",
        "notificationUrl": our_url,
        "resource": "me/events",
        "expirationDateTime": expiry,
        "clientState": config.OUTLOOK_CLIENT_STATE,
    }

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    resp = httpx.post(f"{GRAPH_BASE}/subscriptions", headers=headers, json=body)

    if resp.status_code == 201:
        sub = resp.json()
        conn = get_db()
        conn.execute("""
            INSERT INTO sync_state (source, calendar_id, channel_id, channel_expiry, last_sync)
            VALUES ('outlook', 'default', ?, ?, datetime('now'))
            ON CONFLICT(source, calendar_id) DO UPDATE SET
                channel_id=excluded.channel_id, channel_expiry=excluded.channel_expiry
        """, (sub["id"], expiry))
        conn.commit()
        conn.close()
        log.info(f"Outlook webhook registered: {sub['id']}, expires {expiry}")
        return sub
    else:
        log.error(f"Outlook webhook failed: {resp.status_code} {resp.text}")
        return None

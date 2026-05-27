import logging
from datetime import datetime, timedelta

import httpx

import config
from models import get_db, get_upcoming_events, get_events_range, to_local

log = logging.getLogger("chronicle.discord")

AMBER = 0xFFBF00
GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB


def send_embed(title: str, description: str, color: int = AMBER, fields: list = None):
    if not config.DISCORD_WEBHOOK_URL:
        log.warning("No Discord webhook URL configured")
        return

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.utcnow().isoformat(),
        "footer": {"text": "The Chronicle"},
    }
    if fields:
        embed["fields"] = fields

    payload = {"embeds": [embed]}

    try:
        resp = httpx.post(config.DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            log.error(f"Discord webhook failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Discord send error: {e}")


def send_message(content: str):
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        httpx.post(config.DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
    except Exception as e:
        log.error(f"Discord send error: {e}")


def notify_event_change(event: dict, change_type: str):
    """Notify about an event creation, update, or cancellation."""
    colors = {"created": GREEN, "updated": BLUE, "cancelled": RED}
    titles = {
        "created": "New Event Added",
        "updated": "Event Updated",
        "cancelled": "Event Cancelled",
    }

    dt = to_local(event.get("start_time"))
    time_str = dt.strftime("%a %b %d, %I:%M %p %Z") if dt else event.get("start_time", "")

    fields = [
        {"name": "When", "value": time_str, "inline": True},
        {"name": "Source", "value": event.get("source", "unknown").title(), "inline": True},
    ]
    if event.get("location"):
        fields.append({"name": "Where", "value": event["location"], "inline": True})

    send_embed(
        title=f"{titles.get(change_type, 'Event Changed')}: {event.get('title', '?')}",
        description=event.get("description", "")[:200] or "_No description_",
        color=colors.get(change_type, AMBER),
        fields=fields,
    )


def send_daily_briefing():
    """Send a morning briefing of today's events."""
    conn = get_db()
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0).isoformat()
    today_end = (now.replace(hour=0, minute=0, second=0) + timedelta(days=1)).isoformat()
    events = get_events_range(conn, today_start, today_end)
    conn.close()

    if not events:
        send_embed(
            title="Daily Briefing — No Events",
            description="Your day is clear. Use it wisely.",
            color=GREEN,
        )
        return

    lines = []
    for e in events:
        dt = to_local(e["start_time"])
        if dt:
            time_str = dt.strftime("%I:%M %p")
        else:
            time_str = "All day" if e.get("all_day") else "?"
        source_icon = "🔵" if e["source"] == "google" else "🟠"
        lines.append(f"{source_icon} **{time_str}** — {e['title']}")

    send_embed(
        title=f"Daily Briefing — {len(events)} Events",
        description="\n".join(lines),
        color=AMBER,
        fields=[{"name": "Date", "value": now.strftime("%A, %B %d %Y"), "inline": False}],
    )


def send_llm_analysis(analysis: str, context: str = "Schedule Analysis"):
    """Send an LLM-generated analysis to Discord."""
    # Truncate if needed (Discord embed description limit is 4096)
    if len(analysis) > 3900:
        analysis = analysis[:3900] + "\n\n_...truncated_"

    send_embed(
        title=f"Oracle Analysis — {context}",
        description=analysis,
        color=AMBER,
    )

"""Tool-using calendar agent.

Claude drives an agentic loop via the SDK tool runner: it can read the local
event DB, create and delete events through the provider APIs, and search for
free time. Replaces the old keyword-routing for voice messages and powers the
!ask command. Falls back to a plain schedule-context answer via Ollama when no
Anthropic API key is configured.
"""
import logging
from datetime import datetime, timedelta, time as dtime

from anthropic import beta_tool

import config
import metrics
import models
import google_cal
import outlook_cal
import llm

log = logging.getLogger("chronicle.agent")

# IDs of events created during the current run_agent() call, so callers can
# suppress the duplicate webhook notification (same mechanism as !add).
_created_ids: list[str] = []


def _local_range(start_date: str, end_date: str) -> tuple[str, str]:
    """Local YYYY-MM-DD dates -> canonical UTC storage strings."""
    s = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=config.USER_TIMEZONE)
    e = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=config.USER_TIMEZONE)
    return models.to_utc_storage(s.isoformat()), models.to_utc_storage(e.isoformat())


def _fmt_event(e: dict) -> str:
    if e.get("all_day"):
        start_dt = models.to_local(e["start_time"])
        when = f"{start_dt.strftime('%a %b %d')} (all day)" if start_dt else "?"
    else:
        start_dt = models.to_local(e["start_time"])
        end_dt = models.to_local(e.get("end_time"))
        if start_dt and end_dt:
            when = f"{start_dt.strftime('%a %b %d %I:%M %p')}–{end_dt.strftime('%I:%M %p')}"
        else:
            when = start_dt.strftime("%a %b %d %I:%M %p") if start_dt else "?"
    loc = f" @ {e['location']}" if e.get("location") else ""
    return f"- {when}: {e['title']} [{e['source']}]{loc}"


@beta_tool
def list_events(start_date: str, end_date: str) -> str:
    """List the user's calendar events between two local dates. Always call
    this before answering questions about the schedule.

    Args:
        start_date: First local date to include, YYYY-MM-DD.
        end_date: End local date, YYYY-MM-DD (exclusive — use the day after
            the last day you want).
    """
    try:
        start, end = _local_range(start_date, end_date)
    except ValueError as e:
        return f"Error: bad date format ({e}). Use YYYY-MM-DD."
    conn = models.get_db()
    events = models.get_events_range(conn, start, end)
    conn.close()
    if not events:
        return f"No events between {start_date} and {end_date}."
    lines = [_fmt_event(e) for e in events[:60]]
    if len(events) > 60:
        lines.append(f"...and {len(events) - 60} more")
    return "\n".join(lines)


@beta_tool
def create_calendar_event(summary: str, start: str, end: str, description: str = "",
                          location: str = "", calendar: str = "google") -> str:
    """Create a calendar event.

    Args:
        summary: Event title.
        start: ISO 8601 start datetime WITH UTC offset, e.g. 2026-07-21T15:00:00-04:00.
        end: ISO 8601 end datetime with offset.
        description: Optional description.
        location: Optional location.
        calendar: "google" (personal, default) or "outlook" (work).
    """
    try:
        if calendar == "outlook":
            event = outlook_cal.create_event(summary, start, end, description, location)
        else:
            event = google_cal.create_event(summary, start, end, description, location)
    except Exception as e:
        return f"Error creating event: {e}"
    if not event:
        return f"Failed to create the event on {calendar} (not authenticated?)."
    event_id = event.get("id", "")
    if event_id:
        _created_ids.append(event_id)
    when = models.to_local(models.to_utc_storage(start))
    when_str = when.strftime("%a %b %d, %I:%M %p") if when else start
    return f"Created '{summary}' on {calendar} at {when_str}."


@beta_tool
def delete_event(title_match: str, date: str) -> str:
    """Delete/cancel a calendar event. Only deletes when exactly one event on
    that date matches; otherwise returns the candidates so the user can be
    asked to disambiguate.

    Args:
        title_match: Case-insensitive substring of the event title.
        date: Local date the event starts on, YYYY-MM-DD.
    """
    try:
        start, end = _local_range(date, (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))
    except ValueError as e:
        return f"Error: bad date format ({e}). Use YYYY-MM-DD."
    conn = models.get_db()
    events = models.get_events_range(conn, start, end)
    conn.close()
    matches = [e for e in events if title_match.lower() in (e.get("title") or "").lower()]
    if not matches:
        return f"No event on {date} with a title containing '{title_match}'."
    if len(matches) > 1:
        return "Multiple events match — ask the user which one:\n" + "\n".join(_fmt_event(e) for e in matches)
    target = matches[0]
    if target["source"] == "outlook":
        ok = outlook_cal.delete_event(target["source_id"])
    else:
        ok = google_cal.delete_event(target["source_id"])
    if ok:
        return f"Deleted '{target['title']}' ({target['source']}) on {date}."
    return f"Provider refused to delete '{target['title']}' — check the logs."


@beta_tool
def find_free_time(start_date: str, end_date: str, duration_minutes: int = 60) -> str:
    """Find free time slots between calendar events, within waking hours
    (8 AM – 10 PM local).

    Args:
        start_date: First local date to consider, YYYY-MM-DD.
        end_date: End local date, YYYY-MM-DD (exclusive).
        duration_minutes: Minimum slot length in minutes.
    """
    try:
        start, end = _local_range(start_date, end_date)
        first_day = datetime.strptime(start_date, "%Y-%m-%d").date()
        last_day = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError as e:
        return f"Error: bad date format ({e}). Use YYYY-MM-DD."
    conn = models.get_db()
    events = models.get_events_range(conn, start, end)
    conn.close()

    busy = []
    for e in events:
        s, en = models.to_local(e["start_time"]), models.to_local(e.get("end_time"))
        if s and en:
            busy.append((s, en))
    busy.sort()

    slots = []
    day = first_day
    while day < last_day:
        window_start = datetime.combine(day, dtime(8, 0), tzinfo=config.USER_TIMEZONE)
        window_end = datetime.combine(day, dtime(22, 0), tzinfo=config.USER_TIMEZONE)
        cursor = window_start
        for s, en in busy:
            if en <= window_start or s >= window_end:
                continue
            if s > cursor:
                gap = (min(s, window_end) - cursor).total_seconds() / 60
                if gap >= duration_minutes:
                    slots.append((cursor, min(s, window_end)))
            cursor = max(cursor, en)
        if window_end > cursor:
            gap = (window_end - cursor).total_seconds() / 60
            if gap >= duration_minutes:
                slots.append((cursor, window_end))
        day += timedelta(days=1)

    if not slots:
        return f"No free slots of {duration_minutes}+ minutes between {start_date} and {end_date} (8 AM–10 PM)."
    lines = [f"- {s.strftime('%a %b %d %I:%M %p')} – {e.strftime('%I:%M %p')}" for s, e in slots[:25]]
    return f"Free slots of {duration_minutes}+ minutes:\n" + "\n".join(lines)


def _agent_system() -> str:
    now_local = datetime.now(config.USER_TIMEZONE)
    tz = str(config.USER_TIMEZONE)
    return llm.SYSTEM_PROMPT + f"""

AGENT MODE
You can call tools that read and modify the user's real calendars.
- Always call list_events before answering questions about the schedule — never answer from memory.
- Google is the personal calendar; Outlook is the work calendar. Create on Google unless the user says work/Outlook.
- create_calendar_event datetimes must be ISO 8601 with the correct UTC offset for {tz}.
- delete_event only removes an exact single match; if several match, relay the candidates and ask the user to be more specific. Never guess which event to delete.
- Keep replies short and concrete — this is a Discord chat, not a report.
Current local time: {now_local.strftime('%A, %B %d %Y %I:%M %p')} ({tz})."""


def run_agent(user_text: str) -> dict:
    """Run one agentic turn. Synchronous — call via run_in_executor.

    Returns {"reply": str, "created_ids": list[str]} where created_ids are
    provider event IDs created during the turn (for webhook-notification
    suppression)."""
    client = llm.get_claude()
    if client is None:
        # Ollama fallback: no tool use, just answer with schedule context.
        analysis = llm.analyze_schedule(168)
        prompt = f"""The user said: "{user_text}"

Here is their current schedule context:
{analysis}

Respond to their question or request conversationally and helpfully."""
        reply = llm.query_llm(prompt, llm.SYSTEM_PROMPT, 1024)
        metrics.AGENT_RUNS.labels("fallback").inc()
        return {"reply": reply or "The Oracle is unavailable. Is Ollama running?", "created_ids": []}

    _created_ids.clear()
    try:
        runner = client.beta.messages.tool_runner(
            model=config.CLAUDE_MODEL,
            max_tokens=2048,
            system=_agent_system(),
            tools=[list_events, create_calendar_event, delete_event, find_free_time],
            messages=[{"role": "user", "content": user_text}],
        )
        final = None
        for message in runner:
            final = message
        reply = ""
        if final:
            reply = "".join(b.text for b in final.content if b.type == "text").strip()
        metrics.AGENT_RUNS.labels("success").inc()
        return {"reply": reply or "Done (no further comment).", "created_ids": list(_created_ids)}
    except Exception as e:
        log.error(f"Agent run failed: {e}")
        metrics.AGENT_RUNS.labels("error").inc()
        return {"reply": f"Something went wrong talking to the assistant: {e}", "created_ids": list(_created_ids)}

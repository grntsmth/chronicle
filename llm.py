import json
import re
import logging
from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel

import config
from models import (get_db, get_upcoming_events, get_events_range, to_local,
                    to_utc_storage, find_conflicts, describe_conflicts)

log = logging.getLogger("chronicle.llm")

# --- Claude API (primary) ---

_claude_client = None


def get_claude():
    global _claude_client
    if _claude_client is None and config.ANTHROPIC_API_KEY:
        import anthropic
        _claude_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _claude_client


def query_claude(prompt: str, system: str = "", max_tokens: int = 2048) -> str | None:
    client = get_claude()
    if not client:
        return None
    try:
        msg = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude API failed: {e}")
        return None


# --- Ollama (fallback) ---

def query_ollama(prompt: str, system: str = "") -> str | None:
    body = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 1024},
    }
    if system:
        body["system"] = system

    try:
        resp = httpx.post(
            f"{config.OLLAMA_URL}/api/generate",
            json=body,
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json().get("response", "").strip()
        # Strip qwen3 think tags
        result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
        return result
    except Exception as e:
        log.error(f"Ollama query failed: {e}")
        return None


# --- Unified query: Claude first, Ollama fallback ---

def query_llm(prompt: str, system: str = "", max_tokens: int = 2048) -> str | None:
    result = query_claude(prompt, system, max_tokens)
    if result:
        log.info(f"LLM response via Claude ({config.CLAUDE_MODEL})")
        return result

    log.info("Claude unavailable, falling back to Ollama")
    return query_ollama(prompt, system)


SYSTEM_PROMPT = f"""You are The Chronicle, a personal calendar assistant. You analyze schedules and provide actionable insights.

USER CONTEXT
{config.USER_CONTEXT}

TITLE KEYWORDS — when an event title contains one of these words, classify the event accordingly. Titles without a matching keyword are interpreted normally from available context.

| Keyword | Meaning |
|---|---|
| Shift | A work shift. The user is unavailable for personal scheduling during the start–end window. |
| Meeting | A scheduled meeting (work or personal). |
| Appointment | An appointment with a service provider — medical, dental, automotive, etc. Plan commute + buffer time. |
| Date | DEFCON 5 emergency situation: the user has somehow secured a date. Treat with appropriate gravity and unwavering enthusiasm. |
| Bill | An upcoming cost / payment due. Not a time commitment but a financial obligation to flag. |

GUIDELINES
- Be concise and direct. Use bullet points.
- Focus on conflicts and overlaps, productive gaps, preparation needed, and scheduling suggestions.
- Never be generic. Reference specific events by name and time."""


def analyze_schedule(hours: int = 24) -> str | None:
    conn = get_db()
    events = get_upcoming_events(conn, hours)
    conn.close()

    if not events:
        return f"No upcoming events in the next {hours} hours."

    event_lines = []
    for e in events:
        start_dt = to_local(e["start_time"])
        end_dt = to_local(e.get("end_time"))
        if start_dt and end_dt and start_dt.date() == end_dt.date():
            time_str = f'{start_dt.strftime("%a %b %d %I:%M %p")}–{end_dt.strftime("%I:%M %p %Z")}'
        elif start_dt and end_dt:
            time_str = f'{start_dt.strftime("%a %b %d %I:%M %p")} → {end_dt.strftime("%a %b %d %I:%M %p %Z")}'
        elif start_dt:
            time_str = start_dt.strftime("%a %b %d %I:%M %p %Z")
        else:
            time_str = e["start_time"]
        desc = (e.get("description") or "").replace("\n", " ").strip()
        desc_part = f" — {desc[:200]}" if desc else ""
        event_lines.append(f"- {time_str}: {e['title']} ({e['source']}){' @ ' + e['location'] if e['location'] else ''}{desc_part}")

    # Conflicts are computed deterministically and handed to the model as
    # facts — LLMs doing timestamp arithmetic both miss real overlaps and
    # invent phantom ones.
    conflict_lines = describe_conflicts(find_conflicts(events))
    conflict_text = "\n".join(f"- {l}" for l in conflict_lines) if conflict_lines else "None detected."

    now_local = datetime.now(config.USER_TIMEZONE)
    prompt = f"""Here are the upcoming events for the next {hours} hours:

{chr(10).join(event_lines)}

Scheduling conflicts (computed from the timestamps — treat as authoritative, do not re-derive or add others):
{conflict_text}

Current time: {now_local.strftime('%A, %B %d %Y %I:%M %p %Z')}

Analyze this schedule. Advise how to handle any listed conflicts, suggest optimizations, and note any preparation needed."""

    return query_llm(prompt, SYSTEM_PROMPT)


def analyze_change(event: dict, change_type: str, all_events: list[dict], window_hours: int = 24) -> str | None:
    """Analyze the impact of a calendar change in context of other events within
    ±window_hours of the change event's start time."""
    change_dt = to_local(event.get("start_time"))
    if change_dt is None:
        return None

    window = timedelta(hours=window_hours)
    nearby = []
    for e in all_events:
        if e["id"] == event["id"]:
            continue
        e_dt = to_local(e.get("start_time"))
        if e_dt is None:
            continue
        if abs(e_dt - change_dt) <= window:
            nearby.append((e_dt, e))

    if not nearby:
        return None

    nearby.sort(key=lambda x: x[0])
    other_lines = []
    for e_dt, e in nearby[:15]:
        time_str = e_dt.strftime("%a %b %d %Y, %I:%M %p %Z")
        desc = (e.get("description") or "").replace("\n", " ").strip()
        desc_part = f" — {desc[:200]}" if desc else ""
        other_lines.append(f"- {time_str}: {e['title']}{desc_part}")

    # Deterministic check: does the changed event overlap/crowd anything?
    pair_conflicts = [
        c for c in find_conflicts([event] + [e for _, e in nearby])
        if c["first"].get("id") == event.get("id") or c["second"].get("id") == event.get("id")
    ]
    if pair_conflicts:
        conflict_text = "\n".join(f"- {l}" for l in describe_conflicts(pair_conflicts))
    else:
        conflict_text = "None — the change does not overlap or crowd any nearby event."

    change_time_str = change_dt.strftime("%a %b %d %Y, %I:%M %p %Z")
    end_dt = to_local(event.get("end_time"))
    end_str = end_dt.strftime("%I:%M %p %Z") if end_dt else event.get("end_time", "?")
    prompt = f"""A calendar event was {change_type}:
- Title: {event.get('title', '?')}
- Time: {change_time_str} (ends {end_str})
- Location: {event.get('location', 'none')}

Other events within ±{window_hours}h of that time:
{chr(10).join(other_lines)}

Conflicts involving the changed event (computed from the timestamps — treat as authoritative, do not re-derive or add others):
{conflict_text}

If conflicts are listed, explain the impact and suggest how to resolve them. If none, say all clear in one short sentence."""

    return query_llm(prompt, SYSTEM_PROMPT)


def analyze_period(period: str = "week") -> str | None:
    """Analyze a week, month, or quarter of events. Periods are bounded in
    USER_TIMEZONE — a Sunday-evening weekly review should cover the local
    week, not the UTC one."""
    conn = get_db()
    now = datetime.now(config.USER_TIMEZONE)

    if period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        label = "This Week"
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        label = now.strftime("%B %Y")
    elif period == "quarter":
        q_month = ((now.month - 1) // 3) * 3 + 1
        start = now.replace(month=q_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        end_month = q_month + 3
        if end_month > 12:
            end = start.replace(year=start.year + 1, month=end_month - 12)
        else:
            end = start.replace(month=end_month)
        q_num = (now.month - 1) // 3 + 1
        label = f"Q{q_num} {now.year}"
    else:
        return None

    events = get_events_range(conn, to_utc_storage(start.isoformat()), to_utc_storage(end.isoformat()))
    conn.close()

    if not events:
        return f"No events found for {label}."

    # Group by day
    days = {}
    for e in events:
        dt = to_local(e["start_time"])
        day_key = dt.strftime("%a %b %d") if dt else "Unknown"
        days.setdefault(day_key, []).append(e)

    lines = []
    for day, day_events in days.items():
        lines.append(f"\n{day}:")
        for e in day_events:
            start_dt = to_local(e["start_time"])
            end_dt = to_local(e.get("end_time"))
            if start_dt and end_dt:
                time_str = f'{start_dt.strftime("%I:%M %p")}–{end_dt.strftime("%I:%M %p")}'
            elif start_dt:
                time_str = start_dt.strftime("%I:%M %p")
            else:
                time_str = "?"
            desc = (e.get("description") or "").replace("\n", " ").strip()
            desc_part = f" — {desc[:200]}" if desc else ""
            lines.append(f"  - {time_str}: {e['title']} ({e['source']}){desc_part}")

    event_text = chr(10).join(lines)
    if len(event_text) > 6000:
        event_text = event_text[:6000] + "\n  ...truncated"

    prompt = f"""Review of {label} — {len(events)} events across {len(days)} days:

{event_text}

Provide a {period}ly review:
- How busy was this period? Rate the load (light/moderate/heavy)
- What categories of activity dominate? (work, study, personal, etc.)
- Are there patterns? (e.g., consistent morning study, work clusters)
- Are there days with no events that could be used better?
- Any recurring conflicts or scheduling issues?
- Suggestions for the next {period}"""

    return query_llm(prompt, SYSTEM_PROMPT, max_tokens=4096)


# --- Natural-language event parsing ---

class ParsedEvent(BaseModel):
    summary: str
    start: str  # ISO 8601 with UTC offset
    end: str
    description: str = ""
    location: str = ""


class ParsedEventList(BaseModel):
    events: list[ParsedEvent]


def _parse_prompt(text: str) -> str:
    tz_name = str(config.USER_TIMEZONE)
    now_local = datetime.now(config.USER_TIMEZONE)
    offset_example = now_local.strftime("%z")
    offset_example = f"{offset_example[:3]}:{offset_example[3:]}"
    return f"""Parse this into calendar event(s):
- start/end MUST be ISO 8601 datetimes WITH the UTC offset for {tz_name} (e.g. 2026-07-21T15:00:00{offset_example}).
- end defaults to 1 hour after start if not specified.
- If the request describes multiple events (e.g. "every day Monday to Friday for two weeks"), emit a separate event for EACH occurrence with the correct date.

Today is {now_local.strftime('%A, %B %d %Y')} and the current local time is {now_local.strftime('%I:%M %p')}.

Text: "{text}"
"""


def _parse_with_claude(client, text: str) -> list[dict]:
    """Structured outputs: the API guarantees the response validates against
    the schema — no JSON scraping or retry heuristics needed."""
    resp = client.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=4096,
        system="You are a precise date/time parser for a calendar assistant.",
        messages=[{"role": "user", "content": _parse_prompt(text)}],
        output_format=ParsedEventList,
    )
    events = resp.parsed_output.events
    log.info(f"Structured parse via Claude: {len(events)} event(s)")
    return [e.model_dump() for e in events]


def _parse_with_ollama(text: str) -> list[dict]:
    """Fallback for when no Anthropic API key is configured: prompt for a JSON
    array and scrape it out of the completion."""
    prompt = _parse_prompt(text) + "\nReturn ONLY a valid JSON array of objects with keys: summary, start, end, description, location.\n\nJSON array:"
    result = query_ollama(prompt, "You are a precise date/time parser. Return only a valid JSON array, no explanation.")
    if not result:
        log.error("Ollama returned empty response for event parsing")
        return []
    log.info(f"Ollama parse response: {result[:300]}")
    try:
        arr_start = result.find("[")
        arr_end = result.rfind("]") + 1
        if arr_start >= 0 and arr_end > arr_start:
            parsed = json.loads(result[arr_start:arr_end])
            if isinstance(parsed, list):
                return parsed
        obj_start = result.find("{")
        obj_end = result.rfind("}") + 1
        if obj_start >= 0 and obj_end > obj_start:
            parsed = json.loads(result[obj_start:obj_end])
            if isinstance(parsed, dict):
                return [parsed]
    except json.JSONDecodeError:
        log.error(f"Failed to parse Ollama event response: {result}")
    return []


def parse_natural_language_event(text: str) -> list[dict]:
    """Parse a natural-language event description into structured data.
    Returns a list of events (may be multiple for recurring/multi-day requests)."""
    client = get_claude()
    if client:
        try:
            return _parse_with_claude(client, text)
        except Exception as e:
            log.error(f"Structured parse failed, falling back to Ollama: {e}")
    return _parse_with_ollama(text)

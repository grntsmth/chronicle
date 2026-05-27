import json
import re
import logging
from datetime import datetime, timedelta

import httpx

import config
from models import get_db, get_upcoming_events, get_events_range

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


SYSTEM_PROMPT = """You are The Chronicle, a personal calendar assistant. You analyze schedules and provide actionable insights.

Be concise and direct. Use bullet points. Focus on:
- Conflicts and overlaps
- Gaps that could be used productively
- Preparation needed for upcoming events
- Scheduling suggestions

Never be generic. Reference specific events by name and time."""


def analyze_schedule(hours: int = 24) -> str | None:
    conn = get_db()
    events = get_upcoming_events(conn, hours)
    conn.close()

    if not events:
        return f"No upcoming events in the next {hours} hours."

    event_lines = []
    for e in events:
        try:
            dt = datetime.fromisoformat(e["start_time"].replace("Z", "+00:00"))
            time_str = dt.strftime("%a %b %d %I:%M %p")
        except (ValueError, AttributeError):
            time_str = e["start_time"]
        desc = (e.get("description") or "").replace("\n", " ").strip()
        desc_part = f" — {desc[:200]}" if desc else ""
        event_lines.append(f"- {time_str}: {e['title']} ({e['source']}){' @ ' + e['location'] if e['location'] else ''}{desc_part}")

    prompt = f"""Here are the upcoming events for the next {hours} hours:

{chr(10).join(event_lines)}

Current time: {datetime.utcnow().strftime('%A, %B %d %Y %I:%M %p')} UTC

Analyze this schedule. Identify conflicts, suggest optimizations, and note any preparation needed."""

    return query_llm(prompt, SYSTEM_PROMPT)


def analyze_change(event: dict, change_type: str, all_events: list[dict], window_hours: int = 24) -> str | None:
    """Analyze the impact of a calendar change in context of other events within
    ±window_hours of the change event's start time."""
    try:
        change_dt = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
    except (ValueError, AttributeError, KeyError):
        return None

    window = timedelta(hours=window_hours)
    nearby = []
    for e in all_events:
        if e["id"] == event["id"]:
            continue
        try:
            e_dt = datetime.fromisoformat(e["start_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if abs(e_dt - change_dt) <= window:
            nearby.append((e_dt, e))

    if not nearby:
        return None

    nearby.sort(key=lambda x: x[0])
    other_lines = []
    for e_dt, e in nearby[:15]:
        time_str = e_dt.strftime("%a %b %d %Y, %I:%M %p")
        desc = (e.get("description") or "").replace("\n", " ").strip()
        desc_part = f" — {desc[:200]}" if desc else ""
        other_lines.append(f"- {time_str}: {e['title']}{desc_part}")

    change_time_str = change_dt.strftime("%a %b %d %Y, %I:%M %p")
    prompt = f"""A calendar event was {change_type}:
- Title: {event.get('title', '?')}
- Time: {change_time_str} (ends {event.get('end_time', '?')})
- Location: {event.get('location', 'none')}

Other events within ±{window_hours}h of that time:
{chr(10).join(other_lines)}

Does this change create any real conflicts? Only flag overlaps or tight back-to-backs on the SAME DAY as the change. Events on different days are not conflicts. If no issues, say so briefly."""

    return query_llm(prompt, SYSTEM_PROMPT)


def analyze_period(period: str = "week") -> str | None:
    """Analyze a week, month, or quarter of events."""
    conn = get_db()
    now = datetime.utcnow()

    if period == "week":
        start = now.replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=7)
        label = "This Week"
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        label = now.strftime("%B %Y")
    elif period == "quarter":
        q_month = ((now.month - 1) // 3) * 3 + 1
        start = now.replace(month=q_month, day=1, hour=0, minute=0, second=0)
        end_month = q_month + 3
        if end_month > 12:
            end = start.replace(year=start.year + 1, month=end_month - 12)
        else:
            end = start.replace(month=end_month)
        q_num = (now.month - 1) // 3 + 1
        label = f"Q{q_num} {now.year}"
    else:
        return None

    events = get_events_range(conn, start.isoformat(), end.isoformat())
    conn.close()

    if not events:
        return f"No events found for {label}."

    # Group by day
    days = {}
    for e in events:
        try:
            dt = datetime.fromisoformat(e["start_time"].replace("Z", "+00:00"))
            day_key = dt.strftime("%a %b %d")
        except (ValueError, AttributeError):
            day_key = "Unknown"
        days.setdefault(day_key, []).append(e)

    lines = []
    for day, day_events in days.items():
        lines.append(f"\n{day}:")
        for e in day_events:
            try:
                dt = datetime.fromisoformat(e["start_time"].replace("Z", "+00:00"))
                time_str = dt.strftime("%I:%M %p")
            except (ValueError, AttributeError):
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


def parse_natural_language_event(text: str) -> list[dict]:
    """Use LLM to parse a natural language event description into structured data.
    Returns a list of events (may be multiple for recurring/multi-day requests)."""
    prompt = f"""Parse this into calendar event(s). Return ONLY a valid JSON array of objects, each with:
- summary (string)
- start (ISO 8601 datetime string, assume America/New_York timezone)
- end (ISO 8601 datetime string, default to 1 hour after start if not specified)
- description (string, optional)
- location (string, optional)

If the request describes multiple events (e.g., "every day Monday to Friday for two weeks"), create a separate object for EACH individual event with the correct date.

Today is {datetime.utcnow().strftime('%A, %B %d %Y')}.

Text: "{text}"

JSON array:"""

    system = "You are a precise date/time parser. Return only a valid JSON array, no explanation."
    result = query_llm(prompt, system, max_tokens=4096)
    if not result:
        log.error("LLM returned empty response for event parsing")
        return []

    log.info(f"LLM parse response: {result[:300]}")

    # Try to extract JSON array
    try:
        # Find array
        arr_start = result.find("[")
        arr_end = result.rfind("]") + 1
        if arr_start >= 0 and arr_end > arr_start:
            parsed = json.loads(result[arr_start:arr_end])
            if isinstance(parsed, list):
                return parsed

        # Fallback: try single object
        obj_start = result.find("{")
        obj_end = result.rfind("}") + 1
        if obj_start >= 0 and obj_end > obj_start:
            parsed = json.loads(result[obj_start:obj_end])
            if isinstance(parsed, dict):
                return [parsed]
    except json.JSONDecodeError:
        log.error(f"Failed to parse LLM event response: {result}")
    return []

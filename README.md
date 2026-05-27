# chronicle

Unified calendar assistant: syncs Google Calendar + Outlook, surfaces the day ahead on Discord, and runs short LLM analyses over the combined schedule.

Built to solve a personal problem — my work calendar (Outlook) and personal calendar (Google) never talked to each other, and neither one alerted me to double-bookings or back-to-back meetings I'd regret the next morning.

## What it does

- **Syncs** Google Calendar (incremental via `syncToken` + push webhooks) and Outlook (full-window fetch via Graph API), reconciling deletions on both sides so the DB stays consistent with the source calendars.
- **Stores** events in a local SQLite DB (`models.py` — stdlib `sqlite3`), keyed on `(source, source_id)`. Event titles, times, locations, and descriptions are all retained and surfaced to the LLM during analysis.
- **Posts** a morning briefing to Discord at 7 AM ET with the day's events, flagged conflicts, and an LLM-generated "what to pay attention to" note. Weekly and monthly reviews run on cron.
- **Responds** to Discord prefix commands (`!today`, `!week`, `!add`, `!analyze`, `!review`) for ad-hoc queries.

## Stack

| Layer | Tool |
|---|---|
| API | FastAPI + uvicorn |
| Scheduler | APScheduler (in-process async) |
| Calendar APIs | google-api-python-client, msal (Outlook) |
| Discord | discord.py |
| LLM | Anthropic Claude via `anthropic` SDK, with fallback to a local Ollama endpoint reachable from the cluster |
| Storage | SQLite |
| Deploy | Kubernetes (see `k8s/`), sealed-secret for OAuth + Discord credentials |

## Deployment

Runs in the `ecosystem` namespace of my homelab k3s cluster. See [grntsmth/homelab](https://github.com/grntsmth/homelab) for the cluster itself. The manifests in `k8s/` are:

- `chronicle.yml` — Deployment, PVC for the SQLite DB, Service.
- `traefik-chronicle.yml` — IngressRoute exposing the FastAPI endpoints for OAuth callbacks.

LLM calls to the self-hosted Ollama instance route through a socat relay from the cluster to a Windows workstation on the Tailscale mesh — the infrastructure pattern is documented in the homelab repo.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Provide config via env vars — see config.py for the full list:
#   GOOGLE_CREDS_FILE, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID,
#   DISCORD_BOT_TOKEN, DISCORD_WEBHOOK_URL, ANTHROPIC_API_KEY, OLLAMA_URL,
#   USER_TIMEZONE (default America/New_York), USER_CONTEXT (see Personalization)
export $(cat .env | xargs)

uvicorn app:app --reload --port 8090
```

Hit `/chronicle/oauth/google` and `/chronicle/oauth/outlook` once each to seed the OAuth tokens; refresh is handled thereafter. If a refresh token is revoked or a client secret expires, the next tokenless resync will reap any events that disappeared during the outage so the LLM stops referencing deleted events.

## Personalization

The LLM analysis prompt is composed at startup from two pieces, both edited without touching the rest of the code:

- **`USER_CONTEXT`** (env var) — free-form facts about you. The default tells the model the user works at JPMorgan Chase as a bank teller; override with whatever helps the model interpret your titles in context (employer, recurring locations, people you live with, etc.).
- **Title keyword table** in `llm.py` (inside `SYSTEM_PROMPT`) — a short mapping of trigger words to event categories: `Shift` → work shift, `Meeting` → meeting, `Appointment` → service appointment, `Bill` → cost flag, and so on. Add or edit rows to teach the model your personal vocabulary.

The combination kills the hedging that a fresh LLM produces on bare titles like "JPMC Shift" — between user-level context and title-level keywords, the model classifies confidently instead of asking "is this a meeting or a shift?".

Times are rendered in `USER_TIMEZONE` (default `America/New_York`); Outlook events are normalized to UTC on sync and converted to local for display, so the model sees consistent clock times across both calendars.

## Status

Actively deployed on my homelab. The morning briefing has caught conflicts I would have missed otherwise, which is the bar.

import logging
import asyncio
import secrets
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Query, HTTPException, Depends
from fastapi.responses import RedirectResponse, PlainTextResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

import config
import metrics
import models
import google_cal
import outlook_cal
import discord_bot
import llm
import bot as discord_bot_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("chronicle")

scheduler = AsyncIOScheduler()


# --- Scheduled Jobs ---

async def job_sync_all():
    """Periodic full sync from all calendars."""
    log.info("Running scheduled sync...")
    loop = asyncio.get_event_loop()
    g_count = await loop.run_in_executor(None, google_cal.sync_calendar)
    o_count = await loop.run_in_executor(None, outlook_cal.sync_calendar)
    log.info(f"Sync complete: {g_count} Google, {o_count} Outlook events")


async def job_daily_briefing():
    """Morning briefing at 7 AM ET."""
    log.info("Sending daily briefing...")
    loop = asyncio.get_event_loop()

    # Sync first
    await loop.run_in_executor(None, google_cal.sync_calendar)
    await loop.run_in_executor(None, outlook_cal.sync_calendar)

    # Send event list
    await loop.run_in_executor(None, discord_bot.send_daily_briefing)

    # LLM analysis
    analysis = await loop.run_in_executor(None, llm.analyze_schedule, 16)
    if analysis:
        await loop.run_in_executor(None, discord_bot.send_llm_analysis, analysis, "Daily Briefing")


async def job_renew_webhooks():
    """Renew Google webhook channels before they expire."""
    log.info("Renewing webhooks...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, google_cal.setup_webhook)
    await loop.run_in_executor(None, outlook_cal.setup_webhook)


async def job_weekly_review():
    """Sunday evening weekly review."""
    log.info("Running weekly review...")
    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(None, llm.analyze_period, "week")
    if analysis:
        await loop.run_in_executor(None, discord_bot.send_llm_analysis, analysis, "Weekly Review")


async def job_monthly_review():
    """First of the month review."""
    log.info("Running monthly review...")
    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(None, llm.analyze_period, "month")
    if analysis:
        await loop.run_in_executor(None, discord_bot.send_llm_analysis, analysis, "Monthly Review")


async def job_quarterly_review():
    """Quarterly review (Jan 1, Apr 1, Jul 1, Oct 1)."""
    log.info("Running quarterly review...")
    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(None, llm.analyze_period, "quarter")
    if analysis:
        await loop.run_in_executor(None, discord_bot.send_llm_analysis, analysis, "Quarterly Review")


# --- App Lifecycle ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    models.init_db()
    log.info("Chronicle database initialized")

    # Schedule jobs. Cron triggers carry USER_TIMEZONE so "7 AM" stays 7 AM
    # local across DST — the old fixed-UTC hours drifted an hour every winter.
    tz = str(config.USER_TIMEZONE)
    scheduler.add_job(job_sync_all, "interval", minutes=config.SYNC_INTERVAL_MINUTES, id="sync_all")
    scheduler.add_job(job_daily_briefing, "cron", hour=7, minute=0, timezone=tz, id="daily_briefing")
    scheduler.add_job(job_renew_webhooks, "interval", days=1, id="renew_webhooks")
    scheduler.add_job(job_weekly_review, "cron", day_of_week="sun", hour=18, minute=0, timezone=tz, id="weekly_review")
    scheduler.add_job(job_monthly_review, "cron", day=1, hour=8, minute=0, timezone=tz, id="monthly_review")
    scheduler.add_job(job_quarterly_review, "cron", month="1,4,7,10", day=1, hour=10, minute=0, timezone=tz, id="quarterly_review")
    scheduler.start()
    log.info(f"Scheduler started: sync every {config.SYNC_INTERVAL_MINUTES}m, briefing 7 AM / weekly Sun 6 PM / monthly 8 AM / quarterly 10 AM, all {tz}")

    # Initial sync
    try:
        await job_sync_all()
    except Exception as e:
        log.warning(f"Initial sync failed (may need OAuth): {e}")

    # Register webhooks shortly after the server starts accepting traffic. Graph
    # does a synchronous validation handshake against our notificationUrl; if we
    # call setup_webhook before yield, the server isn't serving yet and Graph
    # gets a BadGateway. Sleep briefly so uvicorn is ready, then run.
    async def deferred_webhook_setup():
        await asyncio.sleep(5)
        try:
            await job_renew_webhooks()
        except Exception as e:
            log.warning(f"Initial webhook setup failed: {e}")

    webhook_task = asyncio.create_task(deferred_webhook_setup())

    # Start Discord bot in background
    bot_task = asyncio.create_task(discord_bot_client.start_bot())

    yield

    # Shutdown bot
    if not discord_bot_client.bot.is_closed():
        await discord_bot_client.bot.close()

    scheduler.shutdown()
    log.info("Chronicle shutting down")


app = FastAPI(title="The Chronicle", lifespan=lifespan)


# --- Auth ---

def require_token(request: Request):
    """Bearer-token gate for the private surface (/api/*, OAuth starts).

    Accepts `Authorization: Bearer <token>` or `?token=<token>` (the latter so
    the OAuth start URLs work from a browser). Fails closed when no token is
    configured — these endpoints are reachable from the public internet."""
    if not config.API_TOKEN:
        raise HTTPException(status_code=503, detail="CHRONICLE_API_TOKEN not configured")
    supplied = request.query_params.get("token", "")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        supplied = auth_header[7:]
    if not secrets.compare_digest(supplied, config.API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


# OAuth CSRF states: state -> issue time. Single-process app, so in-memory is
# fine; a pod restart mid-flow just means redoing the (rare) OAuth dance.
_oauth_states: dict[str, float] = {}
_OAUTH_STATE_TTL = 600


def _issue_oauth_state() -> str:
    now = time.time()
    for s, ts in list(_oauth_states.items()):
        if now - ts > _OAUTH_STATE_TTL:
            del _oauth_states[s]
    state = secrets.token_urlsafe(24)
    _oauth_states[state] = now
    return state


def _consume_oauth_state(state: str | None) -> bool:
    if not state:
        return False
    return _oauth_states.pop(state, None) is not None


# --- OAuth Endpoints ---

@app.get("/chronicle/oauth/google", dependencies=[Depends(require_token)])
async def oauth_google():
    """Start Google OAuth flow."""
    flow = google_cal.get_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=_issue_oauth_state(),
    )
    return RedirectResponse(auth_url)


@app.get("/chronicle/oauth/callback")
async def oauth_google_callback(code: str = Query(...), state: str = Query(None)):
    """Google OAuth callback."""
    if not _consume_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    flow = google_cal.get_oauth_flow()
    flow.fetch_token(code=code)
    google_cal.save_credentials(flow.credentials)
    log.info("Google Calendar authenticated successfully")

    # Trigger initial sync and webhook setup
    loop = asyncio.get_event_loop()
    count = await loop.run_in_executor(None, google_cal.sync_calendar)
    await loop.run_in_executor(None, google_cal.setup_webhook)

    discord_bot.send_embed(
        title="Google Calendar Connected",
        description=f"Successfully linked Google Calendar. Synced {count} events.",
        color=discord_bot.GREEN,
    )
    return PlainTextResponse(f"Google Calendar connected! Synced {count} events. You can close this tab.")


@app.get("/chronicle/oauth/outlook", dependencies=[Depends(require_token)])
async def oauth_outlook():
    """Start Outlook OAuth flow."""
    auth_url = outlook_cal.get_auth_url(state=_issue_oauth_state())
    if not auth_url:
        return PlainTextResponse("Outlook not configured (missing Azure credentials)", status_code=503)
    return RedirectResponse(auth_url)


@app.get("/chronicle/oauth/outlook/callback")
async def oauth_outlook_callback(code: str = Query(...), state: str = Query(None)):
    """Outlook OAuth callback."""
    if not _consume_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    result = outlook_cal.exchange_code(code)
    if not result:
        return PlainTextResponse("Outlook authentication failed", status_code=400)

    loop = asyncio.get_event_loop()
    count = await loop.run_in_executor(None, outlook_cal.sync_calendar)
    await loop.run_in_executor(None, outlook_cal.setup_webhook)

    discord_bot.send_embed(
        title="Outlook Calendar Connected",
        description=f"Successfully linked Outlook Calendar. Synced {count} events.",
        color=discord_bot.GREEN,
    )
    return PlainTextResponse(f"Outlook Calendar connected! Synced {count} events. You can close this tab.")


# --- Webhook Endpoints ---

@app.post("/chronicle/webhook/google")
async def webhook_google(request: Request):
    """Receive Google Calendar push notifications."""
    channel_id = request.headers.get("X-Goog-Channel-ID", "")
    resource_state = request.headers.get("X-Goog-Resource-State", "")

    log.info(f"Google webhook: state={resource_state}, channel={channel_id}")

    if resource_state == "sync":
        return Response(status_code=200)

    # Only process from the currently registered channel to avoid duplicates
    conn = models.get_db()
    row = conn.execute(
        "SELECT channel_id FROM sync_state WHERE source='google' AND calendar_id='primary'"
    ).fetchone()
    conn.close()
    if row and row["channel_id"] and channel_id != row["channel_id"]:
        log.info(f"Google webhook: ignoring stale channel {channel_id} (current: {row['channel_id']})")
        metrics.WEBHOOKS.labels("google", "stale").inc()
        return Response(status_code=200)
    metrics.WEBHOOKS.labels("google", "processed").inc()

    # Something changed — do an incremental sync
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, google_cal.sync_calendar)

    # Get only actually changed future events (upsert_event now skips unchanged).
    # Cutoff is a Python-format string because updated_at/synced_at are written
    # that way — sqlite's datetime('now') space-separated form doesn't sort
    # against them.
    conn = models.get_db()
    now = models.utc_now_str()
    cutoff = (datetime.utcnow() - timedelta(minutes=1)).isoformat(timespec="seconds")
    recent = conn.execute(
        "SELECT * FROM events WHERE source='google' AND updated_at >= ? AND synced_at >= ? AND start_time >= ? AND status != 'cancelled' ORDER BY start_time LIMIT 3",
        (cutoff, cutoff, now)
    ).fetchall()
    conn.close()

    if recent:
        # Filter out events just created via !add (already notified by bot)
        recent = [e for e in recent if dict(e).get("source_id", "") not in discord_bot_client.recently_created]

    if recent:
        conn = models.get_db()
        upcoming = models.get_upcoming_events(conn, 48)
        conn.close()
        for event in recent:
            event_dict = dict(event)
            discord_bot.notify_event_change(event_dict, "updated")
            analysis = await loop.run_in_executor(None, llm.analyze_change, event_dict, "updated", upcoming)
            if analysis:
                discord_bot.send_llm_analysis(analysis, "Schedule Impact")
    else:
        log.info("Google webhook: no changed future events (or suppressed)")

    return Response(status_code=200)


@app.post("/chronicle/webhook/outlook")
async def webhook_outlook(request: Request):
    """Receive Microsoft Graph push notifications."""
    params = request.query_params
    if "validationToken" in params:
        return PlainTextResponse(params["validationToken"])

    body = await request.json()
    notifications = body.get("value", [])

    # Graph echoes back the clientState we registered; anything else is not
    # from our subscription (endpoint is public — anyone can POST here).
    valid = [
        n for n in notifications
        if secrets.compare_digest(n.get("clientState", ""), config.OUTLOOK_CLIENT_STATE)
    ]
    if not valid:
        if notifications:
            log.warning(f"Outlook webhook: rejected {len(notifications)} notification(s) with bad clientState")
            metrics.WEBHOOKS.labels("outlook", "rejected").inc(len(notifications))
        return Response(status_code=200)

    metrics.WEBHOOKS.labels("outlook", "processed").inc(len(valid))
    log.info(f"Outlook webhook: {len(valid)} notifications")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, outlook_cal.sync_calendar)

    conn = models.get_db()
    now = models.utc_now_str()
    cutoff = (datetime.utcnow() - timedelta(minutes=1)).isoformat(timespec="seconds")
    recent = conn.execute(
        "SELECT * FROM events WHERE source='outlook' AND updated_at >= ? AND synced_at >= ? AND start_time >= ? AND status != 'cancelled' ORDER BY start_time LIMIT 3",
        (cutoff, cutoff, now)
    ).fetchall()
    conn.close()

    if recent:
        conn = models.get_db()
        upcoming = models.get_upcoming_events(conn, 48)
        conn.close()
        for event in recent:
            event_dict = dict(event)
            discord_bot.notify_event_change(event_dict, "updated")
            analysis = await loop.run_in_executor(None, llm.analyze_change, event_dict, "updated", upcoming)
            if analysis:
                discord_bot.send_llm_analysis(analysis, "Schedule Impact")

    return Response(status_code=200)


# --- API / Discord Command Endpoints ---

@app.get("/chronicle/api/today", dependencies=[Depends(require_token)])
async def api_today():
    """Get today's events (local-timezone day)."""
    conn = models.get_db()
    start, end = models.local_day_range()
    events = models.get_events_range(conn, start, end)
    conn.close()
    return {"events": events, "count": len(events)}


@app.get("/chronicle/api/upcoming", dependencies=[Depends(require_token)])
async def api_upcoming(hours: int = Query(24)):
    """Get upcoming events."""
    conn = models.get_db()
    events = models.get_upcoming_events(conn, hours)
    conn.close()
    return {"events": events, "count": len(events)}


@app.post("/chronicle/api/add", dependencies=[Depends(require_token)])
async def api_add_event(request: Request):
    """Add an event from natural language or structured data."""
    body = await request.json()
    text = body.get("text", "")
    target = body.get("target", "google")  # which calendar to add to

    loop = asyncio.get_event_loop()

    if text:
        parsed_list = await loop.run_in_executor(None, llm.parse_natural_language_event, text)
        if not parsed_list:
            raise HTTPException(status_code=400, detail="Could not parse event from text")
    else:
        parsed_list = [body]

    created = []
    for parsed in parsed_list:
        summary = parsed.get("summary", parsed.get("title", ""))
        start = parsed.get("start", "")
        end = parsed.get("end", "")
        desc = parsed.get("description", "")
        loc = parsed.get("location", "")

        if not summary or not start:
            continue

        if target == "outlook":
            event = await loop.run_in_executor(None, outlook_cal.create_event, summary, start, end, desc, loc)
        else:
            event = await loop.run_in_executor(None, google_cal.create_event, summary, start, end, desc, loc)

        if event:
            created.append({"summary": summary, "start": start, "event_id": event.get("id", "")})

    if created:
        return {"status": "created", "count": len(created), "events": created}
    raise HTTPException(status_code=500, detail="Failed to create any events")


@app.post("/chronicle/api/analyze", dependencies=[Depends(require_token)])
async def api_analyze(hours: int = Query(24)):
    """Run LLM analysis on upcoming schedule."""
    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(None, llm.analyze_schedule, hours)
    if analysis:
        await loop.run_in_executor(None, discord_bot.send_llm_analysis, analysis, "On-Demand Analysis")
        return {"analysis": analysis}
    raise HTTPException(status_code=500, detail="Analysis failed")


@app.get("/chronicle/metrics")
async def metrics_endpoint():
    """Prometheus metrics. Unauthenticated like /health — aggregates only,
    no event content."""
    conn = models.get_db()
    week_start = models.utc_now_str()
    week_end = (datetime.utcnow() + timedelta(days=7)).isoformat(timespec="seconds")
    for source in ("google", "outlook"):
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source=? AND status != 'cancelled' "
            "AND start_time >= ? AND start_time < ?",
            (source, week_start, week_end),
        ).fetchone()[0]
        metrics.UPCOMING_EVENTS.labels(source).set(n)
    next24 = models.get_upcoming_events(conn, 24)
    conn.close()
    metrics.CONFLICTS_24H.set(len(models.find_conflicts(next24)))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/chronicle/health")
async def health(response: Response):
    """Health check.

    Returns 503 when the primary calendar (Google) is unauthenticated or
    the scheduler died — a constant 200 once hid a broken refresh token
    for weeks because nothing probing this endpoint could tell the
    difference. Outlook is optional and only reported, not gated on.
    """
    google_ok = google_cal.get_credentials() is not None
    outlook_ok = outlook_cal.get_access_token() is not None
    healthy = google_ok and scheduler.running
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "degraded",
        "google_connected": google_ok,
        "outlook_connected": outlook_ok,
        "scheduler_running": scheduler.running,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)

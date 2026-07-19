import logging
import asyncio
from datetime import datetime, timedelta

import discord
from discord.ext import commands

import config
import models
import google_cal
import outlook_cal
import discord_bot
import llm
import voice
import agent

log = logging.getLogger("chronicle.bot")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Track recently created event IDs to suppress duplicate webhook notifications
recently_created = set()


def format_event_line(e: dict) -> str:
    if e.get("all_day"):
        time_str = "All day"
    else:
        dt = models.to_local(e["start_time"])
        time_str = dt.strftime("%I:%M %p") if dt else "?"
    source_icon = "\U0001f535" if e["source"] == "google" else "\U0001f7e0"
    loc = f" @ {e['location']}" if e.get("location") else ""
    return f"{source_icon} **{time_str}** — {e['title']}{loc}"


@bot.event
async def on_ready():
    log.info(f"Chronicle bot connected as {bot.user}")
    for guild in bot.guilds:
        log.info(f"  Connected to server: {guild.name} (id: {guild.id})")
        for channel in guild.text_channels:
            log.info(f"    Channel: #{channel.name} (id: {channel.id})")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Check for voice messages (audio attachments)
    for attachment in message.attachments:
        if attachment.content_type and "audio" in attachment.content_type:
            log.info(f"Voice message from {message.author}: {attachment.filename} ({attachment.content_type})")
            await handle_voice_message(message, attachment)
            return

    log.info(f"Message from {message.author}: {message.content}")
    await bot.process_commands(message)


async def handle_voice_message(message, attachment):
    """Transcribe a voice message and process it."""
    await message.add_reaction("\U0001f3a7")  # headphones emoji = processing

    # Download the audio
    audio_data = await attachment.read()

    # Transcribe
    loop = asyncio.get_event_loop()
    transcript = await loop.run_in_executor(None, voice.transcribe_audio, audio_data, attachment.content_type)

    if not transcript:
        await message.reply("Couldn't understand that voice message. Try again?")
        await message.remove_reaction("\U0001f3a7", bot.user)
        return

    await message.remove_reaction("\U0001f3a7", bot.user)
    await message.add_reaction("\u2705")  # checkmark

    # Show what we heard
    await message.reply(f"*\"{transcript}\"*\n\nProcessing...")

    # The tool-using agent routes the request itself: it can list events,
    # create, delete, and find free time. (Replaced the old keyword table.)
    await send_agent_reply(message.channel, transcript.strip())


async def send_agent_reply(channel, user_text: str):
    """Run the tool-using agent and post its reply as an embed."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, agent.run_agent, user_text)

    # Suppress duplicate webhook notifications for events the agent created
    for event_id in result["created_ids"]:
        recently_created.add(event_id)

        async def _cleanup(eid=event_id):
            await asyncio.sleep(60)
            recently_created.discard(eid)
        asyncio.create_task(_cleanup())

    embed = discord.Embed(
        title="Chronicle",
        description=result["reply"][:3900],
        color=discord_bot.AMBER,
    )
    embed.set_footer(text="The Chronicle")
    await channel.send(embed=embed)


@bot.command(name="help")
async def cmd_help(ctx):
    """Show available commands."""
    embed = discord.Embed(
        title="The Chronicle — Commands",
        color=discord_bot.AMBER,
        description=(
            "**!today** — Today's schedule\n"
            "**!tomorrow** — Tomorrow's schedule\n"
            "**!week** — Next 7 days\n"
            "**!upcoming [hours]** — Next N hours (default 24)\n"
            "**!add [text]** — Add event via natural language (Google)\n"
            "**!add outlook [text]** — Add event to Outlook\n"
            "**!ask [anything]** — Assistant with tools: check, create,\n"
            "  delete events, find free time (voice messages go here too)\n"
            "**!analyze [hours]** — LLM schedule analysis\n"
            "**!review week** — Weekly review\n"
            "**!review month** — Monthly review\n"
            "**!review quarter** — Quarterly review\n"
            "**!sync** — Force sync all calendars\n"
            "**!status** — Connection status\n"
        ),
    )
    embed.set_footer(text="The Chronicle")
    await ctx.send(embed=embed)


@bot.command(name="today")
async def cmd_today(ctx):
    """Show today's events."""
    conn = models.get_db()
    start, end = models.local_day_range()
    events = models.get_events_range(conn, start, end)
    conn.close()

    now_local = datetime.now(config.USER_TIMEZONE)
    if not events:
        embed = discord.Embed(
            title="Today — No Events",
            description="Your day is clear.",
            color=discord_bot.GREEN,
        )
    else:
        lines = [format_event_line(e) for e in events]
        embed = discord.Embed(
            title=f"Today — {len(events)} Events",
            description="\n".join(lines),
            color=discord_bot.AMBER,
        )
        embed.add_field(name="Date", value=now_local.strftime("%A, %B %d %Y"), inline=False)

    embed.set_footer(text="The Chronicle")
    await ctx.send(embed=embed)


@bot.command(name="tomorrow")
async def cmd_tomorrow(ctx):
    """Show tomorrow's events."""
    conn = models.get_db()
    tom_start, tom_end = models.local_day_range(offset_days=1)
    events = models.get_events_range(conn, tom_start, tom_end)
    conn.close()

    tom_date = (datetime.now(config.USER_TIMEZONE) + timedelta(days=1)).strftime("%A, %B %d %Y")

    if not events:
        embed = discord.Embed(
            title=f"Tomorrow — No Events",
            description="Tomorrow is clear.",
            color=discord_bot.GREEN,
        )
    else:
        lines = [format_event_line(e) for e in events]
        embed = discord.Embed(
            title=f"Tomorrow — {len(events)} Events",
            description="\n".join(lines),
            color=discord_bot.AMBER,
        )

    embed.add_field(name="Date", value=tom_date, inline=False)
    embed.set_footer(text="The Chronicle")
    await ctx.send(embed=embed)


@bot.command(name="week")
async def cmd_week(ctx):
    """Show the next 7 days."""
    conn = models.get_db()
    start, end = models.local_day_range(span_days=7)
    events = models.get_events_range(conn, start, end)
    conn.close()

    if not events:
        embed = discord.Embed(
            title="This Week — No Events",
            description="Your week is clear.",
            color=discord_bot.GREEN,
        )
        embed.set_footer(text="The Chronicle")
        await ctx.send(embed=embed)
        return

    # Group by local-timezone day
    days = {}
    for e in events:
        dt = models.to_local(e["start_time"])
        day_key = dt.strftime("%A, %b %d") if dt else "Unknown"
        days.setdefault(day_key, []).append(e)

    lines = []
    for day, day_events in days.items():
        lines.append(f"\n**{day}**")
        for e in day_events:
            lines.append(format_event_line(e))

    desc = "\n".join(lines)
    if len(desc) > 3900:
        desc = desc[:3900] + "\n\n_...truncated_"

    embed = discord.Embed(
        title=f"This Week — {len(events)} Events",
        description=desc,
        color=discord_bot.AMBER,
    )
    embed.set_footer(text="The Chronicle")
    await ctx.send(embed=embed)


@bot.command(name="upcoming")
async def cmd_upcoming(ctx, hours: int = 24):
    """Show upcoming events."""
    conn = models.get_db()
    events = models.get_upcoming_events(conn, hours)
    conn.close()

    if not events:
        embed = discord.Embed(
            title=f"Next {hours}h — No Events",
            description="Nothing scheduled.",
            color=discord_bot.GREEN,
        )
    else:
        lines = [format_event_line(e) for e in events]
        desc = "\n".join(lines)
        if len(desc) > 3900:
            desc = desc[:3900] + "\n\n_...truncated_"
        embed = discord.Embed(
            title=f"Next {hours}h — {len(events)} Events",
            description=desc,
            color=discord_bot.AMBER,
        )

    embed.set_footer(text="The Chronicle")
    await ctx.send(embed=embed)


@bot.command(name="add")
async def cmd_add(ctx, *, text: str):
    """Add event(s) via natural language. Supports multi-event commands."""
    # Check if targeting outlook
    target = "google"
    if text.lower().startswith("outlook "):
        target = "outlook"
        text = text[8:]

    await ctx.send(f"Parsing: *{text}*...")

    loop = asyncio.get_event_loop()
    parsed_list = await loop.run_in_executor(None, llm.parse_natural_language_event, text)

    if not parsed_list:
        await ctx.send("Could not parse that into event(s). Try something like: `!add Dentist Friday at 3pm`")
        return

    created = 0
    failed = 0

    for parsed in parsed_list:
        summary = parsed.get("summary", "")
        start = parsed.get("start", "")
        end = parsed.get("end", "")
        desc = parsed.get("description", "")
        loc = parsed.get("location", "")

        if not summary or not start:
            failed += 1
            continue

        if target == "outlook":
            event = await loop.run_in_executor(None, outlook_cal.create_event, summary, start, end, desc, loc)
        else:
            event = await loop.run_in_executor(None, google_cal.create_event, summary, start, end, desc, loc)

        if event:
            event_id = event.get("id", "")
            if event_id:
                recently_created.add(event_id)
                async def _cleanup(eid=event_id):
                    await asyncio.sleep(60)
                    recently_created.discard(eid)
                asyncio.create_task(_cleanup())
            created += 1
        else:
            failed += 1

    # Summary response
    if created == 1 and len(parsed_list) == 1:
        p = parsed_list[0]
        dt = models.to_local(p.get("start", ""))
        time_str = dt.strftime("%a %b %d, %I:%M %p") if dt else p.get("start", "?")

        embed = discord.Embed(
            title=f"Event Created: {p.get('summary', '?')}",
            color=discord_bot.GREEN,
        )
        embed.add_field(name="When", value=time_str, inline=True)
        embed.add_field(name="Calendar", value=target.title(), inline=True)
        if p.get("location"):
            embed.add_field(name="Where", value=p["location"], inline=True)
        embed.set_footer(text="The Chronicle")
        await ctx.send(embed=embed)
    elif created > 0:
        # Multi-event summary
        lines = []
        for p in parsed_list:
            try:
                dt = datetime.fromisoformat(p.get("start", "").replace("Z", "+00:00"))
                time_str = dt.strftime("%a %b %d, %I:%M %p")
            except (ValueError, AttributeError):
                time_str = p.get("start", "?")
            lines.append(f"- **{time_str}** — {p.get('summary', '?')}")

        desc = "\n".join(lines)
        if len(desc) > 3900:
            desc = desc[:3900] + "\n\n_...truncated_"

        embed = discord.Embed(
            title=f"{created} Events Created",
            description=desc,
            color=discord_bot.GREEN,
        )
        if failed:
            embed.add_field(name="Failed", value=str(failed), inline=True)
        embed.add_field(name="Calendar", value=target.title(), inline=True)
        embed.set_footer(text="The Chronicle")
        await ctx.send(embed=embed)
    else:
        await ctx.send("Failed to create any events. Check the logs.")


@bot.command(name="ask")
async def cmd_ask(ctx, *, text: str):
    """Free-form request handled by the tool-using agent."""
    await ctx.send("Consulting the Chronicle...")
    await send_agent_reply(ctx.channel, text)


@bot.command(name="analyze")
async def cmd_analyze(ctx, hours: int = 24):
    """Run LLM analysis on upcoming schedule."""
    await ctx.send("Consulting the Oracle...")

    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(None, llm.analyze_schedule, hours)

    if analysis:
        embed = discord.Embed(
            title=f"Oracle Analysis — Next {hours}h",
            description=analysis[:3900],
            color=discord_bot.AMBER,
        )
        embed.set_footer(text="The Chronicle")
        await ctx.send(embed=embed)
    else:
        await ctx.send("Oracle is unavailable. Is Ollama running on terminal?")


@bot.command(name="review")
async def cmd_review(ctx, period: str = "week"):
    """Run a weekly, monthly, or quarterly review."""
    period = period.lower()
    if period not in ("week", "month", "quarter"):
        await ctx.send("Usage: `!review week`, `!review month`, or `!review quarter`")
        return

    await ctx.send(f"Compiling {period}ly review...")

    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(None, llm.analyze_period, period)

    if analysis:
        titles = {"week": "Weekly Review", "month": "Monthly Review", "quarter": "Quarterly Review"}
        embed = discord.Embed(
            title=f"Oracle — {titles[period]}",
            description=analysis[:3900],
            color=discord_bot.AMBER,
        )
        embed.set_footer(text="The Chronicle")
        await ctx.send(embed=embed)
    else:
        await ctx.send("Oracle is unavailable. Is Ollama running on terminal?")


@bot.command(name="sync")
async def cmd_sync(ctx):
    """Force sync all calendars."""
    await ctx.send("Syncing...")

    loop = asyncio.get_event_loop()
    g_count = await loop.run_in_executor(None, google_cal.sync_calendar)
    o_count = await loop.run_in_executor(None, outlook_cal.sync_calendar)

    embed = discord.Embed(
        title="Sync Complete",
        description=f"Google: {g_count} events\nOutlook: {o_count} events",
        color=discord_bot.GREEN,
    )
    embed.set_footer(text="The Chronicle")
    await ctx.send(embed=embed)


@bot.command(name="status")
async def cmd_status(ctx):
    """Show connection status."""
    google_ok = google_cal.get_credentials() is not None
    outlook_ok = outlook_cal.get_access_token() is not None

    conn = models.get_db()
    total = conn.execute("SELECT COUNT(*) FROM events WHERE status != 'cancelled'").fetchone()[0]
    conn.close()

    embed = discord.Embed(
        title="Chronicle Status",
        color=discord_bot.GREEN if (google_ok or outlook_ok) else discord_bot.RED,
    )
    embed.add_field(name="Google", value="Connected" if google_ok else "Not connected", inline=True)
    embed.add_field(name="Outlook", value="Connected" if outlook_ok else "Not connected", inline=True)
    embed.add_field(name="Events Tracked", value=str(total), inline=True)
    embed.set_footer(text="The Chronicle")
    await ctx.send(embed=embed)


async def start_bot():
    """Start the Discord bot (called from app.py lifespan)."""
    if not config.DISCORD_BOT_TOKEN:
        log.warning("No DISCORD_BOT_TOKEN set — bot commands disabled")
        return
    try:
        await bot.start(config.DISCORD_BOT_TOKEN)
    except Exception as e:
        log.error(f"Discord bot failed to start: {e}")

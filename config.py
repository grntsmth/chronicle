import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).parent

USER_TIMEZONE = ZoneInfo(os.getenv("USER_TIMEZONE", "America/New_York"))

# Free-form facts about the user, injected into the LLM system prompt so it can
# interpret event titles in context. Override via env var without redeploying.
USER_CONTEXT = os.getenv(
    "USER_CONTEXT",
    "The user works at JPMorgan Chase as a bank teller; events titled 'JPMC Shift' "
    "are work shifts at a Chase Bank branch. The user is based in the Eastern US.",
)

# --- Google Calendar ---
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", str(BASE_DIR / "google-creds.json"))
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", str(BASE_DIR / "data" / "google-token.json"))
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/cloud-platform",
]

# --- Microsoft Outlook ---
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "consumers")  # "consumers" for personal accounts
AZURE_REDIRECT_URI = os.getenv("AZURE_REDIRECT_URI", "https://gigilab.duckdns.org/chronicle/oauth/outlook/callback")
OUTLOOK_TOKEN_FILE = os.getenv("OUTLOOK_TOKEN_FILE", str(BASE_DIR / "data" / "outlook-token.json"))

# --- Discord ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

# --- Anthropic Claude API (cloud — analysis, reviews, natural language) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# --- Ollama (via relay on high-palace — fallback if no API key) ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama-relay:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")

# --- Chronicle ---
DB_PATH = os.getenv("CHRONICLE_DB", str(BASE_DIR / "data" / "chronicle.db"))
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "10"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "https://gigilab.duckdns.org/chronicle")
PORT = int(os.getenv("CHRONICLE_PORT", "8090"))

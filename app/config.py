"""Configuration — reads a project-root .env (if present) then environment vars."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL       = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
HOST               = os.environ.get("HOST", "127.0.0.1")
PORT               = int(os.environ.get("PORT", "8000"))
DB_PATH            = os.environ.get("DB_PATH", str(ROOT / "data" / "app.db"))
SCHEMA_SQL         = ROOT / "db" / "schema.sql"
SEED_SQL           = ROOT / "db" / "seed_rules.sql"
USER_ID            = "user_default"
MIN_SIGNAL         = 120          # chars of usable description before we trust description-only
USE_GEMINI         = bool(GEMINI_API_KEY)

# --- V2: video analysis ---
GEMINI_VIDEO_MODEL = os.environ.get("GEMINI_VIDEO_MODEL", GEMINI_MODEL).strip()
ENABLE_VIDEO       = os.environ.get("ENABLE_VIDEO", "1") == "1"
MEDIA_RESOLUTION   = os.environ.get("MEDIA_RESOLUTION", "low").lower()   # low|medium|high (low = fewer tokens)
MAX_VIDEO_MB       = int(os.environ.get("MAX_VIDEO_MB", "18"))           # inline upload cap for non-YouTube

# --- Instagram / TikTok auth for yt-dlp (login-gated). Provide ONE of these. ---
INSTAGRAM_COOKIES    = os.environ.get("INSTAGRAM_COOKIES", "").strip()     # path to a cookies.txt (recommended on Chrome/Win)
COOKIES_FROM_BROWSER = os.environ.get("COOKIES_FROM_BROWSER", "").strip()  # chrome|edge|firefox (Chrome on Win often fails: DPAPI)

# --- Actions / intent ("what do I want to do with this link?") ---
TRANSLATE_TO = os.environ.get("TRANSLATE_TO", "English").strip()           # default language for the Translate action
ACTIONS      = ("note", "list", "translate")                              # v1 actions (shop/organize/share later)

# --- PKM: spaced-repetition "revisit" (turn saves into a knowledge base that resurfaces itself) ---
# The category taxonomy seeded into the DB (db.init). Kept here for the Gemini prompt + validation.
CATEGORIES        = ("Reading", "Watching", "Listening", "Learning",
                     "Shopping", "Reference", "Cooking", "Travel", "Other")
REVISIT_ENABLED   = os.environ.get("REVISIT_ENABLED", "1") == "1"
# Day-offsets for each successive revisit. Stage N uses the Nth interval; past the end we
# keep reusing the last one. Classic spaced-repetition-ish spacing.
REVISIT_SCHEDULE  = tuple(int(x) for x in
                          os.environ.get("REVISIT_SCHEDULE", "1,3,7,16,35").split(",") if x.strip()) or (1,)
REVISIT_SNOOZE_DAYS   = int(os.environ.get("REVISIT_SNOOZE_DAYS", "1"))     # how far "snooze" pushes a reminder
REVISIT_CHECK_SECONDS = int(os.environ.get("REVISIT_CHECK_SECONDS", "1800"))  # how often the scheduler scans for due items
REMIND_CHAT_ID        = os.environ.get("REMIND_CHAT_ID", "").strip()       # where to send reminders (default: item's origin chat)
# How revisits surface: "digest" = one weekly review message; "peritem" = a nudge per item as it
# comes due (Revisited/Snooze/Learned buttons); "off" = neither (review in Logseq only).
REVISIT_MODE          = os.environ.get("REVISIT_MODE", "digest").strip().lower()
DIGEST_INTERVAL_DAYS  = int(os.environ.get("DIGEST_INTERVAL_DAYS", "7"))    # how often the auto digest is sent
DIGEST_LOOKAHEAD_DAYS = int(os.environ.get("DIGEST_LOOKAHEAD_DAYS", "7"))   # include items due within this window (+ overdue)

# --- Logseq knowledge-base export (see docs/LOGSEQ_PLAN.md). Off unless a graph dir is set. ---
LOGSEQ_GRAPH_DIR    = os.environ.get("LOGSEQ_GRAPH_DIR", "").strip()        # path to your Logseq graph folder
LOGSEQ_ENABLED      = os.environ.get("LOGSEQ_ENABLED", "1") == "1"
LOGSEQ_SYNC_SECONDS = int(os.environ.get("LOGSEQ_SYNC_SECONDS", "60"))      # (Phase 2) read-back poll interval

# --- Security (required before exposing to the internet) ---
APP_PASSWORD   = os.environ.get("APP_PASSWORD", "").strip()                # set this to require login (and to allow non-localhost binding)
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()             # signs the session cookie (falls back to APP_PASSWORD)
COOKIE_SECURE  = os.environ.get("COOKIE_SECURE", "auto").strip().lower()   # auto|on|off — mark session cookie Secure
TELEGRAM_ALLOWED_CHAT_IDS = {c.strip() for c in
                             os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()}

# --- V2: Gemini usage limits (NO balance API exists; these are editable rate-limit guides). ---
# Verify current values at https://ai.google.dev/gemini-api/docs/rate-limits
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "1000000"))  # our own soft daily cap
GEMINI_RPD_LIMIT   = int(os.environ.get("GEMINI_RPD_LIMIT", "250"))        # requests/day (free-tier-ish default)
GEMINI_TPM_LIMIT   = int(os.environ.get("GEMINI_TPM_LIMIT", "250000"))     # tokens/minute (for display)

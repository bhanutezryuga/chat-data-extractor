# Chat Data Extractor

Catch links/files shared into a **Telegram bot**, understand what each is and what it's for, turn it into a **trackable task**, and show status on a **dashboard**. **Google Gemini** is the single LLM provider.

## Why
Useful reels, shorts, videos, PDFs, and articles get lost in chat scrollback and never acted on. This turns them into an actionable backlog.

## How it works (one breath)
Telegram bot (or the dashboard's "Add link" box) → classify via rules → fetch description (PDFs go to Gemini natively) → Gemini extracts a summary + an actionable task → SQLite → dashboard. Reels/shorts/videos with **no usable description** are analyzed as **video** by Gemini multimodal (YouTube by URL; Instagram/TikTok via optional yt-dlp).

## Actions / intent ("what do I want to do with this?")
Every item gets a **smart-default action** Gemini picks from the content, and you can override it — one action per item:
- **📝 Note** — a summary to look back on (the default).
- **📋 List** — surface the enumerated items (books/products/links).
- **🌐 Translate** — render the content in your language (`TRANSLATE_TO`, default English). Auto-chosen when the content isn't already in that language.

Note↔List switching is **instant** (same extraction, different emphasis); Translate runs a translation if one wasn't already made. New actions (Shop/Organize/Share) plug in the same way.

**Two ways to set the action:**
- **Choose first:** send `new <link>` → the bot shows **[📝 Note] [📋 List] [🌐 Translate] [✨ Auto]**; tap one and *then* it processes (Auto = let it decide).
- **Auto + override:** send a bare link → it auto-processes; switch later via the buttons under the result or the **dashboard popup switcher**.

**Each item shows only its action's content** — Translate shows just the translation, List just the list, Note the summary/key-points. Translate target defaults to **English** (`TRANSLATE_TO`); any non-English content is rendered to it.

## Gemini usage (no "balance" exists)
The Gemini API has **rate limits, not a token balance**. The app **meters** every call's real token count into the `gemini_usage` table and shows, in the dashboard header: **tokens used today vs your daily budget** (colored bar), **requests today vs requests/day**, and all-time tokens. A budget guard **defers video calls to NEEDS_REVIEW** once you hit the cap. Tune the limits in `.env` (`DAILY_TOKEN_BUDGET`, `GEMINI_RPD_LIMIT`); verify real numbers at <https://ai.google.dev/gemini-api/docs/rate-limits>.

## Video analysis (v2)
- **YouTube** works out of the box — no install (sent to Gemini by URL).
- **Instagram / TikTok** need **yt-dlp** (optional). Install it, then hit **Reprocess** on any NEEDS_REVIEW item:
  ```powershell
  python -m pip install yt-dlp     # or: winget install yt-dlp
  ```
  Without yt-dlp those simply stay in NEEDS_REVIEW — nothing breaks. (ffmpeg optional, only for videos that need stream-merging.)

## Instagram support
Instagram serves **nothing** to anonymous clients (no caption/og tags — just a JS shell) and requires a **login** even for yt-dlp. Because the app runs locally, you lend it your Instagram session via a **cookies file**:

1. Install **yt-dlp** (once): `python -m pip install yt-dlp`
2. In Chrome (logged into Instagram), install the extension **“Get cookies.txt LOCALLY”**.
3. Open <https://www.instagram.com>, click the extension → **Export** → save as `ig_cookies.txt` in the project folder.
4. In `.env` set: `INSTAGRAM_COOKIES=C:\Users\<you>\chat-data-extractor\ig_cookies.txt`
5. Restart (`python -m app`). Now Instagram works:
   - **Photo / carousel posts** → caption + **all slide images are read by Gemini vision** (lists/books/tips that live *in the images* are captured into the card). Uses Instagram's web API (`app/instagram.py`) since yt-dlp can't get IG image URLs.
   - **Reels / video** → caption + the video is downloaded and analyzed.
   - **Caption-only** when that's all there is.

Notes: the cookies file *is* your Instagram session — keep it private, never commit it; re-export when it expires. `--cookies-from-browser chrome` is unreliable on Windows (Chrome encrypts cookies / DPAPI), which is why we use the file. Using a personal account for automated fetches is against Instagram’s ToS — keep usage light.

---

## Run it locally (current implementation)

**Requirements:** Python 3.11+ (tested on 3.14). **No `pip install` needed — standard library only.**

```bash
# 1. configure secrets
copy .env.example .env        # Windows  (cp .env.example .env on mac/linux)
#   then edit .env and fill in TELEGRAM_BOT_TOKEN and GEMINI_API_KEY

# 2. start the app (web dashboard + Telegram poller)
python -m app           # or double-click run.bat — leave the window open so it keeps running

# 3. open the dashboard
#    http://127.0.0.1:8000
```

> **Keep it running:** launch it in your *own* terminal (`run.bat` on Windows, `./run.sh` on Linux/macOS/Pi) and leave it open — the app runs until you close it / press Ctrl+C. On startup it **auto-requeues** any item left mid-processing by a previous crash, so nothing gets permanently stuck.

**Runs anywhere:** pure Python standard library (yt-dlp optional) — Windows, macOS, Linux, **Raspberry Pi**, or an **old Android phone** (via Termux). A home device keeps your residential IP so Instagram/YouTube keep working. 24/7 host guides: **[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md)** · **[docs/ANDROID_PHONE.md](docs/ANDROID_PHONE.md)**.

- Without `GEMINI_API_KEY`, an **offline stub** runs so the app still works (just less smart).
- Without `TELEGRAM_BOT_TOKEN`, the bot is disabled but the dashboard + "Add link" box still work.
- Reset the database: delete `data/app.db`.

### Get the two credentials
- **Telegram bot token** — in Telegram, message **@BotFather** → `/newbot` → copy the token.
- **Gemini API key** — https://aistudio.google.com/apikey (Google AI Studio "Gemini API" key, starts with `AIza…`).

### Code layout
```
app/
  __main__.py   # entrypoint: python -m app
  config.py     # reads .env + env vars
  db.py         # sqlite helpers (one connection per call -> thread-safe)
  rules.py      # classify a URL via the rules table
  fetch.py      # oEmbed / readability / PDF download
  gemini.py     # Gemini over raw HTTPS (+ offline stub)
  pipeline.py   # ingest -> classify -> fetch -> extract -> task
  telegram.py   # long-poll getUpdates (no public URL needed)
  web.py        # ThreadingHTTPServer: dashboard + JSON API
  static/index.html   # the dashboard UI
db/  schema.sql · seed_rules.sql      # reused by the app on startup
spike/  run.py                        # earlier throwaway proof-of-concept
```

---

## Security & going online
Locally it runs open on `127.0.0.1` (auth off). **Before exposing it to the internet**, set `APP_PASSWORD` (and `SESSION_SECRET`, `TELEGRAM_ALLOWED_CHAT_IDS`) in `.env` — then it requires login, the bot only obeys your chat, and outbound fetches are SSRF-guarded. Without `APP_PASSWORD` the app refuses to bind to anything but localhost. Full runbook (Cloudflare Tunnel + Access, or Tailscale; PC / Raspberry Pi / Android hosting): **[DEPLOYMENT.md](DEPLOYMENT.md)**.

## Design docs
These were written first (PM / Dev / Architect / DBA hats). They describe a Cloudflare-serverless
target; the **current build runs the same pipeline + schema locally in Python** instead.
- [docs/PLAN.md](docs/PLAN.md) — scope, milestones, risks *(PM)*
- [docs/DESIGN.md](docs/DESIGN.md) — flows, prompts, API, dashboard *(Dev/UX)*
- [docs/RULES.md](docs/RULES.md) — content-type rules engine *(the "purpose" brain)*
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — topology + DB schema *(Architect/DBA)*
- [docs/TEST.md](docs/TEST.md) — test strategy *(QA)*

## Database
- [db/schema.sql](db/schema.sql) — schema (SQLite; also valid D1 DDL)
- [db/seed_rules.sql](db/seed_rules.sql) — 7 default rules + seed user

## Status
**v1 + v2 working end-to-end**, validated with live Gemini: classify → extract → task → dashboard, including **multimodal video analysis** (YouTube tested live) and **token-usage metering**. Instagram/TikTok video is enabled by installing yt-dlp.

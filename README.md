# Chat Data Extractor

Catch links/files shared into a **Telegram bot**, understand what each is and what it's for, and write it into your **Logseq knowledge base** — with a weekly review digest so saves don't rot. **Google Gemini** is the single LLM provider. A minimal local dashboard shows capture stats and surfaces anything stuck.

## Why
Useful reels, shorts, videos, PDFs, and articles get lost in chat scrollback and never acted on. This turns them into a linked, backlinked knowledge base you actually revisit.

## How it works (one breath)
Telegram bot → classify via rules → fetch description (PDFs go to Gemini natively) → Gemini extracts a summary/list/recipe + an actionable task → SQLite → a Logseq page. Reels/shorts/videos with **no usable description** are analyzed as **video** by Gemini multimodal (YouTube by URL; Instagram/TikTok via optional yt-dlp).

## Actions / intent ("what do I want to do with this?")
Every item gets a **smart-default action** Gemini picks from the content, and you can override it — one action per item:
- **📝 Note** — a summary to look back on (the default).
- **📋 List** — surface the enumerated items (books/products/links).
- **🌐 Translate** — render the content in your language (`TRANSLATE_TO`, default English). Auto-chosen when the content isn't already in that language.

Note↔List switching is **instant** (same extraction, different emphasis); Translate runs a translation if one wasn't already made. New actions (Shop/Organize/Share) plug in the same way.

**Two ways to set the action, both in Telegram:**
- **Choose first:** send `new <link>` → the bot shows **[📝 Note] [📋 List] [🌐 Translate] [✨ Auto]**; tap one and *then* it processes (Auto = let it decide).
- **Auto + override:** send a bare link → it auto-processes; switch later via the buttons under the result message.

**Each result shows only its action's content** — Translate shows just the translation, List just the list, Note the summary/key-points.

## Recipe capture
If Gemini detects the content **is** a recipe, it's documented in full on the Logseq page — every ingredient (with quantity) under `## Ingredients`, every step in order under `## Steps` — instead of a generic summary. No button needed; it's automatic.

## Knowledge base + revisits (so saves don't rot)
A read-later pile is a graveyard unless it comes back to you. Every processed item also becomes a **knowledge record**: Gemini gives it a **title** and a **category** (Reading, Watching, Listening, Learning, Shopping, Reference, Cooking, Travel, Other), and any enumerated `list_items` (books, products, tools…) are captured as a checkable collection.

Each item is then queued on a **spaced-repetition schedule** (`REVISIT_SCHEDULE`, default `1,3,7,16,35` days). How that surfaces depends on `REVISIT_MODE`:
- **`digest`** (default) — one **weekly Telegram message** listing everything due (`/review` to trigger it on demand).
- **`peritem`** — a **🔔 revisit** nudge per item as it comes due, with **[✅ Revisited] [💤 Snooze] [🎓 Learned]** buttons.
- **`off`** — no reminders; review happens in Logseq itself.

The dashboard's **Due to review** tile shows the current count. Turn revisits off entirely with `REVISIT_ENABLED=0`. Reminders go to the item's origin chat (or `REMIND_CHAT_ID`).

## Logseq export
Point `LOGSEQ_GRAPH_DIR` at a [Logseq](https://logseq.com) graph folder and every capture is written there as a Markdown page — a minimal `title::`/`tags::` properties block, then the summary/key points (or recipe, or list), a translation if made, and the collection as bullets. Categories in `LOGSEQ_TODO_CATEGORIES` (default `Learning,Reading`) get their collection items as checkable `TODO`s and a `SCHEDULED` revisit line that shows in Logseq's agenda; every other category (songs, recipes, videos to watch…) gets plain reference bullets. Tags become `[[pages]]`, so your saves turn into a linked, backlinked knowledge base. The page's `## Notes` section is yours and is preserved across re-exports. A one-line journal breadcrumb is added too (`LOGSEQ_JOURNAL=0` to turn that off). Off unless `LOGSEQ_GRAPH_DIR` is set.

**Content dedup:** a capture that matches an earlier item's content (same list, or same title) is marked a duplicate instead of writing a second page — and when a page *is* list-based, the redundant Summary/Key points sections are skipped so the page just shows the list.

**Bulk export / backfill:** push everything currently in the database to Logseq via the Telegram `/export` command, or backfill a batch of links from a text file (see **Bulk sideload** below). Reprocessing an item preserves your checked-off collection items.

## Recovering stuck items (retry / archive)
Items that hit a transient failure (rate-limits, 503s, an expired Instagram session) land in **FAILED** or **NEEDS_REVIEW** instead of silently vanishing. They show up under **⚠ Needs attention** on the dashboard, with:
- **🔁 Retry now** — reprocess every stuck item in the background (gentle: one at a time, bails out on an Instagram logout, repeated rate-limits, or the daily budget).
- **🗑 Archive gone (N)** — clear out items that are permanently unrecoverable (deleted/removed posts — HTTP 404/410) so they stop cluttering the panel.

Same two actions work from Telegram: `/retry` and `/archivegone`. Or from the command line for finer control (`--limit`, `--delay`, `--all` to include every source, not just sideloaded ones):
```bash
python -m app.sideload --retry [--run] [--limit N] [--all]
python -m app.sideload --archive-gone [--run] [--all]
```

## Bulk sideload
Backfill a batch of links from a `<timestamp> <link>` text file — no Telegram round-trip needed:
```bash
python -m app.sideload links.txt          # dry run: shows what would be processed
python -m app.sideload links.txt --run    # actually process (dedup-safe: re-running just resumes)
```

## Gemini usage (no "balance" exists)
The Gemini API has **rate limits, not a token balance**. The app **meters** every call's real token count into the `gemini_usage` table and shows, in the dashboard header: **tokens used today vs your daily budget** (colored bar) and **requests today vs requests/day**. A budget guard **defers video calls** once you hit the cap. Tune the limits in `.env` (`DAILY_TOKEN_BUDGET`, `GEMINI_RPD_LIMIT`); verify real numbers at <https://ai.google.dev/gemini-api/docs/rate-limits>.

## Video analysis
- **YouTube** works out of the box — no install (sent to Gemini by URL).
- **Instagram / TikTok** need **yt-dlp** (optional):
  ```powershell
  python -m pip install yt-dlp     # or: winget install yt-dlp
  ```
  Without yt-dlp those simply stay in NEEDS_REVIEW — nothing breaks. (ffmpeg optional, only for videos that need stream-merging.)

## Instagram support
Instagram serves **nothing** to anonymous clients (no caption/og tags — just a JS shell) and requires a **login** even for yt-dlp. Because the app runs locally, you lend it your Instagram session via a **cookies file**:

1. Install **yt-dlp** (once): `python -m pip install yt-dlp`
2. In Chrome (logged into Instagram), install the extension **"Get cookies.txt LOCALLY"**.
3. Open <https://www.instagram.com>, click the extension → **Export** → save as `ig_cookies.txt` in the project folder.
4. In `.env` set: `INSTAGRAM_COOKIES=C:\Users\<you>\chat-data-extractor\ig_cookies.txt`
5. Restart (`python -m app`). Now Instagram works:
   - **Photo / carousel posts** → caption + **all slide images are read by Gemini vision** (lists/books/tips that live *in the images* are captured into the card). Uses Instagram's web API (`app/instagram.py`) since yt-dlp can't get IG image URLs.
   - **Reels / video** → caption + the video is downloaded and analyzed.
   - **Caption-only** when that's all there is.

Notes: the cookies file *is* your Instagram session — keep it private, never commit it. **Instagram periodically force-invalidates it** (observed: after roughly 20–30 links even at a conservative pace) — when that happens, captures start landing in FAILED/NEEDS_REVIEW; re-export a fresh `sessionid` via the extension, restart, and `/retry`. `--cookies-from-browser chrome` is unreliable on Windows (Chrome encrypts cookies / DPAPI), which is why we use the file. Using a personal account for automated fetches is against Instagram's ToS — keep usage light.

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

> **Keep it running:** launch it in your *own* terminal (`run.bat` on Windows, `./run.sh` on Linux/macOS/Pi) and leave it open — the app runs until you close it / press Ctrl+C. On startup it **auto-requeues** any item left mid-processing by a previous crash, so nothing gets permanently stuck. On Windows you can also register it to start automatically at log-on — see **[deploy/WINDOWS_AUTOSTART.html](deploy/WINDOWS_AUTOSTART.html)**.

**Runs anywhere:** pure Python standard library (yt-dlp optional) — Windows, macOS, Linux, **Raspberry Pi**, or an **old Android phone** (via Termux). A home device keeps your residential IP so Instagram/YouTube keep working. 24/7 host guides: **[docs/RASPBERRY_PI.html](docs/RASPBERRY_PI.html)** · **[docs/ANDROID_PHONE.html](docs/ANDROID_PHONE.html)**.

- Without `GEMINI_API_KEY`, an **offline stub** runs so the app still works (just less smart).
- Without `TELEGRAM_BOT_TOKEN`, the bot is disabled but the dashboard + `/api/ingest` still work.
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
  pipeline.py   # ingest -> classify -> fetch -> extract -> task -> knowledge record
  revisit.py    # spaced-repetition scheduler + digest/per-item reminders
  logseq.py     # export captures to a Logseq graph (Markdown pages + journal)
  sideload.py   # bulk-import from a file; retry/archive stuck items (CLI + used by the dashboard)
  telegram.py   # long-poll getUpdates (no public URL needed)
  web.py        # ThreadingHTTPServer: dashboard + JSON API
  static/minimal.html   # the dashboard UI
db/  schema.sql · seed_rules.sql      # reused by the app on startup
spike/  run.py                        # earlier throwaway proof-of-concept
```

---

## Security & going online
Locally it runs open on `127.0.0.1` (auth off). **Before exposing it to the internet**, set `APP_PASSWORD` (and `SESSION_SECRET`, `TELEGRAM_ALLOWED_CHAT_IDS`) in `.env` — then it requires login, the bot only obeys your chat, and outbound fetches are SSRF-guarded. Without `APP_PASSWORD` the app refuses to bind to anything but localhost. Full runbook (Cloudflare Tunnel + Access, or Tailscale; PC / Raspberry Pi / Android hosting): **[DEPLOYMENT.html](DEPLOYMENT.html)**.

## Design docs
These were written first (PM / Dev / Architect / DBA hats), describing a Cloudflare-serverless target with a task-tracker dashboard. The project has since pivoted twice: first to running that same pipeline **locally in Python** instead of Cloudflare, then to a **capture → Logseq knowledge base + weekly review** tool — the task-tracker dashboard described here was later replaced by the minimal metrics view above. Kept as historical design intent, not current scope:
- [docs/PLAN.html](docs/PLAN.html) — scope, milestones, risks *(PM)*
- [docs/DESIGN.html](docs/DESIGN.html) — flows, prompts, API, dashboard *(Dev/UX)*
- [docs/RULES.html](docs/RULES.html) — content-type rules engine *(the "purpose" brain)*
- [docs/ARCHITECTURE.html](docs/ARCHITECTURE.html) — topology + DB schema *(Architect/DBA)*
- [docs/TEST.html](docs/TEST.html) — test strategy *(QA)*

## Database
- [db/schema.sql](db/schema.sql) — schema (SQLite; also valid D1 DDL)
- [db/seed_rules.sql](db/seed_rules.sql) — 7 default rules + seed user

## Status
**Live and in daily use.** Captures flow Telegram → Gemini → Logseq, with recipe capture, content dedup, and lean list pages. A minimal dashboard shows capture/usage stats and a **Needs attention** panel — with one-click **retry** and **archive** — for anything stuck. Bulk backfill and stuck-item recovery are also available via the `python -m app.sideload` CLI. Video analysis (YouTube out of the box; Instagram/TikTok via optional yt-dlp) and token-usage metering are both validated live. Docs were migrated from Markdown to HTML (this README is the one deliberate exception, so GitHub still renders it as the repo's landing page).

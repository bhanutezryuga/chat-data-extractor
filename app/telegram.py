"""Telegram ingestion via long-polling getUpdates (no public URL needed — perfect for local).

Runs in a daemon thread started from __main__. Handles:
  - text / captions containing URLs
  - PDF document attachments sent straight to the bot
"""
import json
import threading
import time
import urllib.request
from urllib.parse import urlencode

from . import config, db, logseq, pipeline, revisit

API = "https://api.telegram.org/bot{token}/{method}"
FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
_ICON = {"note": "📝", "list": "📋", "translate": "🌐"}


def _allowed(chat_id):
    """True if the chat may use the bot. Empty allowlist = open (with a one-time hint)."""
    ids = config.TELEGRAM_ALLOWED_CHAT_IDS
    return (not ids) or (str(chat_id) in ids)


def _call(method, **params):
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    data = urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read().decode())


def send_message(chat_id, text, kbd=None):
    params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if kbd:
        params["reply_markup"] = json.dumps(kbd)
    try:
        _call("sendMessage", **params)
    except Exception:
        pass


def edit_message_text(chat_id, message_id, text, kbd=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "disable_web_page_preview": True}
    if kbd:
        params["reply_markup"] = json.dumps(kbd)
    try:
        _call("editMessageText", **params)
    except Exception:
        pass


def _action_kbd(item_id, active):
    # Post-result override offers Note/List only; Translate is chosen up front via `new`.
    row = [{"text": ("• " if a == active else "") + _ICON[a] + " " + a.title(),
            "callback_data": f"act|{item_id}|{a}"} for a in ("note", "list")]
    return {"inline_keyboard": [row]}


def _new_kbd(item_id):
    """Pre-process chooser for the `new <link>` command (includes Auto)."""
    opts = [("note", "📝 Note"), ("list", "📋 List"),
            ("translate", "🌐 Translate"), ("auto", "✨ Auto")]
    b = [{"text": lbl, "callback_data": f"new|{item_id}|{a}"} for a, lbl in opts]
    return {"inline_keyboard": [b[:2], b[2:]]}


def _revisit_kbd(item_id):
    row = [{"text": "✅ Revisited", "callback_data": f"rv|{item_id}|revisited"},
           {"text": "💤 Snooze", "callback_data": f"rv|{item_id}|snoozed"},
           {"text": "🎓 Learned", "callback_data": f"rv|{item_id}|learned"}]
    return {"inline_keyboard": [row]}


def _reminder_chat(item):
    """Where a revisit reminder goes: an explicit REMIND_CHAT_ID, else the item's origin
    chat (numeric only — 'web' isn't a chat), else the first allow-listed chat."""
    if config.REMIND_CHAT_ID:
        return config.REMIND_CHAT_ID
    src = str(item.get("source_chat_id") or "")
    if src.lstrip("-").isdigit():
        return src
    return next(iter(config.TELEGRAM_ALLOWED_CHAT_IDS), None)


def send_reminder(item):
    """Send a spaced-repetition revisit nudge for one item (called by the scheduler)."""
    chat_id = _reminder_chat(item)
    if not chat_id:
        return
    title = item.get("title") or item.get("content_type") or "a saved item"
    cat = item.get("category")
    n = item.get("revisit_count") or 0
    head = "🔔 Time to revisit" + (f" (#{n + 1})" if n else "")
    body = f"{head}\n\n📌 {title}"
    if cat:
        body += f"\n🏷 {cat}"
    if item.get("raw_url"):
        body += f"\n{item['raw_url']}"
    send_message(chat_id, body, _revisit_kbd(item["id"]))


def send_review_digest(chat_id=None):
    """Send one weekly-review message listing items due to revisit. Returns the count.
    chat_id=None (the auto weekly send) goes to REMIND_CHAT_ID / the first allow-listed chat."""
    con = db.connect()
    rows = [dict(r) for r in revisit.due_within(con, config.DIGEST_LOOKAHEAD_DAYS)]
    con.close()
    chat = chat_id or config.REMIND_CHAT_ID or next(iter(config.TELEGRAM_ALLOWED_CHAT_IDS), None)
    if not chat:
        return 0
    if not rows:
        send_message(chat, "📚 Weekly review — nothing due right now. ✅")
        return 0
    lines = [f"📚 Weekly review — {len(rows)} to revisit\n"]
    for i, r in enumerate(rows[:20], 1):
        cat = f"  [{r['category']}]" if r.get("category") else ""
        lines.append(f"{i}. {r.get('title') or r.get('content_type') or 'item'}{cat}")
        if r.get("raw_url"):
            lines.append(f"   {r['raw_url']}")
    if len(rows) > 20:
        lines.append(f"\n+{len(rows) - 20} more")
    lines.append("\nOpen Logseq to review.")
    send_message(chat, "\n".join(lines))
    return len(rows)


def _format_result(item_id):
    """Build the per-action result message (and keyboard) for an item."""
    con = db.connect()
    it = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    ex = con.execute("SELECT * FROM extractions WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
                     (item_id,)).fetchone()
    con.close()
    if not it:
        return None, None
    action = it["action"] or "note"
    # Translate replies are just the translation — no operation header.
    head = "" if action == "translate" else f"{_ICON.get(action, '•')} {action.upper()} · {it['content_type'] or '?'}\n\n"
    body = ""
    if ex:
        if action == "list":
            items = json.loads(ex["list_items"] or "[]")
            if items:
                lines = []
                for x in items[:15]:
                    name = x.get("name") if isinstance(x, dict) else x
                    link = x.get("link") if isinstance(x, dict) else ""
                    show = link and link.lower() != "link not available"
                    lines.append(f"• {name}" + (f"\n   {link}" if show else ""))
                body = "\n".join(lines)
            else:
                body = ex["summary"] or ""
        elif action == "translate":
            body = (ex["translation"] or "").strip() or (ex["summary"] or "(no translation available)")
        else:
            body = ex["summary"] or ""
    text = head + (body[:3500] if body else "(processing…)")
    return text, _action_kbd(item_id, action)


def _handle_callback(cb):
    cb_id = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    if not _allowed(chat_id):
        _call("answerCallbackQuery", callback_query_id=cb_id)
        return
    parts = data.split("|", 2)
    if len(parts) != 3:
        _call("answerCallbackQuery", callback_query_id=cb_id)
        return
    kind, item_id, action = parts

    if kind == "rv":                                    # spaced-repetition revisit response
        res = revisit.mark(item_id, action)
        done = {"revisited": "✅ Nice — resurfacing again later.",
                "snoozed": "💤 Snoozed — I'll remind you again soon.",
                "learned": "🎓 Marked learned — no more reminders."}.get(action, "Done")
        _call("answerCallbackQuery", callback_query_id=cb_id,
              text=(res.get("error") or done)[:180])
        if not res.get("error") and chat_id and mid:
            base = (msg.get("text") or "").split("\n\n")[-1]
            edit_message_text(chat_id, mid, f"{done}\n\n{base}".strip())
        return

    if kind == "new":                                   # pre-process choice: process now with this action
        _call("answerCallbackQuery", callback_query_id=cb_id, text=f"Processing as {action}…")
        try:
            pipeline.process_pending(item_id, action)
        except Exception as e:
            if chat_id and mid:
                edit_message_text(chat_id, mid, f"⚠️ Failed: {e}")
            return
        if chat_id and mid:
            text, kbd = _format_result(item_id)
            edit_message_text(chat_id, mid, text or "done", kbd)
        return

    res = pipeline.set_action(item_id, action)          # post-process override
    _call("answerCallbackQuery", callback_query_id=cb_id,
          text=(res.get("error") or f"Switched to {action}")[:180])
    if not res.get("error") and chat_id and mid:
        text, kbd = _format_result(item_id)
        if text:
            edit_message_text(chat_id, mid, text, kbd)


def _download_file(file_id, limit=20_000_000):
    info = _call("getFile", file_id=file_id)
    path = info.get("result", {}).get("file_path")
    if not path:
        return None
    url = FILE_API.format(token=config.TELEGRAM_BOT_TOKEN, path=path)
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read(limit)


def _ack(results):
    lines = []
    for r in results:
        if not r:
            continue
        icon = {"ACTIONABLE": "✅", "NEEDS_REVIEW": "🔎", "FAILED": "⚠️"}.get(r["status"], "•")
        line = f"{icon} {r.get('content_type') or '?'} → {r['status']}"
        if r.get("task"):
            line += f"\n   📌 {r['task']}"
        lines.append(line)
    return "\n".join(lines) if lines else None


def handle_update(u):
    if u.get("callback_query"):
        return _handle_callback(u["callback_query"])
    msg = u.get("message") or u.get("channel_post")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    msg_id = msg["message_id"]
    if not _allowed(chat_id):
        print(f"  [telegram] ignored message from non-allowlisted chat {chat_id}")
        return
    text = msg.get("text") or msg.get("caption") or ""

    low = text.strip().lower()
    if low.startswith("/start"):
        send_message(chat_id,
                     "Send a link and I'll auto-process it, or use `new <link>` to choose the "
                     "action first (Note / List / Translate / Auto).\n"
                     "`/review` → what's due to revisit this week.\n"
                     + ("`/export` → write everything to your Logseq graph.\n" if logseq.active() else "")
                     + f"Your chat id: {chat_id}  (put it in TELEGRAM_ALLOWED_CHAT_IDS to lock the bot)\n"
                     f"Dashboard: http://{config.HOST}:{config.PORT}")
        return

    if low.startswith("/export"):
        if not logseq.active():
            send_message(chat_id, "Logseq export is off — set LOGSEQ_GRAPH_DIR in .env and restart.")
        else:
            n = logseq.export_all()
            send_message(chat_id, f"⤓ Exported {n} new item(s) to your Logseq graph." if n
                         else "✅ Logseq graph already up to date — no new items to export.")
        return

    if low.startswith("/review"):          # on-demand weekly review digest
        send_review_digest(chat_id=chat_id)
        return

    # `new <link>` (or `/new`) — choose the action BEFORE processing
    if low.startswith("new ") or low.startswith("/new"):
        urls = pipeline.extract_urls(text)
        if not urls:
            send_message(chat_id, "Send a link to choose an action for, e.g.  new https://…")
            return
        for i, url in enumerate(urls):
            r = pipeline.create_pending(raw_url=url, raw_text=text,
                                        source_chat_id=str(chat_id), source_msg_id=f"new{msg_id}:{i}")
            if r and r.get("status") == "AWAITING_ACTION":
                send_message(chat_id, f"🆕 What should I do with this?\n{url}", _new_kbd(r["id"]))
            else:
                send_message(chat_id, "🔁 Already saved this — skipping.")
        return

    results = []

    doc = msg.get("document")
    if doc and ((doc.get("mime_type", "") == "application/pdf")
                or (doc.get("file_name", "") or "").lower().endswith(".pdf")):
        try:
            blob = _download_file(doc["file_id"])
            if blob:
                results.append(pipeline.ingest(
                    raw_text=text,
                    attachment={"bytes": blob, "filename": doc.get("file_name"),
                                "mime": doc.get("mime_type", "application/pdf")},
                    source_chat_id=str(chat_id), source_msg_id=str(msg_id)))
        except Exception as e:
            send_message(chat_id, f"⚠️ Could not read the PDF: {e}")

    for i, url in enumerate(pipeline.extract_urls(text)):
        results.append(pipeline.ingest(
            raw_url=url, raw_text=text,
            source_chat_id=str(chat_id), source_msg_id=f"{msg_id}:{i}"))

    sent = False
    for r in results:
        if not r:
            continue
        sent = True
        if r["status"] == "DUPLICATE":
            send_message(chat_id, "🔁 Already saved this — skipping.")
        elif r["status"] == "ACTIONABLE":
            text_out, kbd = _format_result(r["id"])
            send_message(chat_id, text_out or "✅ done", kbd)
        else:
            icon = {"NEEDS_REVIEW": "🔎", "FAILED": "⚠️"}.get(r["status"], "•")
            send_message(chat_id, f"{icon} {r.get('content_type') or '?'} → {r['status']}")
    if not sent and not doc:
        send_message(chat_id, "I didn't find a link or PDF in that message. Send me a URL or a PDF file.")


def _loop():
    offset = 0
    print("  [telegram] poller started")
    while True:
        try:
            resp = _call("getUpdates", offset=offset, timeout=50)
            for u in resp.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception as e:
                    print(f"  [telegram] handle error: {e}")
        except Exception as e:
            print(f"  [telegram] poll error: {e}; retrying in 3s")
            time.sleep(3)


def start_poller():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t

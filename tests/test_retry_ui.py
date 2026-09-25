"""Tests for the dashboard/Telegram retry + archive-gone UI (app/web.py, app/telegram.py).

Hermetic: temp DB, stub mode, auth off, Logseq off, no real Telegram/Gemini/Instagram network —
`sideload.retry`/`sideload.archive_gone` and `telegram._call` are monkeypatched with fast fakes so
this exercises the orchestration (background thread, running-lock, 409, scope defaults, message
plumbing) added this session, not the already-covered retry/archive-gone logic itself
(see tests/test_sideload.py). Run:
    python tests/test_retry_ui.py
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time

if hasattr(sys.stdout, "reconfigure"):    # Windows console (cp1252) can't print the emoji Telegram
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # messages carry; this test only

_TMP = tempfile.mkdtemp(prefix="cde_retry_ui_")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""    # never write to a real graph
os.environ["APP_PASSWORD"] = ""        # auth off (isolate from a real .env's APP_PASSWORD)
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""   # open (isolate from a real .env's allowlist)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, sideload, telegram, web   # noqa: E402
from http.server import ThreadingHTTPServer            # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _seed_item(status, source, reason=None):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,source_chat_id,raw_url,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (iid, config.USER_ID, source, f"https://x.test/{iid[:6]}", status, db.now(), db.now()))
    con.commit()
    if reason:
        db.log(con, iid, "extract", "error", reason)
    con.close()
    return iid


def _item_status(iid):
    con = db.connect()
    st = con.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()["status"]
    con.close()
    return st


def _wait_until(pred, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# =========================================================================
# web.py: /api/failures, /api/retry, /api/archive-gone
# =========================================================================
print("\n=== retry/archive-gone: web.py ===\n")

_srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()


def http_req(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    raw = r.read().decode()
    conn.close()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    return r.status, parsed


# ---- baseline: no stuck items yet ----
status, data = http_req("GET", "/api/failures")
check("GET /api/failures: 200, has retry_running/last_retry keys",
      status == 200 and data.get("retry_running") is False and data.get("last_retry") is None,
      str(data))

# ---- seed stuck items from a non-sideload source (mirrors real Telegram captures) ----
tg_item = _seed_item("FAILED", "telegram-chat-1", "HTTP Error 429: Too Many Requests")
sl_item = _seed_item("FAILED", "sideload", "HTTP Error 503: Service Unavailable")

status, data = http_req("GET", "/api/failures")
check("GET /api/failures: lists both sideload and non-sideload stuck items",
      status == 200 and data["count"] == 2, str(data))

# ---- POST /api/retry: fake sideload.retry so this tests orchestration, not retry logic ----
_retry_calls = []
_orig_retry = sideload.retry


def _fake_retry(*, execute, limit, sideload_only, out=None):
    _retry_calls.append({"execute": execute, "limit": limit, "sideload_only": sideload_only})
    time.sleep(0.25)
    return {"processed": 2, "recovered": 1, "stopped": None}


sideload.retry = _fake_retry
try:
    status, data = http_req("POST", "/api/retry", {})
    check("POST /api/retry: 202 started (non-blocking)", status == 202 and data.get("started") is True, str(data))

    status2, data2 = http_req("POST", "/api/retry", {})
    check("POST /api/retry while running: 409 already-running",
          status2 == 409 and "already" in (data2.get("error") or ""), str(data2))

    check("retry runs with sideload_only=False by default (matches what the panel shows)",
          len(_retry_calls) == 1 and _retry_calls[0]["sideload_only"] is False, str(_retry_calls))

    ok = _wait_until(lambda: web._retry_state["running"] is False)
    check("retry finishes and clears the running flag", ok)

    status3, data3 = http_req("GET", "/api/failures")
    check("GET /api/failures reflects the finished retry's summary",
          status3 == 200 and data3.get("retry_running") is False
          and data3.get("last_retry", {}).get("recovered") == 1, str(data3))

    # explicit sideload_only=True is still honored when asked for
    _retry_calls.clear()
    status4, _ = http_req("POST", "/api/retry", {"sideload_only": True})
    _wait_until(lambda: web._retry_state["running"] is False)
    check("POST /api/retry honors an explicit sideload_only=True",
          _retry_calls and _retry_calls[0]["sideload_only"] is True, str(_retry_calls))
finally:
    sideload.retry = _orig_retry

# ---- POST /api/archive-gone: real archive_gone (DB-only, no network) ----
gone_sideload = _seed_item("FAILED", "sideload", "HTTP Error 404: Not Found")
gone_other = _seed_item("NEEDS_REVIEW", "telegram-chat-1", "HTTP Error 410: Gone")

status, data = http_req("POST", "/api/archive-gone", {})
check("POST /api/archive-gone: default sideload_only=False archives non-sideload gone items too",
      status == 200 and data.get("archived", 0) >= 2, str(data))
check("archived item's status flips to ARCHIVED",
      _item_status(gone_sideload) == "ARCHIVED" and _item_status(gone_other) == "ARCHIVED")

gone_scoped = _seed_item("FAILED", "telegram-chat-2", "HTTP Error 404: Not Found")
status, data = http_req("POST", "/api/archive-gone", {"sideload_only": True})
check("POST /api/archive-gone honors an explicit sideload_only=True (leaves non-sideload item alone)",
      _item_status(gone_scoped) == "FAILED", f"status={_item_status(gone_scoped)}")

_srv.shutdown()
_srv.server_close()


# =========================================================================
# telegram.py: /retry and /archivegone commands
# =========================================================================
print("\n=== retry/archive-gone: telegram.py ===\n")

_tg_calls = []


def _fake_call(method, **params):
    _tg_calls.append((method, params))
    return {"ok": True, "result": {}}


telegram._call = _fake_call


def _msg(chat_id, text, msg_id=1):
    return {"message": {"chat": {"id": chat_id}, "message_id": msg_id, "text": text}}


def _sent_texts(chat_id=None):
    return [p.get("text", "") for m, p in _tg_calls
            if m == "sendMessage" and (chat_id is None or p.get("chat_id") == chat_id)]


# ---- /retry: fake web._run_retry_bg so this tests command plumbing, not retry logic ----
_bg_calls = []
_orig_bg = web._run_retry_bg


def _fake_bg(limit, sideload_only):
    _bg_calls.append((limit, sideload_only))
    time.sleep(0.2)
    web._retry_state["last"] = {"processed": 3, "recovered": 2, "stopped": None}
    web._retry_state["running"] = False


web._run_retry_bg = _fake_bg
try:
    web._retry_state["running"] = False
    web._retry_state["last"] = None
    _tg_calls.clear()

    telegram.handle_update(_msg(555, "/retry"))
    check("/retry: sends an immediate 'retrying in background' ack",
          any("background" in t.lower() for t in _sent_texts(555)), str(_sent_texts(555)))
    check("/retry: sets running=True synchronously before returning",
          web._retry_state["running"] is True)

    # a second /retry while the first is still running must not start a second background run
    telegram.handle_update(_msg(555, "/retry"))
    check("/retry while running: tells the user one is already running, doesn't double-run",
          any("already running" in t.lower() for t in _sent_texts(555)) and len(_bg_calls) == 1,
          str(_sent_texts(555)))

    ok = _wait_until(lambda: web._retry_state["running"] is False)
    check("/retry background thread completes", ok)
    check("/retry: sends a follow-up with the result",
          any("reprocessed 3" in t and "recovered 2" in t for t in _sent_texts(555)),
          str(_sent_texts(555)))
    check("/retry calls through with sideload_only=False (matches the dashboard panel's scope)",
          _bg_calls[0] == (None, False), str(_bg_calls))
finally:
    web._run_retry_bg = _orig_bg

# ---- /archivegone: fake sideload.archive_gone (DB-only in reality, but keep it hermetic) ----
_ag_calls = []


def _fake_archive_gone(*, execute, sideload_only, out=None, ids=None):
    _ag_calls.append({"execute": execute, "sideload_only": sideload_only, "ids": ids})
    return {"gone": 1, "archived": 1 if execute else 0,
            "items": [{"id": "fake-gone-1", "raw_url": "https://x.test/gone"}]}


_orig_archive_gone = sideload.archive_gone
sideload.archive_gone = _fake_archive_gone
try:
    _tg_calls.clear()
    telegram.handle_update(_msg(555, "/archivegone"))
    check("/archivegone: first sends a dry-run preview (execute=False), not an archive",
          _ag_calls and _ag_calls[0]["execute"] is False, str(_ag_calls))
    check("/archivegone preview calls through with sideload_only=False",
          _ag_calls and _ag_calls[0]["sideload_only"] is False, str(_ag_calls))
    _kbd = json.loads(next((p.get("reply_markup") for m, p in _tg_calls
                            if m == "sendMessage" and p.get("reply_markup")), "{}"))
    _confirm = [b["callback_data"] for row in _kbd.get("inline_keyboard", []) for b in row
                if b["callback_data"].endswith("|confirm")]
    _tg_calls.clear()
    telegram.handle_update({"callback_query": {"id": "c", "data": _confirm[0] if _confirm else "",
                                               "message": {"chat": {"id": 555}, "message_id": 9}}})
    _edits = [p.get("text", "") for m, p in _tg_calls if m == "editMessageText"]
    check("/archivegone confirm: archives the previewed ids and replies with the count",
          len(_ag_calls) == 2 and _ag_calls[1]["execute"] is True and _ag_calls[1]["ids"] == ["fake-gone-1"]
          and any("archived 1" in t.lower() for t in _edits), f"{_ag_calls} {_edits}")
finally:
    sideload.archive_gone = _orig_archive_gone

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

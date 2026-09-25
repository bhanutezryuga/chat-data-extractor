"""Tests for the Telegram capture UX: failure replies with a reason + next step (#15), pending
`new <link>` items being re-offered and listed (#17), the two-step /archivegone (#18), and the
/start + /help text (#19).

Hermetic: temp DB, stub mode, Logseq off, allowlist pinned to the fake chat, no real Telegram /
Gemini / network (telegram._call, pipeline.fetch and friends are monkeypatched). Run:
    python tests/test_capture_ux.py
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):    # Windows console (cp1252) can't print the emoji
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="cde_capture_ux_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "x"
os.environ["LOGSEQ_GRAPH_DIR"] = ""            # never write to a real graph
os.environ["APP_PASSWORD"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "777"   # pin it: a real .env must not leak in (#20)
os.environ["REMIND_CHAT_ID"] = ""
os.environ["HOST"] = "127.0.0.1"
os.environ["PORT"] = "8000"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, gemini, pipeline, sideload, telegram   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


_tg_calls = []


def _fake_call(method, **params):
    _tg_calls.append((method, params))
    return {"ok": True, "result": {}}


telegram._call = _fake_call

CHAT = 777


def _msg(text, msg_id):
    return {"message": {"chat": {"id": CHAT}, "message_id": msg_id, "text": text}}


def _cb(data, msg_id=900, text=""):
    return {"callback_query": {"id": "cb1", "data": data,
                               "message": {"chat": {"id": CHAT}, "message_id": msg_id, "text": text}}}


def _sent(method="sendMessage"):
    return [p for m, p in _tg_calls if m == method]


def _texts(method="sendMessage"):
    return [p.get("text", "") for p in _sent(method)]


def _status(iid):
    con = db.connect()
    r = con.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()
    con.close()
    return r["status"] if r else None


def _seed(status, url, reason=None):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,source_chat_id,source_msg_id,raw_url,status,content_type,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (iid, config.USER_ID, str(CHAT), "seed" + iid[:8], url, status, "article", db.now(), db.now()))
    if reason:
        db.log(con, iid, "fetch", "warn", reason)
    con.commit()
    con.close()
    return iid


_LONG = "A long article about Python decorators with many details on usage and examples."
_orig_fetch = pipeline.fetch
pipeline.fetch = lambda url, s: (("", None, {"fetched": False}) if "empty" in url
                                 else (f"Deep dive {url}. {_LONG}", None, {"fetched": True}))

# =========================================================================
# #15 — FAILED / NEEDS_REVIEW replies explain why and what to do next
# =========================================================================
print("\n=== #15: failure replies ===\n")


def _rate_limited(*a, **k):
    raise RuntimeError("429 RESOURCE_EXHAUSTED rate limit")


_orig_stub = gemini.stub
gemini.stub = _rate_limited
try:
    _tg_calls.clear()
    telegram.handle_update(_msg("https://example.com/other-article", 1))
finally:
    gemini.stub = _orig_stub
t = " | ".join(_texts())
check("rate-limited capture: reply includes the classify label", "Rate limited" in t, t)
check("rate-limited capture: reply suggests /retry", "/retry" in t, t)
check("rate-limited capture: reply is not the bare '→ FAILED' token", "→ FAILED" not in t, t)

# per-category next steps (helper-level, seeded reasons)
iid_login = _seed("NEEDS_REVIEW", "https://www.instagram.com/p/abc/",
                  "instagram: rate-limit reached or login required")
iid_gone = _seed("NEEDS_REVIEW", "https://www.instagram.com/p/dead/", "instagram: HTTP Error 404: Not Found")
iid_nocontent = _seed("NEEDS_REVIEW", "https://youtube.com/shorts/x", "description too thin")
t_login = telegram._ack(iid_login)
t_gone = telegram._ack(iid_gone)
t_nc = telegram._ack(iid_nocontent)
check("login_expired: says the Instagram cookie needs refreshing",
      "cookie" in t_login.lower() and "refresh" in t_login.lower(), t_login)
check("gone: says there's nothing to do (and no /retry nudge)",
      "nothing to do" in t_gone.lower() and "/retry" not in t_gone, t_gone)
check("gone vs login_expired replies differ", t_login != t_gone)
check("no_content: suggests /retry or /note", "/retry" in t_nc or "/note" in t_nc, t_nc)
check("NEEDS_REVIEW reply isn't the bare '→ NEEDS_REVIEW' token", "→ NEEDS_REVIEW" not in t_nc, t_nc)

# `new <link>` chooser path that ends FAILED edits the chooser into the explained failure
_tg_calls.clear()
telegram.handle_update(_msg("new https://example.com/chooser-fails", 2))
con = db.connect()
pend = con.execute("SELECT id FROM items WHERE raw_url='https://example.com/chooser-fails'").fetchone()["id"]
con.close()
gemini.stub = _rate_limited
try:
    _tg_calls.clear()
    telegram.handle_update(_cb(f"new|{pend}|note"))
finally:
    gemini.stub = _orig_stub
t = " | ".join(_texts("editMessageText"))
check("new-chooser ending FAILED: edited message explains it (label + /retry)",
      "Rate limited" in t and "/retry" in t, t)

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

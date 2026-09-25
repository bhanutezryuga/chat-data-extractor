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

# =========================================================================
# #17 — pending `new <link>` items are re-offered and listable
# =========================================================================
print("\n=== #17: pending items ===\n")


def _chooser_items(params_list):
    """Item ids referenced by new|<id>|… buttons in the given sendMessage params."""
    import json
    ids = set()
    for p in params_list:
        kbd = json.loads(p.get("reply_markup") or "{}")
        for row in kbd.get("inline_keyboard", []):
            for b in row:
                if b.get("callback_data", "").startswith("new|"):
                    ids.add(b["callback_data"].split("|")[1])
    return ids


_tg_calls.clear()
telegram.handle_update(_msg("new https://example.com/pending-one", 10))
con = db.connect()
pend1 = con.execute("SELECT id FROM items WHERE raw_url='https://example.com/pending-one'").fetchone()["id"]
con.close()
_tg_calls.clear()                                    # user never taps a button, re-sends the link
telegram.handle_update(_msg("https://example.com/pending-one", 11))
t = " | ".join(_texts())
check("re-sending a pending link does not say 'Already saved'", "already saved" not in t.lower(), t)
check("...it re-offers the chooser for the same item", _chooser_items(_sent()) == {pend1}, t)
check("...and the item is still AWAITING_ACTION (not auto-processed)", _status(pend1) == "AWAITING_ACTION")

_tg_calls.clear()                                    # same via `new <link>` again
telegram.handle_update(_msg("new https://example.com/pending-one", 12))
t = " | ".join(_texts())
check("`new <link>` on a pending link re-offers the chooser too",
      "already saved" not in t.lower() and _chooser_items(_sent()) == {pend1}, t)

# a genuinely processed link still says Already saved
_tg_calls.clear()
telegram.handle_update(_msg("https://example.com/processed-one", 13))
_tg_calls.clear()
telegram.handle_update(_msg("https://example.com/processed-one", 14))
check("a processed duplicate still replies 'Already saved'",
      any("already saved" in x.lower() for x in _texts()), _texts())

_tg_calls.clear()
telegram.handle_update(_msg("new https://example.com/pending-two", 15))
con = db.connect()
pend2 = con.execute("SELECT id FROM items WHERE raw_url='https://example.com/pending-two'").fetchone()["id"]
con.close()
_tg_calls.clear()
telegram.handle_update(_msg("/pending", 16))
check("/pending re-sends a chooser for every AWAITING_ACTION item",
      {pend1, pend2} <= _chooser_items(_sent()), _texts())
check("/pending never says 'I didn't find a link'",
      not any("didn't find a link" in x for x in _texts()), _texts())

telegram.handle_update(_cb(f"new|{pend1}|auto"))
telegram.handle_update(_cb(f"new|{pend2}|note"))
_tg_calls.clear()
telegram.handle_update(_msg("/pending", 17))
check("/pending with nothing pending says so, with no buttons",
      any("nothing" in x.lower() for x in _texts()) and not _chooser_items(_sent()), _texts())

_tg_calls.clear()
telegram.handle_update(_msg("/start", 18))
check("/start help mentions /pending", any("/pending" in x for x in _texts()), _texts())

# =========================================================================
# #18 — /archivegone previews first; only the confirm button archives
# =========================================================================
print("\n=== #18: /archivegone confirm ===\n")


def _buttons(params_list):
    import json
    out = []
    for p in params_list:
        kbd = json.loads(p.get("reply_markup") or "{}")
        for row in kbd.get("inline_keyboard", []):
            out.extend(b.get("callback_data", "") for b in row)
    return out


g1 = _seed("FAILED", "https://www.instagram.com/p/gone-one/", "instagram: HTTP Error 410: Gone")
keep = _seed("FAILED", "https://example.com/transient", "HTTP Error 503: Service Unavailable")
gone_now = {iid_gone, g1}                             # iid_gone was seeded in the #15 section

_tg_calls.clear()
telegram.handle_update(_msg("/archivegone", 30))
t = " | ".join(_texts())
check("/archivegone does not change any status", all(_status(i) != "ARCHIVED" for i in gone_now | {keep}))
check("/archivegone preview states the count", "2" in t, t)
check("/archivegone preview shows a sample URL", "instagram.com/p/gone-one" in t, t)
btns = _buttons(_sent())
confirm = [b for b in btns if b.startswith("ag|") and b.endswith("|confirm")]
cancel = [b for b in btns if b.startswith("ag|") and b.endswith("|cancel")]
check("/archivegone preview has ag|…|confirm and ag|…|cancel buttons", confirm and cancel, btns)
check("ag| callback data fits Telegram's 64-byte limit", all(len(b.encode()) <= 64 for b in btns), btns)

g2 = _seed("NEEDS_REVIEW", "https://www.instagram.com/p/gone-later/", "HTTP Error 404: Not Found")
_tg_calls.clear()
telegram.handle_update(_cb(confirm[0]))
check("confirm archives exactly the previewed items",
      all(_status(i) == "ARCHIVED" for i in gone_now), [_status(i) for i in gone_now])
check("...not an item that became gone after the preview", _status(g2) == "NEEDS_REVIEW", _status(g2))
check("...and never a non-gone item", _status(keep) == "FAILED", _status(keep))
t = " | ".join(_texts("editMessageText") + _texts())
check("confirm replies with the archived count", "Archived 2" in t, t)

_tg_calls.clear()                                     # same button tapped twice: no-op
telegram.handle_update(_cb(confirm[0]))
check("re-tapping a used confirm doesn't archive anything new", _status(g2) == "NEEDS_REVIEW")

_tg_calls.clear()
telegram.handle_update(_msg("/archivegone", 31))
cancel2 = [b for b in _buttons(_sent()) if b.endswith("|cancel")]
confirm2 = [b for b in _buttons(_sent()) if b.endswith("|confirm")]
_tg_calls.clear()
telegram.handle_update(_cb(cancel2[0]))
check("cancel changes nothing", _status(g2) == "NEEDS_REVIEW")
check("cancel edits the preview to say so",
      any("cancel" in x.lower() for x in _texts("editMessageText")), _texts("editMessageText"))
telegram.handle_update(_cb(confirm2[0]))
check("confirm after cancel is a no-op", _status(g2) == "NEEDS_REVIEW")

telegram.handle_update(_cb("ag|nosuchtoken|confirm"))
check("unknown/expired token archives nothing", _status(g2) == "NEEDS_REVIEW")

con = db.connect()
con.execute("UPDATE items SET status='ARCHIVED' WHERE id=?", (g2,))
con.commit()
con.close()
_tg_calls.clear()
telegram.handle_update(_msg("/archivegone", 32))
t = " | ".join(_texts())
check("with 0 gone items the reply says so", "no" in t.lower() and "gone" in t.lower(), t)
check("...and offers no button", not _buttons(_sent()), _buttons(_sent()))

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

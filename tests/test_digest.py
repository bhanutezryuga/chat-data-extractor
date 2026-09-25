"""Tests for the weekly review loop: digest cards with action buttons (#10), study-only review
(#12), digest rotation/cap (#13), the dashboard revisit endpoint (#14) and Telegram send-failure
handling (#16).

Hermetic: temp DB, stub mode, Logseq off, auth off, and `telegram._call` replaced with a
recording fake — nothing touches the network, the real DB or a real Logseq graph. Run:
    python tests/test_digest.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):    # Windows console can't print the emoji Telegram messages carry
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="cde_digest_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "x"
os.environ["LOGSEQ_GRAPH_DIR"] = ""               # never write to a real graph
os.environ["APP_PASSWORD"] = ""                   # auth off
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "777"   # isolate from a real .env
os.environ["REMIND_CHAT_ID"] = ""
os.environ["LOGSEQ_TODO_CATEGORIES"] = "Learning,Reading"
os.environ["REVISIT_SCHEDULE"] = "1,3,7"
os.environ["DIGEST_LOOKAHEAD_DAYS"] = "7"
os.environ["DIGEST_MAX_CARDS"] = "10"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, revisit, telegram   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


_calls = []
_fail = {"on": False}
_next_mid = [100]


def _fake_call(method, **params):
    if _fail["on"]:
        raise RuntimeError("HTTP Error 400: Bad Request: simulated failure")
    _calls.append((method, params))
    _next_mid[0] += 1
    return {"ok": True, "result": {"message_id": _next_mid[0]}}


telegram._call = _fake_call


def _captured(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


# =========================================================================
print("\n=== #16: send_message / edit_message_text report failures ===\n")

_calls.clear()
check("send_message returns truthy on success", bool(telegram.send_message(777, "hi")))
check("edit_message_text returns truthy on success", bool(telegram.edit_message_text(777, 1, "hi")))

_fail["on"] = True
r1, out1 = _captured(telegram.send_message, 777, "hello")
r2, out2 = _captured(telegram.edit_message_text, 777, 1, "hello")
_fail["on"] = False
check("send_message returns falsy when _call raises", not r1, repr(r1))
check("...and logs a [telegram] line with the method and error",
      "[telegram]" in out1 and "sendMessage" in out1 and "simulated failure" in out1, out1)
check("edit_message_text returns falsy when _call raises", not r2, repr(r2))
check("...and logs a [telegram] line with the method and error",
      "[telegram]" in out2 and "editMessageText" in out2 and "simulated failure" in out2, out2)

# Telegram answers some errors with HTTP 200 + {"ok": false}
telegram._call = lambda m, **p: {"ok": False, "description": "Bad Request: chat not found"}
r3, out3 = _captured(telegram.send_message, 777, "hello")
telegram._call = _fake_call
check("an {'ok': false} response also counts as a failure (and is logged)",
      not r3 and "chat not found" in out3, out3)

_calls.clear()
long_text = "\n".join(f"line {i} " + "x" * 80 for i in range(120))    # ~10.5k chars
ok = telegram.send_message(777, long_text, {"inline_keyboard": [[{"text": "a", "callback_data": "b"}]]})
texts = [p["text"] for m, p in _calls if m == "sendMessage"]
check("over-long send_message is split into several sends", ok and len(texts) >= 3, len(texts))
check("...each <= 4096 chars", all(len(t) <= 4096 for t in texts), [len(t) for t in texts])
check("...with no content lost", "".join(texts).replace("\n", "") == long_text.replace("\n", ""))
check("...and the keyboard only on the last chunk",
      [("reply_markup" in p) for m, p in _calls] == [False] * (len(texts) - 1) + [True])

_calls.clear()
telegram.edit_message_text(777, 1, "y" * 5000)
check("over-long edit_message_text is truncated to <= 4096",
      _calls and len(_calls[-1][1]["text"]) <= 4096, _calls and len(_calls[-1][1]["text"]))



# ---- helpers for seeding study items ----
def _seed(title, category="Reading", deadline_days=-1, url=None):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,raw_url,status,title,category,learn_status,revisit_stage,"
                "deadline,source_chat_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, config.USER_ID, url or f"https://example.com/{iid[:8]}", "ACTIONABLE", title, category,
                 "active", 0, revisit._future(deadline_days), "777", db.now(), db.now()))
    con.commit()
    con.close()
    return iid


def _item(iid):
    con = db.connect()
    row = dict(con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
    con.close()
    return row


def _reset_items():
    con = db.connect()
    con.execute("DELETE FROM revisits")
    con.execute("DELETE FROM items")
    con.commit()
    con.close()


# =========================================================================
print("\n=== #16: schedulers only record 'sent' when the send succeeded ===\n")

_reset_items()
a = _seed("Overdue A")
revisit._scan_once(lambda it: False)
check("_scan_once does NOT mark an item reminded when its send failed", _item(a)["reminded_at"] is None)
revisit._scan_once(lambda it: True)
check("_scan_once marks it reminded when the send succeeded", _item(a)["reminded_at"] is not None)

_fail["on"] = True
r, _ = _captured(telegram.send_reminder, _item(a))
_fail["on"] = False
check("send_reminder returns falsy when the send fails", not r)
check("send_reminder returns truthy when the send succeeds", bool(telegram.send_reminder(_item(a))))

old_ts = revisit._future(-(config.DIGEST_INTERVAL_DAYS + 1))
revisit.set_meta("last_digest_sent", old_ts)
revisit._digest_tick(lambda: False)
check("_digest_tick does NOT advance last_digest_sent when the digest failed",
      revisit.get_meta("last_digest_sent") == old_ts)
revisit._digest_tick(lambda: True)
check("_digest_tick advances last_digest_sent when the digest was sent",
      revisit.get_meta("last_digest_sent") > old_ts)

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

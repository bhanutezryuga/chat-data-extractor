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
    detail = str(detail).replace("\n", " | ")[:300] if detail != "" else ""
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

# =========================================================================
print("\n=== #10: digest = header + one card per item with action buttons ===\n")


def _sends():
    return [p for m, p in _calls if m == "sendMessage"]


def _card_for(iid):
    """The recorded sendMessage whose keyboard targets this item, plus the message_id it got."""
    for p in _sends():
        kb = json.loads(p.get("reply_markup") or "{}")
        datas = [b["callback_data"] for row in kb.get("inline_keyboard", []) for b in row]
        if any(d.startswith(f"rv|{iid}|") for d in datas):
            return p, datas
    return None, []


def _tap(card, data, mid=555):
    telegram.handle_update({"callback_query": {"id": "cb1", "data": data, "message": {
        "chat": {"id": 777}, "message_id": mid, "text": card["text"]}}})


def _review_titles():
    _calls.clear()
    telegram.handle_update({"message": {"chat": {"id": 777}, "message_id": 9, "text": "/review"}})
    return [p["text"] for p in _sends()]


_reset_items()
a = _seed("Decorators deep dive", url="https://example.com/decorators")
b = _seed("SQL window functions", category="Learning")
_seed("Pasta recipe", category="Cooking")                      # non-study: never reviewed (#12)
_calls.clear()
check("send_review_digest returns truthy when delivered", bool(telegram.send_review_digest(chat_id=777)))
sends = _sends()
check("first message is a header (no buttons) with the due count",
      sends and "Weekly review" in sends[0]["text"] and "2" in sends[0]["text"]
      and "reply_markup" not in sends[0], sends and sends[0])
card, datas = _card_for(a)
check("each due item gets its own card with Revisited/Snooze/Learned buttons",
      card is not None and datas == [f"rv|{a}|revisited", f"rv|{a}|snoozed", f"rv|{a}|learned"], datas)
check("card shows title, category and URL",
      card and "Decorators deep dive" in card["text"] and "Reading" in card["text"]
      and "https://example.com/decorators" in card["text"], card and card["text"])
check("a card for the other study item too", _card_for(b)[0] is not None)
check("non-study items get no card", len(sends) == 3, len(sends))

_calls.clear()
_tap(card, f"rv|{a}|revisited")
con = db.connect()
nrev = con.execute("SELECT count(*) n FROM revisits WHERE item_id=?", (a,)).fetchone()["n"]
con.close()
check("tapping a digest card's button writes a revisits row", nrev == 1, nrev)
check("...and moves the deadline into the future", _item(a)["deadline"] > db.now(), _item(a)["deadline"])
edits = [p for m, p in _calls if m == "editMessageText"]
check("...and edits that card in place (buttons removed)",
      edits and edits[0]["message_id"] == 555 and "reply_markup" not in edits[0]
      and "Decorators deep dive" in edits[0]["text"], edits)

texts = _review_titles()
check("the next /review no longer lists the revisited item",
      not any("Decorators deep dive" in t for t in texts) and any("SQL window functions" in t for t in texts),
      texts)
card_b, _ = _card_for(b)
_tap(card_b, f"rv|{b}|learned")
check("learned from a digest card stops reviews", _item(b)["learn_status"] == "learned")
texts = _review_titles()
check("/review with nothing due says so", len(texts) == 1 and "nothing due" in texts[0], texts)

# over-long titles still fit in one Telegram message
_reset_items()
_seed("T" * 5000)
_calls.clear()
telegram.send_review_digest(chat_id=777)
check("every digest message is <= 4096 chars", all(len(p["text"]) <= 4096 for p in _sends()),
      [len(p["text"]) for p in _sends()])

# a failed digest isn't reported as delivered and doesn't consume rotation
_fail["on"] = True
r, _ = _captured(telegram.send_review_digest, 777)
_fail["on"] = False
check("send_review_digest returns falsy when the send fails", not r)

# =========================================================================
print("\n=== #13: capped cards + rotation through the backlog ===\n")

_reset_items()
ids = [_seed(f"Backlog item {i:02d}", deadline_days=-30 + i) for i in range(25)]
_calls.clear()
telegram.send_review_digest(chat_id=777)
head = _sends()[0]["text"]
shown1 = {i for i in ids if _card_for(i)[0]}
check("digest shows at most DIGEST_MAX_CARDS cards", len(shown1) == config.DIGEST_MAX_CARDS == 10, len(shown1))
check("header states the total and how many are shown", "25" in head and "10" in head, head)
check("header says how to see the rest (/review)", "/review" in head, head)
_calls.clear()
telegram.send_review_digest(chat_id=777)
shown2 = {i for i in ids if _card_for(i)[0]}
check("two consecutive digests with no action show different items", shown1.isdisjoint(shown2),
      len(shown1 & shown2))
_calls.clear()
telegram.send_review_digest(chat_id=777)
shown3 = {i for i in ids if _card_for(i)[0]}
check("every due item appears within ceil(N/cap) digests", shown1 | shown2 | shown3 == set(ids),
      len(set(ids) - (shown1 | shown2 | shown3)))

# snoozed items must not hog the front of the queue
_reset_items()
s = _seed("Snoozed one", deadline_days=-40)
revisit.mark(s, "snoozed")
con = db.connect()
con.execute("UPDATE items SET deadline=?, reminded_at=? WHERE id=?",               # snoozed a day ago;
            (revisit._future(-0.01), revisit._future(-1), s))                     # the snooze has elapsed
con.commit()
con.close()
fresh = [_seed(f"Fresh {i:02d}", deadline_days=-2) for i in range(12)]
_calls.clear()
telegram.send_review_digest(chat_id=777)
check("an elapsed snooze doesn't jump ahead of never-shown items", _card_for(s)[0] is None)
_calls.clear()
telegram.send_review_digest(chat_id=777)
check("...but it still comes round in a later digest", _card_for(s)[0] is not None)

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

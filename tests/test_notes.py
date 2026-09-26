"""Tests for manually-typed notes: pipeline.create_note(), the rule_note seed row it depends on
(items.rule_id has a FK to rules), and the Telegram `/note <text>` command built on top of it.

Hermetic: temp DB, stub mode, auth off, Logseq off, no real Telegram/Gemini network. Run:
    python tests/test_notes.py
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):    # Windows console (cp1252) can't print the emoji/em-dashes
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="cde_notes_")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""    # never write to a real graph
os.environ["APP_PASSWORD"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, gemini, pipeline, rules, telegram   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _item_row(item_id):
    con = db.connect()
    row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def _extraction_row(item_id):
    con = db.connect()
    row = con.execute("SELECT * FROM extractions WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
                      (item_id,)).fetchone()
    con.close()
    return dict(row) if row else None


# =========================================================================
# rule_note seed row (satisfies items.rule_id's FK; must never be reachable via classify())
# =========================================================================
print("\n=== rule_note seed row ===\n")

con = db.connect()
rule_note = con.execute("SELECT * FROM rules WHERE id='rule_note'").fetchone()
con.close()
check("rule_note exists after db.init()", rule_note is not None)
if rule_note:
    check("rule_note is disabled (enabled=0)", rule_note["enabled"] == 0, rule_note["enabled"])
    check("rule_note's content_type is 'note'", rule_note["content_type"] == "note")

con = db.connect()
for url in ("https://example.com/anything", "https://www.instagram.com/reel/abc123/",
           "https://youtube.com/watch?v=xyz", "not-a-url-at-all", ""):
    matched = rules.classify(con, url)
    check(f"classify({url!r}) never returns rule_note",
          matched is None or matched["id"] != "rule_note",
          matched["id"] if matched else None)
con.close()


# =========================================================================
# pipeline.create_note()
# =========================================================================
print("\n=== pipeline.create_note() ===\n")

check("empty string -> None, nothing saved", pipeline.create_note("") is None)
check("whitespace-only -> None, nothing saved", pipeline.create_note("   \n\t  ") is None)
check("None -> None, nothing saved", pipeline.create_note(None) is None)

NOTE_TEXT = "Ping the landlord about the leak before Friday."
r = pipeline.create_note(NOTE_TEXT, source_chat_id="123", source_msg_id="note1")
check("a real note returns a dict with id/status", bool(r) and "id" in r and r.get("status") == "ACTIONABLE", r)

item = _item_row(r["id"]) if r else None
check("item status is ACTIONABLE", item and item["status"] == "ACTIONABLE", item and item["status"])
check("item action is forced to 'note'", item and item["action"] == "note", item and item["action"])
check("item content_type is 'note'", item and item["content_type"] == "note", item and item["content_type"])
check("item rule_id is 'rule_note'", item and item["rule_id"] == "rule_note", item and item["rule_id"])
check("item raw_text is the verbatim input", item and item["raw_text"] == NOTE_TEXT, item and item["raw_text"])
check("item has no raw_url (no link involved)", item and item["raw_url"] is None, item and item["raw_url"])

con = db.connect()
valid_categories = {row["name"] for row in con.execute("SELECT name FROM categories")}
con.close()
check("a category was assigned from the seeded taxonomy",
      r.get("category") in valid_categories, r.get("category"))
check("create_note's return dict carries the same title as the DB row",
      r.get("title") == (item and item.get("title")), (r.get("title"), item and item.get("title")))

ex = _extraction_row(r["id"]) if r else None
check("extraction summary is forced to the verbatim note text (never paraphrased/dropped)",
      ex and ex["summary"] == NOTE_TEXT, ex and ex["summary"])
check("extraction list_items is forced empty (a note is prose, not a list)",
      ex and ex["list_items"] == "[]", ex and ex["list_items"])
check("extraction translation is forced empty",
      ex and (ex["translation"] or "") == "", ex and ex["translation"])
check("extraction recipe is forced empty",
      ex and ex["recipe"] in ("{}", None), ex and ex["recipe"])

# duplicate detection: a second, identical note should be recognized as a content duplicate
r2 = pipeline.create_note(NOTE_TEXT, source_chat_id="123", source_msg_id="note2")
check("an identical second note becomes a DUPLICATE",
      bool(r2) and r2.get("status") == "DUPLICATE", r2)
check("the duplicate points back at the original note's id",
      r2 and r2.get("duplicate_of") == r["id"], r2)

# a sufficiently different note is NOT treated as a duplicate
r3 = pipeline.create_note("Buy oat milk and check the tyre pressure this weekend.",
                          source_chat_id="123", source_msg_id="note3")
check("a genuinely different note is saved as its own ACTIONABLE item",
      bool(r3) and r3.get("status") == "ACTIONABLE" and r3.get("id") != r["id"], r3)

# Gemini raising mid-extract must fall back to the offline stub, never lose the note
_orig_use_gemini = config.USE_GEMINI
_orig_extract = gemini.extract


def _boom(*a, **k):
    raise RuntimeError("simulated Gemini outage")


config.USE_GEMINI = True
gemini.extract = _boom
try:
    r4 = pipeline.create_note("Note written while Gemini is down.", source_chat_id="123", source_msg_id="note4")
    check("a Gemini exception mid-extract still saves the note (falls back to the stub)",
          bool(r4) and r4.get("status") == "ACTIONABLE", r4)
    item4 = _item_row(r4["id"]) if r4 else None
    check("...and the item still ends up ACTIONABLE with action='note' in the DB",
          item4 and item4["status"] == "ACTIONABLE" and item4["action"] == "note", item4)
    ex4 = _extraction_row(r4["id"]) if r4 else None
    check("...and the summary is still the verbatim text despite the Gemini failure",
          ex4 and ex4["summary"] == "Note written while Gemini is down.", ex4 and ex4["summary"])
finally:
    config.USE_GEMINI = _orig_use_gemini
    gemini.extract = _orig_extract


# =========================================================================
# Telegram `/note <text>` command
# =========================================================================
print("\n=== telegram.py: /note command ===\n")

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

_tg_calls.clear()
telegram.handle_update(_msg(777, "/note"))
check("/note with no body sends a usage hint, not an error",
      any("send your note" in t.lower() for t in _sent_texts(777)), _sent_texts(777))

_tg_calls.clear()
telegram.handle_update(_msg(777, "/note   "))
check("/note with only whitespace also sends the usage hint",
      any("send your note" in t.lower() for t in _sent_texts(777)), _sent_texts(777))

_tg_calls.clear()
telegram.handle_update(_msg(777, "/note Call the dentist about the appointment next week.", msg_id=2))
sent = _sent_texts(777)
check("/note <text> replies with a saved confirmation",
      any("note saved" in t.lower() for t in sent), sent)
con = db.connect()
saved = con.execute(
    "SELECT * FROM items WHERE raw_text='Call the dentist about the appointment next week.'").fetchone()
con.close()
check("the note text was actually saved to the DB", saved is not None)
if saved:
    check("...with status ACTIONABLE and action note", saved["status"] == "ACTIONABLE" and saved["action"] == "note")

# sending the exact same note again via Telegram should surface as a duplicate, not a second save
_tg_calls.clear()
telegram.handle_update(_msg(777, "/note Call the dentist about the appointment next week.", msg_id=3))
check("/note with duplicate text tells the user it's already saved",
      any("already saved" in t.lower() for t in _sent_texts(777)), _sent_texts(777))

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

"""Tests for the PKM / spaced-repetition revisit feature.

Hermetic: temp DB + stub mode (no Gemini, no network). Exercises knowledge-record
population, collection materialization, the revisit schedule (advance/snooze/learned),
and the due-item query. Run:
    python tests/test_revisit.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="cde_revisit_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""        # stub mode
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""       # NEVER write to a real graph from tests (isolate from .env)
os.environ["REVISIT_SCHEDULE"] = "1,3,7"  # deterministic 3-stage schedule
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, pipeline, revisit   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _make_item(url="https://shop.example.com/x"):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,raw_url,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (iid, config.USER_ID, url, "ACTIONABLE", db.now(), db.now()))
    con.commit()
    con.close()
    return iid


def _item(iid):
    con = db.connect()
    row = dict(con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
    con.close()
    return row


print("\n=== PKM / revisit feature ===\n")

# --- taxonomy seeded ---
con = db.connect()
cat_names = {r["name"] for r in con.execute("SELECT name FROM categories")}
wishlist = con.execute("SELECT collection FROM categories WHERE name='Shopping'").fetchone()["collection"]
con.close()
check("category taxonomy seeded (9 rows)", len(cat_names) == 9, str(len(cat_names)))
check("Shopping maps to Wishlist collection", wishlist == "Wishlist", str(wishlist))

# --- knowledge record population + collection materialization ---
iid = _make_item()
data = {
    "title": "Best mechanical keyboards 2026",
    "category": "shopping",   # lower-case on purpose: matching is case-insensitive
    "task": {"title": "Pick a keyboard", "priority": "high"},
    "list_items": [
        {"name": "Keychron K2", "note": "hot-swappable", "link": "keychron.com/k2"},
        {"name": "NuPhy Air75", "note": "low profile", "link": "link not available"},
    ],
}
con = db.connect()
pipeline._write_knowledge(con, iid, data)
con.commit()
con.close()
it = _item(iid)
check("title populated", it["title"] == "Best mechanical keyboards 2026", it["title"])
check("source domain parsed (www stripped)", it["source"] == "shop.example.com", it["source"])
check("category matched case-insensitively", it["category"] == "Shopping", it["category"])
check("priority mirrors task priority (upper)", it["priority"] == "HIGH", it["priority"])

con = db.connect()
coll = [dict(r) for r in con.execute("SELECT * FROM collection_items WHERE item_id=?", (iid,))]
con.close()
check("two collection items materialized", len(coll) == 2, str(len(coll)))
check("all in Wishlist collection", all(c["collection"] == "Wishlist" for c in coll))
check("real link kept", any(c["link"] == "keychron.com/k2" for c in coll))
check("'link not available' stored as NULL", any(c["name"] == "NuPhy Air75" and c["link"] is None for c in coll))

# --- revisit scheduling ---
check("new item scheduled at stage 0", it["revisit_stage"] == 0 and it["deadline"] is not None)
check("first deadline ~1 day out (schedule[0]=1)", (it["deadline"] or "")[:10] >= db.now()[:10])
check("starts active", it["learn_status"] == "active")

# schedule_new must be idempotent (never resets a user's progress on reprocess)
first_deadline = it["deadline"]
con = db.connect()
revisit.schedule_new(con, iid)
con.commit()
con.close()
check("schedule_new idempotent", _item(iid)["deadline"] == first_deadline)

# --- advance the schedule ---
res = revisit.mark(iid, "revisited")
it = _item(iid)
check("revisited advances stage -> 1", it["revisit_stage"] == 1, str(it["revisit_stage"]))
check("revisit_count incremented", it["revisit_count"] == 1)
check("progress advanced", it["progress"] > 0, str(it["progress"]))
check("reminded_at cleared on revisit", it["reminded_at"] is None)

# --- snooze ---
revisit.mark(iid, "snoozed")
it = _item(iid)
check("snooze keeps stage", it["revisit_stage"] == 1)
check("snooze keeps it active with a deadline", it["learn_status"] == "active" and it["deadline"])

# --- learned stops reminders ---
revisit.mark(iid, "learned")
it = _item(iid)
check("learned clears deadline", it["deadline"] is None)
check("learned sets status + full progress", it["learn_status"] == "learned" and it["progress"] == 100)

# --- audit trail ---
con = db.connect()
actions = [r["action"] for r in con.execute("SELECT action FROM revisits WHERE item_id=? ORDER BY created_at", (iid,))]
con.close()
check("revisits logged (revisited,snoozed,learned)", actions == ["revisited", "snoozed", "learned"], str(actions))

# --- unknown category falls back to Other (no collection) ---
iid2 = _make_item("https://www.example.org/thing")
con = db.connect()
pipeline._write_knowledge(con, iid2, {"title": "?", "category": "Nonsense", "list_items": [{"name": "x"}]})
con.commit()
n_coll = con.execute("SELECT count(*) n FROM collection_items WHERE item_id=?", (iid2,)).fetchone()["n"]
con.close()
check("unknown category -> Other", _item(iid2)["category"] == "Other")
check("Other has no collection -> no collection items", n_coll == 0, str(n_coll))
check("www. stripped from source", _item(iid2)["source"] == "example.org")

# --- due query ---
con = db.connect()
con.execute("UPDATE items SET deadline='2000-01-01 00:00:00' WHERE id=?", (iid2,))  # far past
con.commit()
due_ids = {r["id"] for r in revisit.due(con)}
con.close()
check("past-deadline active item is due", iid2 in due_ids)
check("learned item is not due", iid not in due_ids)

# --- due_count reflects it ---
con = db.connect()
dc = revisit.due_count(con)
con.close()
check("due_count >= 1", dc >= 1, str(dc))

# --- weekly digest: due_within window + digest timing ---
from datetime import datetime, timezone, timedelta   # noqa: E402


def _set_deadline(iid, days):
    con = db.connect()
    dl = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("UPDATE items SET deadline=?, learn_status='active' WHERE id=?", (dl, iid))
    con.commit()
    con.close()


soon = _make_item("https://d/soon"); _set_deadline(soon, 3)     # due in 3 days
far = _make_item("https://d/far");  _set_deadline(far, 20)      # due in 20 days
over = _make_item("https://d/over"); _set_deadline(over, -1)    # overdue
con = db.connect()
within = {r["id"] for r in revisit.due_within(con, 7)}
con.close()
check("due_within(7) includes items due soon + overdue", soon in within and over in within)
check("due_within(7) excludes far-future items", far not in within)

check("meta get/set roundtrips", (revisit.set_meta("t", "v") or True) and revisit.get_meta("t") == "v")
con = db.connect(); con.execute("DELETE FROM meta WHERE key='last_digest_sent'"); con.commit(); con.close()
check("_digest_due seeds on first run and returns False",
      revisit._digest_due() is False and revisit.get_meta("last_digest_sent"))
revisit.set_meta("last_digest_sent", db.now())
check("_digest_due False right after a send", revisit._digest_due() is False)
old = (datetime.now(timezone.utc) - timedelta(days=config.DIGEST_INTERVAL_DAYS + 1)).strftime("%Y-%m-%d %H:%M:%S")
revisit.set_meta("last_digest_sent", old)
check("_digest_due True once the interval has elapsed", revisit._digest_due() is True)

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

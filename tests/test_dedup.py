"""Tests for content-level dedup (pipeline.content_fingerprint / _write_knowledge).

Hermetic: temp DB, stub mode, Logseq off (so nothing can touch a real graph). Covers the
fingerprint's invariance (order/case/punctuation), category scoping, and the end-to-end
behaviour: a second capture of the same material — even from a different URL — is marked
DUPLICATE, keeps no collection rows, and gets no revisit schedule. Run:  python tests/test_dedup.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="cde_dedup_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""                    # never write to a real graph
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, pipeline   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _mk_item(url):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,raw_url,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (iid, config.USER_ID, url, "PROCESSING", db.now(), db.now()))
    con.commit()
    con.close()
    return iid


def _process(iid, data):
    """Mimic _write_result's order: item is already ACTIONABLE when _write_knowledge runs."""
    con = db.connect()
    con.execute("UPDATE items SET status='ACTIONABLE' WHERE id=?", (iid,))
    dup = pipeline._write_knowledge(con, iid, data)
    con.commit()
    row = dict(con.execute("SELECT status, deadline, content_key FROM items WHERE id=?", (iid,)).fetchone())
    ncoll = con.execute("SELECT count(*) n FROM collection_items WHERE item_id=?", (iid,)).fetchone()["n"]
    con.close()
    return dup, row, ncoll


print("\n=== content dedup ===\n")

BOOKS = {"title": "Book Recommendations for Guys", "category": "Reading",
         "list_items": [{"name": "Red Rising by Pierce Brown"},
                        {"name": "Billy Summers by Stephen King"},
                        {"name": "11/22/63 by Stephen King"}]}
# Same three books, reordered + different case/punctuation + a different Gemini title.
BOOKS_RESHARE = {"title": "Books Every Guy Should Read", "category": "Reading",
                 "list_items": [{"name": "11-22-63  by  stephen king"},
                                {"name": "RED RISING by Pierce Brown"},
                                {"name": "Billy Summers, by Stephen King"}]}
OTHER = {"title": "Cozy fantasy picks", "category": "Reading",
         "list_items": [{"name": "Legends & Lattes"}, {"name": "The House in the Cerulean Sea"}]}

# ---------- fingerprint properties ----------
fp = pipeline.content_fingerprint
check("fingerprint: order/case/punctuation invariant", fp(BOOKS, "Reading") == fp(BOOKS_RESHARE, "Reading"))
check("fingerprint: different list -> different key", fp(BOOKS, "Reading") != fp(OTHER, "Reading"))
check("fingerprint: category-scoped", fp(BOOKS, "Reading") != fp(BOOKS, "Watching"))
check("fingerprint: title-based when no list", fp({"title": "How to sharpen a knife"}, "Reference")
      == fp({"title": "how to SHARPEN a knife"}, "Reference"))
check("fingerprint: too-thin title -> None (no dedup)", fp({"title": "hi"}, "Other") is None)
check("fingerprint: empty -> None", fp({}, "Other") is None)

# ---------- end-to-end: original then a reshare from a different URL ----------
a = _mk_item("https://instagram.com/reel/AAA")
dup_a, row_a, ncoll_a = _process(a, BOOKS)
check("original: not a duplicate", dup_a is None)
check("original: stays ACTIONABLE with a content_key", row_a["status"] == "ACTIONABLE" and row_a["content_key"])
check("original: collection rows materialized", ncoll_a == 3)
check("original: revisit scheduled (deadline set)", bool(row_a["deadline"]))

b = _mk_item("https://instagram.com/reel/BBB")          # different URL, same content
dup_b, row_b, ncoll_b = _process(b, BOOKS_RESHARE)
check("reshare: flagged as duplicate of the original", dup_b == a)
check("reshare: status -> DUPLICATE", row_b["status"] == "DUPLICATE")
check("reshare: no collection rows kept", ncoll_b == 0)
check("reshare: not scheduled for revisit (deadline cleared)", row_b["deadline"] is None)

# ---------- a genuinely different list is NOT deduped ----------
c = _mk_item("https://instagram.com/reel/CCC")
dup_c, row_c, _ = _process(c, OTHER)
check("different content: not a duplicate", dup_c is None and row_c["status"] == "ACTIONABLE")

# ---------- duplicates point at the original, never at each other ----------
d = _mk_item("https://instagram.com/reel/DDD")          # a third capture of the same books
dup_d, _, _ = _process(d, BOOKS)
check("second reshare: still points at the ORIGINAL (not the first dup)", dup_d == a)

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

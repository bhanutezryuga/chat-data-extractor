"""Tests for the Logseq export (Phase 1 — write). See docs/LOGSEQ_PLAN.md.

Hermetic: temp DB + temp graph dir + stub mode (no Gemini/network). Verifies the pure
Markdown mapping, page + journal writing, journal idempotency, and — critically — that a
user's `## Notes` subtree survives a re-export. Run:  python tests/test_logseq.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="cde_logseq_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = os.path.join(_TMP, "graph")
os.environ["LOGSEQ_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, pipeline, logseq   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _seed_item(url="https://shop.example.com/keebs", category="shopping", with_list=True):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,raw_url,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (iid, config.USER_ID, url, "ACTIONABLE", db.now(), db.now()))
    con.commit()
    con.close()
    con = db.connect()
    con.execute("INSERT INTO extractions (id,item_id,summary,key_points,list_items,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (db.new_id(), iid, "Two great keyboards.", '["hot-swap","low profile"]', "[]", db.now()))
    con.commit()
    con.close()
    data = {"title": "Best keyboards 2026", "category": category,
            "task": {"title": "Pick a keyboard", "priority": "high"},
            "list_items": ([{"name": "Keychron K2", "note": "hot-swap", "link": "keychron.com/k2"},
                            {"name": "NuPhy Air75", "note": "low profile", "link": "link not available"}]
                           if with_list else [])}
    con = db.connect()
    pipeline._write_knowledge(con, iid, data)
    con.commit()
    con.close()
    return iid


def _page_path(iid):
    con = db.connect()
    item = dict(con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())
    con.close()
    return os.path.join(config.LOGSEQ_GRAPH_DIR, "pages", logseq._page_filename(item))


print("\n=== Logseq export (Phase 1) ===\n")

# --- pure render_page ---
item = {"id": "abcd1234ef", "title": "Systems thinking", "category": "Reading",
        "source": "example.com", "raw_url": "https://example.com/x",
        "created_at": "2026-08-07 10:00:00", "learn_status": "active",
        "deadline": "2026-08-08 10:00:00", "content_type": "article"}
ex = {"summary": "A good read.", "key_points": '["a","b"]', "translation": ""}
coll = [{"id": "c1", "collection": "To-Read", "name": "Thinking in Systems",
         "note": "Meadows", "link": "ex.com/tis", "done": 0},
        {"id": "c2", "collection": "To-Read", "name": "Fifth Discipline",
         "note": None, "link": None, "done": 1}]
md = logseq.render_page(item, ex, coll, {"title": "Read these"})
check("render: item-id property present", "item-id:: abcd1234ef" in md)
check("render: title property", "title:: Systems thinking" in md)
check("render: category as [[link]]", "category:: [[Reading]]" in md)
check("render: url + source", "url:: https://example.com/x" in md and "source:: example.com" in md)
check("render: revisit-next date + SCHEDULED", "revisit-next:: [[2026-08-08]]" in md and "SCHEDULED: <2026-08-08" in md)
check("render: collection heading is a [[page]]", "- ## [[To-Read]]" in md)
check("render: undone item -> TODO, done -> DONE", "TODO Thinking in Systems" in md and "DONE Fifth Discipline" in md)
check("render: cid on collection blocks", "cid:: c1" in md and "cid:: c2" in md)
check("render: summary + key points sections", "## Summary" in md and "- b" in md)
check("render: user Notes area present", logseq.NOTES_MARKER in md)

# learned item -> no scheduling
md2 = logseq.render_page({**item, "learn_status": "learned", "deadline": None}, ex, [], {})
check("render: learned item drops revisit-next/SCHEDULED", "revisit-next" not in md2 and "SCHEDULED" not in md2)
check("render: status reflects learned", "status:: learned" in md2)

# --- export_item writes real files ---
iid = _seed_item()
ok = logseq.export_item(iid)
path = _page_path(iid)
check("export_item returns True + writes a page", ok and os.path.exists(path))
page = open(path, encoding="utf-8").read()
check("page file has the knowledge record", "item-id:: " + iid in page and "category:: [[Shopping]]" in page)
check("page file has the Wishlist collection + cids", "[[Wishlist]]" in page and "cid:: " in page)
check("page: 'link not available' -> no link line for that item", page.count("link:: ") == 1)

# --- journal breadcrumb + idempotency ---
jdir = os.path.join(config.LOGSEQ_GRAPH_DIR, "journals")
jfile = os.path.join(jdir, db.now()[:10].replace("-", "_") + ".md")
check("journal breadcrumb written", os.path.exists(jfile) and "Captured [[Best keyboards 2026]]" in open(jfile, encoding="utf-8").read())
logseq.export_item(iid)  # re-export
crumbs = open(jfile, encoding="utf-8").read().count("Captured [[Best keyboards 2026]]")
check("journal breadcrumb is idempotent (no dup on re-export)", crumbs == 1, f"count={crumbs}")

# --- user Notes preserved across re-export ---
page = open(path, encoding="utf-8").read()
page_with_note = page.rstrip() + "\n\t- MY PRIVATE NOTE: buy the K2\n"
open(path, "w", encoding="utf-8").write(page_with_note)
logseq.export_item(iid)  # re-export must NOT clobber the note
after = open(path, encoding="utf-8").read()
check("re-export preserves user's ## Notes edits", "MY PRIVATE NOTE: buy the K2" in after)

# --- disabled -> no-op ---
old = config.LOGSEQ_GRAPH_DIR
config.LOGSEQ_GRAPH_DIR = ""
check("disabled (no graph dir) -> export_item no-ops", logseq.export_item(iid) is False and not logseq.active())
config.LOGSEQ_GRAPH_DIR = old

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

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


# ================= Phase 2: read-back (Logseq -> DB) =================
print("\n--- Phase 2: two-way read-back ---")


def _edit(path, old_s, new_s):
    txt = open(path, encoding="utf-8").read().replace(old_s, new_s, 1)
    open(path, "w", encoding="utf-8").write(txt)
    mt = os.path.getmtime(path) + 10          # force a newer mtime so the watcher detects the change
    os.utime(path, (mt, mt))


# pure parse
sample = ("item-id:: XYZ\nstatus:: learned\n\n- ## [[Wishlist]]\n"
          "\t- DONE Alpha\n\t  cid:: c1\n\t- TODO Beta\n\t  cid:: c2\n")
p = logseq.parse_page(sample)
check("parse_page: item-id + status", p["item_id"] == "XYZ" and p["status"] == "learned")
check("parse_page: per-block done via cid", p["coll"] == {"c1": True, "c2": False}, str(p["coll"]))

# end-to-end sync
iid2 = _seed_item(url="https://shop.example.com/p2")
logseq.export_item(iid2)
path2 = _page_path(iid2)
con = db.connect()
cids = {r["name"]: r["id"] for r in con.execute("SELECT name, id FROM collection_items WHERE item_id=?", (iid2,))}
con.close()
logseq.sync_from_logseq()   # first pass records mtimes, no user change yet


def _done(cid):
    con = db.connect()
    v = con.execute("SELECT done FROM collection_items WHERE id=?", (cid,)).fetchone()["done"]
    con.close()
    return v


# user checks off "Keychron K2" in Logseq
_edit(path2, "- TODO Keychron K2", "- DONE Keychron K2")
r = logseq.sync_from_logseq()
check("read-back: checking a box -> collection_items.done=1", _done(cids["Keychron K2"]) == 1, str(r))
check("read-back: the other item stays unchecked", _done(cids["NuPhy Air75"]) == 0)

# user un-checks it
_edit(path2, "- DONE Keychron K2", "- TODO Keychron K2")
logseq.sync_from_logseq()
check("read-back: un-checking -> done=0", _done(cids["Keychron K2"]) == 0)

# user marks the whole item learned in Logseq
_edit(path2, "status:: active", "status:: learned")
logseq.sync_from_logseq()
con = db.connect()
it2 = con.execute("SELECT learn_status, deadline FROM items WHERE id=?", (iid2,)).fetchone()
con.close()
check("read-back: status:: learned -> learn_status=learned + deadline cleared",
      it2["learn_status"] == "learned" and it2["deadline"] is None)

# completion-only: a stale 'active' must NOT revert a learned item
_edit(path2, "status:: learned", "status:: active")
logseq.sync_from_logseq()
con = db.connect()
ls = con.execute("SELECT learn_status FROM items WHERE id=?", (iid2,)).fetchone()["learn_status"]
con.close()
check("read-back: never reverts learned -> active (completion-only)", ls == "learned", ls)

# unchanged file is skipped on the next pass (mtime state table)
r2 = logseq.sync_from_logseq()
check("read-back: unchanged files skipped (mtime state)", r2["scanned"] == 0, str(r2))

# conflicted files left by two-way sync (Syncthing copies, git merge markers) must be ignored
check("_is_conflicted flags conflict copies + git markers, not clean files",
      logseq._is_conflicted("p.sync-conflict-20260816.md", "") and
      logseq._is_conflicted("p.md", "a\n<<<<<<< HEAD\nb") and
      logseq._is_conflicted(".hidden.md", "") and
      not logseq._is_conflicted("p.md", "clean page"))
iid4 = _seed_item(url="https://shop.example.com/p4")
logseq.export_item(iid4)
logseq.sync_from_logseq()  # baseline (records mtimes)
con = db.connect()
cidK = con.execute("SELECT id FROM collection_items WHERE item_id=? AND name='Keychron K2'", (iid4,)).fetchone()["id"]
con.close()
pages_dir = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
_body = f"item-id:: {iid4}\n- ## [[Wishlist]]\n\t- DONE Keychron K2\n\t  cid:: {cidK}\n"  # would set done=1 if read
open(os.path.join(pages_dir, "p4.sync-conflict-20260816-000000-ABCDEF.md"), "w", encoding="utf-8").write(_body)
open(os.path.join(pages_dir, "p4-gitconflict.md"), "w", encoding="utf-8").write("<<<<<<< HEAD\n" + _body)
logseq.sync_from_logseq()
con = db.connect()
doneK = con.execute("SELECT done FROM collection_items WHERE id=?", (cidK,)).fetchone()["done"]
con.close()
check("read-back: conflict copy + git-marker file do NOT change DB state", doneK == 0, f"done={doneK}")


# ================= Phase 3: bulk export + reprocess preservation =================
print("\n--- Phase 3: export_all + reprocess preservation ---")

con = db.connect()
n_actionable = con.execute("SELECT count(*) n FROM items WHERE status='ACTIONABLE'").fetchone()["n"]
con.close()
exported = logseq.export_all()
check("export_all exports every ACTIONABLE item", exported == n_actionable and exported >= 2, f"{exported}/{n_actionable}")

# reprocess must NOT wipe a checked-off collection item (preserve done + stable cid)
iid3 = _seed_item(url="https://shop.example.com/p3")
con = db.connect()
cid_k = con.execute("SELECT id FROM collection_items WHERE item_id=? AND name='Keychron K2'", (iid3,)).fetchone()["id"]
con.execute("UPDATE collection_items SET done=1 WHERE id=?", (cid_k,))
con.commit(); con.close()
# re-run _write_knowledge with the SAME list_items (what reprocess does)
same = {"title": "Best keyboards 2026", "category": "shopping",
        "task": {"title": "Pick a keyboard", "priority": "high"},
        "list_items": [{"name": "Keychron K2", "note": "hot-swap", "link": "keychron.com/k2"},
                       {"name": "NuPhy Air75", "note": "low profile", "link": "link not available"}]}
con = db.connect(); pipeline._write_knowledge(con, iid3, same); con.commit(); con.close()
con = db.connect()
row = con.execute("SELECT id, done FROM collection_items WHERE item_id=? AND name='Keychron K2'", (iid3,)).fetchone()
n_now = con.execute("SELECT count(*) n FROM collection_items WHERE item_id=?", (iid3,)).fetchone()["n"]
con.close()
check("reprocess preserves checked-off done state", row["done"] == 1)
check("reprocess reuses the same cid (Logseq link stable)", row["id"] == cid_k)
check("reprocess doesn't duplicate collection rows", n_now == 2, str(n_now))

# export off -> export_all returns 0
old2 = config.LOGSEQ_GRAPH_DIR
config.LOGSEQ_GRAPH_DIR = ""
check("export_all no-ops when disabled", logseq.export_all() == 0)
config.LOGSEQ_GRAPH_DIR = old2

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

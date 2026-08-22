"""Tests for the Logseq export. See docs/LOGSEQ_PLAN.md.

Hermetic: temp DB + temp graph dir + stub mode (no Gemini/network). Covers the minimal page
format (title + tags only), study-gated TODO markers, bulk export, reprocess preservation, and
the retained (currently-unwired) read-back helpers. Run:  python tests/test_logseq.py
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
os.environ["LOGSEQ_JOURNAL"] = "1"                     # deterministic regardless of a real .env value
os.environ["LOGSEQ_TODO_CATEGORIES"] = "Learning,Reading"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, pipeline, logseq   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _seed_item(url="https://x.com/i", category="Learning"):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,raw_url,content_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (iid, config.USER_ID, url, "post", "ACTIONABLE", db.now(), db.now()))
    con.execute("INSERT INTO extractions (id,item_id,summary,key_points,list_items,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (db.new_id(), iid, "A concise but real summary of the captured item.", '["p1","p2"]', "[]", db.now()))
    con.commit()
    con.close()
    data = {"title": "Best keyboards 2026", "category": category,
            "task": {"title": "Pick a keyboard", "priority": "high", "tags": ["mechanical", "deep work"]},
            "list_items": [{"name": "Keychron K2", "note": "hot-swap", "link": "keychron.com/k2"},
                           {"name": "NuPhy Air75", "note": "low profile", "link": "link not available"}]}
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


print("\n=== Logseq export (minimal format) ===\n")

# ---------- pure render: STUDY item (Reading/Learning -> TODO) ----------
study_item = {"id": "abcd1234ef", "title": "Systems thinking", "category": "Reading",
              "source": "example.com", "raw_url": "https://example.com/x", "content_type": "article",
              "created_at": "2026-08-07 10:00:00", "learn_status": "active", "deadline": "2026-08-08 10:00:00"}
ex = {"summary": "A good read.", "key_points": '["a","b"]', "translation": ""}
coll = [{"id": "c1", "collection": "To-Read", "name": "Thinking in Systems", "note": "Meadows", "link": "ex.com/tis", "done": 0},
        {"id": "c2", "collection": "To-Read", "name": "Fifth Discipline", "note": None, "link": None, "done": 1}]
md = logseq.render_page(study_item, ex, coll, {"title": "Read these", "tags": '["complexity"]'})

check("render: title property present", "title:: Systems thinking" in md)
check("render: tags include category + content-type + gemini tag",
      "tags:: " in md and "Reading" in md.split("tags:: ")[1].split("\n")[0]
      and "article" in md and "complexity" in md)
for junk in ("item-id::", "url::", "source::", "status::", "captured::", "category::", "revisit-next::", "cid::"):
    check(f"render: NO {junk} clutter", junk not in md)
check("render: collection heading is a [[page]]", "- ## [[To-Read]]" in md)
check("render (study): items are TODO/DONE", "TODO Thinking in Systems" in md and "DONE Fifth Discipline" in md)
check("render (study): Task is a TODO", "## Task" in md and "TODO Read these" in md)
check("render (study): scheduled Revisit TODO", "TODO Revisit —" in md and "SCHEDULED: <2026-08-08" in md)
check("render: user Notes area present", logseq.NOTES_MARKER in md)
check("render: link kept / 'link not available' -> none", "link:: ex.com/tis" in md and md.count("link:: ") == 1)

# ---------- pure render: NON-STUDY item (song/recipe -> NO todo) ----------
song = {"id": "song1234", "title": "Lofi mix", "category": "Listening", "content_type": "reel",
        "created_at": "2026-08-07 10:00:00", "learn_status": "active", "deadline": "2026-08-08 10:00:00"}
scoll = [{"id": "s1", "collection": "To-Listen", "name": "Track A", "note": None, "link": None, "done": 0}]
smd = logseq.render_page(song, ex, scoll, {"title": "Listen later", "tags": '["chill"]'})
check("render (non-study): NO TODO/DONE markers", "TODO" not in smd and "DONE" not in smd)
check("render (non-study): NO Task section, NO SCHEDULED", "## Task" not in smd and "SCHEDULED" not in smd)
check("render (non-study): collection items are plain bullets", "\t- Track A" in smd)
check("render (non-study): still tagged for review", "tags:: " in smd and "Listening" in smd)

# ---------- export_item writes real files ----------
iid = _seed_item(category="Learning")
ok = logseq.export_item(iid)
path = _page_path(iid)
check("export_item writes a page", ok and os.path.exists(path))
page = open(path, encoding="utf-8").read()
check("page: minimal props (title + tags, nothing else)",
      page.startswith("title:: ") and "tags:: " in page.split("\n\n")[0]
      and "item-id::" not in page and "status::" not in page)
check("page (Learning=study): collection items are TODO", "TODO Keychron K2" in page)

# ---------- ## Notes preserved across re-export ----------
_edited = open(path, encoding="utf-8").read().rstrip() + "\n\t- MY NOTE\n"
open(path, "w", encoding="utf-8").write(_edited)
logseq.export_item(iid)
check("re-export preserves user's ## Notes edits", "MY NOTE" in open(path, encoding="utf-8").read())

# ---------- journal breadcrumb (idempotent) ----------
jfile = os.path.join(config.LOGSEQ_GRAPH_DIR, "journals", db.now()[:10].replace("-", "_") + ".md")
check("journal breadcrumb written", os.path.exists(jfile) and "Captured [[Best keyboards 2026]]" in open(jfile, encoding="utf-8").read())
logseq.export_item(iid)
check("journal breadcrumb idempotent", open(jfile, encoding="utf-8").read().count("Captured [[Best keyboards 2026]]") == 1)

# ---------- export_all ----------
con = db.connect(); n_actionable = con.execute("SELECT count(*) n FROM items WHERE status='ACTIONABLE'").fetchone()["n"]; con.close()
check("export_all exports every ACTIONABLE item", logseq.export_all() == n_actionable and n_actionable >= 1)

# ---------- duplicate title:: disambiguation (prevents Logseq "page already exists") ----------
_udir = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
open(os.path.join(_udir, "utitle-aaaaaaaa.md"), "w", encoding="utf-8").write("title:: Unique Test Title\n")
check("_unique_title: collision -> ' (2)'",
      logseq._unique_title("Unique Test Title", "utitle-bbbbbbbb.md", _udir) == "Unique Test Title (2)")
check("_unique_title: no collision -> base", logseq._unique_title("Totally Fresh XYZ", "x.md", _udir) == "Totally Fresh XYZ")
check("_unique_title: the item's own file is ignored",
      logseq._unique_title("Unique Test Title", "utitle-aaaaaaaa.md", _udir) == "Unique Test Title")

# ---------- content gate: don't export items with no extracted content ----------
check("_has_content: real summary -> True", logseq._has_content({"summary": "A meaningful summary of the article about vector databases."}))
check("_has_content: 'no content' refusal -> False",
      not logseq._has_content({"summary": "No content was provided in the prompt to summarize.",
                               "key_points": '["Unable to extract key points as no content was provided."]'}))
check("_has_content: empty -> False", not logseq._has_content({}))
check("_has_content: list_items make it exportable", logseq._has_content({"summary": "", "list_items": '[{"name":"x"}]'}))
_junk = db.new_id()
con = db.connect()
con.execute("INSERT INTO items (id,user_id,raw_url,status,category,title,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (_junk, config.USER_ID, "http://x", "ACTIONABLE", "Other", "Content Not Provided", db.now(), db.now()))
con.execute("INSERT INTO extractions (id,item_id,summary,key_points,list_items,created_at) VALUES (?,?,?,?,?,?)",
            (db.new_id(), _junk, "No content was provided in the prompt.",
             '["Unable to extract key points as no content was provided."]', "[]", db.now()))
con.commit(); con.close()
check("export_item skips a content-less item (no page)",
      logseq.export_item(_junk) is False and not os.path.exists(_page_path(_junk)))

# ---------- re-titling on reprocess must not orphan the old page ----------
rt = _seed_item(category="Learning")
logseq.export_item(rt)
con = db.connect(); con.execute("UPDATE items SET title=? WHERE id=?", ("Totally New Title", rt)); con.commit(); con.close()
logseq.export_item(rt)
_pdir = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
_matches = [n for n in os.listdir(_pdir) if n.endswith(f"-{rt[:8]}.md")]
check("re-title leaves exactly one page (no orphan)",
      len(_matches) == 1 and _matches[0].startswith("totally-new-title"), str(_matches))

# ---------- reprocess preserves collection done + row id (DB-level) ----------
iid3 = _seed_item(category="Learning")
con = db.connect()
cid_k = con.execute("SELECT id FROM collection_items WHERE item_id=? AND name='Keychron K2'", (iid3,)).fetchone()["id"]
con.execute("UPDATE collection_items SET done=1 WHERE id=?", (cid_k,)); con.commit(); con.close()
same = {"title": "Best keyboards 2026", "category": "Learning", "task": {"title": "Pick a keyboard"},
        "list_items": [{"name": "Keychron K2", "note": "hot-swap", "link": "keychron.com/k2"},
                       {"name": "NuPhy Air75", "note": "low profile", "link": "link not available"}]}
con = db.connect(); pipeline._write_knowledge(con, iid3, same); con.commit()
row = con.execute("SELECT id, done FROM collection_items WHERE item_id=? AND name='Keychron K2'", (iid3,)).fetchone()
n_now = con.execute("SELECT count(*) n FROM collection_items WHERE item_id=?", (iid3,)).fetchone()["n"]
con.close()
check("reprocess preserves checked-off done state", row["done"] == 1)
check("reprocess reuses the same collection id", row["id"] == cid_k)
check("reprocess doesn't duplicate collection rows", n_now == 2)

# ---------- retained read-back helpers (code kept, watcher not started) ----------
check("_is_conflicted flags conflict copies + git markers, not clean files",
      logseq._is_conflicted("p.sync-conflict-1.md", "") and logseq._is_conflicted("p.md", "a\n<<<<<<< HEAD\nb")
      and logseq._is_conflicted(".hidden.md", "") and not logseq._is_conflicted("p.md", "clean"))
# a hand-crafted page WITH item-id still round-trips a completion status (proves _apply/sync work if re-enabled)
active_id = _seed_item(category="Reading")
pages_dir = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
open(os.path.join(pages_dir, "manual.md"), "w", encoding="utf-8").write(f"item-id:: {active_id}\nstatus:: learned\n- ## Notes\n\t-\n")
open(os.path.join(pages_dir, "conflicted.sync-conflict-1.md"), "w", encoding="utf-8").write(f"item-id:: {active_id}\nstatus:: learned\n")
logseq.sync_from_logseq()
con = db.connect(); ls = con.execute("SELECT learn_status FROM items WHERE id=?", (active_id,)).fetchone()["learn_status"]; con.close()
check("read-back: status:: learned applied from a keyed page", ls == "learned", ls)

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

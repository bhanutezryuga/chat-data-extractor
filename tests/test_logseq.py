"""Tests for the Logseq export. See docs/LOGSEQ_PLAN.md.

Hermetic: temp DB + temp graph dir + stub mode (no Gemini/network). Covers the minimal page
format (title + tags only), study-gated TODO markers, bulk export, and reprocess preservation.
Run:  python tests/test_logseq.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="cde_logseq_")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = os.path.join(_TMP, "graph")
os.environ["LOGSEQ_ENABLED"] = "1"
os.environ["LOGSEQ_JOURNAL"] = "1"                     # deterministic regardless of a real .env value
os.environ["LOGSEQ_TODO_CATEGORIES"] = "Learning,Reading"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, pipeline, logseq, gemini   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


_seed_n = 0


def _seed_item(url=None, category="Learning"):
    """Seed a distinct capture. Content varies per call (a token) so the content-dedup in
    _write_knowledge treats each as its own item, not a duplicate. Returns (item_id, data)."""
    global _seed_n
    _seed_n += 1
    tok = _seed_n
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,raw_url,content_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (iid, config.USER_ID, url or f"https://x.com/i{tok}", "post", "ACTIONABLE", db.now(), db.now()))
    con.execute("INSERT INTO extractions (id,item_id,summary,key_points,list_items,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (db.new_id(), iid, "A concise but real summary of the captured item.", '["p1","p2"]', "[]", db.now()))
    con.commit()
    con.close()
    data = {"title": f"Best keyboards 2026 (set {tok})", "category": category,
            "task": {"title": "Pick a keyboard", "priority": "high", "tags": ["mechanical", "deep work"]},
            "list_items": [{"name": f"Keychron K2 v{tok}", "note": "hot-swap", "link": "keychron.com/k2"},
                           {"name": f"NuPhy Air75 v{tok}", "note": "low profile", "link": "link not available"}]}
    con = db.connect()
    pipeline._write_knowledge(con, iid, data)
    con.commit()
    con.close()
    return iid, data


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
_tagline = md.split("tags:: ")[1].split("\n")[0]
check("render: tags = category + lowercase topics, no content-type",
      _tagline.startswith("Reading") and "complexity" in _tagline and "article" not in _tagline)
for junk in ("item-id::", "url::", "source::", "status::", "captured::", "category::", "revisit-next::", "cid::"):
    check(f"render: NO {junk} clutter", junk not in md)
check("render: collection heading is a [[page]]", "- ## [[To-Read]]" in md)
check("render (study): items are TODO/DONE", "TODO Thinking in Systems" in md and "DONE Fifth Discipline" in md)
check("render (study): Task is a TODO", "## Task" in md and "TODO Read these" in md)
check("render (study): scheduled Revisit TODO", "TODO Revisit —" in md and "SCHEDULED: <2026-08-08" in md)
check("render: user Notes area present", logseq.NOTES_MARKER in md)
check("render: link kept / 'link not available' -> none", "link:: ex.com/tis" in md and md.count("link:: ") == 1)
# list content: the bullets ARE the value, so Summary/Key points are omitted as redundant
check("render (has list): NO ## Summary / ## Key points", "## Summary" not in md and "## Key points" not in md)

# ---------- pure render: LONG-FORM item (no list -> keep Summary/Key points) ----------
article = {"id": "art12345", "title": "On Complexity", "category": "Reading", "content_type": "article",
           "created_at": "2026-08-07 10:00:00", "learn_status": "active"}
amd = logseq.render_page(article, {"summary": "A dense essay worth a recap.", "key_points": '["one","two"]'}, [], {})
check("render (no list): keeps ## Summary + ## Key points",
      "## Summary" in amd and "A dense essay" in amd and "## Key points" in amd and "\t- one" in amd)

# ---------- pure render: RECIPE item -> full recipe, supersedes summary/list ----------
recipe_item = {"id": "rcp12345", "title": "Miso Ramen", "category": "Cooking", "content_type": "reel",
               "created_at": "2026-09-01 10:00:00", "learn_status": "active"}
recipe_ex = {"summary": "A quick miso ramen.", "key_points": '["fast","cozy"]',
             "recipe": '{"servings":"2","time":"25 min",'
                       '"ingredients":["200g ramen noodles","3 tbsp miso paste"],'
                       '"steps":["Boil the noodles.","Stir in the miso.","Serve hot."]}'}
rmd = logseq.render_page(recipe_item, recipe_ex, [], {})
check("render (recipe): ## Ingredients lists each ingredient verbatim",
      "## Ingredients" in rmd and "200g ramen noodles" in rmd and "3 tbsp miso paste" in rmd)
check("render (recipe): ## Steps numbered in order",
      "## Steps" in rmd and "1. Boil the noodles." in rmd and "3. Serve hot." in rmd)
check("render (recipe): servings/time line", "Servings: 2" in rmd and "Time: 25 min" in rmd)
check("render (recipe): NO Summary/Key points (recipe supersedes)",
      "## Summary" not in rmd and "## Key points" not in rmd)
rcoll = [{"id": "rc1", "collection": "Recipes", "name": "200g ramen noodles", "note": None, "link": None, "done": 0}]
check("render (recipe): supersedes the generic collection list too",
      "## [[Recipes]]" not in logseq.render_page(recipe_item, recipe_ex, rcoll, {})
      and "## Ingredients" in logseq.render_page(recipe_item, recipe_ex, rcoll, {}))
check("_has_content: a recipe-only extraction still exports", logseq._has_content({"summary": "", "recipe": recipe_ex["recipe"]}))
check("_has_content: empty recipe {} is ignored", not logseq._has_content({"summary": "", "recipe": "{}"}))

# stub auto-detects recipe content (keeps the offline path exercising the recipe field)
_rule = {"content_type": "reel"}
_rs = gemini.stub(_rule, "https://insta/reel/x", "Easy pancake recipe: mix and cook")
check("stub: recipe content -> Cooking + populated recipe", _rs["category"] == "Cooking" and _rs["recipe"].get("ingredients"))
check("stub: non-recipe -> empty recipe {}", gemini.stub(_rule, "https://insta/reel/y", "A book review video")["recipe"] == {})

# recipe round-trips: DB recipe column -> export_item (SELECT *) -> rendered page
_rid = db.new_id()
con = db.connect()
con.execute("INSERT INTO items (id,user_id,raw_url,content_type,status,title,category,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_rid, config.USER_ID, "https://insta/reel/rcp", "reel", "ACTIONABLE", "Test Curry", "Cooking", db.now(), db.now()))
con.execute("INSERT INTO extractions (id,item_id,summary,key_points,list_items,recipe,created_at) VALUES (?,?,?,?,?,?,?)",
            (db.new_id(), _rid, "thin summary", "[]", "[]",
             '{"servings":"4","time":"40 min","ingredients":["1 onion","2 cloves garlic"],"steps":["Chop.","Simmer."]}',
             db.now()))
con.commit(); con.close()
check("export_item exports a recipe item", logseq.export_item(_rid) in ("created", "updated"))
_rpage = open(_page_path(_rid), encoding="utf-8").read()
check("recipe page (from DB) has ## Ingredients + numbered ## Steps",
      "## Ingredients" in _rpage and "1 onion" in _rpage and "1. Chop." in _rpage and "## Summary" not in _rpage)

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
iid, d1 = _seed_item(category="Learning")
ok = logseq.export_item(iid)
path = _page_path(iid)
check("export_item writes a page", ok and os.path.exists(path))
page = open(path, encoding="utf-8").read()
check("page: minimal props (title + tags, nothing else)",
      page.startswith("title:: ") and "tags:: " in page.split("\n\n")[0]
      and "item-id::" not in page and "status::" not in page)
check("page (Learning=study): collection items are TODO", f'TODO {d1["list_items"][0]["name"]}' in page)

# ---------- ## Notes preserved across re-export ----------
_edited = open(path, encoding="utf-8").read().rstrip() + "\n\t- MY NOTE\n"
open(path, "w", encoding="utf-8").write(_edited)
logseq.export_item(iid)
check("re-export preserves user's ## Notes edits", "MY NOTE" in open(path, encoding="utf-8").read())

# ---------- journal breadcrumb (idempotent) ----------
jfile = os.path.join(config.LOGSEQ_GRAPH_DIR, "journals", db.now()[:10].replace("-", "_") + ".md")
_crumb = f'Captured [[{d1["title"]}]]'
check("journal breadcrumb written", os.path.exists(jfile) and _crumb in open(jfile, encoding="utf-8").read())
logseq.export_item(iid)
check("journal breadcrumb idempotent", open(jfile, encoding="utf-8").read().count(_crumb) == 1)

# ---------- export reports NEWLY-created pages (delta), not the running total ----------
check("export_item returns 'created' for a new page", ok == "created")
check("export_item returns 'updated' when the page already exists", logseq.export_item(iid) == "updated")
os.remove(path)                                        # page missing from the graph -> re-export is 'new'
check("export_all counts only newly-created pages", logseq.export_all() == 1)
check("export_all: re-run with nothing new returns 0", logseq.export_all() == 0)

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

# ---------- detail mode: sections (points + examples) render as ## Details ----------
_secs = ('[{"heading":"Pricing","points":["Anchor high first."],'
         '"examples":["A $99 plan made the $49 plan sell 3x more."]},'
         '{"heading":"Empty","points":[],"examples":[]}, "junk"]')
dmd = logseq.render_page(study_item, dict(ex, sections=_secs), [], {})
check("detail: ## Details block with the heading and its point",
      "- ## Details" in dmd and "\t- **Pricing**" in dmd and "\t\t- Anchor high first." in dmd, dmd)
check("detail: examples are nested under an Examples bullet",
      "\t\t- Examples\n\t\t\t- A $99 plan made the $49 plan sell 3x more." in dmd, dmd)
check("detail: empty / malformed sections are dropped", "Empty" not in dmd and "junk" not in dmd, dmd)
check("detail: Summary is kept and the user's ## Notes area still comes last",
      "## Summary" in dmd and dmd.rstrip().endswith("- ## Notes\n\t-"), dmd)
dlist = logseq.render_page(study_item, dict(ex, sections=_secs), coll, {})
check("detail: shown alongside a collection list", "- ## Details" in dlist and "## [[To-Read]]" in dlist, dlist)
check("no sections -> no ## Details block", "## Details" not in md)
check("_has_content: sections alone make it exportable", logseq._has_content({"summary": "", "sections": _secs}))
check("_has_content: empty sections are ignored", not logseq._has_content({"summary": "", "sections": "[]"}))
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
rt, _ = _seed_item(category="Learning")
logseq.export_item(rt)
con = db.connect(); con.execute("UPDATE items SET title=? WHERE id=?", ("Totally New Title", rt)); con.commit(); con.close()
logseq.export_item(rt)
_pdir = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
_matches = [n for n in os.listdir(_pdir) if n.endswith(f"-{rt[:8]}.md")]
check("re-title leaves exactly one page (no orphan)",
      len(_matches) == 1 and _matches[0].startswith("totally-new-title"), str(_matches))

# ---------- reprocess preserves collection done + row id (DB-level) ----------
iid3, d3 = _seed_item(category="Learning")
_kname = d3["list_items"][0]["name"]
con = db.connect()
cid_k = con.execute("SELECT id FROM collection_items WHERE item_id=? AND name=?", (iid3, _kname)).fetchone()["id"]
con.execute("UPDATE collection_items SET done=1 WHERE id=?", (cid_k,)); con.commit(); con.close()
con = db.connect(); pipeline._write_knowledge(con, iid3, d3); con.commit()   # reprocess with the SAME content
row = con.execute("SELECT id, done FROM collection_items WHERE item_id=? AND name=?", (iid3, _kname)).fetchone()
n_now = con.execute("SELECT count(*) n FROM collection_items WHERE item_id=?", (iid3,)).fetchone()["n"]
con.close()
check("reprocess preserves checked-off done state", row["done"] == 1)
check("reprocess reuses the same collection id", row["id"] == cid_k)
check("reprocess doesn't duplicate collection rows", n_now == 2)

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

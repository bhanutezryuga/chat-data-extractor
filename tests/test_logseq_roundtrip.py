"""Regression tests for issue #11: a DONE the user sets in Logseq must survive re-export and sync
back to the DB (collection bullet -> collection_items.done=1; Revisit block -> revisit.mark).

Hermetic: temp DB + temp graph dir + stub mode (no Gemini/network). Run:
    python tests/test_logseq_roundtrip.py
"""
import glob
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="cde_logseq_rt_")
_GRAPH = os.path.join(_TMP, "graph")
os.makedirs(_GRAPH)
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""
os.environ["REMIND_CHAT_ID"] = ""
os.environ["APP_PASSWORD"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = _GRAPH
os.environ["LOGSEQ_ENABLED"] = "1"
os.environ["LOGSEQ_JOURNAL"] = "1"
os.environ["LOGSEQ_TODO_CATEGORIES"] = "Learning,Reading"
os.environ["REVISIT_ENABLED"] = "1"
os.environ["REVISIT_SCHEDULE"] = "1,3,7"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, logseq, pipeline, revisit   # noqa: E402

assert os.path.abspath(config.LOGSEQ_GRAPH_DIR) == os.path.abspath(_GRAPH), "graph dir not isolated"
db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _page(iid):
    return glob.glob(os.path.join(_GRAPH, "pages", f"*-{iid[:8]}.md"))[0]


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(p, text):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _q(sql, args=()):
    con = db.connect()
    rows = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return rows


def _set_deadline(iid, dl):
    con = db.connect()
    con.execute("UPDATE items SET deadline=? WHERE id=?", (dl, iid))
    con.commit()
    con.close()


_n = 0


def _seed(with_list=True, category="Learning"):
    """A study item with a collection list, a task, and an overdue revisit deadline."""
    global _n
    _n += 1
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,raw_url,content_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (iid, config.USER_ID, f"https://x.com/rt{_n}", "post", "ACTIONABLE", db.now(), db.now()))
    con.execute("INSERT INTO extractions (id,item_id,summary,key_points,list_items,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (db.new_id(), iid, "A concise but real summary of the captured item.", '["p1"]', "[]", db.now()))
    con.commit()
    con.close()
    data = {"title": f"Roundtrip set {_n}", "category": category,
            "task": {"title": f"Study the set {_n}", "priority": "high", "tags": ["x"]},
            "list_items": ([{"name": f"Alpha book {_n}", "note": "first", "link": "a.com"},
                            {"name": f"Beta book {_n}", "note": None, "link": None}] if with_list else [])}
    con = db.connect()
    pipeline._write_knowledge(con, iid, data)
    con.commit()
    con.close()
    _set_deadline(iid, "2020-01-01 00:00:00")
    return iid, data


print("\n=== Logseq DONE round-trip (#11) ===\n")

# ---------- issue repro: pipeline.ingest article, tick both TODOs, export_all ----------
pipeline.fetch = lambda url, s: (f"Deep dive {url}. A long article about Python decorators with many "
                                 "details on usage.", None, {"fetched": True})
r = pipeline.ingest(raw_url="https://example.com/article-a", raw_text="x",
                    source_chat_id="777", source_msg_id="1")
aid = r["id"]
_set_deadline(aid, "2020-01-01 00:00:00")
logseq.export_item(aid)
p = _page(aid)
before = _read(p)
check("repro: page starts with a TODO Revisit", "- TODO Revisit" in before)
_write(p, before.replace("TODO Revisit", "DONE Revisit").replace("TODO Review", "DONE Review"))
logseq.export_all()
after = _read(p)
check("repro: no `TODO Revisit` line remains for the ticked cycle",
      "SCHEDULED: <2020-01-01" not in after, after)
row = _q("SELECT learn_status, deadline, revisit_stage, revisit_count FROM items WHERE id=?", (aid,))[0]
check("repro: DONE Revisit advanced the schedule", row["deadline"] > "2020-01-01 00:00:00"
      and row["revisit_stage"] == 1 and row["revisit_count"] == 1, str(row))
revs = _q("SELECT action FROM revisits WHERE item_id=?", (aid,))
check("repro: exactly one `revisited` log row", [x["action"] for x in revs] == ["revisited"], str(revs))
check("repro: new page shows a fresh TODO Revisit with the next SCHEDULED date",
      "- TODO Revisit" in after and f"SCHEDULED: <{row['deadline'][:10]}" in after, after)
if "DONE Review" in before.replace("TODO Review", "DONE Review") and "TODO Review" in before:
    check("repro: ticked task/review line stays DONE", "DONE Review" in after and "TODO Review" not in after, after)

# a second re-export must not mark again (exactly once per cycle)
logseq.export_all()
check("repro: re-export again does not re-mark", len(_q("SELECT 1 FROM revisits WHERE item_id=?", (aid,))) == 1)

# ---------- collection bullet DONE -> DB + stays DONE ----------
iid, d = _seed()
logseq.export_item(iid)
p = _page(iid)
alpha, beta = d["list_items"][0]["name"], d["list_items"][1]["name"]
t = _read(p)
check("coll: bullets start as TODO", f"TODO {alpha}" in t and f"TODO {beta}" in t)
_write(p, t.replace(f"TODO {alpha}", f"DONE {alpha}"))
logseq.export_item(iid)
t2 = _read(p)
check("coll: DONE bullet stays DONE after re-export", f"DONE {alpha}" in t2 and f"TODO {alpha}" not in t2, t2)
check("coll: untouched bullet stays TODO", f"TODO {beta}" in t2)
rows = {x["name"]: x["done"] for x in _q("SELECT name, done FROM collection_items WHERE item_id=?", (iid,))}
check("coll: collection_items.done synced to 1 for the ticked bullet", rows.get(alpha) == 1, str(rows))
check("coll: other bullet still done=0", rows.get(beta) == 0, str(rows))
check("coll: a collection DONE alone does not mark a revisit",
      not _q("SELECT 1 FROM revisits WHERE item_id=?", (iid,)))

# ---------- Logseq decorations: LOGBOOK drawers, collapsed::, spaces vs tabs ----------
iid, d = _seed()
logseq.export_item(iid)
p = _page(iid)
alpha, beta = d["list_items"][0]["name"], d["list_items"][1]["name"]
t = _read(p)
t = t.replace(f"\t- TODO {alpha}",
              f"  - DONE {alpha}\n    collapsed:: true\n    :LOGBOOK:\n    CLOCK: [2026-09-01 Tue 10:00]--"
              f"[2026-09-01 Tue 10:05] =>  00:05:00\n    :END:")
t = t.replace("- TODO Revisit", "- DONE Revisit")
# add a logbook drawer under the Revisit block too (Logseq writes one when a state changes)
lines = t.split("\n")
for i, ln in enumerate(lines):
    if ln.startswith("- DONE Revisit"):
        lines.insert(i + 1, "  :LOGBOOK:\n  * State \"DONE\" from \"TODO\" [2026-09-02 Wed 09:00]\n  :END:")
        break
_write(p, "\n".join(lines))
res = logseq.export_item(iid)
t2 = _read(p)
check("decorated: export still succeeds", res in ("created", "updated"), str(res))
check("decorated: DONE bullet (spaces + drawer + collapsed) kept", f"DONE {alpha}" in t2 and f"TODO {alpha}" not in t2, t2)
check("decorated: collection_items.done synced",
      _q("SELECT done FROM collection_items WHERE item_id=? AND name=?", (iid, alpha))[0]["done"] == 1)
check("decorated: DONE Revisit with LOGBOOK counted once",
      len(_q("SELECT 1 FROM revisits WHERE item_id=? AND action='revisited'", (iid,))) == 1)

# ---------- renamed / unknown DONE bullet: no crash, no bogus sync ----------
iid, d = _seed()
logseq.export_item(iid)
p = _page(iid)
alpha = d["list_items"][0]["name"]
_write(p, _read(p).replace(f"TODO {alpha}", "DONE Something the user renamed entirely"))
res = logseq.export_item(iid)
check("renamed: export does not raise and still writes", res == "updated", str(res))
check("renamed: no row marked done",
      not _q("SELECT 1 FROM collection_items WHERE item_id=? AND done=1", (iid,)))

# ---------- a corrupt/unreadable old page never raises ----------
iid, d = _seed()
logseq.export_item(iid)
p = _page(iid)
with open(p, "wb") as f:
    f.write(b"\xff\xfe\x00garbage- DONE Revisit\n\x00\x00")
res = logseq.export_item(iid)
check("garbage page: export_item returns without raising", res in ("updated", False), str(res))

# ---------- revisit.mark from elsewhere does not recurse / double-mark ----------
iid, d = _seed()
logseq.export_item(iid)
p = _page(iid)
_write(p, _read(p).replace("TODO Revisit", "DONE Revisit"))
out = revisit.mark(iid, "revisited")          # e.g. the Telegram button, while page says DONE
n = len(_q("SELECT 1 FROM revisits WHERE item_id=?", (iid,)))
check("mark(): one Telegram revisit + the page DONE for the SAME cycle is not double-counted",
      n == 1, f"{n} revisits")
check("mark(): page now shows fresh TODO Revisit", "- TODO Revisit" in _read(p))

# ---------- untouched pages: byte-identical re-export ----------
iid, d = _seed()
logseq.export_item(iid)
p = _page(iid)
t = _read(p)
logseq.export_item(iid)
check("untouched: re-export is byte-identical", _read(p) == t)
check("untouched: no revisit logged", not _q("SELECT 1 FROM revisits WHERE item_id=?", (iid,)))

print(f"\n{sum(_passed)}/{len(_passed)} passed")
sys.exit(0 if all(_passed) else 1)

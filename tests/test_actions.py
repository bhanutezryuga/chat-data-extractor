"""Tests for the per-item Action / intent feature (note | list | detail | translate).

Isolated: temp DB + stub mode (no Gemini). Verifies the smart-default decision, that
switching List<->Note is instant (no reprocess), that a mode chosen up front shapes the
extraction itself (prompt + stored sections), and the data plumbing. Run:
    python tests/test_actions.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="cde_actions_")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""        # stub mode
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""       # NEVER write to a real graph from tests (isolate from .env)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, gemini, pipeline, config   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def item_action(item_id):
    con = db.connect()
    a = con.execute("SELECT action FROM items WHERE id=?", (item_id,)).fetchone()["action"]
    con.close()
    return a


print("\n=== Action / intent feature ===\n")

# _decide_action heuristics
check("translate when content not in target language",
      pipeline._decide_action({"detected_language": "Spanish", "translation": "hello", "list_items": []}) == "translate")
check("list when >= 2 enumerated items",
      pipeline._decide_action({"suggested_action": "", "translation": "", "list_items": [{"name": "a"}, {"name": "b"}]}) == "list")
check("note as the safe default",
      pipeline._decide_action({"suggested_action": "", "translation": "", "list_items": []}) == "note")
check("honors Gemini's explicit suggested_action",
      pipeline._decide_action({"suggested_action": "list", "list_items": []}) == "list")

# Writing a result assigns an action (offline, no network): insert item -> _write_result
con = db.connect()
iid = db.new_id()
con.execute("INSERT INTO items (id,user_id,status,created_at,updated_at) VALUES (?,?,?,?,?)",
            (iid, config.USER_ID, "PROCESSING", db.now(), db.now()))
con.commit()
rule = {"id": "rule_article", "content_type": "article", "extraction_strategy": "readability"}
data = gemini.stub(rule, "http://x", "a sentence. another sentence. a third one.")
pipeline._write_result(con, iid, rule, {}, data, "text")
con.commit()
con.close()
check("writing a result assigns a smart-default action", item_action(iid) in config.ACTIONS,
      f"action={item_action(iid)}")

# Override: switch to list (instant, no Gemini) then back to note
r1 = pipeline.set_action(iid, "list")
r2 = pipeline.set_action(iid, "note")
check("override list<->note works and persists",
      r1.get("action") == "list" and r2.get("action") == "note" and item_action(iid) == "note")
check("rejects unknown action", pipeline.set_action(iid, "bogus").get("error") is not None)

# `new <link>` command: save without processing, then process-with-action
p = pipeline.create_pending(raw_url="xtest://pending/1", raw_text="new",
                            source_chat_id="N", source_msg_id="n1")
con = db.connect()
st = con.execute("SELECT status, action FROM items WHERE id=?", (p["id"],)).fetchone()
con.close()
check("create_pending saves AWAITING_ACTION, unprocessed",
      st["status"] == "AWAITING_ACTION" and not st["action"])
check("create_pending dedups on (chat,msg)",
      pipeline.create_pending(raw_url="x", source_chat_id="N", source_msg_id="n1") is None)
r = pipeline.process_pending(p["id"], "list")   # unmatched url -> NEEDS_REVIEW, must not crash
check("process_pending runs without error",
      r is not None and r.get("status") in ("NEEDS_REVIEW", "ACTIONABLE", "FAILED"),
      f"status={r.get('status') if r else None}")

# ---------- a chosen mode shapes the prompt (the choice reaches the LLM) ----------
_rule = {"id": "rule_article", "content_type": "article", "purpose": "p", "action_template": "t"}
p_none = gemini._prompt(_rule, "http://x")
p_note = gemini._prompt(dict(_rule, mode="note"), "http://x")
p_list = gemini._prompt(dict(_rule, mode="list"), "http://x")
p_detail = gemini._prompt(dict(_rule, mode="detail"), "http://x")
check("no mode: prompt has no sections schema and still asks the model to decide",
      '"sections"' not in p_none and "decide what the user most likely wants" in p_none)
check("detail mode: prompt asks for sections with examples",
      '"sections"' in p_detail and '"examples"' in p_detail and "DETAILED NOTES" in p_detail)
check("note/list modes: own focus line, no sections schema",
      "ASKED FOR A SUMMARY" in p_note and "ASKED FOR THE LIST" in p_list
      and '"sections"' not in p_note and '"sections"' not in p_list)
check("a chosen mode drops the 'decide the action' question",
      all("decide what the user most likely wants" not in p for p in (p_note, p_list, p_detail)))
check("an unknown mode is ignored", gemini._prompt(dict(_rule, mode="bogus"), "http://x") == p_none)

# ---------- ...and the pipeline carries it end to end ----------
_LONG = "A long article about Python decorators with many details on usage and examples."
pipeline.fetch = lambda url, s: (f"Deep dive {url}. {_LONG}", None, {"fetched": True})


def _row(item_id):
    con = db.connect()
    it = con.execute("SELECT status, action FROM items WHERE id=?", (item_id,)).fetchone()
    ex = con.execute("SELECT sections FROM extractions WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
                     (item_id,)).fetchone()
    con.close()
    import json
    return it["status"], it["action"], json.loads(ex["sections"] or "[]") if ex else None


def _pending(n):
    return pipeline.create_pending(raw_url=f"https://example.com/mode-{n}", raw_text="x",
                                   source_chat_id="M", source_msg_id=f"m{n}")["id"]


d = _pending("detail")
pipeline.process_pending(d, "detail")
st, act, secs = _row(d)
check("process_pending(detail): ACTIONABLE, action=detail, sections stored",
      st == "ACTIONABLE" and act == "detail" and secs and secs[0].get("examples"), (st, act, secs))

n = _pending("note")
pipeline.process_pending(n, "note")
st, act, secs = _row(n)
check("process_pending(note): action=note and no sections", st == "ACTIONABLE" and act == "note" and secs == [],
      (st, act, secs))

a = _pending("auto")
pipeline.process_pending(a, "auto")
st, act, secs = _row(a)
check("process_pending(auto): smart default, no sections",
      st == "ACTIONABLE" and act in config.ACTIONS and secs == [], (st, act, secs))

r = pipeline.set_action(n, "detail")
check("set_action(detail) on a summary-only item asks for a reprocess and changes nothing",
      r.get("needs_reprocess") and item_action(n) == "note", r)
check("set_action(detail) is instant once sections exist", pipeline.set_action(d, "detail").get("action") == "detail")
check("set_action(detail) on an item with no link is refused (nothing to re-read)",
      pipeline.set_action(iid, "detail").get("error") is not None and item_action(iid) == "note")

# a failed run keeps the chosen mode, so /retry (reprocess) re-runs it the same way
f = _pending("fails")
_orig_stub = gemini.stub


def _boom(*a, **k):
    raise RuntimeError("429 RESOURCE_EXHAUSTED rate limit")


gemini.stub = _boom
try:
    pipeline.process_pending(f, "detail")
finally:
    gemini.stub = _orig_stub
st, act, secs = _row(f)
check("a failed detail run keeps action=detail", st == "FAILED" and act == "detail", (st, act))
pipeline.reprocess(f)
st, act, secs = _row(f)
check("reprocess re-runs it in detail mode", st == "ACTIONABLE" and act == "detail" and secs, (st, act, secs))

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

"""Tests for the per-item Action / intent feature (note | list | translate).

Isolated: temp DB + stub mode (no Gemini). Verifies the smart-default decision, that
switching List<->Note is instant (no reprocess), and the data plumbing. Run:
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
    _passed.append(ok)


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

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

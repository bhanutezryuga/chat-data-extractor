"""Tests for the dashboard's pending-links list (#17): links saved with `new <link>` sit in
AWAITING_ACTION until an action is chosen, so the dashboard must list them and let the user
process or drop each one.

Hermetic: temp DB, stub mode, auth off, Logseq off, no network — `pipeline.process_pending` is
monkeypatched with a recording fake so this exercises the web plumbing, not extraction. Run:
    python tests/test_pending_ui.py
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="cde_pending_ui_")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""            # never write to a real graph
os.environ["APP_PASSWORD"] = ""                # auth off (isolate from a real .env's APP_PASSWORD)
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""   # isolate from a real .env's allowlist
os.environ["REMIND_CHAT_ID"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, pipeline, web   # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _status(iid):
    con = db.connect()
    row = con.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()
    con.close()
    return row["status"] if row else None


def _wait_until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


_processed = []
pipeline.process_pending = lambda item_id, action: _processed.append((item_id, action)) or {"status": "ACTIONABLE"}

_srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
threading.Thread(target=_srv.serve_forever, daemon=True).start()


def _req(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", _srv.server_address[1], timeout=5)
    c.request(method, path, body=json.dumps(body) if body is not None else None,
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    data = json.loads(r.read().decode() or "{}")
    c.close()
    return r.status, data


print("\n=== pending links on the dashboard (#17) ===\n")

a = pipeline.create_pending(raw_url="https://example.com/a", raw_text="new https://example.com/a",
                            source_chat_id="1", source_msg_id="n1")["id"]
b = pipeline.create_pending(raw_url="https://example.com/b", raw_text="new https://example.com/b",
                            source_chat_id="1", source_msg_id="n2")["id"]

st, data = _req("GET", "/api/pending")
ids = [it["id"] for it in data.get("items", [])]
check("GET /api/pending lists AWAITING_ACTION items", st == 200 and set(ids) == {a, b}, (st, data))
check("pending count matches", data.get("count") == 2, data.get("count"))
check("pending rows carry the link", all(it.get("raw_url") for it in data.get("items", [])))

st, data = _req("POST", "/api/pending", {"id": a, "action": "note"})
check("processing a pending link is accepted (202, runs in background)", st == 202, (st, data))
check("process_pending called with the chosen action",
      _wait_until(lambda: (a, "note") in _processed), _processed)

st, data = _req("POST", "/api/pending", {"id": b, "action": "drop"})
check("dropping a pending link succeeds", st == 200, (st, data))
check("dropped link is archived, not deleted", _status(b) == "ARCHIVED", _status(b))
st, data = _req("GET", "/api/pending")
check("dropped link leaves the pending list", b not in [it["id"] for it in data.get("items", [])])

st, _ = _req("POST", "/api/pending", {"id": a, "action": "explode"})
check("unknown action -> 400", st == 400, st)
st, _ = _req("POST", "/api/pending", {"id": "nope", "action": "note"})
check("unknown id -> 404", st == 404, st)
st, _ = _req("POST", "/api/pending", {"id": b, "action": "note"})
check("an item that is no longer pending -> 404", st == 404, st)

html = (config.ROOT / "app" / "static" / "minimal.html").read_text(encoding="utf-8")
check("dashboard loads /api/pending", "/api/pending" in html)

_srv.shutdown()
print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

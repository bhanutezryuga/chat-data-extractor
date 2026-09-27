"""Tests for undoing an archive from the dashboard.

Clicking "Archive gone" moved items into a collapsed Archived section with no way back, which
reshuffled the page and left the user lost. Archived items can now be restored — by the Undo in
the post-archive notice, or a Restore button per archived item — to the status they had before.

Hermetic: temp DB, stub mode, auth off, Logseq off, no network. Run:
    python tests/test_unarchive.py
"""
import http.client
import json
import os
import sys
import tempfile
import threading

os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
_TMP = tempfile.mkdtemp(prefix="cde_unarchive_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""
os.environ["APP_PASSWORD"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""
os.environ["REMIND_CHAT_ID"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, pipeline, sideload, web   # noqa: E402
from http.server import ThreadingHTTPServer            # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _seed(status, reason):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,source_chat_id,raw_url,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (iid, config.USER_ID, "sideload", f"https://x.test/{iid[:6]}", status, db.now(), db.now()))
    con.commit()
    db.log(con, iid, "extract", "error", reason)
    con.close()
    return iid


def _status(iid):
    con = db.connect()
    st = con.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()["status"]
    con.close()
    return st


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


print("\n=== undo / restore archived items ===\n")

failed = _seed("FAILED", "HTTP Error 404: Not Found")
review = _seed("NEEDS_REVIEW", "HTTP Error 410: Gone")
alive = _seed("FAILED", "HTTP Error 429: Too Many Requests")   # not gone: never archived

st, res = _req("POST", "/api/archive-gone", {})
archived_ids = [it["id"] for it in res.get("items", [])]
check("archive-gone reports which items it archived", st == 200 and set(archived_ids) == {failed, review},
      (st, res))
check("both gone items are ARCHIVED", _status(failed) == "ARCHIVED" and _status(review) == "ARCHIVED")

st, res = _req("POST", "/api/unarchive", {"ids": archived_ids})
check("undo succeeds and reports the count", st == 200 and res.get("restored") == 2, (st, res))
check("FAILED item restored to FAILED", _status(failed) == "FAILED", _status(failed))
check("NEEDS_REVIEW item restored to NEEDS_REVIEW", _status(review) == "NEEDS_REVIEW", _status(review))
st, fails = _req("GET", "/api/failures")
check("restored items are back in Needs attention",
      {failed, review} <= {it["id"] for it in fails.get("items", [])})
st, arch = _req("GET", "/api/archived")
check("restored items left the Archived list", arch.get("count") == 0, arch.get("count"))

st, res = _req("POST", "/api/unarchive", {"ids": [alive, "no-such-id"]})
check("items that aren't archived are left alone", st == 200 and res.get("restored") == 0
      and _status(alive) == "FAILED", (st, res, _status(alive)))

st, _ = _req("POST", "/api/unarchive", {"ids": "not-a-list"})
check("ids must be a list -> 400", st == 400, st)

# A pending link dropped from the dashboard comes back as pending, not as a failure.
pend = pipeline.create_pending(raw_url="https://x.test/pending", raw_text="new https://x.test/pending",
                               source_chat_id="1", source_msg_id="p1")["id"]
_req("POST", "/api/pending", {"id": pend, "action": "drop"})
st, res = _req("POST", "/api/unarchive", {"ids": [pend]})
check("a dropped pending link is restored to AWAITING_ACTION",
      res.get("restored") == 1 and _status(pend) == "AWAITING_ACTION", (res, _status(pend)))

html = (config.ROOT / "app" / "static" / "minimal.html").read_text(encoding="utf-8")
check("dashboard calls /api/unarchive", "/api/unarchive" in html)
check("dashboard has a persistent archive notice", 'id="archive-notice"' in html)
# .notice is display:flex, which beats the `hidden` attribute unless a [hidden] rule restores it —
# without that the empty notice shows on page load (caught in the browser, not by the API checks).
check("the notice's `hidden` attribute actually hides it", ".notice[hidden]" in html)

_srv.shutdown()
print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

"""Tests for the "many links at once / app was offline" scenarios.

Runs in ISOLATION: a temp DB + stub mode (no Gemini key) so it's free, instant,
deterministic, and never touches your real data. Run:  python tests/test_burst_offline.py
"""
import os
import sys
import tempfile
import threading
import time

# --- isolate BEFORE importing app (config reads env at import) ---
_TMP = tempfile.mkdtemp(prefix="cde_test_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""        # force offline stub: instant, no network/cost
os.environ["TELEGRAM_BOT_TOKEN"] = ""    # no real poller
os.environ["LOGSEQ_GRAPH_DIR"] = ""      # NEVER write to a real graph from tests (isolate from .env)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, pipeline   # noqa: E402

assert not config.USE_GEMINI, "test must run in stub mode (no Gemini)"
db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(ok)


def n_items(chat):
    con = db.connect()
    n = con.execute("SELECT count(*) FROM items WHERE source_chat_id=?", (chat,)).fetchone()[0]
    con.close()
    return n


# Non-matching scheme -> classified 'unknown' -> no network/Gemini, pure DB-path concurrency.
def burst(chat, count, msg_ids):
    errors = []
    def w(mid):
        try:
            pipeline.ingest(raw_url=f"xtest://{chat}/{mid}", raw_text="x",
                            source_chat_id=chat, source_msg_id=str(mid))
        except Exception as e:
            errors.append(repr(e))
    ts = [threading.Thread(target=w, args=(m,)) for m in msg_ids]
    t0 = time.time()
    for t in ts: t.start()
    for t in ts: t.join()
    return errors, time.time() - t0


print("\n=== Scenario: multiple links at once / app offline ===\n")

# A. 25 different links fired simultaneously
errs, dt = burst("A", 25, range(25))
check("A. 25 links sent simultaneously -> all stored, no DB errors",
      n_items("A") == 25 and not errs, f"{n_items('A')}/25 in {dt:.2f}s, errors={len(errs)}")

# B. The SAME link delivered 5x at once (e.g. Telegram retries / double-send)
errs, _ = burst("B", 5, ["same"] * 5)
check("B. same link x5 at once -> exactly 1 item (dedup)", n_items("B") == 1, f"{n_items('B')} item(s)")

# C. App was OFFLINE: Telegram re-delivers the same update on restart
r1 = pipeline.ingest(raw_url="xtest://C/1", raw_text="x", source_chat_id="C", source_msg_id="1")
r2 = pipeline.ingest(raw_url="xtest://C/1", raw_text="x", source_chat_id="C", source_msg_id="1")  # redelivery
check("C. offline re-delivery of same msg -> not duplicated",
      r1 is not None and r2 is None, f"first={'created' if r1 else None}, redelivery={'dup!' if r2 else 'skipped'}")

# C2. A genuinely NEW buffered message (different id) IS processed on restart
r3 = pipeline.ingest(raw_url="xtest://C/2", raw_text="x", source_chat_id="C", source_msg_id="2")
check("C2. new buffered message after downtime -> processed", r3 is not None, "new id ingested")

# D. One message containing several links -> one item per link, distinct ids
text = "look at https://a.test/1 then https://b.test/2 and https://c.test/3"
urls = pipeline.extract_urls(text)
for i, u in enumerate(urls):
    # use non-matching marker ids; real flow uses f'{msg_id}:{i}'
    pipeline.ingest(raw_url=f"xtest://D/{i}", raw_text=text, source_chat_id="D", source_msg_id=f"99:{i}")
check("D. one message, 3 links -> 3 distinct items", len(urls) == 3 and n_items("D") == 3,
      f"extracted {len(urls)} urls, {n_items('D')} items")

# E. GAP CHECK: an item left PROCESSING by a crash is NOT auto-recovered on restart
con = db.connect()
con.execute("INSERT INTO items (id,user_id,source_chat_id,source_msg_id,raw_url,status,created_at,updated_at)"
            " VALUES ('crashrow','user_default','E','1','xtest://E/1','PROCESSING',datetime('now'),datetime('now'))")
con.commit(); con.close()
db.init()  # simulate restart
con = db.connect()
stuck = con.execute("SELECT count(*) FROM items WHERE status='PROCESSING'").fetchone()[0]
con.close()
check("E. crash mid-processing leaves item stuck in PROCESSING", stuck == 1,
      f"{stuck} stuck before requeue")

# F. startup auto-requeue heals the stuck item
pipeline.requeue_stuck()
con = db.connect()
still = con.execute("SELECT count(*) FROM items WHERE status='PROCESSING'").fetchone()[0]
con.close()
check("F. requeue_stuck() heals items stuck in PROCESSING", still == 0, f"{still} still stuck after requeue")

# G. Concurrent reprocess must NOT deadlock: the SQLite write lock must be released
#    before the (slow) extract call, or a second reprocess dies "database is locked".
import app.pipeline as _P            # noqa: E402
import app.gemini as _G             # noqa: E402

_fake_rule = {"id": "rule_article", "name": "Article", "content_type": "article",
              "extraction_strategy": "readability", "purpose": "p", "action_template": "Summarize."}
_orig_classify, _orig_fetch, _orig_stub, _orig_connect = _P.classify, _P.fetch, _G.stub, db.connect
_P.classify = lambda con, url: _fake_rule
_P.fetch = lambda url, strat: ("body with plenty of real signal text here " * 6, None, {"fetched": True})


def _slow_stub(rule, url, text):
    time.sleep(0.6)                  # simulate a slow Gemini call; lock must NOT be held here
    return _orig_stub(rule, url, text)


_G.stub = _slow_stub
# shorten the busy-timeout so a regression fails fast (< the hold time) instead of hanging
db.connect = lambda: (lambda c: (c.execute("PRAGMA busy_timeout=300"), c)[1])(_orig_connect())

_gids = []
con = db.connect()
for i in range(4):
    iid = db.new_id(); _gids.append(iid)
    con.execute("INSERT INTO items (id,user_id,raw_url,status,source_chat_id,source_msg_id,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (iid, config.USER_ID, f"https://g.test/{i}", "ACTIONABLE", "G", f"g{i}", db.now(), db.now()))
con.commit(); con.close()

_rerr = []


def _rp(iid):
    try:
        pipeline.reprocess(iid)
    except Exception as e:
        _rerr.append(repr(e))


_gts = [threading.Thread(target=_rp, args=(i,)) for i in _gids]
for t in _gts: t.start()
for t in _gts: t.join()
db.connect, _P.classify, _P.fetch, _G.stub = _orig_connect, _orig_classify, _orig_fetch, _orig_stub
check("G. concurrent reprocess -> no 'database is locked' (lock freed before extract)",
      not _rerr, f"errors={_rerr}")

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

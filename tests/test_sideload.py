"""Tests for the bulk sideloader (app/sideload.py).

Hermetic: temp DB, stub mode, Logseq off (never touches a real graph). Driver logic (parsing,
dedup classification, --limit, SSRF skip, budget stop, stable source ids) is tested with an
injected fake ingest; idempotency is tested against the real offline pipeline. Run:
    python tests/test_sideload.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="cde_sideload_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""                    # never write to a real graph
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, usage, sideload   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _items():
    con = db.connect()
    n = con.execute("SELECT count(*) n FROM items").fetchone()["n"]
    con.close()
    return n


def _silent(*a, **k):
    pass


print("\n=== sideload ===\n")

# ---------- parsing ----------
f1 = os.path.join(_TMP, "links.txt")
_write(f1, [
    "1699999999 https://a.test/1",
    "1699999998   https://b.test/2   (trailing note)",
    "# a comment https://ignored.test/x",
    "",
    "1699999997 not a url at all",
    "2020-01-01T00:00:00Z https://c.test/3",
])
urls, malformed = sideload.parse_file(f1)
check("parse: extracts the 3 URLs (timestamp + trailing text ignored)",
      urls == ["https://a.test/1", "https://b.test/2", "https://c.test/3"], str(urls))
check("parse: comment line skipped", all("ignored.test" not in u for u in urls))
check("parse: 1 malformed line, at line 5", len(malformed) == 1 and malformed[0][0] == 5)

# ---------- dry-run classification (seed one existing URL) ----------
con = db.connect()
con.execute("INSERT INTO items (id,user_id,raw_url,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (db.new_id(), config.USER_ID, "https://a.test/1", "ACTIONABLE", db.now(), db.now()))
con.commit(); con.close()
_before = _items()
calls = []
s = sideload.run(f1, execute=False, ingest_fn=lambda **k: calls.append(k), out=_silent)
check("dry-run: 2 new, 1 duplicate", s["new"] == 2 and s["duplicate"] == 1, str(s))
check("dry-run: calls no ingest, writes nothing", calls == [] and _items() == _before)

# ---------- --run processes only the new links (via fake ingest) ----------
calls = []
def _fake(**k):
    calls.append(k)
    return {"status": "ACTIONABLE"}
s = sideload.run(f1, execute=True, delay=0, allow_nonpublic=True, ingest_fn=_fake, out=_silent)
check("run: processes the 2 new links (skips the duplicate)", len(calls) == 2 and s["processed"] == 2)
check("run: tally by status", s["by_status"].get("ACTIONABLE") == 2)
check("run: source_chat_id='sideload' + stable url-derived source_msg_id",
      calls[0]["source_chat_id"] == "sideload"
      and calls[0]["source_msg_id"] == sideload._sid("https://b.test/2"))

# ---------- --limit ----------
calls = []
sideload.run(f1, execute=True, delay=0, limit=1, allow_nonpublic=True,
             ingest_fn=lambda **k: calls.append(k) or {"status": "ACTIONABLE"}, out=_silent)
check("run: --limit 1 processes only one", len(calls) == 1)

# ---------- SSRF gate skips a private URL (and never ingests it) ----------
f2 = os.path.join(_TMP, "unsafe.txt")
_write(f2, ["1 http://127.0.0.1/admin", "2 http://169.254.169.254/latest/meta-data/"])
calls = []
s = sideload.run(f2, execute=True, delay=0,
                 ingest_fn=lambda **k: calls.append(k) or {"status": "ACTIONABLE"}, out=_silent)
check("run: non-public URLs skipped, never ingested", s["skipped_unsafe"] == 2 and calls == [])

# ---------- budget stop ----------
_orig = usage.budget_ok
usage.budget_ok = lambda con: False
calls = []
s = sideload.run(f1, execute=True, delay=0, allow_nonpublic=True,
                 ingest_fn=lambda **k: calls.append(k) or {"status": "ACTIONABLE"}, out=_silent)
usage.budget_ok = _orig
check("run: stops cleanly when budget is exhausted", s["budget_stopped"] and s["processed"] == 0 and calls == [])

# ---------- idempotency against the real offline pipeline ----------
f3 = os.path.join(_TMP, "fresh.txt")
_write(f3, ["1 https://fresh.test/1", "2 https://fresh.test/2"])
_b = _items()
sideload.run(f3, execute=True, delay=0, allow_nonpublic=True, out=_silent)   # real pipeline.ingest
check("run (real): creates one item per new link", _items() - _b == 2)
s = sideload.run(f3, execute=True, delay=0, allow_nonpublic=True, out=_silent)
check("run (real): re-run is idempotent (all duplicates, nothing processed)",
      s["new"] == 0 and s["duplicate"] == 2 and s["processed"] == 0, str(s))

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

"""Tests for switching Gemini models when one is out of limits or overloaded: the ordered model
list, counting every attempt (failed ones too — Google counts them), skipping a model that is
out of its daily/per-minute limit or cooling down after a 503, waiting instead of downgrading
in background (patient) runs, the "fallback model" label, and /retry stopping after 3 busy
failures in a row.

Hermetic: temp DB, fake clock, no real network (urllib.request.urlopen is monkeypatched). Run:
    python tests/test_model_switch.py
"""
import io
import json
import os
import sys
import tempfile
import urllib.error
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="cde_model_switch_")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["GEMINI_MODEL"] = "ignored-when-a-list-is-set"
os.environ["GEMINI_MODELS"] = "m-a, m-b,m-c"
os.environ["GEMINI_LIMITS"] = "m-a=2/1000/3, m-b=5/100000/50"     # m-c: limits unknown
os.environ["GEMINI_OVERLOAD_COOLDOWN"] = "300"
os.environ["TELEGRAM_BOT_TOKEN"] = "x"
os.environ["LOGSEQ_GRAPH_DIR"] = ""    # never write to a real graph
os.environ["APP_PASSWORD"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "777"
os.environ["REMIND_CHAT_ID"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, failures, gemini, logseq, pipeline, sideload, telegram, usage   # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


# ---- fake clock + fake Gemini -------------------------------------------------------------
CLOCK = [datetime(2026, 10, 8, 13, 0, 0, tzinfo=timezone.utc)]      # 06:00 Pacific
usage._utcnow = lambda: CLOCK[0]
_slept = []


def _fake_sleep(s):
    _slept.append(s)
    CLOCK[0] += timedelta(seconds=s)


gemini._sleep = _fake_sleep

SCRIPT = {}      # model -> list of outcomes consumed per call ("ok" | 429 body | int HTTP code); default ok
CALLS = []       # models actually requested, in order

BODY_DAY = json.dumps({"error": {"code": 429, "message": "quota", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "3"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"}]}}).encode()
BODY_MIN = json.dumps({"error": {"code": 429, "message": "quota", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "2"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "20s"}]}}).encode()


class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    model = req.full_url.split("/models/")[1].split(":")[0]
    CALLS.append(model)
    step = SCRIPT.get(model, [])
    out = step.pop(0) if step else "ok"
    if out == "ok":
        return _Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(
            {"title": f"T {model}", "summary": f"by {model}", "key_points": ["k"]})}]}}],
            "usageMetadata": {"totalTokenCount": 100}}).encode())
    if isinstance(out, bytes):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(out))
    raise urllib.error.HTTPError(req.full_url, out, {503: "Service Unavailable", 400: "Bad Request"}[out],
                                 {}, io.BytesIO(b"{}"))


gemini.urllib.request.urlopen = _fake_urlopen


def reset(minutes=0):
    """Fresh state: forget every call/limit/cooldown, optionally move the clock."""
    con = db.connect()
    con.execute("DELETE FROM gemini_calls")
    con.execute("DELETE FROM gemini_usage")
    con.execute("DELETE FROM meta WHERE key='gemini_quota'")
    con.commit()
    con.close()
    usage._overloaded.clear()
    SCRIPT.clear()
    CALLS.clear()
    _slept.clear()
    CLOCK[0] += timedelta(minutes=minutes)


def run():
    return gemini.extract({"action_template": "x", "content_type": "article", "purpose": "p"}, "https://e.x", "text")


def calls_today(model):
    con = db.connect()
    s = usage.summary(con)
    con.close()
    return {m["model"]: m for m in s["models"]}[model]["today_requests"]


# =========================================================================
print("\n=== settings ===\n")
check("GEMINI_MODELS is the ordered list", config.GEMINI_MODELS == ["m-a", "m-b", "m-c"], config.GEMINI_MODELS)
check("the first model in the list is the main one", config.GEMINI_MODEL == "m-a", config.GEMINI_MODEL)
check("GEMINI_LIMITS gives per-model rpm/tpm/rpd",
      config.GEMINI_LIMITS == {"m-a": {"rpm": 2, "tpm": 1000, "rpd": 3},
                               "m-b": {"rpm": 5, "tpm": 100000, "rpd": 50}}, config.GEMINI_LIMITS)

# =========================================================================
print("\n=== normal: the first model answers ===\n")
reset()
d = run()
check("uses the first model", CALLS == ["m-a"] and d["_model"] == "m-a", CALLS)
check("not marked as a fallback", d.get("_fallback") is False, d.get("_fallback"))
check("the attempt is counted", calls_today("m-a") == 1, calls_today("m-a"))

# =========================================================================
print("\n=== overloaded (503): switch now, then leave that model alone for a while ===\n")
reset()
SCRIPT["m-a"] = [503]
d = run()
check("a 503 on the first model falls through to the second", CALLS == ["m-a", "m-b"] and d["_model"] == "m-b", CALLS)
check("the result is marked as made by a fallback model", d.get("_fallback") is True, d.get("_fallback"))
check("the failed attempt counts against the first model (Google counts it)", calls_today("m-a") == 1,
      calls_today("m-a"))
CALLS.clear()
d = run()
check("while it cools down, the overloaded model is not even tried", CALLS == ["m-b"], CALLS)
CLOCK[0] += timedelta(seconds=301)
CALLS.clear()
d = run()
check("after the cooldown the first model is used again", CALLS == ["m-a"] and d["_fallback"] is False, CALLS)

# =========================================================================
print("\n=== daily limit ===\n")
reset()
SCRIPT["m-a"] = [BODY_DAY]
d = run()
check("a per-day 429 switches to the next model", CALLS == ["m-a", "m-b"] and d["_model"] == "m-b", CALLS)
CLOCK[0] += timedelta(hours=2)                       # long past the 30s retry hint
CALLS.clear()
run()
check("...and that model stays skipped until the daily reset, not just the retry hint", CALLS == ["m-b"], CALLS)
block = usage.availability("m-a")
check("availability says why and until when", block and block["reason"] == "daily"
      and block["until"] == usage.quota_day()[1], block)
CLOCK[0] = usage.quota_day()[1] + timedelta(seconds=1)
CALLS.clear()
run()
check("after the reset it is used again", CALLS == ["m-a"], CALLS)

reset()
for _ in range(3):
    usage.record_call("m-a", "503")
run()
check("the app's own count (3/3 attempts, all failed) also skips it — no wasted request", CALLS == ["m-b"], CALLS)

# When Google's per-day refusal carries a long wait, that wait IS its reset time — follow it rather
# than the documented midnight-Pacific rule (seen live: the hint pointed at midnight UTC instead).
reset()
BODY_DAY_LONG = BODY_DAY.replace(b'"30s"', b'"18000s"')            # "come back in 5 hours"
for _ in range(3):
    usage.record_call("m-a", "ok")                                 # 3/3 used, by the app's count
usage.note_429("m-a", BODY_DAY_LONG)
told = CLOCK[0] + timedelta(seconds=18000)
check("a long retry time on a per-day refusal is taken as the reset time",
      usage.availability("m-a") == {"reason": "daily", "until": told}, usage.availability("m-a"))
check("...which is earlier than the documented Pacific reset", told < usage.quota_day()[1], (told, usage.quota_day()[1]))
CLOCK[0] = told + timedelta(seconds=1)
CALLS.clear()
run()
check("after Google's stated time the model is used again — the earlier attempts no longer count",
      CALLS == ["m-a"] and calls_today("m-a") == 1, (CALLS, calls_today("m-a")))
usage.record_call("m-a", "ok"); usage.record_call("m-a", "ok")     # 3 since Google's reset
check("...and its daily count runs from that reset", (usage.availability("m-a") or {}).get("reason") == "daily",
      usage.availability("m-a"))
CLOCK[0] = datetime(2026, 10, 8, 13, 0, 0, tzinfo=timezone.utc)

# =========================================================================
print("\n=== per-minute limit: live switches, background waits ===\n")
reset()
run(); run()                                         # m-a is now at 2/2 this minute
CALLS.clear()
d = run()
check("live: at the per-minute limit it switches at once", CALLS == ["m-b"] and not _slept, (CALLS, _slept))
check("...with the fallback mark", d["_fallback"] is True)

reset(minutes=5)
run(); run()
CALLS.clear()
with gemini.patient():
    d = run()
check("background: it waits for the preferred model instead of downgrading",
      CALLS == ["m-a"] and d["_fallback"] is False, CALLS)
check("...for about a minute at most", 0 < sum(_slept) <= 65, sum(_slept))

reset(minutes=5)
SCRIPT["m-a"] = [BODY_MIN]
run()
CALLS.clear()
run()
check("a per-minute 429 skips the model only until Google's retry time", CALLS == ["m-b"], CALLS)
CLOCK[0] += timedelta(seconds=21)
CALLS.clear()
run()
check("...then it is used again", CALLS == ["m-a"], CALLS)

reset(minutes=5)
for _ in range(3):
    usage.record_call("m-a", "ok")                   # m-a out for the day
_slept.clear()
with gemini.patient():
    run()
check("background: no waiting for a model that is out for the whole day", CALLS == ["m-b"] and not _slept,
      (CALLS, _slept))

# =========================================================================
print("\n=== a model with unknown limits is tried until Google says no ===\n")
reset()
for _ in range(500):
    usage.record_call("m-c", "ok")
check("no configured limit -> never blocked by the app's own count", usage.availability("m-c") is None,
      usage.availability("m-c"))
con = db.connect()
lim = {m["model"]: m for m in usage.summary(con)["models"]}["m-c"]["limits"]
con.close()
check("its limits are reported as unknown", all(lim[k] == {"value": None, "source": "unknown"}
                                              for k in ("rpm", "tpm", "rpd")), lim)

# =========================================================================
print("\n=== nothing left ===\n")
reset()
for m in ("m-a", "m-b", "m-c"):
    SCRIPT[m] = [BODY_DAY]
try:
    run(); err = None
except Exception as e:      # noqa: BLE001
    err = e
check("every model rate-limited in one go: the last 429 propagates", isinstance(err, urllib.error.HTTPError)
      and err.code == 429 and CALLS == ["m-a", "m-b", "m-c"], (err, CALLS))
CALLS.clear()
try:
    run(); err = None
except Exception as e:      # noqa: BLE001
    err = e
check("afterwards: no request is sent at all", CALLS == [] and isinstance(err, gemini.NoModelAvailable), (err, CALLS))
cat, label = failures.classify(str(err), "extract")
check("...and the failure reads as quota used up (retry after the reset)", cat == "budget", (cat, label, str(err)))
con = db.connect()
check("the budget guard reports no model left", usage.budget_ok(con) is False)
con.close()

reset()
for m in ("m-a", "m-b", "m-c"):
    usage.note_503(m)
try:
    run(); err = None
except Exception as e:      # noqa: BLE001
    err = e
cat, label = failures.classify(str(err), "extract")
check("every model overloaded: no request, and it reads as the AI service being busy",
      CALLS == [] and cat == "unavailable" and "AI service" in label, (CALLS, cat, label, str(err)))
con = db.connect()
check("...which is temporary, so the budget guard still says OK", usage.budget_ok(con) is True)
con.close()

reset()
SCRIPT["m-a"] = [400]
try:
    run(); err = None
except Exception as e:      # noqa: BLE001
    err = e
check("other errors (e.g. 400) are not a reason to switch", isinstance(err, urllib.error.HTTPError)
      and err.code == 400 and CALLS == ["m-a"], (err, CALLS))

# =========================================================================
print("\n=== dashboard data: one entry per model ===\n")
reset()
SCRIPT["m-a"] = [BODY_DAY]
run()
con = db.connect()
s = usage.summary(con)
con.close()
by = {m["model"]: m for m in s["models"]}
check("models are listed in preference order", [m["model"] for m in s["models"]] == ["m-a", "m-b", "m-c"])
check("the blocked model says so, with when it frees up",
      by["m-a"]["status"] == "daily" and by["m-a"]["blocked_until"] == usage.quota_day()[1].strftime("%Y-%m-%dT%H:%M:%SZ"),
      by["m-a"])
check("the model in use is flagged", s["active_model"] == "m-b" and by["m-b"]["status"] == "ok", s["active_model"])
check("each model carries its own counts", by["m-a"]["today_requests"] == 1 and by["m-b"]["today_requests"] == 1
      and by["m-b"]["today_tokens"] == 0, (by["m-a"], by["m-b"]))

# =========================================================================
print("\n=== the fallback label ===\n")
reset()
pipeline.fetch = lambda url, s: ("A long article about Python decorators with many details on usage "
                                 "and examples, enough text to count as a real description of it.", None,
                                 {"fetched": True})
SCRIPT["m-a"] = [503]
r = pipeline.ingest(raw_url="https://example.com/fallback-article", source_chat_id="777", source_msg_id="fb1")
con = db.connect()
ex = dict(con.execute("SELECT * FROM extractions WHERE item_id=?", (r["id"],)).fetchone())
item = dict(con.execute("SELECT * FROM items WHERE id=?", (r["id"],)).fetchone())
con.close()
check("saved, by the second model", r["status"] == "ACTIONABLE" and ex["model"] == "m-b", (r, ex["model"]))
check("the extraction remembers it was a fallback", json.loads(ex["raw_metadata"]).get("fallback") is True,
      ex["raw_metadata"])
md = logseq.render_page(item, ex, [], {"tags": '["python"]'})
tags_line = next((ln for ln in md.splitlines() if ln.startswith("tags::")), "")
check("the Logseq page is tagged fallback-model", "fallback-model" in tags_line, tags_line)
text, _ = telegram._format_result(r["id"])
check("the Telegram reply names the fallback model", "allback model" in text and "m-b" in text, text)

CLOCK[0] += timedelta(seconds=301)
r2 = pipeline.ingest(raw_url="https://example.com/normal-article", source_chat_id="777", source_msg_id="fb2")
con = db.connect()
ex2 = dict(con.execute("SELECT * FROM extractions WHERE item_id=?", (r2["id"],)).fetchone())
item2 = dict(con.execute("SELECT * FROM items WHERE id=?", (r2["id"],)).fetchone())
con.close()
md2 = logseq.render_page(item2, ex2, [], {"tags": '["python"]'})
text2, _ = telegram._format_result(r2["id"])
check("an item made by the main model carries no fallback label",
      ex2["model"] == "m-a" and "fallback-model" not in md2 and "allback model" not in text2, (ex2["model"], text2))

# =========================================================================
print("\n=== /retry stops after 3 busy failures in a row ===\n")
reset()


def _seed_failed(n):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,source_chat_id,source_msg_id,raw_url,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (iid, config.USER_ID, "sideload", f"busy{n}",
                                             f"https://example.com/busy{n}", "FAILED", db.now(), db.now()))
    con.commit()
    db.log(con, iid, "extract", "error", "timed out")
    con.close()
    return iid


for n in range(6):
    _seed_failed(n)
done = []


def _busy(iid):
    done.append(iid)
    con = db.connect()
    db.log(con, iid, "extract", "error", "HTTP Error 503: Service Unavailable")
    con.close()
    return {"id": iid, "status": "FAILED"}


s = sideload.retry(execute=True, delay=0, reprocess_fn=_busy, out=lambda *a, **k: None)
check("stops after the third overloaded failure in a row", len(done) == 3 and s["stopped"] == "ai_busy",
      (len(done), s["stopped"]))

done.clear()


def _mixed(iid):
    done.append(iid)
    con = db.connect()
    busy = len(done) % 3 != 0                 # every third one fails for a different reason
    db.log(con, iid, "extract" if busy else "fetch", "error",
           "HTTP Error 503: Service Unavailable" if busy else "description too thin")
    con.close()
    return {"id": iid, "status": "FAILED"}


s = sideload.retry(execute=True, delay=0, reprocess_fn=_mixed, out=lambda *a, **k: None)
check("a different failure in between resets the count (the older 4-in-a-row rule stops it instead)",
      s["stopped"] == "consecutive_failures" and len(done) == 4, (len(done), s["stopped"]))

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

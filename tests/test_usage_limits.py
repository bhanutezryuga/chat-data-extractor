"""Tests for the Gemini rate-limit view: the quota day follows Google's reset (midnight Pacific,
not UTC), requests/tokens in the last minute, the real limits learned from a 429 reply, and the
"settings vs confirmed by Google" source label on each limit.

Hermetic: temp DB, auth off, no real network (urllib.request.urlopen is monkeypatched). Run:
    python tests/test_usage_limits.py
"""
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="cde_usage_limits_")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["GEMINI_MODEL"] = "gemini-test-flash"
os.environ["GEMINI_RPM_LIMIT"] = "10"
os.environ["GEMINI_TPM_LIMIT"] = "250000"
os.environ["GEMINI_RPD_LIMIT"] = "250"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""    # never write to a real graph
os.environ["APP_PASSWORD"] = ""
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, gemini, usage, web                 # noqa: E402
from http.server import ThreadingHTTPServer            # noqa: E402

db.init()
_passed = []
MODEL = "gemini-test-flash"


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _use(at, tokens=100):
    """Record one Gemini request (gemini_calls) and its metered tokens at a given UTC time."""
    usage.record_call(MODEL, "ok", now=at)
    con = db.connect()
    con.execute("INSERT INTO gemini_usage (id,item_id,model,kind,prompt_tokens,output_tokens,total_tokens,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (db.new_id(), None, MODEL, "text", tokens, 0, tokens, at.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    con.close()


def _summary(now):
    con = db.connect()
    s = usage.summary(con, now=now)
    con.close()
    return s


# =========================================================================
# the quota day = midnight Pacific -> midnight Pacific
# =========================================================================
print("\n=== quota day (midnight Pacific) ===\n")

start, nxt = usage.quota_day(utc(2026, 10, 8, 13, 0))            # PDT (UTC-7)
check("summer: the day started at 07:00 UTC", start == utc(2026, 10, 8, 7), start)
check("summer: the next reset is 07:00 UTC tomorrow", nxt == utc(2026, 10, 9, 7), nxt)

start, nxt = usage.quota_day(utc(2026, 10, 8, 6, 59))            # still "yesterday" in Pacific
check("just before the reset: still the previous Pacific day", start == utc(2026, 10, 7, 7), start)
check("just before the reset: reset is one minute away", nxt == utc(2026, 10, 8, 7), nxt)

start, nxt = usage.quota_day(utc(2026, 12, 15, 13, 0))           # PST (UTC-8)
check("winter: the day started at 08:00 UTC", start == utc(2026, 12, 15, 8), start)
check("winter: the next reset is 08:00 UTC tomorrow", nxt == utc(2026, 12, 16, 8), nxt)

start, nxt = usage.quota_day(utc(2026, 11, 1, 12, 0))            # clocks go back at 09:00 UTC this day
check("autumn clock change: day starts at 07:00 UTC, next reset 08:00 UTC",
      start == utc(2026, 11, 1, 7) and nxt == utc(2026, 11, 2, 8), (start, nxt))

start, nxt = usage.quota_day(utc(2026, 3, 8, 12, 0))             # clocks go forward at 10:00 UTC this day
check("spring clock change: day starts at 08:00 UTC, next reset 07:00 UTC",
      start == utc(2026, 3, 8, 8) and nxt == utc(2026, 3, 9, 7), (start, nxt))


# =========================================================================
# counting: today = since the last Pacific midnight; "this minute" = last 60s
# =========================================================================
print("\n=== counting against the quota day and the last minute ===\n")

NOW = utc(2026, 10, 8, 13, 0, 0)
_use(utc(2026, 10, 8, 6, 30), tokens=5000)      # same UTC date, but BEFORE the 07:00 UTC reset
_use(utc(2026, 10, 8, 9, 0), tokens=700)        # today, long ago
_use(NOW - timedelta(seconds=90), tokens=300)   # today, outside the last minute
_use(NOW - timedelta(seconds=30), tokens=40)    # today, inside the last minute
_use(NOW - timedelta(seconds=5), tokens=60)     # today, inside the last minute

s = _summary(NOW)
check("today's requests exclude a call made before the Pacific reset", s["today_requests"] == 4, s["today_requests"])
check("today's tokens exclude it too", s["today_tokens"] == 1100, s["today_tokens"])
check("requests_remaining follows the same day", s["requests_remaining"] == 246, s["requests_remaining"])
check("last minute: 2 requests", s["minute_requests"] == 2, s["minute_requests"])
check("last minute: 100 tokens", s["minute_tokens"] == 100, s["minute_tokens"])
check("reset_at is the next Pacific midnight (UTC, ISO)", s["reset_at"] == "2026-10-09T07:00:00Z", s["reset_at"])
check("reset_in_seconds counts down to it", s["reset_in_seconds"] == 18 * 3600, s["reset_in_seconds"])


# =========================================================================
# limits: from settings until Google confirms one in a 429
# =========================================================================
print("\n=== limits: settings vs confirmed by Google ===\n")

lim = s["limits"]
check("all three limits come from settings before any 429",
      all(lim[k]["source"] == "settings" for k in ("rpm", "tpm", "rpd")), lim)
check("...with the configured values",
      (lim["rpm"]["value"], lim["tpm"]["value"], lim["rpd"]["value"]) == (10, 250000, 250), lim)
check("no 429 recorded yet", s["last_429"] is None, s["last_429"])

BODY_RPD = json.dumps({"error": {
    "code": 429, "status": "RESOURCE_EXHAUSTED",
    "message": "You exceeded your current quota, please check your plan and billing details.",
    "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "quotaDimensions": {"location": "global", "model": MODEL},
            "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}).encode()

usage.note_429(MODEL, BODY_RPD, now=NOW)
s = _summary(NOW + timedelta(seconds=10))
lim = s["limits"]
check("a 429 naming the per-day quota sets the real requests/day limit", lim["rpd"]["value"] == 20, lim["rpd"])
check("...marked as confirmed by Google", lim["rpd"]["source"] == "google", lim["rpd"])
check("...with when it was seen", lim["rpd"].get("seen_at") == "2026-10-08T13:00:00Z", lim["rpd"])
check("the other limits stay as settings", lim["rpm"]["source"] == "settings" and lim["tpm"]["source"] == "settings", lim)
check("rpd_limit (used by the budget guard) follows the confirmed value", s["rpd_limit"] == 20, s["rpd_limit"])
check("last_429 carries the retry delay", (s["last_429"] or {}).get("retry_delay_s") == 37, s["last_429"])
check("last_429 says when retrying is allowed", (s["last_429"] or {}).get("retry_at") == "2026-10-08T13:00:37Z",
      s["last_429"])
check("last_429 names which limit was hit", (s["last_429"] or {}).get("kinds") == ["rpd"], s["last_429"])

BODY_MIN = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
         "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "5"},
        {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count",
         "quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier", "quotaValue": "125000"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12.5s"}]}}).encode()
usage.note_429(MODEL, BODY_MIN, now=NOW)
lim = _summary(NOW)["limits"]
check("per-minute request quota -> rpm", lim["rpm"] == {"value": 5, "source": "google",
                                                        "seen_at": "2026-10-08T13:00:00Z"}, lim["rpm"])
check("per-minute token quota -> tpm", lim["tpm"]["value"] == 125000 and lim["tpm"]["source"] == "google", lim["tpm"])
check("the earlier confirmed requests/day limit is kept", lim["rpd"]["value"] == 20, lim["rpd"])

usage.note_429("some-other-model", BODY_RPD.replace(b'"20"', b'"999"'), now=NOW)
check("a limit confirmed for a different model doesn't leak into this one",
      _summary(NOW)["limits"]["rpd"]["value"] == 20, _summary(NOW)["limits"]["rpd"])

# Google sometimes sends a bare 429 with no quota details — record it, change no limits, never raise.
for name, body in (("bare 429 body", b'{"error": {"code": 429, "message": "Resource has been exhausted"}}'),
                   ("non-JSON body", b"<html>busy</html>"), ("empty body", b""), ("None body", None)):
    try:
        usage.note_429(MODEL, body, now=NOW + timedelta(minutes=5))
        ok = True
    except Exception as e:       # noqa: BLE001
        ok = False
        print("   raised:", e)
    check(f"{name}: doesn't raise", ok)
s = _summary(NOW + timedelta(minutes=5))
check("a bare 429 keeps the limits already confirmed", s["limits"]["rpd"]["value"] == 20, s["limits"])
check("a bare 429 is still recorded as the latest one", (s["last_429"] or {}).get("at") == "2026-10-08T13:05:00Z",
      s["last_429"])
check("...with no retry time claimed", (s["last_429"] or {}).get("retry_at") is None, s["last_429"])


# =========================================================================
# gemini._generate: a 429 is noted, then re-raised unchanged
# =========================================================================
print("\n=== gemini._generate records a 429 ===\n")

_noted = []
_orig_note, _orig_urlopen = usage.note_429, gemini.urllib.request.urlopen
usage.note_429 = lambda model, body, now=None: _noted.append((model, body))


def _raise(code, body):
    def _f(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, {429: "Too Many Requests",
                                                          503: "Service Unavailable"}[code], {}, io.BytesIO(body))
    return _f


try:
    gemini.urllib.request.urlopen = _raise(429, BODY_RPD)
    try:
        gemini._generate([{"text": "hi"}], MODEL)
        err = None
    except urllib.error.HTTPError as e:
        err = e
    check("the 429 still propagates as an HTTPError", err is not None and err.code == 429, err)
    check("...with the text failures.classify matches on", "Error 429" in str(err), str(err))
    check("the 429 body reached usage.note_429 for that model",
          _noted == [(MODEL, BODY_RPD)], [(m, len(b or b"")) for m, b in _noted])

    _noted.clear()
    gemini.urllib.request.urlopen = _raise(503, b'{"error": {"code": 503}}')
    try:
        gemini._generate([{"text": "hi"}], MODEL)
        err = None
    except urllib.error.HTTPError as e:
        err = e
    check("a 503 propagates and is NOT treated as a rate limit", err is not None and err.code == 503 and not _noted,
          (err, _noted))
finally:
    usage.note_429, gemini.urllib.request.urlopen = _orig_note, _orig_urlopen


# =========================================================================
# GET /api/usage carries it all
# =========================================================================
print("\n=== GET /api/usage ===\n")

_srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
try:
    conn = http.client.HTTPConnection("127.0.0.1", _srv.server_address[1], timeout=5)
    conn.request("GET", "/api/usage")
    r = conn.getresponse()
    data = json.loads(r.read().decode())
    conn.close()
    check("GET /api/usage: 200", r.status == 200, r.status)
    check("...has limits, the reset time, the last-minute counts and the last 429",
          all(k in data for k in ("limits", "reset_at", "reset_in_seconds", "minute_requests",
                                  "minute_tokens", "last_429")), sorted(data))
    check("...keeps the keys other code already reads",
          all(k in data for k in ("today_tokens", "today_requests", "daily_token_budget", "rpd_limit",
                                  "tpm_limit", "model", "enabled")), sorted(data))
finally:
    _srv.shutdown()
    _srv.server_close()

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

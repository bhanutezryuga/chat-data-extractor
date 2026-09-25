"""Tests for app/failures.classify() — turning a raw processing-log error string into a human
category + label — plus the sideload._is_gone() refactor that now delegates to it, and the
error_category/error_label fields added to GET /api/failures and the new GET /api/archived.

Hermetic: temp DB, stub mode, auth off, no real network. Run:
    python tests/test_failures.py
"""
import http.client
import json
import os
import sys
import tempfile
import threading

if hasattr(sys.stdout, "reconfigure"):    # Windows console (cp1252) can't print the em-dashes
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # some labels carry

_TMP = tempfile.mkdtemp(prefix="cde_failures_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""    # never write to a real graph
os.environ["APP_PASSWORD"] = ""        # auth off (isolate from a real .env's APP_PASSWORD)
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = ""   # open (isolate from a real .env's allowlist)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, failures, sideload, web   # noqa: E402
from http.server import ThreadingHTTPServer            # noqa: E402

db.init()
_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


# =========================================================================
# failures.classify()
# =========================================================================
print("\n=== failures.classify() ===\n")

# (reason, expected category) — pulled from the actual strings pipeline.py/instagram.py/fetch.py log.
CASES = [
    ("HTTP Error 404: Not Found", "gone"),
    ("instagram: Instagram API: HTTP Error 404: Not Found", "gone"),
    ("HTTP Error 410: Gone", "gone"),
    ("HTTP Error 429: Too Many Requests", "rate_limited"),
    ("HTTP Error 503: Service Unavailable", "unavailable"),
    ("Unable to download webpage: The read operation timed out "
     "(caused by TransportError('The read operation timed out'))", "timeout"),
    ("ERROR: [Instagram] xyz: Requested content is not available, rate-limit reached or login "
     "required. Use --cookies-from-browser or --cookies for the authentication.", "login_expired"),
    ("instagram: no Instagram cookies configured (set INSTAGRAM_COOKIES)", "login_expired"),
    ("blocked non-public URL", "blocked"),
    ("blocked: not a public http(s) URL", "blocked"),
    ("daily Gemini budget reached -> deferred", "budget"),
    ("no rule matched -> unknown", "unsupported"),
    ("instagram: not an Instagram post/reel URL", "unsupported"),
    ("video download unavailable: no such file", "no_content"),
    ("video disabled / no key -> NEEDS_REVIEW", "no_content"),
    ("instagram: no usable media or caption", "no_content"),
    ("description too thin -> NEEDS_REVIEW", "no_content"),
    ("chars=0", "no_content"),
]

for reason, expected_cat in CASES:
    cat, label = failures.classify(reason)
    check(f"classify({reason[:55]!r}) -> {expected_cat}", cat == expected_cat, f"got ({cat!r}, {label!r})")
    check("  ...with a non-empty label", bool(label), label)

# order-sensitivity: an Instagram login-expired message also contains "rate-limit", but must
# classify as login_expired (checked first in _RULES), not rate_limited.
cat, _ = failures.classify(
    "ERROR: [Instagram] abc123: Requested content is not available, rate-limit reached or login "
    "required. Use --cookies-from-browser or --cookies for the authentication.")
check("Instagram 'rate-limit reached or login required' -> login_expired, not rate_limited",
      cat == "login_expired", cat)

# fallbacks / edge cases
cat, label = failures.classify("some totally unrecognized failure text")
check("unrecognized reason falls back to category 'error'", cat == "error", cat)
check("unrecognized reason's label is the raw text", label == "some totally unrecognized failure text", label)

cat, label = failures.classify(None)
check("None reason -> ('error', 'Unknown error')", cat == "error" and label == "Unknown error", (cat, label))

cat, label = failures.classify("")
check("empty-string reason -> ('error', 'Unknown error')", cat == "error" and label == "Unknown error", (cat, label))

cat, label = failures.classify("   ")
check("whitespace-only reason -> ('error', 'Unknown error')", cat == "error" and label == "Unknown error", (cat, label))

long_reason = "z" * 500
cat, label = failures.classify(long_reason)
check("very long unrecognized reason is trimmed to <=140 chars", len(label) <= 140, len(label))


# =========================================================================
# sideload._is_gone() — now delegates to failures.classify(); behavior must be unchanged
# =========================================================================
print("\n=== sideload._is_gone() (delegates to failures.classify) ===\n")

check("_is_gone: HTTP 404 -> True", sideload._is_gone("HTTP Error 404: Not Found") is True)
check("_is_gone: HTTP 410 -> True", sideload._is_gone("HTTP Error 410: Gone") is True)
check("_is_gone: HTTP 429 -> False (rate-limited, not gone)",
      sideload._is_gone("HTTP Error 429: Too Many Requests") is False)
check("_is_gone: HTTP 503 -> False", sideload._is_gone("HTTP Error 503: Service Unavailable") is False)
check("_is_gone: None -> False", sideload._is_gone(None) is False)
check("_is_gone: unrecognized text -> False", sideload._is_gone("some other failure") is False)


# =========================================================================
# GET /api/failures and GET /api/archived — error_category/error_label fields
# =========================================================================
print("\n=== /api/failures + /api/archived: error_category/error_label ===\n")


def _seed_item(status, source, reason=None):
    con = db.connect()
    iid = db.new_id()
    con.execute("INSERT INTO items (id,user_id,source_chat_id,raw_url,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (iid, config.USER_ID, source, f"https://x.test/{iid[:6]}", status, db.now(), db.now()))
    con.commit()
    if reason:
        db.log(con, iid, "extract", "error", reason)
    con.close()
    return iid


_srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()


def http_req(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    parsed = json.loads(r.read().decode())
    conn.close()
    return r.status, parsed

try:
    gone_id = _seed_item("FAILED", "sideload", "HTTP Error 404: Not Found")
    rate_id = _seed_item("NEEDS_REVIEW", "telegram-chat-1", "HTTP Error 429: Too Many Requests")

    status, data = http_req("GET", "/api/failures")
    by_id = {it["id"]: it for it in data.get("items", [])}
    check("GET /api/failures: 200, includes both seeded items",
          status == 200 and gone_id in by_id and rate_id in by_id, str(data))
    check("GET /api/failures: 404 item classified error_category='gone'",
          by_id.get(gone_id, {}).get("error_category") == "gone", by_id.get(gone_id))
    check("GET /api/failures: 429 item classified error_category='rate_limited'",
          by_id.get(rate_id, {}).get("error_category") == "rate_limited", by_id.get(rate_id))
    check("GET /api/failures: both items carry a non-empty error_label",
          bool(by_id.get(gone_id, {}).get("error_label")) and bool(by_id.get(rate_id, {}).get("error_label")))
    check("GET /api/failures: raw 'reason' text is still present (not replaced)",
          by_id.get(gone_id, {}).get("reason") == "HTTP Error 404: Not Found", by_id.get(gone_id))

    status, data = http_req("GET", "/api/archived")
    check("GET /api/archived: 200, empty before anything is archived",
          status == 200 and data.get("count") == 0, str(data))

    ares = sideload.archive_gone(execute=True, sideload_only=False, out=lambda *a, **k: None)
    check("archive_gone archived the 404 item (and only that one)",
          ares.get("archived") == 1, ares)

    status, data = http_req("GET", "/api/failures")
    by_id = {it["id"]: it for it in data.get("items", [])}
    check("GET /api/failures: archived item no longer listed",
          gone_id not in by_id, list(by_id.keys()))
    check("GET /api/failures: the still-stuck 429 item remains listed",
          rate_id in by_id, list(by_id.keys()))

    status, data = http_req("GET", "/api/archived")
    arch_by_id = {it["id"]: it for it in data.get("items", [])}
    check("GET /api/archived: archived item now listed",
          status == 200 and gone_id in arch_by_id, str(data))
    check("GET /api/archived: carries the same error classification it had before archiving",
          arch_by_id.get(gone_id, {}).get("error_category") == "gone", arch_by_id.get(gone_id))
    check("GET /api/archived: item has an updated_at timestamp",
          bool(arch_by_id.get(gone_id, {}).get("updated_at")))
finally:
    _srv.shutdown()
    _srv.server_close()

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

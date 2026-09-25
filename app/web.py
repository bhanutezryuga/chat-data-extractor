"""Local web app: serves the dashboard and a small JSON API.
Uses ThreadingHTTPServer so the Telegram poller and browser requests coexist.
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import auth, config, db, failures, logseq, netguard, pipeline, revisit, sideload, usage

STATIC = config.ROOT / "app" / "static"
MAX_BODY = 1_000_000   # 1 MB request cap
MAX_URL = 2048

_login_hits = {}
_login_lock = threading.Lock()

_retry_lock = threading.Lock()
_retry_state = {"running": False, "last": None}


def _run_retry_bg(limit, sideload_only):
    try:
        summary = sideload.retry(execute=True, limit=limit, sideload_only=sideload_only,
                                 out=lambda *a, **k: None)
        _retry_state["last"] = summary
    finally:
        _retry_state["running"] = False


def _rate_ok(ip, limit=8, window=300):
    """Crude per-IP login throttle to blunt brute force."""
    now = time.time()
    with _login_lock:
        hits = [t for t in _login_hits.get(ip, []) if now - t < window]
        hits.append(now)
        _login_hits[ip] = hits
        return len(hits) <= limit


def _rows(con, q, args=()):
    return [dict(r) for r in con.execute(q, args).fetchall()]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json", extra_headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body_bytes(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > MAX_BODY:
            return b""
        return self.rfile.read(n)

    def _body_json(self):
        try:
            return json.loads(self._body_bytes().decode() or "{}")
        except Exception:
            return {}

    def _authed(self):
        if not auth.enabled():
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if part.strip().startswith(auth.COOKIE + "="):
                return auth.valid_token(part.strip()[len(auth.COOKIE) + 1:])
        return False

    def _client_ip(self):
        return (self.headers.get("CF-Connecting-IP")
                or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def _cookie_secure(self):
        mode = config.COOKIE_SECURE
        if mode == "on":
            return True
        if mode == "off":
            return False
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    # ---- routing ----
    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/healthz":
            return self._send(200, {"ok": True, "auth": auth.enabled()})
        if path == "/login":
            return self._send(200, auth.login_page(), "text/html; charset=utf-8")
        if path == "/logout":
            return self._send(303, "", "text/plain", extra_headers=[
                ("Location", "/login"),
                ("Set-Cookie", f"{auth.COOKIE}=; Path=/; HttpOnly; Max-Age=0")])
        if not self._authed():
            if path.startswith("/api/"):
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, auth.login_page(), "text/html; charset=utf-8")

        if path in ("/", "/minimal.html"):     # minimal metrics view
            try:
                return self._send(200, (STATIC / "minimal.html").read_text(encoding="utf-8"),
                                  "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, f"dashboard missing: {e}", "text/plain")

        if path == "/api/stats":
            con = db.connect()
            statuses = {r["status"]: r["n"] for r in
                        con.execute("SELECT status, count(*) n FROM items GROUP BY status")}
            types = {r["content_type"] or "?": r["n"] for r in
                     con.execute("SELECT content_type, count(*) n FROM items GROUP BY content_type")}
            categories = {r["category"]: r["n"] for r in con.execute(
                "SELECT category, count(*) n FROM items WHERE category IS NOT NULL GROUP BY category ORDER BY n DESC")}
            learned = con.execute("SELECT count(*) n FROM items WHERE learn_status='learned'").fetchone()["n"]
            revisit_due = revisit.due_count(con)
            con.close()
            return self._send(200, {"statuses": statuses, "types": types, "categories": categories,
                                    "learned": learned, "revisit_due": revisit_due,
                                    "logseq": logseq.active()})

        if path == "/api/usage":
            con = db.connect()
            data = usage.summary(con)
            con.close()
            return self._send(200, data)

        if path == "/api/items":
            con = db.connect()
            items = _rows(con,
                          "SELECT i.id, i.content_type, i.action, i.status, i.raw_url, i.confidence, i.created_at, "
                          "i.title, i.category, i.deadline, i.learn_status, i.revisit_count, "
                          "e.summary FROM items i "
                          "LEFT JOIN extractions e ON e.item_id = i.id "
                          "GROUP BY i.id ORDER BY i.created_at DESC LIMIT 200")
            con.close()
            return self._send(200, items)

        if path == "/api/failures":     # stuck items (FAILED / NEEDS_REVIEW) + why, for the dashboard
            con = db.connect()
            rows = _rows(con,
                         "SELECT i.id, i.title, i.raw_url, i.status, i.category, "
                         "(SELECT p.detail FROM processing_logs p WHERE p.item_id=i.id "
                         " AND p.status IN ('warn','error') ORDER BY p.created_at DESC LIMIT 1) AS reason "
                         "FROM items i WHERE i.status IN ('FAILED','NEEDS_REVIEW') "
                         "ORDER BY i.status, i.created_at DESC LIMIT 300")
            con.close()
            for r in rows:
                r["error_category"], r["error_label"] = failures.classify(r["reason"])
            return self._send(200, {"count": len(rows), "items": rows,
                                    "retry_running": _retry_state["running"],
                                    "last_retry": _retry_state["last"]})

        if path == "/api/archived":     # items archived via --archive-gone / the dashboard button
            con = db.connect()
            rows = _rows(con,
                         "SELECT i.id, i.title, i.raw_url, i.category, i.updated_at, "
                         "(SELECT p.detail FROM processing_logs p WHERE p.item_id=i.id "
                         " AND p.status IN ('warn','error') ORDER BY p.created_at DESC LIMIT 1) AS reason "
                         "FROM items i WHERE i.status='ARCHIVED' ORDER BY i.updated_at DESC LIMIT 300")
            con.close()
            for r in rows:
                r["error_category"], r["error_label"] = failures.classify(r["reason"])
            return self._send(200, {"count": len(rows), "items": rows})

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/login":
            if not _rate_ok(self._client_ip()):
                return self._send(429, auth.login_page("Too many attempts — wait a few minutes."),
                                  "text/html; charset=utf-8")
            raw = self._body_bytes().decode("utf-8", "replace")
            pw = (parse_qs(raw).get("password") or [""])[0]
            if auth.check_password(pw):
                secure = "; Secure" if self._cookie_secure() else ""
                cookie = (f"{auth.COOKIE}={auth.make_token()}; Path=/; HttpOnly; "
                          f"SameSite=Lax; Max-Age=2592000{secure}")
                return self._send(303, "", "text/plain",
                                  extra_headers=[("Location", "/"), ("Set-Cookie", cookie)])
            return self._send(401, auth.login_page("Wrong password"), "text/html; charset=utf-8")

        if not self._authed():
            return self._send(401, {"error": "unauthorized"})

        body = self._body_json()

        if path == "/api/ingest":
            url = (body.get("url") or "").strip()
            if not url or len(url) > MAX_URL:
                return self._send(400, {"error": "url required (and < 2KB)"})
            if not netguard.is_safe_public_url(url):
                return self._send(400, {"error": "blocked: not a public http(s) URL"})
            res = pipeline.ingest(raw_url=url, raw_text=url, source_chat_id="web",
                                  source_msg_id=db.now() + ":" + url[:40])
            return self._send(200, res or {"error": "duplicate"})

        if path == "/api/retry":       # reprocess stuck FAILED/NEEDS_REVIEW items, in the background
            with _retry_lock:
                if _retry_state["running"]:
                    return self._send(409, {"error": "a retry is already running"})
                _retry_state["running"] = True
            limit = body.get("limit")
            limit = int(limit) if isinstance(limit, (int, float, str)) and str(limit).strip() else None
            # sideload_only=False: the failures panel shows stuck items from every source, so retry
            # should cover what's actually shown, not just the CLI's sideload-backlog-only default.
            threading.Thread(target=_run_retry_bg, args=(limit, bool(body.get("sideload_only"))),
                             daemon=True).start()
            return self._send(202, {"started": True})

        if path == "/api/archive-gone":   # mark permanently-404/410 stuck items ARCHIVED (no network)
            res = sideload.archive_gone(execute=True, sideload_only=bool(body.get("sideload_only")),
                                        out=lambda *a, **k: None)
            return self._send(200, res)

        return self._send(404, {"error": "not found"})


def serve():
    host = config.HOST
    if not auth.enabled() and host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  [web] ⚠️  APP_PASSWORD not set — refusing to bind to {host}; using 127.0.0.1 only.")
        host = "127.0.0.1"
    srv = ThreadingHTTPServer((host, config.PORT), Handler)
    print(f"  [web] dashboard at http://{host}:{config.PORT}"
          + ("  (auth ON)" if auth.enabled() else "  (auth OFF — localhost only)"))
    if auth.enabled() and not config.SESSION_SECRET:
        print("  [web] note: SESSION_SECRET unset — cookies signed with APP_PASSWORD (set it for stable sessions)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  shutting down")
        srv.shutdown()

"""Local web app: serves the dashboard and a small JSON API.
Uses ThreadingHTTPServer so the Telegram poller and browser requests coexist.
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import auth, config, db, netguard, pipeline, revisit, usage

STATIC = config.ROOT / "app" / "static"
MAX_BODY = 1_000_000   # 1 MB request cap
MAX_URL = 2048

_login_hits = {}
_login_lock = threading.Lock()


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

        if path in ("/", "/index.html"):
            try:
                return self._send(200, (STATIC / "index.html").read_text(encoding="utf-8"),
                                  "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, f"dashboard missing: {e}", "text/plain")

        if path == "/api/stats":
            con = db.connect()
            statuses = {r["status"]: r["n"] for r in
                        con.execute("SELECT status, count(*) n FROM items GROUP BY status")}
            types = {r["content_type"] or "?": r["n"] for r in
                     con.execute("SELECT content_type, count(*) n FROM items GROUP BY content_type")}
            revisit_due = revisit.due_count(con)
            con.close()
            return self._send(200, {"statuses": statuses, "types": types,
                                    "revisit_due": revisit_due})

        if path == "/api/collections":
            con = db.connect()
            cats = _rows(con, "SELECT name, collection FROM categories ORDER BY name")
            items = _rows(con,
                          "SELECT id, collection, name, note, link, done, item_id "
                          "FROM collection_items ORDER BY done, created_at DESC LIMIT 500")
            due = _rows(con,
                        "SELECT id, title, category, content_type, raw_url, deadline, "
                        "revisit_count, progress FROM items WHERE learn_status='active' "
                        "AND deadline IS NOT NULL AND deadline <= ? ORDER BY deadline LIMIT 100",
                        (db.now(),))
            con.close()
            return self._send(200, {"categories": cats, "items": items, "due": due})

        if path == "/api/usage":
            con = db.connect()
            data = usage.summary(con)
            con.close()
            return self._send(200, data)

        m = re.match(r"^/api/items/([0-9a-f]+)$", path)
        if m:
            item_id = m.group(1)
            con = db.connect()
            item = con.execute(
                "SELECT id, content_type, action, status, raw_url, confidence, created_at, "
                "title, source, category, priority AS item_priority, deadline, revisit_stage, "
                "revisit_count, learn_status, progress "
                "FROM items WHERE id=?", (item_id,)).fetchone()
            if not item:
                con.close()
                return self._send(404, {"error": "not found"})
            ex = con.execute(
                "SELECT summary, key_points, list_items, translation, detected_language, source, model, transcript "
                "FROM extractions WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
                (item_id,)).fetchone()
            task = con.execute(
                "SELECT id, title, description, suggested_use_case, priority, status, tags "
                "FROM tasks WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
                (item_id,)).fetchone()
            logs = _rows(con,
                         "SELECT step, status, detail, created_at FROM processing_logs "
                         "WHERE item_id=? ORDER BY created_at", (item_id,))
            coll = _rows(con,
                         "SELECT id, collection, name, note, link, done FROM collection_items "
                         "WHERE item_id=? ORDER BY done, created_at", (item_id,))
            con.close()
            return self._send(200, {
                "item": dict(item),
                "extraction": dict(ex) if ex else None,
                "task": dict(task) if task else None,
                "collection_items": coll,
                "logs": logs})

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

        if path == "/api/tasks":
            con = db.connect()
            tasks = _rows(con,
                          "SELECT t.id, t.title, t.description, t.suggested_use_case, t.priority, "
                          "t.status, t.tags, i.content_type, i.action, i.raw_url, i.id AS item_id, "
                          "e.list_items "
                          "FROM tasks t JOIN items i ON i.id = t.item_id "
                          "LEFT JOIN extractions e ON e.item_id = i.id "
                          "GROUP BY t.id ORDER BY t.created_at DESC LIMIT 200")
            con.close()
            return self._send(200, tasks)

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

        m = re.match(r"^/api/tasks/([0-9a-f]+)$", path)
        if m:
            status = (body.get("status") or "").upper()
            if status not in ("TODO", "IN_PROGRESS", "DONE", "DISMISSED"):
                return self._send(400, {"error": "bad status"})
            con = db.connect()
            con.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                        (status, db.now(), m.group(1)))
            con.commit(); con.close()
            return self._send(200, {"ok": True})

        m = re.match(r"^/api/items/([0-9a-f]+)/reprocess$", path)
        if m:
            return self._send(200, pipeline.reprocess(m.group(1)) or {"error": "not found"})

        m = re.match(r"^/api/items/([0-9a-f]+)/action$", path)
        if m:
            return self._send(200, pipeline.set_action(m.group(1), body.get("action", "")))

        m = re.match(r"^/api/items/([0-9a-f]+)/revisit$", path)
        if m:
            return self._send(200, revisit.mark(m.group(1), body.get("action", "")))

        m = re.match(r"^/api/collection-items/([0-9a-f]+)/toggle$", path)
        if m:
            con = db.connect()
            cur = con.execute("UPDATE collection_items SET done = 1 - done WHERE id=?", (m.group(1),))
            con.commit()
            row = con.execute("SELECT done FROM collection_items WHERE id=?", (m.group(1),)).fetchone()
            con.close()
            if not cur.rowcount:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"ok": True, "done": row["done"]})

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
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  shutting down")
        srv.shutdown()

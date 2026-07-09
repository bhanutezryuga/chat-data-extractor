#!/usr/bin/env python3
"""
Chat Data Extractor — proof-of-concept spike (pure stdlib).

Validates the core idea before the real Cloudflare/TypeScript build:
    link -> classify (rules engine) -> fetch description -> extract + make a task -> dashboard

This is a THROWAWAY spike. It reuses the real schema (db/schema.sql) and rules
(db/seed_rules.sql) so what we learn maps 1:1 onto the production design.

v1 = description-only. Reels/shorts/videos with no usable description go to
NEEDS_REVIEW (video analysis is V2).

Usage:
    python spike/run.py demo               # ingest sample URLs, build dashboard
    python spike/run.py ingest <url>       # ingest one URL
    python spike/run.py dashboard          # rebuild spike/dashboard.html
    python spike/run.py reset              # wipe the spike DB

Gemini: set GEMINI_API_KEY to use real extraction. Without it, a deterministic
offline stub runs so the pipeline is still demonstrable. Force stub with --stub.
"""
import json, os, re, sqlite3, sys, uuid, html, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH    = os.path.join(ROOT, "spike", "poc.db")
SCHEMA_SQL = os.path.join(ROOT, "db", "schema.sql")
SEED_SQL   = os.path.join(ROOT, "db", "seed_rules.sql")
DASH_HTML  = os.path.join(ROOT, "spike", "dashboard.html")
USER_ID    = "user_default"
GEMINI_MODEL = "gemini-2.5-flash"
MIN_SIGNAL = 120  # chars of usable description before we trust description-only

SAMPLE_URLS = [
    "https://arxiv.org/pdf/1706.03762",                 # PDF: "Attention Is All You Need"
    "https://en.wikipedia.org/wiki/Retrieval-augmented_generation",  # article
    "https://www.youtube.com/shorts/aBcDeFgHiJk",        # short -> thin desc -> NEEDS_REVIEW (v1)
    "https://www.instagram.com/reel/CxyzAbc123/",        # reel  -> thin desc -> NEEDS_REVIEW (v1)
]

# ---------------------------------------------------------------- db helpers
def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    fresh = not os.path.exists(DB_PATH)
    con = db()
    con.executescript(open(SCHEMA_SQL, encoding="utf-8").read())
    con.executescript(open(SEED_SQL, encoding="utf-8").read())
    con.commit()
    if fresh:
        print(f"  initialized spike DB at {DB_PATH}")
    con.close()

def log(con, item_id, step, status, detail=""):
    con.execute(
        "INSERT INTO processing_logs (id,item_id,step,status,detail,created_at) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, item_id, step, status, detail[:500], now()))

# ---------------------------------------------------------------- 1. classify
def classify(con, url):
    rules = con.execute(
        "SELECT * FROM rules WHERE enabled=1 ORDER BY priority ASC").fetchall()
    host = urllib.parse.urlparse(url).netloc.lower()
    for r in rules:
        kind, pat = r["matcher_kind"], r["matcher"]
        if kind == "url_regex" and re.search(pat, url, re.I):
            return r
        if kind == "host" and re.search(pat, host, re.I):
            return r
    return None  # -> unknown

# ---------------------------------------------------------------- 2. fetch
def http_get(url, accept="text/html", limit=200_000):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (chat-data-extractor spike)",
        "Accept": accept})
    with urllib.request.urlopen(req, timeout=15) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read(limit)
    return ctype, raw

def strip_html(h):
    h = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", h)
    text = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()

def meta_content(h, *names):
    for n in names:
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)' % re.escape(n),
            h, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""

def fetch_content(url, strategy):
    """Return (text, metadata_dict). Best-effort; network may be unavailable."""
    meta = {"strategy": strategy, "fetched": False}
    try:
        if strategy == "description_first" and ("youtube.com" in url or "youtu.be" in url):
            oe = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
                {"url": url, "format": "json"})
            try:
                _, raw = http_get(oe, accept="application/json")
                j = json.loads(raw.decode("utf-8", "replace"))
                meta.update(fetched=True, title=j.get("title"), author=j.get("author_name"))
                return (j.get("title", ""), meta)
            except Exception:
                pass  # fall through to generic fetch

        if strategy == "pdf":
            # Real system hands the PDF bytes to Gemini natively. The spike can't
            # parse PDF in stdlib, so we capture what the link/headers tell us.
            try:
                ctype, raw = http_get(url, accept="application/pdf", limit=4096)
                meta.update(fetched=True, content_type_header=ctype, bytes_peeked=len(raw))
            except Exception as e:
                meta["fetch_error"] = str(e)
            slug = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
            return (f"PDF document: {slug or url}", meta)

        # description_first (non-youtube) and readability: fetch HTML, read meta+body
        ctype, raw = http_get(url)
        h = raw.decode("utf-8", "replace")
        title = meta_content(h, "og:title", "twitter:title") or \
                (re.search(r"(?is)<title>(.*?)</title>", h) or [None, ""])[1].strip()
        desc = meta_content(h, "og:description", "description", "twitter:description")
        body = strip_html(h)[:3000] if strategy == "readability" else ""
        meta.update(fetched=True, title=title, og_description=desc)
        text = " ".join(p for p in [title, desc, body] if p).strip()
        return (text, meta)
    except Exception as e:
        meta["fetch_error"] = str(e)
        return ("", meta)

def has_signal(text):
    if not text or len(text) < MIN_SIGNAL:
        return False
    stripped = re.sub(r"[#@]\w+|\s+|[\U0001F000-\U0001FAFF]", "", text)
    return len(stripped) >= MIN_SIGNAL * 0.6

# ---------------------------------------------------------------- 3. analyze
def analyze_gemini(api_key, rule, url, text):
    prompt = (
        f"{rule['action_template']}\n\n"
        f"CONTENT TYPE: {rule['content_type']}\nPURPOSE: {rule['purpose']}\n"
        f"SOURCE URL: {url}\nCONTENT:\n{text[:8000]}\n\n"
        'Respond ONLY with JSON of shape: {"summary": str, "key_points": [str], '
        '"task": {"title": str, "description": str, "suggested_use_case": str, '
        '"priority": "LOW|MEDIUM|HIGH", "tags": [str]}, "confidence": 0.0}')
    endpoint = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_MODEL}:generateContent?key={api_key}")
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}}).encode()
    req = urllib.request.Request(endpoint, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode())
    text_out = out["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(text_out)
    data["_model"] = GEMINI_MODEL
    return data

def analyze_stub(rule, url, text):
    """Deterministic offline extraction so the pipeline runs without a key/network."""
    snippet = (text or url).strip()
    summary = (snippet[:200] + "…") if len(snippet) > 200 else snippet or f"Shared {rule['content_type']}: {url}"
    points = [s.strip() for s in re.split(r"[.!?]\s", snippet) if s.strip()][:3] or ["(no description available)"]
    title = (points[0][:60] if points else rule["content_type"].title())
    return {
        "summary": summary,
        "key_points": points,
        "task": {
            "title": f"Review {rule['content_type']}: {title}",
            "description": f"Look at this {rule['content_type']} and decide a next step.",
            "suggested_use_case": f"Captured from chat; classified as {rule['content_type']}.",
            "priority": "MEDIUM",
            "tags": [rule["content_type"]],
        },
        "confidence": 0.4,
        "_model": "offline-stub",
    }

# ---------------------------------------------------------------- pipeline
def process(con, item_id, rule, url, use_gemini, api_key):
    text, meta = fetch_content(url, rule["extraction_strategy"])
    log(con, item_id, "fetch", "ok" if meta.get("fetched") else "warn",
        meta.get("fetch_error", f"chars={len(text)}"))

    # v1 description-only: thin video-ish content is deferred, not downloaded
    if rule["extraction_strategy"] == "description_first" and not has_signal(text):
        con.execute("UPDATE items SET status='NEEDS_REVIEW', content_type=?, rule_id=?, "
                    "confidence=?, updated_at=? WHERE id=?",
                    (rule["content_type"], rule["id"], 0.0, now(), item_id))
        log(con, item_id, "classify", "warn",
            "description too thin; video analysis is V2 -> NEEDS_REVIEW")
        con.commit()
        return "NEEDS_REVIEW"

    try:
        data = (analyze_gemini(api_key, rule, url, text) if use_gemini
                else analyze_stub(rule, url, text))
        log(con, item_id, "extract", "ok", f"model={data.get('_model')}")
    except Exception as e:
        con.execute("UPDATE items SET status='FAILED', updated_at=? WHERE id=?", (now(), item_id))
        log(con, item_id, "extract", "error", str(e))
        con.commit()
        return "FAILED"

    con.execute(
        "INSERT INTO extractions (id,item_id,summary,transcript,key_points,raw_metadata,source,model,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, item_id, data.get("summary"), None,
         json.dumps(data.get("key_points", [])), json.dumps(meta),
         rule["extraction_strategy"], data.get("_model"), now()))

    t = data.get("task", {})
    con.execute(
        "INSERT INTO tasks (id,item_id,user_id,title,description,suggested_use_case,priority,status,tags,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, item_id, USER_ID, t.get("title", "Untitled"),
         t.get("description"), t.get("suggested_use_case"),
         (t.get("priority") or "MEDIUM").upper(), "TODO",
         json.dumps(t.get("tags", [])), now(), now()))

    con.execute("UPDATE items SET status='ACTIONABLE', content_type=?, rule_id=?, "
                "confidence=?, updated_at=? WHERE id=?",
                (rule["content_type"], rule["id"], data.get("confidence", 0.5), now(), item_id))
    log(con, item_id, "generate_task", "ok", t.get("title", ""))
    con.commit()
    return "ACTIONABLE"

def ingest(url, use_gemini, api_key):
    con = db()
    item_id = uuid.uuid4().hex
    con.execute("INSERT INTO items (id,user_id,raw_text,raw_url,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (item_id, USER_ID, url, url, "PROCESSING", now(), now()))
    con.commit()
    rule = classify(con, url)
    if rule is None:
        con.execute("UPDATE items SET status='NEEDS_REVIEW', content_type='unknown', updated_at=? WHERE id=?",
                    (now(), item_id))
        log(con, item_id, "classify", "warn", "no rule matched -> unknown")
        con.commit(); con.close()
        print(f"  {url}\n    -> unknown -> NEEDS_REVIEW")
        return
    log(con, item_id, "classify", "ok", f"{rule['name']} -> {rule['content_type']}")
    status = process(con, item_id, rule, url, use_gemini, api_key)
    con.close()
    print(f"  {url}\n    -> {rule['content_type']:8s} -> {status}")

# ---------------------------------------------------------------- dashboard
def esc(s):
    return html.escape(str(s if s is not None else ""))

def render_dashboard():
    con = db()
    items = con.execute("SELECT * FROM items ORDER BY created_at DESC").fetchall()
    counts = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, count(*) n FROM items GROUP BY status")}
    tasks = con.execute(
        "SELECT t.*, i.content_type, i.raw_url FROM tasks t JOIN items i ON i.id=t.item_id "
        "ORDER BY t.created_at DESC").fetchall()
    extr = {r["item_id"]: r for r in con.execute("SELECT * FROM extractions").fetchall()}
    con.close()

    color = {"ACTIONABLE": "#16a34a", "PROCESSING": "#ca8a04", "NEW": "#64748b",
             "NEEDS_REVIEW": "#d97706", "FAILED": "#dc2626"}
    chips = "".join(
        f'<span class="chip" style="background:{color.get(s,"#888")}">{esc(s)} {n}</span>'
        for s, n in counts.items())

    item_rows = ""
    for it in items:
        ex = extr.get(it["id"])
        summary = esc(ex["summary"]) if ex else ""
        item_rows += (
            f'<div class="item"><div><b>{esc(it["content_type"] or "?")}</b> '
            f'<span class="status" style="color:{color.get(it["status"],"#888")}">{esc(it["status"])}</span></div>'
            f'<a href="{esc(it["raw_url"])}" target="_blank">{esc(it["raw_url"])}</a>'
            f'<div class="sum">{summary}</div></div>')

    cols = {"TODO": "", "IN_PROGRESS": "", "DONE": ""}
    for t in tasks:
        prio = t["priority"]
        pc = {"HIGH": "#dc2626", "MEDIUM": "#ca8a04", "LOW": "#16a34a"}.get(prio, "#888")
        card = (f'<div class="card"><div class="prio" style="background:{pc}">{esc(prio)}</div>'
                f'<b>{esc(t["title"])}</b><div class="uc">{esc(t["suggested_use_case"])}</div>'
                f'<div class="tag">{esc(t["content_type"])}</div></div>')
        cols.setdefault(t["status"], "")
        cols[t["status"]] = cols.get(t["status"], "") + card

    kanban = "".join(
        f'<div class="kcol"><h3>{esc(k)} ({cols[k].count("card") })</h3>{cols[k] or "<div class=empty>—</div>"}</div>'
        for k in ["TODO", "IN_PROGRESS", "DONE"])

    page = f"""<!doctype html><meta charset=utf-8>
<title>Chat Data Extractor — spike dashboard</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}}
 header{{padding:16px 24px;background:#1e293b;position:sticky;top:0}}
 h1{{margin:0 0 8px;font-size:18px}} .chip{{color:#fff;padding:2px 10px;border-radius:999px;margin-right:6px;font-size:12px}}
 .wrap{{display:grid;grid-template-columns:360px 1fr;gap:0;height:calc(100vh - 78px)}}
 .feed{{overflow:auto;border-right:1px solid #334155;padding:12px}}
 .item{{background:#1e293b;border-radius:8px;padding:10px;margin-bottom:8px}}
 .item a{{color:#60a5fa;font-size:12px;word-break:break-all;text-decoration:none}}
 .status{{font-weight:700;font-size:12px;float:right}} .sum{{color:#94a3b8;font-size:12px;margin-top:6px}}
 .board{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:12px;overflow:auto}}
 .kcol{{background:#1e293b;border-radius:8px;padding:10px}} .kcol h3{{margin:0 0 8px;font-size:13px;color:#cbd5e1}}
 .card{{background:#0f172a;border:1px solid #334155;border-radius:8px;padding:10px;margin-bottom:8px;position:relative}}
 .prio{{position:absolute;top:8px;right:8px;color:#fff;font-size:10px;padding:1px 6px;border-radius:4px}}
 .uc{{color:#94a3b8;font-size:12px;margin-top:4px}} .tag{{margin-top:6px;font-size:11px;color:#38bdf8}}
 .empty{{color:#475569}} footer{{padding:8px 24px;color:#64748b;font-size:11px}}
</style>
<header><h1>Chat Data Extractor <span style="color:#64748b;font-weight:400">— proof-of-concept</span></h1>{chips}</header>
<div class="wrap">
 <div class="feed"><h3 style="margin-top:0;color:#cbd5e1">Items</h3>{item_rows or "<i>none yet</i>"}</div>
 <div class="board">{kanban}</div>
</div>
<footer>Generated {now()} UTC · spike DB: spike/poc.db · v1 description-only (video analysis = V2)</footer>
"""
    open(DASH_HTML, "w", encoding="utf-8").write(page)
    print(f"  dashboard -> {DASH_HTML}")

# ---------------------------------------------------------------- cli
def main():
    args = [a for a in sys.argv[1:] if a != "--stub"]
    force_stub = "--stub" in sys.argv
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    use_gemini = bool(api_key) and not force_stub
    cmd = args[0] if args else "demo"

    if cmd == "reset":
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        print("  spike DB removed")
        return

    init_db()
    mode = f"Gemini ({GEMINI_MODEL})" if use_gemini else "offline stub (no GEMINI_API_KEY)"
    print(f"  extraction mode: {mode}\n")

    if cmd == "ingest":
        if len(args) < 2:
            print("usage: python spike/run.py ingest <url>"); sys.exit(1)
        ingest(args[1], use_gemini, api_key)
    elif cmd == "demo":
        for u in SAMPLE_URLS:
            ingest(u, use_gemini, api_key)
    elif cmd == "dashboard":
        pass
    else:
        print(__doc__); sys.exit(1)

    render_dashboard()

if __name__ == "__main__":
    main()

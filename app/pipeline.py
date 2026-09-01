"""The core pipeline: ingest -> classify -> fetch -> extract -> task.

Used by both the Telegram poller and the web 'add link' box.

v1: description-first. v2: if a reel/short/video has no usable description, analyze the
video with Gemini multimodal (YouTube by URL; Instagram/TikTok via yt-dlp if installed).
Expensive video calls are skipped when the daily Gemini budget is reached.
"""
import hashlib
import json
import re
import sqlite3
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from . import config, db, gemini, instagram, logseq, media, revisit, usage
from .rules import classify
from .fetch import fetch

URL_RE = re.compile(r'https?://[^\s<>"\')]+')
VIDEO_TYPES = ("reel", "short", "video")


def extract_urls(text):
    return URL_RE.findall(text or "")


# Tracking/junk query params to ignore when deciding if two links are "the same".
_DROP_PARAMS = {"igsh", "igshid", "utm_source", "utm_medium", "utm_campaign", "utm_term",
                "utm_content", "fbclid", "gclid", "si", "ref", "ref_src", "feature"}


def _norm_url(u):
    """Normalize a URL for dedup: lowercase host, drop tracking params + fragment + trailing slash
    (so the same reel/article pasted with different share tokens is recognized as one)."""
    u = (u or "").strip()
    try:
        p = urlparse(u)
        if not p.scheme:
            return u.lower().rstrip("/")
        q = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _DROP_PARAMS])
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", "", q, ""))
    except Exception:
        return u.lower().rstrip("/")


def find_duplicate(con, user_id, url):
    """Return an existing item row with the same normalized URL, or None — so the same link pasted
    again is neither re-saved nor re-processed."""
    n = _norm_url(url)
    if not n:
        return None
    for r in con.execute("SELECT id, content_type, raw_url FROM items "
                         "WHERE user_id=? AND raw_url IS NOT NULL", (user_id,)):
        if _norm_url(r["raw_url"]) == n:
            return r
    return None


_NORM_RE = re.compile(r"[^0-9a-z]+")


def _norm_name(s):
    return _NORM_RE.sub(" ", str(s or "").lower()).strip()


def content_fingerprint(data, category):
    """A stable key for the *content* of a capture, so the same material saved from a different
    URL (a reshared reel, a repost) is recognized as a duplicate even though URL-dedup can't see it.
    For list/recommendation captures the identity is the set of item names; otherwise it's the
    title. Category-scoped so two unrelated lists don't collide. Returns None when there's too
    little to dedup on safely."""
    names = []
    for x in (data.get("list_items") or []):
        nm = _norm_name(x.get("name") if isinstance(x, dict) else x)
        if nm:
            names.append(nm)
    if names:
        basis = "list:" + "|".join(sorted(set(names)))
    else:
        title = _norm_name(data.get("title") or (data.get("task") or {}).get("title"))
        if len(title) < 6:                              # too thin to dedup on safely
            return None
        basis = "title:" + title
    basis = _norm_name(category) + "::" + basis
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _find_content_dup(con, item_id, fp):
    """The earliest ACTIONABLE item with the same content fingerprint (the original this capture
    duplicates), or None. Duplicates always point at the original, never at each other."""
    if not fp:
        return None
    row = con.execute(
        "SELECT id FROM items WHERE content_key=? AND id!=? AND status='ACTIONABLE' "
        "ORDER BY created_at LIMIT 1", (fp, item_id)).fetchone()
    return row["id"] if row else None


def _dup_result(item_id, rule, dup_of):
    return {"id": item_id, "content_type": rule["content_type"] if rule else None,
            "status": "DUPLICATE", "task": None, "duplicate_of": dup_of}


def has_signal(text):
    if not text or len(text) < config.MIN_SIGNAL:
        return False
    stripped = re.sub(r"[#@]\w+|\s+", "", text)
    return len(stripped) >= config.MIN_SIGNAL * 0.6


def _set_status(con, item_id, status, rule=None, confidence=None):
    if rule is not None:
        con.execute("UPDATE items SET status=?, content_type=?, rule_id=?, confidence=?, updated_at=? WHERE id=?",
                    (status, rule["content_type"], rule["id"], confidence, db.now(), item_id))
    else:
        con.execute("UPDATE items SET status=?, updated_at=? WHERE id=?", (status, db.now(), item_id))


def _write_result(con, item_id, rule, meta, data, kind):
    if data.get("_usage"):
        usage.record(con, item_id, data.get("_model"), kind, data["_usage"])
    con.execute(
        "INSERT INTO extractions (id,item_id,summary,transcript,key_points,list_items,translation,detected_language,recipe,raw_metadata,source,model,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (db.new_id(), item_id, data.get("summary"), data.get("transcript"),
         json.dumps(data.get("key_points", [])), json.dumps(data.get("list_items", [])),
         data.get("translation"), data.get("detected_language"),
         json.dumps(data.get("recipe") or {}),
         json.dumps(meta), f"gemini_{kind}" if config.USE_GEMINI else "stub",
         data.get("_model"), db.now()))
    t = data.get("task", {}) or {}
    con.execute(
        "INSERT INTO tasks (id,item_id,user_id,title,description,suggested_use_case,priority,status,tags,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (db.new_id(), item_id, config.USER_ID, t.get("title", "Untitled"),
         t.get("description"), t.get("suggested_use_case"),
         (t.get("priority") or "MEDIUM").upper(), "TODO",
         json.dumps(t.get("tags", [])), db.now(), db.now()))
    _set_status(con, item_id, "ACTIONABLE", rule, data.get("confidence", 0.5))
    con.execute("UPDATE items SET action=? WHERE id=?", (_decide_action(data), item_id))
    dup_of = _write_knowledge(con, item_id, data)
    db.log(con, item_id, "generate_task", "ok", t.get("title", ""))
    if dup_of:
        return dup_of                 # a content duplicate — no Logseq page (see _write_knowledge)
    logseq.export_item(item_id)   # post-commit (db.log committed above); no-op unless configured
    return None


def _domain(url):
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else (h or None)
    except Exception:
        return None


def _valid_category(con, name):
    """Match Gemini's category to a seeded one (case-insensitive); fall back to 'Other'."""
    name = (name or "").strip()
    row = con.execute("SELECT name, collection FROM categories WHERE lower(name)=lower(?)",
                      (name,)).fetchone()
    if row:
        return row["name"], row["collection"]
    other = con.execute("SELECT name, collection FROM categories WHERE name='Other'").fetchone()
    return ("Other", None) if not other else (other["name"], other["collection"])


def _write_knowledge(con, item_id, data):
    """Turn a processed item into a knowledge record: title, source domain, category, priority,
    a spaced-repetition schedule, and materialized collection items. Returns the id of an existing
    item this one duplicates (same content from a different URL), or None."""
    it = con.execute("SELECT raw_url FROM items WHERE id=?", (item_id,)).fetchone()
    url = it["raw_url"] if it else None
    task = data.get("task") or {}
    title = (data.get("title") or task.get("title") or "").strip()[:200] or None
    category, collection = _valid_category(con, data.get("category"))
    priority = (task.get("priority") or "MEDIUM").upper()
    fp = content_fingerprint(data, category)
    con.execute("UPDATE items SET title=?, source=?, category=?, priority=?, content_key=? WHERE id=?",
                (title, _domain(url), category, priority, fp, item_id))

    dup_of = _find_content_dup(con, item_id, fp)
    if dup_of:
        # Same material already captured (typically the same reel reshared under a new URL). Keep
        # this row as a lightweight tombstone so re-sends are cheap to detect, but don't duplicate
        # the knowledge: no collection rows, no revisit schedule, and no second Logseq page.
        con.execute("DELETE FROM collection_items WHERE item_id=?", (item_id,))
        con.execute("UPDATE items SET status='DUPLICATE', deadline=NULL WHERE id=?", (item_id,))
        db.log(con, item_id, "dedup", "ok", f"content duplicate of {dup_of}")
        return dup_of

    # Rebuild the collection, but preserve the user's checked-off state — and reuse the same row
    # id, so the Logseq `cid::` links don't churn — across a reprocess, matched by name.
    prev = {r["name"]: (r["id"], r["done"]) for r in
            con.execute("SELECT name, id, done FROM collection_items WHERE item_id=?", (item_id,))}
    con.execute("DELETE FROM collection_items WHERE item_id=?", (item_id,))
    if collection:
        for x in (data.get("list_items") or []):
            if isinstance(x, dict):
                name, note, link = (x.get("name") or "").strip(), x.get("note"), x.get("link")
            else:
                name, note, link = str(x).strip(), None, None
            if not name:
                continue
            if link and str(link).lower() == "link not available":
                link = None
            keep = prev.get(name[:300])
            cid, done = (keep[0], keep[1]) if keep else (db.new_id(), 0)
            con.execute(
                "INSERT INTO collection_items (id,user_id,item_id,collection,name,note,link,done,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, config.USER_ID, item_id, collection, name[:300], note, link, done, db.now()))

    revisit.schedule_new(con, item_id)
    return None


def _decide_action(data):
    """Smart default: Gemini's suggested_action, with a safe heuristic fallback."""
    a = (data.get("suggested_action") or "").lower()
    if a in config.ACTIONS:
        return a
    if (data.get("translation") or "").strip():
        return "translate"
    if len(data.get("list_items") or []) >= 2:
        return "list"
    return "note"


def set_action(item_id, action):
    """Override the active action. List/Note are instant (re-emphasis); Translate
    computes a translation on demand if one wasn't pre-generated."""
    action = (action or "").lower()
    if action not in config.ACTIONS:
        return {"error": "unknown action"}
    con = db.connect()
    it = con.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        con.close()
        return {"error": "not found"}
    if action == "translate" and config.USE_GEMINI:
        ex = con.execute("SELECT id, translation, transcript, summary FROM extractions "
                         "WHERE item_id=? ORDER BY created_at DESC LIMIT 1", (item_id,)).fetchone()
        if ex and not (ex["translation"] or "").strip():
            base = ex["transcript"] or ex["summary"] or ""
            try:
                data = gemini.translate(base)
                con.execute("UPDATE extractions SET translation=?, detected_language=COALESCE(detected_language,?) WHERE id=?",
                            (data.get("translation"), data.get("detected_language"), ex["id"]))
                if data.get("_usage"):
                    usage.record(con, item_id, data.get("_model"), "translate", data["_usage"])
            except Exception as e:
                con.close()
                return {"error": f"translate failed: {e}"}
    con.execute("UPDATE items SET action=?, updated_at=? WHERE id=?", (action, db.now(), item_id))
    con.commit()
    con.close()
    return {"id": item_id, "action": action}


def _result(item_id, rule, status, task=None):
    return {"id": item_id, "content_type": rule["content_type"] if rule else None,
            "status": status, "task": task}


def _run_text_extraction(con, item_id, rule, url, text, pdf_bytes):
    try:
        if config.USE_GEMINI:
            data = gemini.extract(rule, url, text, pdf_bytes)
        else:
            data = gemini.stub(rule, url, text)
        db.log(con, item_id, "extract", "ok", f"model={data.get('_model')}")
    except Exception as e:
        _set_status(con, item_id, "FAILED")
        db.log(con, item_id, "extract", "error", str(e))
        con.commit()
        return _result(item_id, rule, "FAILED")
    dup_of = _write_result(con, item_id, rule, {"text_len": len(text or "")}, data,
                           "pdf" if pdf_bytes else "text")
    con.commit()
    if dup_of:
        return _dup_result(item_id, rule, dup_of)
    return _result(item_id, rule, "ACTIONABLE", (data.get("task") or {}).get("title"))


def _run_video_analysis(con, item_id, rule, url):
    """V2: analyze the actual video. Returns a result dict."""
    if not usage.budget_ok(con):
        _set_status(con, item_id, "NEEDS_REVIEW", rule, 0)
        db.log(con, item_id, "extract", "warn", "daily Gemini budget reached -> deferred")
        con.commit()
        return _result(item_id, rule, "NEEDS_REVIEW")
    try:
        if media.is_youtube(url):
            data = gemini.analyze_video(rule, url, youtube_url=media.youtube_watch_url(url))
            db.log(con, item_id, "extract", "ok", "video via YouTube URL")
        else:
            blob, info = media.download(url)
            if blob is None:
                _set_status(con, item_id, "NEEDS_REVIEW", rule, 0)
                db.log(con, item_id, "fetch", "warn", f"video download unavailable: {info}")
                con.commit()
                return _result(item_id, rule, "NEEDS_REVIEW")
            data = gemini.analyze_video(rule, url, video_bytes=blob)
            db.log(con, item_id, "extract", "ok", f"video via download ({len(blob)//1024} KB)")
    except Exception as e:
        # YouTube-by-URL can fail (region/age/availability) — try a download fallback once.
        if media.is_youtube(url) and media.ytdlp_available():
            blob, info = media.download(url)
            if blob is not None:
                try:
                    data = gemini.analyze_video(rule, url, video_bytes=blob)
                    db.log(con, item_id, "extract", "ok", "video via download (YouTube fallback)")
                    dup_of = _write_result(con, item_id, rule, {"source": "gemini_video"}, data, "video")
                    con.commit()
                    if dup_of:
                        return _dup_result(item_id, rule, dup_of)
                    return _result(item_id, rule, "ACTIONABLE", (data.get("task") or {}).get("title"))
                except Exception as e2:
                    e = e2
        _set_status(con, item_id, "FAILED")
        db.log(con, item_id, "extract", "error", str(e))
        con.commit()
        return _result(item_id, rule, "FAILED")
    dup_of = _write_result(con, item_id, rule, {"source": "gemini_video"}, data, "video")
    con.commit()
    if dup_of:
        return _dup_result(item_id, rule, dup_of)
    return _result(item_id, rule, "ACTIONABLE", (data.get("task") or {}).get("title"))


def _run_instagram(con, item_id, rule, url, caption_text):
    """V2 Instagram: caption + carousel images (vision) or video, via Instagram's web API."""
    if not usage.budget_ok(con):
        _set_status(con, item_id, "NEEDS_REVIEW", rule, 0)
        db.log(con, item_id, "extract", "warn", "daily Gemini budget reached -> deferred")
        con.commit()
        return _result(item_id, rule, "NEEDS_REVIEW")

    info = instagram.fetch_media(url)
    if info.get("error"):
        db.log(con, item_id, "fetch", "warn", f"instagram: {info['error']}")
        if has_signal(caption_text):                       # fall back to caption-only
            return _run_text_extraction(con, item_id, rule, url, caption_text, None)
        _set_status(con, item_id, "NEEDS_REVIEW", rule, 0)
        con.commit()
        return _result(item_id, rule, "NEEDS_REVIEW")

    caption = info.get("caption") or caption_text or ""
    try:
        if info.get("images"):
            imgs = []
            for u in info["images"][:6]:
                try:
                    imgs.append(instagram.download(u))
                except Exception:
                    pass
            if imgs:
                data = gemini.analyze_images(rule, url, imgs, caption)
                db.log(con, item_id, "extract", "ok", f"vision on {len(imgs)} image(s)")
                dup_of = _write_result(con, item_id, rule, {"source": "gemini_image", "images": len(imgs)},
                                       data, "image")
                con.commit()
                if dup_of:
                    return _dup_result(item_id, rule, dup_of)
                return _result(item_id, rule, "ACTIONABLE", (data.get("task") or {}).get("title"))
        if info.get("videos"):
            blob = instagram.download(info["videos"][0], limit=config.MAX_VIDEO_MB * 1_000_000)
            data = gemini.analyze_video(rule, url, video_bytes=blob)
            db.log(con, item_id, "extract", "ok", f"video via instagram api ({len(blob)//1024} KB)")
            dup_of = _write_result(con, item_id, rule, {"source": "gemini_video"}, data, "video")
            con.commit()
            if dup_of:
                return _dup_result(item_id, rule, dup_of)
            return _result(item_id, rule, "ACTIONABLE", (data.get("task") or {}).get("title"))
    except Exception as e:
        _set_status(con, item_id, "FAILED")
        db.log(con, item_id, "extract", "error", str(e))
        con.commit()
        return _result(item_id, rule, "FAILED")

    if has_signal(caption):                                # no media but a real caption
        return _run_text_extraction(con, item_id, rule, url, caption, None)
    _set_status(con, item_id, "NEEDS_REVIEW", rule, 0)
    db.log(con, item_id, "classify", "warn", "instagram: no usable media or caption")
    con.commit()
    return _result(item_id, rule, "NEEDS_REVIEW")


def _process_classified(con, item_id, rule, url, text, pdf_bytes):
    """Shared by ingest + reprocess once a rule is known."""
    db.log(con, item_id, "classify", "ok", f"{rule['name']} -> {rule['content_type']}")

    if instagram.is_instagram(url) and config.USE_GEMINI:
        return _run_instagram(con, item_id, rule, url, text)

    if rule["extraction_strategy"] == "description_first" and not pdf_bytes and not has_signal(text):
        # No usable description.
        if config.ENABLE_VIDEO and config.USE_GEMINI and rule["content_type"] in VIDEO_TYPES:
            return _run_video_analysis(con, item_id, rule, url)   # V2
        _set_status(con, item_id, "NEEDS_REVIEW", rule, 0)
        reason = ("video disabled / no key" if rule["content_type"] in VIDEO_TYPES
                  else "description too thin")
        db.log(con, item_id, "classify", "warn", f"{reason} -> NEEDS_REVIEW")
        con.commit()
        return _result(item_id, rule, "NEEDS_REVIEW")

    return _run_text_extraction(con, item_id, rule, url, text, pdf_bytes)


def ingest(raw_url=None, raw_text=None, attachment=None,
           source_chat_id=None, source_msg_id=None):
    """attachment: {'bytes','filename','mime'} for a direct file (e.g. Telegram PDF).
    Returns a small dict for acknowledgement, or None if it was a duplicate."""
    con = db.connect()
    item_id = db.new_id()
    url = raw_url or (attachment or {}).get("filename")
    if raw_url:
        dup = find_duplicate(con, config.USER_ID, raw_url)
        if dup:
            con.close()
            return {"id": dup["id"], "content_type": dup["content_type"],
                    "status": "DUPLICATE", "task": None}   # same link already saved
    try:
        con.execute(
            "INSERT INTO items (id,user_id,source_chat_id,source_msg_id,raw_text,raw_url,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, config.USER_ID, source_chat_id, source_msg_id, raw_text, url,
             "PROCESSING", db.now(), db.now()))
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return None  # duplicate (chat_id, msg_id)

    try:
        if attachment and ((attachment.get("mime", "").endswith("pdf"))
                           or (attachment.get("filename", "") or "").lower().endswith(".pdf")):
            rule = con.execute("SELECT * FROM rules WHERE id='rule_pdf'").fetchone()
            res = _run_text_extraction(con, item_id, rule, url,
                                       f"PDF document: {attachment.get('filename')}",
                                       attachment["bytes"])
        elif raw_url:
            rule = classify(con, raw_url)
            if rule is None:
                _set_status(con, item_id, "NEEDS_REVIEW")
                con.execute("UPDATE items SET content_type='unknown' WHERE id=?", (item_id,))
                db.log(con, item_id, "classify", "warn", "no rule matched -> unknown")
                con.commit()
                res = {"id": item_id, "content_type": "unknown", "status": "NEEDS_REVIEW", "task": None}
            else:
                text, pdf_bytes, meta = fetch(raw_url, rule["extraction_strategy"])
                db.log(con, item_id, "fetch", "ok" if meta.get("fetched") else "warn",
                       meta.get("fetch_error", f"chars={len(text)}"))
                res = _process_classified(con, item_id, rule, raw_url, text, pdf_bytes)
        else:
            _set_status(con, item_id, "NEEDS_REVIEW")
            con.commit()
            res = {"id": item_id, "content_type": None, "status": "NEEDS_REVIEW", "task": None}
    finally:
        con.close()
    return res


def requeue_stuck():
    """Re-run items left in PROCESSING by a previous crash. Called on startup.
    (On a fresh start nothing is genuinely processing, so every PROCESSING row is stale.)"""
    con = db.connect()
    ids = [r["id"] for r in con.execute("SELECT id FROM items WHERE status='PROCESSING'").fetchall()]
    con.close()
    for iid in ids:
        try:
            reprocess(iid)
        except Exception as e:
            c = db.connect()
            db.log(c, iid, "requeue", "error", str(e))
            c.commit()
            c.close()
    return ids


def create_pending(raw_url=None, raw_text=None, source_chat_id=None, source_msg_id=None):
    """Save a link WITHOUT processing it (the 'new <link>' command). Returns {id} or None (dup)."""
    con = db.connect()
    item_id = db.new_id()
    if raw_url:
        dup = find_duplicate(con, config.USER_ID, raw_url)
        if dup:
            con.close()
            return {"id": dup["id"], "status": "DUPLICATE"}   # same link already saved
    try:
        con.execute(
            "INSERT INTO items (id,user_id,source_chat_id,source_msg_id,raw_text,raw_url,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, config.USER_ID, source_chat_id, source_msg_id, raw_text, raw_url,
             "AWAITING_ACTION", db.now(), db.now()))
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return None
    con.close()
    return {"id": item_id, "status": "AWAITING_ACTION"}


def process_pending(item_id, action):
    """Process a pending item, then force the chosen action ('auto' keeps the smart default)."""
    res = reprocess(item_id)
    if action and action.lower() != "auto" and (res or {}).get("status") == "ACTIONABLE":
        set_action(item_id, action.lower())
    return res


def reprocess(item_id):
    """Re-run an existing item (e.g. a NEEDS_REVIEW reel once V2/yt-dlp is available)."""
    con = db.connect()
    it = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        con.close()
        return None
    con.execute("DELETE FROM tasks WHERE item_id=?", (item_id,))
    con.execute("DELETE FROM extractions WHERE item_id=?", (item_id,))
    _set_status(con, item_id, "PROCESSING")
    con.commit()

    url = it["raw_url"]
    rule = classify(con, url) if url else None
    if rule is None:
        _set_status(con, item_id, "NEEDS_REVIEW")
        con.commit(); con.close()
        return {"id": item_id, "status": "NEEDS_REVIEW"}
    text, pdf_bytes, meta = fetch(url, rule["extraction_strategy"])
    db.log(con, item_id, "fetch", "ok" if meta.get("fetched") else "warn", f"chars={len(text)}")
    res = _process_classified(con, item_id, rule, url, text, pdf_bytes)
    con.close()
    return {"id": item_id, "status": res["status"]}

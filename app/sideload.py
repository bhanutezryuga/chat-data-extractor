"""Bulk-import links from a text file into the capture pipeline — no Telegram, no server.

The file is one `<timestamp> <link>` per line (the timestamp is ignored; captures are stamped
`now`). Each new link is fed straight through `pipeline.ingest`, which already handles URL-dedup,
classify → fetch → Gemini extract → content-dedup, and writes the Logseq page. This module is a
thin, safe driver around that: a dry-run by default, an SSRF pre-check, a daily-budget stop, and a
throttle between links.

    python -m app.sideload <file> [--run] [--limit N] [--delay 2.0] [--allow-nonpublic]

Re-running is safe: already-saved links are skipped (dedup), so a run stopped on budget or a crash
resumes by simply running again.

`--retry` reprocesses items stuck in FAILED / NEEDS_REVIEW (mostly transient failures — rate-limits,
503s, blips) instead of reading a file:

    python -m app.sideload --retry [--run] [--limit N] [--delay S] [--all]
"""
import argparse
import hashlib
import time

from . import config, db, failures, instagram, netguard, pipeline, usage


# ---- parsing --------------------------------------------------------------

def parse_file(path):
    """Read the links file → (urls, malformed). Blank lines and `#` comments are skipped; a line
    with no http(s) URL is 'malformed' (returned as (line_no, text)) rather than aborting the run.
    The leading timestamp token is ignored — `extract_urls` finds the URL anywhere on the line."""
    urls, malformed = [], []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            found = pipeline.extract_urls(line)
            if found:
                urls.append(found[0])
            else:
                malformed.append((n, line.strip()))
    return urls, malformed


def _existing_norm_urls(con):
    """The normalized URL of every item already saved — the fast dedup set (mirrors what
    `pipeline.find_duplicate` decides, without an O(n) scan per link)."""
    return {pipeline._norm_url(r["raw_url"]) for r in con.execute(
        "SELECT raw_url FROM items WHERE user_id=? AND raw_url IS NOT NULL", (config.USER_ID,))}


def _sid(url):
    """Stable, URL-derived source_msg_id so the UNIQUE(source_chat_id, source_msg_id) constraint is
    a second idempotency guard on top of find_duplicate."""
    return hashlib.sha1(pipeline._norm_url(url).encode("utf-8")).hexdigest()


# ---- driver ---------------------------------------------------------------

def run(path, *, execute=False, limit=None, delay=2.0, allow_nonpublic=False,
        ingest_fn=None, out=print):
    """Parse `path`, classify links new-vs-duplicate, and (when execute=True) process the new ones
    through `ingest_fn` (default: pipeline.ingest). Returns a summary dict."""
    ingest_fn = ingest_fn or pipeline.ingest
    urls, malformed = parse_file(path)

    con = db.connect()
    existing = _existing_norm_urls(con)
    new, duplicate, seen = [], [], set()
    for u in urls:
        n = pipeline._norm_url(u)
        if n in seen or n in existing:      # already saved, or repeated within the file
            duplicate.append(u)
        else:
            seen.add(n)
            new.append(u)

    summary = {"parsed": len(urls), "new": len(new), "duplicate": len(duplicate),
               "malformed": len(malformed), "processed": 0, "by_status": {},
               "skipped_unsafe": 0, "budget_stopped": False}

    out(f"[sideload] {path}")
    out(f"  {len(urls)} link(s): {len(new)} new, {len(duplicate)} duplicate; "
        f"{len(malformed)} malformed line(s)")
    for n, text in malformed[:5]:
        out(f"    ! line {n}: {text[:80]}")
    if len(malformed) > 5:
        out(f"    ! (+{len(malformed) - 5} more malformed)")

    if not execute:
        out("  DRY RUN - nothing processed. Add --run to process the new link(s).")
        for u in new[:10]:
            out(f"    + {u}")
        if len(new) > 10:
            out(f"    + (+{len(new) - 10} more)")
        con.close()
        return summary

    todo = new if limit is None else new[:limit]
    out(f"  processing {len(todo)} link(s)" + (f" (limit {limit})" if limit is not None else "") + "...")
    for i, u in enumerate(todo, 1):
        if not allow_nonpublic and not netguard.is_safe_public_url(u):
            summary["skipped_unsafe"] += 1
            out(f"  [{i}/{len(todo)}] skip (non-public URL): {u}")
            continue
        if not usage.budget_ok(con):
            s = usage.summary(con)
            summary["budget_stopped"] = True
            out(f"  budget reached (tokens {s['today_tokens']}/{s['daily_token_budget']}, "
                f"requests {s['today_requests']}/{s['rpd_limit']}). Stopping - re-run later to resume.")
            break
        try:
            res = ingest_fn(raw_url=u, raw_text=u, source_chat_id="sideload", source_msg_id=_sid(u))
        except Exception as e:
            res = {"status": "ERROR"}
            out(f"  [{i}/{len(todo)}] ERROR: {u}: {e}")
        st = (res or {}).get("status", "DUPLICATE")   # a None result = UNIQUE-constraint duplicate
        summary["by_status"][st] = summary["by_status"].get(st, 0) + 1
        summary["processed"] += 1
        out(f"  [{i}/{len(todo)}] {st}: {u}")
        if delay and i < len(todo):
            time.sleep(delay)
    con.close()

    parts = ", ".join(f"{k}={v}" for k, v in sorted(summary["by_status"].items()))
    out(f"[sideload] done - processed {summary['processed']}"
        + (f" ({parts})" if parts else "")
        + (f", {summary['skipped_unsafe']} skipped-unsafe" if summary["skipped_unsafe"] else "")
        + (" - STOPPED on budget (re-run to resume)" if summary["budget_stopped"] else ""))
    return summary


def _recent_429(con, item_id):
    """True if this item logged an Instagram 429 in the last few minutes (this reprocess)."""
    return bool(con.execute(
        "SELECT 1 FROM processing_logs WHERE item_id=? AND detail LIKE '%429%' "
        "AND created_at >= datetime('now','-5 minutes') LIMIT 1", (item_id,)).fetchone())


def _is_gone(reason):
    """A permanently unrecoverable failure — a deleted/removed post (HTTP 404 Not Found / 410 Gone).
    Retrying these only wastes a request and, since they always re-fail, would trip the
    consecutive-non-recovery safety before the recoverable items are even reached."""
    return failures.classify(reason)[0] == "gone"


def retry(*, execute=False, limit=None, delay=8.0, sideload_only=True,
          reprocess_fn=None, out=print):
    """Reprocess items stuck in FAILED / NEEDS_REVIEW (mostly transient — rate-limits, 503s, blips).
    Gentle: one at a time with a delay, bailing fast on an Instagram logout, 2 consecutive 429s,
    4 consecutive non-recoveries, or the daily budget — so it never hammers a dead session.
    Dry-run unless execute=True. Returns a summary dict."""
    reprocess_fn = reprocess_fn or pipeline.reprocess
    con = db.connect()
    q = ("SELECT i.id, i.raw_url, i.status, "
         "(SELECT p.detail FROM processing_logs p WHERE p.item_id=i.id AND p.status IN ('warn','error') "
         " ORDER BY p.created_at DESC LIMIT 1) AS reason "
         "FROM items i WHERE i.status IN ('FAILED','NEEDS_REVIEW')"
         + ("" if not sideload_only else " AND i.source_chat_id='sideload'") + " ORDER BY i.created_at")
    rows = [dict(r) for r in con.execute(q)]
    con.close()

    # Drop permanently-gone items (deleted/removed posts) up front: retrying them is pointless and
    # they'd otherwise trip the consecutive-non-recovery bail-out before any recoverable item runs.
    gone = [r for r in rows if _is_gone(r.get("reason"))]
    live = [r for r in rows if not _is_gone(r.get("reason"))]

    before = {}
    for r in rows:
        before[r["status"]] = before.get(r["status"], 0) + 1
    summary = {"candidates": len(rows), "retryable": len(live), "skipped_gone": len(gone),
               "before": before, "processed": 0, "recovered": 0, "results": {}, "stopped": None}

    scope = "sideload only" if sideload_only else "all sources"
    out(f"[retry] {len(rows)} stuck item(s) [{scope}]: "
        + (", ".join(f"{k}={v}" for k, v in sorted(before.items())) or "none")
        + (f"  — {len(gone)} permanently gone (404/410), skipping" if gone else ""))
    if not execute:
        out("  DRY RUN - add --run to reprocess them.")
        return summary

    targets = live if limit is None else live[:limit]
    out(f"  reprocessing {len(targets)}" + (f" (limit {limit})" if limit is not None else "") + "...")
    consec_429 = consec_fail = 0
    for i, it in enumerate(targets, 1):
        if "instagram.com" in (it["raw_url"] or "") and not instagram._cookie_header():
            summary["stopped"] = "logged_out"
            out("  sessionid gone -> Instagram logged out -> STOP (refresh the cookie)"); break
        con = db.connect(); ok_budget = usage.budget_ok(con); con.close()
        if not ok_budget:
            summary["stopped"] = "budget"
            out("  daily Gemini budget reached -> STOP (resume later)"); break
        try:
            st = (reprocess_fn(it["id"]) or {}).get("status", "?")
        except Exception as e:
            st = "ERROR"; out(f"  [{i}/{len(targets)}] ERROR {it['id'][:8]}: {e}")
        con = db.connect(); got_429 = _recent_429(con, it["id"]); con.close()
        summary["results"][st] = summary["results"].get(st, 0) + 1
        summary["processed"] += 1
        if st == "ACTIONABLE":
            summary["recovered"] += 1
        out(f"  [{i}/{len(targets)}] {st}{' [429]' if got_429 else ''}  {(it['raw_url'] or it['id'])[:60]}")
        consec_429 = consec_429 + 1 if got_429 else 0
        consec_fail = 0 if st == "ACTIONABLE" else consec_fail + 1
        if consec_429 >= 2:
            summary["stopped"] = "rate_limited"; out("  2 consecutive 429s -> STOP before escalation"); break
        if consec_fail >= 4:
            summary["stopped"] = "consecutive_failures"; out("  4 non-recoveries in a row -> STOP"); break
        if delay and i < len(targets):
            time.sleep(delay)

    parts = ", ".join(f"{k}={v}" for k, v in sorted(summary["results"].items()))
    out(f"[retry] done - reprocessed {summary['processed']}, recovered {summary['recovered']}"
        + (f" ({parts})" if parts else "")
        + (f" - STOPPED: {summary['stopped']}" if summary["stopped"] else ""))
    return summary


def archive_gone(*, execute=False, sideload_only=True, out=print, ids=None):
    """Mark permanently-gone (deleted/removed: HTTP 404/410) stuck items as ARCHIVED so they drop off
    the failures panel and are never retried. No network, just a status change. Dry-run unless execute.
    ids: optionally restrict to these item ids (e.g. exactly what a preview showed). The result's
    "items" lists the matched [{id, raw_url}]."""
    con = db.connect()
    q = ("SELECT i.id, i.raw_url, i.status, "
         "(SELECT p.detail FROM processing_logs p WHERE p.item_id=i.id AND p.status IN ('warn','error') "
         " ORDER BY p.created_at DESC LIMIT 1) AS reason "
         "FROM items i WHERE i.status IN ('FAILED','NEEDS_REVIEW')"
         + ("" if not sideload_only else " AND i.source_chat_id='sideload'"))
    gone = [r for r in (dict(x) for x in con.execute(q)) if _is_gone(r.get("reason"))]
    if ids is not None:
        ids = set(ids)
        gone = [r for r in gone if r["id"] in ids]
    listed = [{"id": r["id"], "raw_url": r["raw_url"]} for r in gone]
    out(f"[archive] {len(gone)} permanently-gone (404/410) item(s)"
        + (" [sideload only]" if sideload_only else " [all sources]"))
    if not execute:
        con.close()
        out("  DRY RUN - add --run to archive them.")
        return {"gone": len(gone), "archived": 0, "items": listed}
    for it in gone:
        archive_item(con, it["id"], it["status"], "permanently gone (404/410) - archived")
    con.close()
    out(f"  archived {len(gone)} item(s) -> ARCHIVED (off the failures panel, never retried)")
    return {"gone": len(gone), "archived": len(gone), "items": listed}


_RESTORABLE = ("FAILED", "NEEDS_REVIEW", "AWAITING_ACTION")


def archive_item(con, item_id, prev_status, why):
    """Set an item ARCHIVED, remembering its previous status so unarchive can put it back."""
    con.execute("UPDATE items SET status='ARCHIVED', updated_at=? WHERE id=?", (db.now(), item_id))
    db.log(con, item_id, "archive_prev", "ok", prev_status)
    db.log(con, item_id, "archive", "ok", why)


def unarchive(ids):
    """Undo an archive: put each ARCHIVED item in `ids` back to the status it had before (FAILED if
    that wasn't recorded). Ids that aren't archived are ignored. Returns {"restored": n, "items": [...]}"""
    con = db.connect()
    restored = []
    for item_id in dict.fromkeys(ids):
        row = con.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
        if not row or row["status"] != "ARCHIVED":
            continue
        prev = con.execute("SELECT detail FROM processing_logs WHERE item_id=? AND step='archive_prev' "
                           "ORDER BY created_at DESC LIMIT 1", (item_id,)).fetchone()
        status = prev["detail"] if prev and prev["detail"] in _RESTORABLE else "FAILED"
        con.execute("UPDATE items SET status=?, updated_at=? WHERE id=?", (status, db.now(), item_id))
        db.log(con, item_id, "unarchive", "ok", f"restored to {status}")
        restored.append({"id": item_id, "status": status})
    con.commit()
    con.close()
    return {"restored": len(restored), "items": restored}


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m app.sideload",
        description="Bulk-import links from a '<timestamp> <link>' file, or --retry stuck items.")
    p.add_argument("file", nargs="?", help="path to the links file (omit when using --retry)")
    p.add_argument("--retry", action="store_true",
                   help="reprocess stuck FAILED/NEEDS_REVIEW items instead of reading a file")
    p.add_argument("--all", action="store_true",
                   help="with --retry: include items from all sources (default: sideload only)")
    p.add_argument("--run", action="store_true",
                   help="actually process (default: dry-run report only)")
    p.add_argument("--limit", type=int, default=None, help="max items to process this run")
    p.add_argument("--delay", type=float, default=2.0,
                   help="seconds to sleep between items (be gentle on Gemini/Instagram)")
    p.add_argument("--archive-gone", action="store_true",
                   help="mark permanently-gone (404/410) stuck items as ARCHIVED (off the failures panel)")
    p.add_argument("--allow-nonpublic", action="store_true",
                   help="disable the SSRF public-URL pre-check (off by default)")
    a = p.parse_args(argv)
    db.init()
    if a.archive_gone:
        archive_gone(execute=a.run, sideload_only=not a.all)
    elif a.retry:
        retry(execute=a.run, limit=a.limit, delay=a.delay, sideload_only=not a.all)
    elif a.file:
        run(a.file, execute=a.run, limit=a.limit, delay=a.delay, allow_nonpublic=a.allow_nonpublic)
    else:
        p.error("provide a links file, or use --retry / --archive-gone")


if __name__ == "__main__":
    main()

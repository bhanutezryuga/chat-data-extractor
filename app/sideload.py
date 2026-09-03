"""Bulk-import links from a text file into the capture pipeline — no Telegram, no server.

The file is one `<timestamp> <link>` per line (the timestamp is ignored; captures are stamped
`now`). Each new link is fed straight through `pipeline.ingest`, which already handles URL-dedup,
classify → fetch → Gemini extract → content-dedup, and writes the Logseq page. This module is a
thin, safe driver around that: a dry-run by default, an SSRF pre-check, a daily-budget stop, and a
throttle between links.

    python -m app.sideload <file> [--run] [--limit N] [--delay 2.0] [--allow-nonpublic]

Re-running is safe: already-saved links are skipped (dedup), so a run stopped on budget or a crash
resumes by simply running again.
"""
import argparse
import hashlib
import time

from . import config, db, netguard, pipeline, usage


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


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m app.sideload",
        description="Bulk-import links from a '<timestamp> <link>' text file into the capture pipeline.")
    p.add_argument("file", help="path to the links file")
    p.add_argument("--run", action="store_true",
                   help="actually process the new links (default: dry-run report only)")
    p.add_argument("--limit", type=int, default=None, help="max NEW links to process this run")
    p.add_argument("--delay", type=float, default=2.0,
                   help="seconds to sleep between links (default 2; be gentle on Gemini/Instagram)")
    p.add_argument("--allow-nonpublic", action="store_true",
                   help="disable the SSRF public-URL pre-check (off by default)")
    a = p.parse_args(argv)
    db.init()
    run(a.file, execute=a.run, limit=a.limit, delay=a.delay, allow_nonpublic=a.allow_nonpublic)


if __name__ == "__main__":
    main()

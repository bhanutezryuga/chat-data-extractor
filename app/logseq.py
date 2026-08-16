"""Logseq export (Phase 1 — the write half of the two-way plan; see docs/LOGSEQ_PLAN.md).

Renders a processed item into a Logseq Markdown page (one page per capture) plus a one-line
journal breadcrumb, written into a Logseq graph folder (`LOGSEQ_GRAPH_DIR`). Logseq is
file-based, so it auto-indexes what we write — no plugin/API needed.

Field ownership: everything the app writes (summary/list/task/schedule) is app-owned; the page's
`## Notes` subtree is USER-owned and preserved verbatim across re-exports. Read-back (updating the
DB from user edits) is Phase 2.

Pure `render_page` keeps the Markdown mapping unit-testable; `export_item` does the I/O.
"""
import json
import os
import re
import threading
import time
from datetime import datetime

from . import config, db


def active():
    """Logseq export is on only when enabled AND a graph folder is configured."""
    return bool(config.LOGSEQ_ENABLED and config.LOGSEQ_GRAPH_DIR)


# ---- pure rendering -------------------------------------------------------

def _prop(k, v):
    return f"{k}:: {v}"


def _as_list(v):
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, list) else [str(parsed)]
    except (json.JSONDecodeError, TypeError):
        return [str(v)]


def _oneline(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def _section(head, children):
    """A `- ## Head` block with one child bullet per item."""
    lines = [f"- {head}"]
    for c in children:
        lines.append(f"\t- {_oneline(c)}")
    return "\n".join(lines)


def _scheduled(date):
    try:
        dow = datetime.strptime(date, "%Y-%m-%d").strftime("%a")
        return f"SCHEDULED: <{date} {dow}>"
    except ValueError:
        return f"SCHEDULED: <{date}>"


NOTES_MARKER = "- ## Notes"


def render_page(item, extraction, coll_items, task):
    """Item (+ latest extraction/task/collection rows) -> Logseq Markdown. Pure."""
    ex = extraction or {}
    active_learn = (item.get("learn_status") or "active") == "active"
    scheduled = active_learn and item.get("deadline")

    # page properties block (top of file, no bullet)
    props = [_prop("item-id", item["id"])]
    if item.get("title"):
        props.append(_prop("title", _oneline(item["title"])))
    if item.get("category"):
        props.append(_prop("category", f"[[{item['category']}]]"))
    if item.get("source"):
        props.append(_prop("source", item["source"]))
    if item.get("raw_url"):
        props.append(_prop("url", item["raw_url"]))
    props.append(_prop("captured", f"[[{(item.get('created_at') or db.now())[:10]}]]"))
    props.append(_prop("status", item.get("learn_status") or "active"))
    if scheduled:
        props.append(_prop("revisit-next", f"[[{item['deadline'][:10]}]]"))

    blocks = []
    if ex.get("summary"):
        blocks.append(_section("## Summary", [ex["summary"]]))
    kps = _as_list(ex.get("key_points"))
    if kps:
        blocks.append(_section("## Key points", kps))
    if (ex.get("translation") or "").strip():
        blocks.append(_section("## Translation", [ex["translation"]]))

    if coll_items:
        coll = coll_items[0]["collection"]
        lines = [f"- ## [[{coll}]]"]
        for c in coll_items:
            marker = "DONE" if c.get("done") else "TODO"
            txt = _oneline(c["name"]) + (f" — {_oneline(c['note'])}" if c.get("note") else "")
            lines.append(f"\t- {marker} {txt}")
            lines.append(f"\t  cid:: {c['id']}")          # links this block back to collection_items
            if c.get("link"):
                lines.append(f"\t  link:: {c['link']}")
        blocks.append("\n".join(lines))

    if task and task.get("title"):
        blocks.append(_section("## Task", [f"TODO {task['title']}"]))

    if scheduled:  # a scheduled TODO so due items also surface in Logseq's agenda
        label = _oneline(item.get("title") or item.get("content_type") or "item")
        blocks.append(f"- TODO Revisit — {label}\n  {_scheduled(item['deadline'][:10])}")

    blocks.append(f"{NOTES_MARKER}\n\t-")            # USER-owned area, preserved on re-export

    return "\n".join(props) + "\n\n" + "\n".join(blocks) + "\n"


# ---- I/O ------------------------------------------------------------------

def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:50].rstrip("-")) or "item"


def _page_filename(item):
    return f"{_slug(item.get('title') or item.get('content_type') or 'item')}-{item['id'][:8]}.md"


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _merge_notes(new_md, old_text):
    """Preserve the user's `## Notes` subtree (everything from the marker to EOF) from an
    existing page, so re-exporting never clobbers notes the user added under it."""
    oi = old_text.find(NOTES_MARKER)
    if oi == -1:
        return new_md
    old_notes = old_text[oi:]
    ni = new_md.find(NOTES_MARKER)
    if ni == -1:
        return new_md.rstrip() + "\n" + old_notes
    return new_md[:ni] + old_notes


def _write_page(item, md):
    pages = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
    os.makedirs(pages, exist_ok=True)
    path = os.path.join(pages, _page_filename(item))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            md = _merge_notes(md, f.read())
    _atomic_write(path, md)
    return path


def _write_journal(item):
    date = (item.get("created_at") or db.now())[:10]
    journals = os.path.join(config.LOGSEQ_GRAPH_DIR, "journals")
    os.makedirs(journals, exist_ok=True)
    path = os.path.join(journals, date.replace("-", "_") + ".md")
    title = _oneline(item.get("title") or item.get("raw_url") or item.get("content_type") or "item")
    link = f"[[{title}]]"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if link in f.read():
                return  # already breadcrumbed (idempotent)
    line = f"- Captured {link}" + (f" #{item['category']}" if item.get("category") else "") + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)


def export_all():
    """Export every processed (ACTIONABLE) item to Logseq — for backfilling a graph after
    enabling the feature. Returns the count written. No-op (0) if disabled."""
    if not active():
        return 0
    con = db.connect()
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM items WHERE status='ACTIONABLE' ORDER BY created_at")]
    con.close()
    return sum(1 for iid in ids if export_item(iid))


def export_item(item_id):
    """Render + write one item's Logseq page and journal breadcrumb. No-op if disabled;
    never raises into the pipeline (a Logseq problem must not fail a capture)."""
    if not active():
        return False
    try:
        con = db.connect()
        item = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            con.close()
            return False
        item = dict(item)
        ex = con.execute("SELECT * FROM extractions WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
                         (item_id,)).fetchone()
        task = con.execute("SELECT * FROM tasks WHERE item_id=? ORDER BY created_at DESC LIMIT 1",
                          (item_id,)).fetchone()
        coll = [dict(r) for r in con.execute(
            "SELECT * FROM collection_items WHERE item_id=? ORDER BY created_at", (item_id,))]
        con.close()
        md = render_page(item, dict(ex) if ex else {}, coll, dict(task) if task else {})
        _write_page(item, md)
        _write_journal(item)
        return True
    except Exception as e:
        print(f"  [logseq] export failed for {item_id}: {e}")
        return False


# ---- Phase 2: read-back (Logseq -> DB) ------------------------------------
# The page file is a projection of DB state that the app refreshes on every change; the user's
# edits we read back are the checkbox done-state and a completion status. Anything the app owns
# (summary/task/schedule) is ignored on read-back. See docs/LOGSEQ_PLAN.md for the conflict policy.

_TASK_RE = re.compile(r"-\s+(TODO|DOING|NOW|LATER|DONE|CANCELED|CANCELLED)\b")


def parse_page(text):
    """Extract the read-back-relevant bits from a page: item id, page status, and the done-state
    of each collection block (keyed by its `cid::`). Pure — no I/O."""
    item_id = status = None
    coll = {}
    cur_done = None                      # done-state of the most recent task bullet
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("- "):
            m = _TASK_RE.match(s)
            cur_done = (m.group(1) == "DONE") if m else None
        elif s.startswith("item-id::") and item_id is None:
            item_id = s.split("::", 1)[1].strip()
        elif s.startswith("status::") and status is None:
            status = s.split("::", 1)[1].strip().strip("[]")
        elif s.startswith("cid::") and cur_done is not None:
            coll[s.split("::", 1)[1].strip()] = cur_done
    return {"item_id": item_id, "status": status, "coll": coll}


def _apply(parsed):
    """Apply parsed user-edits to the DB. Returns (done_changes, status_changes)."""
    if not parsed["item_id"]:
        return (0, 0)
    con = db.connect()
    it = con.execute("SELECT id, learn_status FROM items WHERE id=?", (parsed["item_id"],)).fetchone()
    if not it:
        con.close()
        return (0, 0)
    dchg = 0
    for cid, done in parsed["coll"].items():
        cur = con.execute("SELECT done FROM collection_items WHERE id=? AND item_id=?",
                          (cid, it["id"])).fetchone()
        if cur is not None and bool(cur["done"]) != done:
            con.execute("UPDATE collection_items SET done=? WHERE id=?", (1 if done else 0, cid))
            dchg += 1
    # Completion-only status read-back: user may mark learned/archived in Logseq; never revert to
    # active from a (possibly stale) file — un-archiving is done in the app.
    schg = 0
    st = (parsed["status"] or "").lower()
    if st in ("learned", "archived") and it["learn_status"] == "active":
        extra = ", deadline=NULL, progress=100" if st == "learned" else ""
        con.execute(f"UPDATE items SET learn_status=?{extra}, updated_at=? WHERE id=?",
                    (st, db.now(), it["id"]))
        schg = 1
    if dchg or schg:
        con.commit()
    con.close()
    return (dchg, schg)


_GIT_CONFLICT_MARKER = "<<<<<<< "


def _is_conflicted(name, text):
    """A page we must NOT read back: a hidden file, a Syncthing conflict copy
    (`*.sync-conflict-*`), or a file with an unresolved git merge marker. Two-way sync tools leave
    these behind; treating them as real pages would apply stale or duplicated state."""
    return name.startswith(".") or ".sync-conflict-" in name or _GIT_CONFLICT_MARKER in text


def sync_from_logseq():
    """Scan the graph's pages for files changed since last sync and read user edits back into the
    DB. Idempotent; safe to run repeatedly (re-reading the app's own writes is a no-op).
    Conflicted files left by two-way sync (Syncthing copies, git merge markers) are skipped."""
    empty = {"scanned": 0, "done_updates": 0, "status_updates": 0}
    if not active():
        return empty
    pages = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
    if not os.path.isdir(pages):
        return empty
    con = db.connect()
    seen = {r["path"]: r["mtime"] for r in con.execute("SELECT path, mtime FROM logseq_state")}
    con.close()
    scanned = dc = sc = 0
    for name in os.listdir(pages):
        if not name.endswith(".md"):
            continue
        path = os.path.join(pages, name)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        if seen.get(path) is not None and mt <= seen[path]:
            continue                                     # unchanged since last sync
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        if _is_conflicted(name, text):
            continue
        d, s = _apply(parse_page(text))
        dc += d
        sc += s
        scanned += 1
        con = db.connect()
        con.execute("INSERT INTO logseq_state (path, mtime, synced_at) VALUES (?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, synced_at=excluded.synced_at",
                    (path, mt, db.now()))
        con.commit()
        con.close()
    return {"scanned": scanned, "done_updates": dc, "status_updates": sc}


def start_watcher(apply_fn=None):
    """Poll the graph for user edits every LOGSEQ_SYNC_SECONDS. No-op unless configured."""
    if not active():
        return None
    fn = apply_fn or sync_from_logseq

    def loop():
        print(f"  [logseq] read-back watcher started (every {config.LOGSEQ_SYNC_SECONDS}s "
              f"<- {config.LOGSEQ_GRAPH_DIR})")
        while True:
            try:
                r = fn()
                if r.get("done_updates") or r.get("status_updates"):
                    print(f"  [logseq] read back: {r['done_updates']} done, {r['status_updates']} status")
            except Exception as e:
                print(f"  [logseq] sync error: {e}")
            time.sleep(max(15, config.LOGSEQ_SYNC_SECONDS))

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

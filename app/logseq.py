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

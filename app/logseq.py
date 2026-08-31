"""Logseq export (Phase 1 — the write half of the two-way plan; see docs/LOGSEQ_PLAN.md).

Renders a processed item into a Logseq Markdown page (one page per capture) plus a one-line
journal breadcrumb, written into a Logseq graph folder (`LOGSEQ_GRAPH_DIR`). Logseq is
file-based, so it auto-indexes what we write — no plugin/API needed.

Field ownership: everything the app writes (summary/list/task/schedule) is app-owned; the page's
`## Notes` subtree is USER-owned and preserved verbatim across re-exports.

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


def _is_study(category):
    """Study items (things you work through to learn) get TODO markers; everything else
    (songs, recipes, videos to watch…) is written as a plain reference bullet."""
    return (category or "") in config.LOGSEQ_TODO_CATEGORIES


def _tags(item, task):
    """Review tags grouped by action: the category (kept as the group anchor, e.g. Reading) followed
    by Gemini's topic tags, normalized to lowercase-hyphenated single tokens so the same concept
    doesn't fragment into `Book-Recommendation` vs `book-recommendations`. Content-type (reel/post/…)
    is intentionally excluded as noise."""
    out, seen = [], set()
    cat = (item.get("category") or "").strip()
    if cat:
        out.append(cat)
        seen.add(cat.lower())
    for t in _as_list((task or {}).get("tags")):
        tok = re.sub(r"[^0-9a-z-]", "", re.sub(r"\s+", "-", str(t).strip().lower())).strip("-")
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out[:8]


def render_page(item, extraction, coll_items, task):
    """Item (+ latest extraction/task/collection rows) -> Logseq Markdown. Pure."""
    ex = extraction or {}
    active_learn = (item.get("learn_status") or "active") == "active"
    scheduled = active_learn and item.get("deadline")
    study = _is_study(item.get("category"))

    # page properties block (top of file, no bullet) — kept minimal: just title + review tags
    props = []
    if item.get("title"):
        props.append(_prop("title", _oneline(item["title"])))
    tags = _tags(item, task)
    if tags:
        props.append(_prop("tags", ", ".join(tags)))

    blocks = []
    # For list/recommendation captures the collection bullets ARE the content — a Summary and
    # Key points would just restate the list — so we omit both when the page carries a list.
    # Long-form captures (no list) still get them.
    if not coll_items:
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
            txt = _oneline(c["name"]) + (f" — {_oneline(c['note'])}" if c.get("note") else "")
            if study:
                lines.append(f"\t- {'DONE' if c.get('done') else 'TODO'} {txt}")
            else:
                lines.append(f"\t- {txt}")               # reference bullet, no TODO
            if c.get("link"):
                lines.append(f"\t  link:: {c['link']}")
        blocks.append("\n".join(lines))

    # TODO is reserved for study items; a song/recipe just gets its content, no task to "do".
    if study and task and task.get("title"):
        blocks.append(_section("## Task", [f"TODO {task['title']}"]))

    if scheduled and study:  # a scheduled TODO so study items surface in Logseq's agenda
        label = _oneline(item.get("title") or item.get("content_type") or "item")
        blocks.append(f"- TODO Revisit — {label}\n  {_scheduled(item['deadline'][:10])}")

    blocks.append(f"{NOTES_MARKER}\n\t-")            # USER-owned area, preserved on re-export

    header = "\n".join(props)
    return (header + "\n\n" if header else "") + "\n".join(blocks) + "\n"


# ---- I/O ------------------------------------------------------------------

def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:50].rstrip("-")) or "item"


def _page_filename(item):
    return f"{_slug(item.get('title') or item.get('content_type') or 'item')}-{item['id'][:8]}.md"


def _unique_title(base, my_filename, pages_dir):
    """Logseq keys a page by its `title::`, so two items with the same title collide ("page already
    exists"). If another page already claims this title, append ' (2)', ' (3)', … The filename stays
    keyed to the item id, so this only affects the displayed title and is stable once on disk."""
    if not base:
        return base
    others = set()
    try:
        names = os.listdir(pages_dir)
    except OSError:
        return base
    for name in names:
        if name == my_filename or not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(pages_dir, name), encoding="utf-8", errors="replace") as f:
                head = f.read(4000)
        except OSError:
            continue
        m = re.search(r"^title::\s*(.+)$", head, re.M)
        if m:
            others.add(m.group(1).strip())
    if base not in others:
        return base
    n = 2
    while f"{base} ({n})" in others:
        n += 1
    return f"{base} ({n})"


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
    """Write the item's page. Returns True if this created a NEW page, False if it overwrote an
    existing one (a page already on disk for this item, under its current or a previous title)."""
    pages = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
    os.makedirs(pages, exist_ok=True)
    fname = _page_filename(item)
    path = os.path.join(pages, fname)
    existed = os.path.exists(path)
    # remove any stale page for this same item (the filename slug changes if Gemini re-titles it
    # on reprocess) so re-titling never leaves an orphan duplicate behind.
    suffix = f"-{item['id'][:8]}.md"
    for name in os.listdir(pages):
        if name.endswith(suffix) and name != fname:
            existed = True                          # already had a page (under an older title)
            try:
                os.remove(os.path.join(pages, name))
            except OSError:
                pass
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            md = _merge_notes(md, f.read())
    _atomic_write(path, md)
    return not existed


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


_NO_CONTENT_PHRASES = ("no content was provided", "content not provided", "no content provided",
                       "not provided in the prompt", "unable to extract", "unable to summarize",
                       "cannot summarize", "please provide the", "no information was provided")


def _has_content(ex):
    """False when the extraction is essentially a 'could not read the content' result — we don't
    make a Logseq page for those (nothing to record). A real list of items always counts."""
    if not ex:
        return False
    if _as_list(ex.get("list_items")):
        return True
    summ = (ex.get("summary") or "").strip()
    if len(summ) < 20:
        return False
    blob = (summ + " " + " ".join(str(x) for x in _as_list(ex.get("key_points")))).lower()
    return not any(p in blob for p in _NO_CONTENT_PHRASES)


def export_all():
    """Re-export every processed (ACTIONABLE) item to Logseq. Returns the number of pages this run
    NEWLY created (items not already in the graph) — not the running total, so a repeat export of an
    up-to-date graph returns 0. No-op (0) if disabled."""
    if not active():
        return 0
    con = db.connect()
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM items WHERE status='ACTIONABLE' ORDER BY created_at")]
    con.close()
    return sum(1 for iid in ids if export_item(iid) == "created")


def export_item(item_id):
    """Render + write one item's Logseq page and journal breadcrumb. Returns 'created' (new page),
    'updated' (overwrote an existing one), or False (disabled / skipped / no content / error).
    Never raises into the pipeline (a Logseq problem must not fail a capture)."""
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
        if not _has_content(dict(ex) if ex else {}):
            return False                            # nothing was extracted -> don't make a page
        # keep the filename keyed to the item (stable), but make title:: unique so Logseq
        # never merges two captures that Gemini happened to give the same title.
        pages_dir = os.path.join(config.LOGSEQ_GRAPH_DIR, "pages")
        render_item = {**item, "title": _unique_title(item.get("title"), _page_filename(item), pages_dir)}
        md = render_page(render_item, dict(ex) if ex else {}, coll, dict(task) if task else {})
        created = _write_page(item, md)             # ORIGINAL item -> stable filename
        if config.LOGSEQ_JOURNAL:
            _write_journal(item)
        return "created" if created else "updated"
    except Exception as e:
        print(f"  [logseq] export failed for {item_id}: {e}")
        return False

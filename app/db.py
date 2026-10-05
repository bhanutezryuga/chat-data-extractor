"""SQLite helpers. A fresh connection per call keeps things thread-safe
(web handler threads + the Telegram poller thread each get their own)."""
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from . import config


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_id():
    return uuid.uuid4().hex


def connect():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init():
    con = connect()
    con.executescript(config.SCHEMA_SQL.read_text(encoding="utf-8"))
    # lightweight migrations for DBs created before a column existed
    ex_cols = {r[1] for r in con.execute("PRAGMA table_info(extractions)").fetchall()}
    if "list_items" not in ex_cols:
        con.execute("ALTER TABLE extractions ADD COLUMN list_items TEXT")
    if "translation" not in ex_cols:
        con.execute("ALTER TABLE extractions ADD COLUMN translation TEXT")
    if "detected_language" not in ex_cols:
        con.execute("ALTER TABLE extractions ADD COLUMN detected_language TEXT")
    if "recipe" not in ex_cols:
        con.execute("ALTER TABLE extractions ADD COLUMN recipe TEXT")
    if "sections" not in ex_cols:
        con.execute("ALTER TABLE extractions ADD COLUMN sections TEXT")
    it_cols = {r[1] for r in con.execute("PRAGMA table_info(items)").fetchall()}
    if "action" not in it_cols:
        con.execute("ALTER TABLE items ADD COLUMN action TEXT")
    # PKM knowledge-record columns on items
    for col, ddl in (("title", "TEXT"), ("source", "TEXT"), ("category", "TEXT"),
                     ("priority", "TEXT"), ("deadline", "TEXT"),
                     ("revisit_stage", "INTEGER NOT NULL DEFAULT 0"),
                     ("revisit_count", "INTEGER NOT NULL DEFAULT 0"),
                     ("learn_status", "TEXT NOT NULL DEFAULT 'active'"),
                     ("progress", "INTEGER NOT NULL DEFAULT 0"), ("reminded_at", "TEXT"),
                     ("content_key", "TEXT")):
        if col not in it_cols:
            con.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
    # indexes on the PKM columns — here (not in schema.sql) so they run AFTER the ALTERs above;
    # on a pre-existing items table the columns don't exist until the migration completes.
    for stmt in ("CREATE INDEX IF NOT EXISTS idx_items_deadline ON items(deadline)",
                 "CREATE INDEX IF NOT EXISTS idx_items_category ON items(category)",
                 "CREATE INDEX IF NOT EXISTS idx_items_learn    ON items(learn_status)",
                 "CREATE INDEX IF NOT EXISTS idx_items_content  ON items(content_key)"):
        con.execute(stmt)
    # seed the category taxonomy (extensible)
    if con.execute("SELECT count(*) FROM categories").fetchone()[0] == 0:
        for name, coll in (("Reading", "To-Read"), ("Watching", "To-Watch"),
                           ("Listening", "To-Listen"), ("Learning", "Study"),
                           ("Shopping", "Wishlist"), ("Reference", "Reference"),
                           ("Cooking", "Recipes"), ("Travel", "Places"), ("Other", None)):
            con.execute("INSERT INTO categories (id, name, collection, created_at) "
                        "VALUES (?, ?, ?, datetime('now'))", (new_id(), name, coll))

    if con.execute("SELECT count(*) FROM rules").fetchone()[0] == 0:
        # fresh DB: full seed (already includes rule_ig_post)
        con.executescript(config.SEED_SQL.read_text(encoding="utf-8"))
    else:
        # already-seeded DB: ensure the default user and back-fill newer built-in rules (idempotent)
        con.execute("INSERT OR IGNORE INTO users (id, display_name) VALUES (?, ?)",
                    (config.USER_ID, "Default User"))
        con.execute(
            "INSERT OR IGNORE INTO rules "
            "(id,name,matcher,matcher_kind,content_type,purpose,extraction_strategy,analyzer,action_template,priority,enabled) "
            "VALUES ('rule_ig_post','Instagram Post','^https?://(www\\.)?instagram\\.com/p/','url_regex','post',"
            "'Instagram photo/carousel/video post; the caption often holds the value (lists, tips).',"
            "'description_first','gemini_text',"
            "'Summarize the content in one short paragraph, list up to 3 key points, and propose ONE concrete actionable task (title, why it matters, first step). Tag the topic. Return as JSON.',"
            "13,1)")
        # Not reached via classify() (enabled=0, matcher never matches) — exists only so
        # items.rule_id's foreign key is satisfied for manually-typed notes (pipeline.create_note).
        con.execute(
            "INSERT OR IGNORE INTO rules "
            "(id,name,matcher,matcher_kind,content_type,purpose,extraction_strategy,analyzer,action_template,priority,enabled) "
            "VALUES ('rule_note','Manual note','(?!)','url_regex','note',"
            "'A personal note the user chose to write down directly (not fetched from a link).',"
            "'text','gemini_text',"
            "'Give this note a short, specific title (not a full sentence) and classify it into the best-fitting category. Keep the summary and key points brief - they will not be shown; the note text itself is preserved verbatim on the saved page.',"
            "999,0)")
    con.commit()
    con.close()


def log(con, item_id, step, status, detail=""):
    con.execute(
        "INSERT INTO processing_logs (id, item_id, step, status, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (new_id(), item_id, step, status, str(detail)[:500], now()))
    # Commit each log line immediately. Logs are durable checkpoints, and — crucially —
    # this releases the SQLite write lock BEFORE the long Gemini/network calls that follow
    # (extract/fetch). Otherwise the write transaction stays open across that call and a
    # concurrent reprocess/ingest blocks past the busy-timeout with "database is locked".
    con.commit()

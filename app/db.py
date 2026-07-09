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
    it_cols = {r[1] for r in con.execute("PRAGMA table_info(items)").fetchall()}
    if "action" not in it_cols:
        con.execute("ALTER TABLE items ADD COLUMN action TEXT")

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
    con.commit()
    con.close()


def log(con, item_id, step, status, detail=""):
    con.execute(
        "INSERT INTO processing_logs (id, item_id, step, status, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (new_id(), item_id, step, status, str(detail)[:500], now()))

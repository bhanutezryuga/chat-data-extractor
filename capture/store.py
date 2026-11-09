"""SQLite store for items and jobs.

Statements autocommit on their own. Inside `with store.transaction():` they are
grouped and commit or roll back together. One Store may be shared across threads;
an RLock keeps statements from different threads from interleaving.
"""
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ITEM_STATUSES = ("queued", "saved", "needs_you")
JOB_STATUSES = ("pending", "running", "done", "failed")
TYPE_HINTS = ("recipe", "tech", "book", "note")

_UPDATABLE = ("title", "status", "status_reason")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    url TEXT,
    url_norm TEXT,
    share_text TEXT NOT NULL DEFAULT '',
    type_hint TEXT CHECK (type_hint IS NULL OR type_hint IN ({_in(TYPE_HINTS)})),
    client_id TEXT,
    title TEXT,
    status TEXT NOT NULL CHECK (status IN ({_in(ITEM_STATUSES)})),
    status_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS items_url_norm ON items(url_norm) WHERE url_norm IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS items_client_id ON items(client_id) WHERE client_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ({_in(JOB_STATUSES)})),
    attempts INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    run_after TEXT NOT NULL,
    first_failed_at TEXT,
    last_error TEXT,
    locked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status_run_after ON jobs(status, run_after);
"""


class Store:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _all(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def _one(self, sql: str, params=()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params=()) -> None:
        with self._lock:
            self._db.execute(sql, params)

    # items

    def insert_item(self, *, url, url_norm, share_text, type_hint, client_id, now) -> str:
        item_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO items (id, url, url_norm, share_text, type_hint, client_id,"
            " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (item_id, url, url_norm, share_text, type_hint, client_id, iso(now), iso(now)),
        )
        return item_id

    def get_item(self, item_id: str) -> dict | None:
        return self._one("SELECT * FROM items WHERE id = ?", (item_id,))

    def find_item(self, *, url_norm=None, client_id=None) -> dict | None:
        if url_norm is not None:
            found = self._one("SELECT * FROM items WHERE url_norm = ?", (url_norm,))
            if found:
                return found
        if client_id is not None:
            return self._one("SELECT * FROM items WHERE client_id = ?", (client_id,))
        return None

    def list_items(self, limit: int = 100) -> list[dict]:
        return self._all(
            "SELECT * FROM items ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        )

    def update_item(self, item_id: str, *, now: datetime, **fields) -> None:
        bad = set(fields) - set(_UPDATABLE)
        if bad:
            raise ValueError(f"cannot update item fields: {sorted(bad)}")
        sets = "".join(f"{k} = ?, " for k in fields)
        self._exec(
            f"UPDATE items SET {sets}updated_at = ? WHERE id = ?",
            (*fields.values(), iso(now), item_id),
        )

    # jobs

    def insert_job(self, item_id: str, *, now: datetime, kind: str = "process",
                   priority: int = 0) -> str:
        job_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO jobs (id, item_id, kind, priority, status, run_after,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (job_id, item_id, kind, priority, iso(now), iso(now), iso(now)),
        )
        return job_id

    def get_job(self, job_id: str) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def claim_job(self, *, now: datetime, stale_before: datetime) -> dict | None:
        return self._one(
            "UPDATE jobs SET status = 'running', locked_at = ?, attempts = attempts + 1,"
            " updated_at = ? WHERE id = (SELECT id FROM jobs WHERE"
            " (status = 'pending' AND run_after <= ?) OR (status = 'running' AND locked_at < ?)"
            " ORDER BY priority DESC, run_after, created_at LIMIT 1) RETURNING *",
            (iso(now), iso(now), iso(now), iso(stale_before)),
        )

    def finish_job(self, job_id: str, *, now: datetime) -> None:
        self._exec(
            "UPDATE jobs SET status = 'done', locked_at = NULL, updated_at = ? WHERE id = ?",
            (iso(now), job_id),
        )

    def reschedule_job(self, job_id: str, *, run_after: datetime, error: str,
                       now: datetime) -> None:
        self._exec(
            "UPDATE jobs SET status = 'pending', run_after = ?, last_error = ?,"
            " failures = failures + 1, first_failed_at = COALESCE(first_failed_at, ?),"
            " locked_at = NULL, updated_at = ? WHERE id = ?",
            (iso(run_after), error, iso(now), iso(now), job_id),
        )

    def fail_job(self, job_id: str, *, error: str, now: datetime) -> None:
        self._exec(
            "UPDATE jobs SET status = 'failed', last_error = ?, failures = failures + 1,"
            " first_failed_at = COALESCE(first_failed_at, ?), locked_at = NULL,"
            " updated_at = ? WHERE id = ?",
            (error, iso(now), iso(now), job_id),
        )

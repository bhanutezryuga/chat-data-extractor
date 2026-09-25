"""PKM spaced-repetition: schedule "revisit" reminders for saved knowledge, advance the
schedule when the user revisits, and stop once they mark it learned.

The schedule is a list of day-offsets (config.REVISIT_SCHEDULE). A new item is queued at
stage 0 (deadline = now + schedule[0]). Each `revisited` advances the stage (deadline =
now + schedule[stage]); `snoozed` pushes the deadline out without advancing; `learned`
stops reminders. Every action is logged to the `revisits` table for metrics.

A daemon (start_scheduler) scans for due items and hands each to a caller-supplied
`send_reminder` callback — so this module stays free of any Telegram dependency.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, db, logseq


def _interval_days(stage):
    sched = config.REVISIT_SCHEDULE or (1,)
    return sched[min(max(stage, 0), len(sched) - 1)]


def _future(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _progress(stage):
    return min(100, int(round(100 * stage / max(1, len(config.REVISIT_SCHEDULE)))))


def is_study(category):
    """Only *study* items are reviewed. The study set is the single config value
    LOGSEQ_TODO_CATEGORIES — the same one that decides which items get a `TODO Revisit` in
    Logseq — so the schedule, the digest and Logseq all agree on what is reviewable."""
    return (category or "") in config.LOGSEQ_TODO_CATEGORIES


def _study_sql():
    """(sql, args) restricting a query on `items` to study categories. Applied at query time,
    so rows scheduled before this rule existed simply drop out — no destructive migration."""
    cats = sorted(config.LOGSEQ_TODO_CATEGORIES)
    if not cats:
        return "0", []
    return f"category IN ({','.join('?' * len(cats))})", cats


def schedule_new(con, item_id):
    """Queue an item for its first revisit — but only if revisit is on, the item is a study item
    (see is_study), still active, and not already scheduled (so reprocessing never resets progress)."""
    if not config.REVISIT_ENABLED:
        return
    row = con.execute("SELECT deadline, learn_status, category FROM items WHERE id=?",
                      (item_id,)).fetchone()
    if not row or row["learn_status"] != "active" or row["deadline"] or not is_study(row["category"]):
        return
    con.execute("UPDATE items SET revisit_stage=0, deadline=?, updated_at=? WHERE id=?",
                (_future(_interval_days(0)), db.now(), item_id))


def _log(con, item_id, action):
    con.execute("INSERT INTO revisits (id, item_id, action, created_at) VALUES (?,?,?,?)",
                (db.new_id(), item_id, action, db.now()))


def mark(item_id, action):
    """Apply a revisit action (revisited|snoozed|learned) and return the item's new state."""
    action = (action or "").lower()
    if action not in ("revisited", "snoozed", "learned"):
        return {"error": "unknown revisit action"}
    con = db.connect()
    it = con.execute("SELECT revisit_stage FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        con.close()
        return {"error": "not found"}

    if action == "learned":
        con.execute("UPDATE items SET learn_status='learned', progress=100, deadline=NULL, "
                    "updated_at=? WHERE id=?", (db.now(), item_id))
    elif action == "snoozed":
        con.execute("UPDATE items SET deadline=?, reminded_at=NULL, updated_at=? WHERE id=?",
                    (_future(config.REVISIT_SNOOZE_DAYS), db.now(), item_id))
    else:  # revisited: advance a stage and re-arm
        stage = (it["revisit_stage"] or 0) + 1
        con.execute("UPDATE items SET revisit_stage=?, revisit_count=revisit_count+1, deadline=?, "
                    "reminded_at=NULL, progress=?, learn_status='active', updated_at=? WHERE id=?",
                    (stage, _future(_interval_days(stage)), _progress(stage), db.now(), item_id))
    _log(con, item_id, action)
    con.commit()
    row = dict(con.execute(
        "SELECT id, deadline, revisit_stage, revisit_count, learn_status, progress "
        "FROM items WHERE id=?", (item_id,)).fetchone())
    con.close()
    logseq.export_item(item_id)   # keep the Logseq page in sync with the new state (no-op if off)
    return row


def due(con, limit=50):
    """Active study items whose revisit deadline has passed and that we haven't already reminded
    for this cycle — the peritem scheduler's queue. (deadline is fixed-width UTC text, so string
    comparison sorts correctly.)"""
    study, args = _study_sql()
    return con.execute(
        f"SELECT * FROM items WHERE learn_status='active' AND deadline IS NOT NULL AND {study} "
        "AND deadline <= ? AND (reminded_at IS NULL OR reminded_at < deadline) "
        "ORDER BY deadline LIMIT ?", (*args, db.now(), limit)).fetchall()


def due_count(con):
    study, args = _study_sql()
    return con.execute(
        f"SELECT count(*) n FROM items WHERE learn_status='active' AND deadline IS NOT NULL AND {study} "
        "AND deadline <= ?", (*args, db.now())).fetchone()["n"]


def due_within(con, days, limit=200):
    """Active study items due to revisit within the next `days` (including overdue) — the weekly-review set."""
    study, args = _study_sql()
    return con.execute(
        f"SELECT * FROM items WHERE learn_status='active' AND deadline IS NOT NULL AND {study} "
        "AND deadline <= ? ORDER BY deadline LIMIT ?", (*args, _future(days), limit)).fetchall()


def _mark_reminded(item_id):
    con = db.connect()
    con.execute("UPDATE items SET reminded_at=? WHERE id=?", (db.now(), item_id))
    con.commit()
    con.close()


def _scan_loop(send_reminder):
    print("  [revisit] scheduler started"
          f" (schedule {list(config.REVISIT_SCHEDULE)} days, scan every {config.REVISIT_CHECK_SECONDS}s)")
    while True:
        _scan_once(send_reminder)
        time.sleep(max(60, config.REVISIT_CHECK_SECONDS))


def _scan_once(send_reminder):
    """One scheduler pass. An item is marked reminded only if `send_reminder` reports success
    (truthy), so a dropped reminder is retried on the next scan instead of being lost."""
    try:
        con = db.connect()
        rows = due(con)
        con.close()
        for it in rows:
            try:
                if send_reminder(dict(it)):
                    _mark_reminded(it["id"])
            except Exception as e:
                print(f"  [revisit] reminder error for {it['id']}: {e}")
    except Exception as e:
        print(f"  [revisit] scan error: {e}")


def start_scheduler(send_reminder):
    """Start the due-item scanner in a daemon thread. `send_reminder(item_dict)` is called
    for each due item (e.g. telegram.send_reminder). No-op without revisit + a bot token."""
    if not (config.REVISIT_ENABLED and config.TELEGRAM_BOT_TOKEN):
        return None
    t = threading.Thread(target=_scan_loop, args=(send_reminder,), daemon=True)
    t.start()
    return t


# ---- weekly review digest -------------------------------------------------

def get_meta(key):
    con = db.connect()
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else None


def set_meta(key, value):
    con = db.connect()
    con.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()
    con.close()


def _digest_due():
    """True if a weekly digest is due (>= DIGEST_INTERVAL_DAYS since the last one). On first run,
    seeds the timestamp so the first auto digest lands one interval later (use /review to test now)."""
    last = get_meta("last_digest_sent")
    if not last:
        set_meta("last_digest_sent", db.now())
        return False
    return last <= _future(-config.DIGEST_INTERVAL_DAYS)   # last is older than interval-days ago


def _digest_loop(send_digest):
    print(f"  [revisit] weekly digest mode (every {config.DIGEST_INTERVAL_DAYS}d, "
          f"items due within {config.DIGEST_LOOKAHEAD_DAYS}d)")
    while True:
        _digest_tick(send_digest)
        time.sleep(max(300, config.REVISIT_CHECK_SECONDS))


def _digest_tick(send_digest):
    """One digest-loop pass. `last_digest_sent` advances only when `send_digest()` reports
    success (truthy); a failed send is retried on the next tick instead of a week later."""
    try:
        if _digest_due():
            if send_digest():
                set_meta("last_digest_sent", db.now())
            else:
                print("  [revisit] digest not delivered — will retry")
    except Exception as e:
        print(f"  [revisit] digest error: {e}")


def start_digest(send_digest):
    """Start the weekly-digest daemon. `send_digest()` builds+sends the review message
    (e.g. telegram.send_review_digest). No-op without revisit + a bot token + digest mode."""
    if not (config.REVISIT_ENABLED and config.TELEGRAM_BOT_TOKEN):
        return None
    t = threading.Thread(target=_digest_loop, args=(send_digest,), daemon=True)
    t.start()
    return t

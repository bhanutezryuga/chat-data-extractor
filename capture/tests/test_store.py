import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from capture.store import Store, iso

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def add_item(store, n=1, **kw):
    args = dict(url=f"https://x/{n}", url_norm=f"x/{n}", share_text="t",
                type_hint=None, client_id=None, now=NOW)
    args.update(kw)
    return store.insert_item(**args)


def test_insert_and_get_item(store):
    item_id = add_item(store)
    item = store.get_item(item_id)
    assert item["status"] == "queued"
    assert item["created_at"] == item["updated_at"] == iso(NOW) == "2026-11-09T12:00:00Z"
    assert store.get_item("nope") is None


def test_duplicate_url_norm_raises(store):
    add_item(store, 1)
    with pytest.raises(sqlite3.IntegrityError):
        add_item(store, 2, url_norm="x/1")


def test_duplicate_client_id_raises(store):
    add_item(store, 1, client_id="c1")
    with pytest.raises(sqlite3.IntegrityError):
        add_item(store, 2, client_id="c1")


def test_null_url_norm_allowed_twice(store):
    add_item(store, 1, url=None, url_norm=None)
    add_item(store, 2, url=None, url_norm=None)
    assert len(store.list_items()) == 2


def test_find_item(store):
    a = add_item(store, 1, client_id="c1")
    assert store.find_item(url_norm="x/1")["id"] == a
    assert store.find_item(client_id="c1")["id"] == a
    assert store.find_item(url_norm="zzz") is None
    assert store.find_item() is None


def test_list_items_newest_first(store):
    a = add_item(store, 1, now=NOW)
    b = add_item(store, 2, now=NOW + timedelta(seconds=5))
    assert [i["id"] for i in store.list_items()] == [b, a]
    assert len(store.list_items(limit=1)) == 1


def test_update_item(store):
    a = add_item(store)
    later = NOW + timedelta(minutes=1)
    store.update_item(a, now=later, title="T", status="saved")
    item = store.get_item(a)
    assert (item["title"], item["status"], item["updated_at"]) == ("T", "saved", iso(later))


def test_update_item_rejects_unknown_field(store):
    a = add_item(store)
    with pytest.raises(ValueError):
        store.update_item(a, now=NOW, url="x")


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            add_item(store, 1)
            raise RuntimeError("boom")
    assert store.list_items() == []


def test_transaction_commits_together(store):
    with store.transaction():
        a = add_item(store)
        store.insert_job(a, now=NOW)
    assert len(store.list_items()) == 1


def test_integrity_error_in_transaction_rolls_back_item(store):
    add_item(store, 1)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction():
            add_item(store, 2)
            add_item(store, 3, url_norm="x/1")
    assert len(store.list_items()) == 1


def _job(store, n=1, now=NOW, **kw):
    return store.insert_job(add_item(store, n), now=now, **kw)


def claim(store, now=NOW, stale=None):
    return store.claim_job(now=now, stale_before=stale or now - timedelta(minutes=10))


def test_claim_respects_run_after(store):
    _job(store)
    assert claim(store, now=NOW - timedelta(seconds=1)) is None
    assert claim(store) is not None


def test_claim_orders_by_priority(store):
    _job(store, 1, now=NOW)
    hi = _job(store, 2, now=NOW + timedelta(seconds=1), priority=1)
    assert claim(store, now=NOW + timedelta(seconds=2))["id"] == hi


def test_claim_marks_running_and_counts_attempt(store):
    j = _job(store)
    got = claim(store)
    assert got["id"] == j and got["status"] == "running"
    assert got["attempts"] == 1 and got["failures"] == 0
    assert got["locked_at"] == iso(NOW)
    assert claim(store) is None


def test_claim_reclaims_stale_running(store):
    _job(store)
    claim(store)
    later = NOW + timedelta(minutes=11)
    got = claim(store, now=later)
    assert got["attempts"] == 2 and got["failures"] == 0


def test_claim_skips_fresh_running(store):
    _job(store)
    claim(store)
    assert claim(store, now=NOW + timedelta(minutes=5)) is None


def test_finish_job(store):
    j = _job(store)
    claim(store)
    store.finish_job(j, now=NOW)
    assert store.get_job(j)["status"] == "done"


def test_reschedule_keeps_first_failed_at(store):
    j = _job(store)
    claim(store)
    t2 = NOW + timedelta(minutes=1)
    store.reschedule_job(j, run_after=t2, error="e1", now=NOW)
    t3 = NOW + timedelta(minutes=9)
    store.reschedule_job(j, run_after=t3, error="e2", now=t2)
    job = store.get_job(j)
    assert job["first_failed_at"] == iso(NOW)
    assert job["failures"] == 2 and job["last_error"] == "e2"
    assert job["status"] == "pending" and job["run_after"] == iso(t3)
    assert job["locked_at"] is None


def test_fail_job(store):
    j = _job(store)
    claim(store)
    store.fail_job(j, error="dead", now=NOW)
    job = store.get_job(j)
    assert job["status"] == "failed" and job["failures"] == 1
    assert job["first_failed_at"] == iso(NOW) and job["last_error"] == "dead"


def test_store_reopens_existing_file(settings):
    s = Store(settings.db_path)
    a = add_item(s)
    s.close()
    s2 = Store(settings.db_path)
    assert s2.get_item(a)["id"] == a
    s2.close()


def test_creates_parent_dir(tmp_path):
    s = Store(str(tmp_path / "sub" / "dir" / "x.db"))
    assert s.list_items() == []
    s.close()

from datetime import datetime, timedelta, timezone

from capture.capture import capture
from capture.queue import GAVE_UP_REASON, next_job, retry_later, succeed
from capture.store import iso

T = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def make_job(store):
    capture(store, "https://example.com/post", now=T)
    return next_job(store, T)


def test_gaps_are_1_5_30_min_then_3h(store):
    job = make_job(store)
    now = T
    for gap in (60, 300, 1800, 10800, 10800):
        assert retry_later(store, job, "boom", now) is True
        job = store.get_job(job["id"])
        assert job["status"] == "pending"
        assert job["run_after"] == iso(now + timedelta(seconds=gap))
        now = now + timedelta(seconds=gap)
        job = next_job(store, now)
        assert job is not None


def test_gives_up_after_a_day(store):
    job = make_job(store)
    assert retry_later(store, job, "boom", T) is True
    job = store.get_job(job["id"])
    assert retry_later(store, job, "boom", T + timedelta(hours=24)) is False
    job = store.get_job(job["id"])
    assert job["status"] == "failed"
    item = store.get_item(job["item_id"])
    assert item["status"] == "needs_you"
    assert item["status_reason"] == "Couldn't process this link for a day: boom"
    assert item["status_reason"] == GAVE_UP_REASON.format(error="boom")


def test_still_retrying_just_under_a_day(store):
    job = make_job(store)
    retry_later(store, job, "boom", T)
    job = store.get_job(job["id"])
    assert retry_later(store, job, "boom", T + timedelta(hours=23, minutes=59)) is True
    assert store.get_job(job["id"])["status"] == "pending"


def test_next_job_reclaims_stale(store):
    job = make_job(store)
    assert next_job(store, T + timedelta(seconds=599)) is None
    again = next_job(store, T + timedelta(seconds=601))
    assert again is not None and again["id"] == job["id"]


def test_succeed_marks_done(store):
    job = make_job(store)
    succeed(store, job, T)
    assert store.get_job(job["id"])["status"] == "done"
    assert next_job(store, T + timedelta(days=1)) is None

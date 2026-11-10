import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from capture.capture import capture
from capture.store import iso
from capture.titlefetch import Blocked, parse_title
from capture.worker import BLOCKED_REASON, run_once

T = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
FIX = Path(__file__).parent / "fixtures"
REEL = "https://www.instagram.com/reel/C9xYz12AbC/?igsh=x"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_title_prefers_og():
    assert parse_title(read("og_title.html")) == "Butter Chicken – Restaurant Style"


def test_parse_title_falls_back():
    assert parse_title(read("title_only.html")) == "How I set up Tailscale"


def test_parse_title_none():
    assert parse_title("<html></html>") is None


def test_worker_saves_title(store):
    item_id = capture(store, REEL, now=T).item_id
    assert run_once(store, T, fetch=lambda u: "Butter Chicken") is True
    item = store.get_item(item_id)
    assert (item["status"], item["title"]) == ("saved", "Butter Chicken")
    assert store._one("SELECT status FROM jobs")["status"] == "done"
    assert run_once(store, T, fetch=lambda u: "x") is False


def test_worker_fetches_the_raw_url(store):
    capture(store, REEL, now=T)
    seen = []
    run_once(store, T, fetch=lambda u: seen.append(u) or "t")
    assert seen == [REEL]


def test_worker_text_item_uses_first_line(store):
    item_id = capture(store, "\n  sourdough ratio\nmore", now=T).item_id
    run_once(store, T, fetch=lambda u: 1 / 0)
    item = store.get_item(item_id)
    assert (item["status"], item["title"]) == ("saved", "sourdough ratio")


def test_worker_blocked_url_needs_you(store):
    item_id = capture(store, "http://192.168.1.5/admin", now=T).item_id

    def blocked(u):
        raise Blocked(u)

    assert run_once(store, T, fetch=blocked) is True
    item = store.get_item(item_id)
    assert (item["status"], item["status_reason"]) == ("needs_you", BLOCKED_REASON)
    assert store._one("SELECT status FROM jobs")["status"] == "done"


def test_worker_network_error_retries(store):
    item_id = capture(store, REEL, now=T).item_id

    def boom(u):
        raise urllib.error.URLError("down")

    assert run_once(store, T, fetch=boom) is True
    assert store.get_item(item_id)["status"] == "queued"
    job = store._one("SELECT * FROM jobs")
    assert job["status"] == "pending"
    assert job["run_after"] == iso(T + timedelta(seconds=60))
    assert "URLError" in job["last_error"]


def test_worker_no_title_falls_back_to_url(store):
    item_id = capture(store, REEL, now=T).item_id
    run_once(store, T, fetch=lambda u: None)
    assert store.get_item(item_id)["title"] == "instagram.com/p/C9xYz12AbC"


def test_run_once_idle_returns_false(store):
    assert run_once(store, T) is False

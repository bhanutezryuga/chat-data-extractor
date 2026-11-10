import socket
import sqlite3
from datetime import datetime, timezone

import pytest

from capture.capture import MAX_TEXT, capture, share_to_text

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def job_count(store, item_id=None):
    return len(store._all("SELECT * FROM jobs" + (" WHERE item_id = ?" if item_id else ""),
                          (item_id,) if item_id else ()))


def test_capture_creates_item_and_job(store):
    r = capture(store, "https://example.com/post", now=NOW)
    assert r.duplicate is False
    item = store.get_item(r.item_id)
    assert item["status"] == "queued"
    assert item["url"] == "https://example.com/post"
    assert item["url_norm"] == "https://example.com/post"
    jobs = store._all("SELECT * FROM jobs")
    assert len(jobs) == 1 and jobs[0]["status"] == "pending" and jobs[0]["item_id"] == r.item_id


def test_same_reel_twice_is_duplicate(store):
    a = capture(store, "look https://www.instagram.com/reel/C9xYz12AbC/?igsh=abc", now=NOW)
    b = capture(store, "https://m.instagram.com/chef.anna/p/C9xYz12AbC/", now=NOW)
    assert b.item_id == a.item_id and b.duplicate is True
    assert job_count(store) == 1


def test_text_without_url_is_saved(store):
    r = capture(store, "remember: sourdough starter ratio 1:5:5", now=NOW)
    item = store.get_item(r.item_id)
    assert item["url"] is None
    assert item["share_text"] == "remember: sourdough starter ratio 1:5:5"


def test_replayed_client_id_without_url_is_duplicate(store):
    a = capture(store, "just a note", now=NOW, client_id="c1")
    b = capture(store, "just a note", now=NOW, client_id="c1")
    assert b.item_id == a.item_id and b.duplicate is True
    assert len(store.list_items()) == 1


def test_known_url_with_new_client_id_returns_existing_unchanged(store):
    a = capture(store, "https://example.com/x", now=NOW, client_id="c1")
    b = capture(store, "https://example.com/x", now=NOW, client_id="c2")
    assert b.item_id == a.item_id and b.duplicate is True
    assert store.get_item(a.item_id)["client_id"] == "c1"


def test_empty_text_rejected(store):
    with pytest.raises(ValueError, match="empty"):
        capture(store, "   ", now=NOW)


def test_bad_type_hint_rejected(store):
    with pytest.raises(ValueError, match="bad type_hint"):
        capture(store, "hello", now=NOW, type_hint="video")


def test_type_hint_stored(store):
    r = capture(store, "pasta", now=NOW, type_hint="recipe")
    assert store.get_item(r.item_id)["type_hint"] == "recipe"


def test_long_text_truncated(store):
    r = capture(store, "a" * 20_000, now=NOW)
    assert len(store.get_item(r.item_id)["share_text"]) == MAX_TEXT == 10_000


def test_share_to_text_dedupes_parts():
    assert share_to_text("Reel", "Look https://x.com/a", "https://x.com/a") == "https://x.com/a\nLook https://x.com/a\nReel"


def test_share_to_text_skips_empty_parts():
    assert share_to_text(None, "  ", "https://x.com/a") == "https://x.com/a"


def test_simultaneous_share_returns_existing_as_duplicate(store, monkeypatch):
    first = capture(store, "https://example.com/race", now=NOW)
    real = store.find_item
    calls = []

    def flaky(**kw):
        calls.append(kw)
        return None if len(calls) == 1 else real(**kw)

    monkeypatch.setattr(store, "find_item", flaky)
    r = capture(store, "https://example.com/race", now=NOW)
    assert r.item_id == first.item_id and r.duplicate is True
    assert job_count(store) == 1


def test_integrity_error_with_nothing_found_is_reraised(store, monkeypatch):
    capture(store, "https://example.com/race", now=NOW)
    monkeypatch.setattr(store, "find_item", lambda **kw: None)
    with pytest.raises(sqlite3.IntegrityError):
        capture(store, "https://example.com/race", now=NOW)


def test_capture_makes_no_network_calls(store, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    r = capture(store, "https://www.instagram.com/reel/C9xYz12AbC/", now=NOW)
    assert store.get_item(r.item_id) is not None


def test_job_insert_failure_leaves_no_orphan_item(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("job failed")

    monkeypatch.setattr(store, "insert_job", boom)
    with pytest.raises(RuntimeError):
        capture(store, "https://example.com/x", now=NOW)
    assert store.list_items() == []

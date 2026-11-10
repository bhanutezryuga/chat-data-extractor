"""The capture step: save a share locally and queue it. Never touches the network."""
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from capture.store import TYPE_HINTS, Store
from capture.urls import extract_url, normalize_url

MAX_TEXT = 10_000


@dataclass(frozen=True)
class CaptureResult:
    item_id: str
    duplicate: bool


def share_to_text(title: str | None, text: str | None, url: str | None) -> str:
    parts: list[str] = []
    for part in (url, text, title):
        part = (part or "").strip()
        if part and not any(part in earlier for earlier in parts):
            parts.append(part)
    return "\n".join(parts)


def _existing(store: Store, url_norm: str | None, client_id: str | None) -> dict | None:
    if client_id is not None:
        found = store.find_item(client_id=client_id)
        if found:
            return found
    if url_norm is not None:
        return store.find_item(url_norm=url_norm)
    return None


def capture(store: Store, text: str, *, now: datetime,
            type_hint: str | None = None, client_id: str | None = None) -> CaptureResult:
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        raise ValueError("empty")
    if type_hint is not None and type_hint not in TYPE_HINTS:
        raise ValueError("bad type_hint")

    url = extract_url(text)
    url_norm = normalize_url(url) if url else None

    found = _existing(store, url_norm, client_id)
    if found:
        return CaptureResult(found["id"], True)

    try:
        with store.transaction():
            item_id = store.insert_item(url=url, url_norm=url_norm, share_text=text,
                                        type_hint=type_hint, client_id=client_id, now=now)
            store.insert_job(item_id, now=now)
    except sqlite3.IntegrityError:
        found = _existing(store, url_norm, client_id)
        if not found:
            raise
        return CaptureResult(found["id"], True)
    return CaptureResult(item_id, False)

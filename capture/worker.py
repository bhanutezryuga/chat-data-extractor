"""Background worker: claims jobs and fills in item titles."""
import time
from datetime import datetime, timezone
from typing import Callable

from capture.config import load_settings
from capture.queue import next_job, retry_later, succeed
from capture.store import Store
from capture.titlefetch import Blocked, fetch_title

BLOCKED_REASON = "This link points at a private address, so it wasn't opened."
TEXT_TITLE_MAX = 80
IDLE_SLEEP = 2


def _text_title(share_text: str) -> str | None:
    for line in (share_text or "").splitlines():
        line = line.strip()
        if line:
            return line[:TEXT_TITLE_MAX]
    return None


def process(store: Store, job: dict, now: datetime,
            fetch: Callable[[str], str | None] = fetch_title) -> None:
    item = store.get_item(job["item_id"])
    if item is None:
        return
    if not item["url"]:
        store.update_item(item["id"], now=now, title=_text_title(item["share_text"]),
                          status="saved")
        return
    try:
        title = fetch(item["url"])
    except Blocked:
        store.update_item(item["id"], now=now, status="needs_you", status_reason=BLOCKED_REASON)
        return
    title = title or (item["url_norm"] or item["url"]).removeprefix("https://")
    store.update_item(item["id"], now=now, title=title, status="saved")


def run_once(store: Store, now: datetime | None = None,
             fetch: Callable[[str], str | None] = fetch_title) -> bool:
    now = now or datetime.now(timezone.utc)
    job = next_job(store, now)
    if job is None:
        return False
    try:
        process(store, job, now, fetch)
    except Exception as exc:
        retry_later(store, job, f"{type(exc).__name__}: {exc}", now)
        outcome = "retry"
    else:
        succeed(store, job, now)
        outcome = "done"
    print(f"job {job['id'][:8]} item {job['item_id'][:8]} {outcome}", flush=True)
    return True


def main() -> None:
    store = Store(load_settings().db_path)
    try:
        while True:
            if not run_once(store):
                time.sleep(IDLE_SLEEP)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()

"""Job queue policy: retry gaps, give-up, stale reclaim. Storage lives in Store."""
from datetime import datetime, timedelta, timezone

from capture.store import Store

RETRY_GAPS = (60, 300, 1800, 10800)  # seconds; the last one repeats
GIVE_UP_AFTER = 86400  # seconds since first failure
STALE_AFTER = 600  # seconds a "running" job may sit before it is reclaimed
GAVE_UP_REASON = "Couldn't process this link for a day: {error}"


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def next_job(store: Store, now: datetime) -> dict | None:
    return store.claim_job(now=now, stale_before=now - timedelta(seconds=STALE_AFTER))


def succeed(store: Store, job: dict, now: datetime) -> None:
    store.finish_job(job["id"], now=now)


def retry_later(store: Store, job: dict, error: str, now: datetime) -> bool:
    """Reschedule with backoff (True), or give up and flag the item (False)."""
    first = _parse(job["first_failed_at"]) if job["first_failed_at"] else now
    if (now - first).total_seconds() >= GIVE_UP_AFTER:
        with store.transaction():
            store.fail_job(job["id"], error=error, now=now)
            store.update_item(
                job["item_id"], now=now, status="needs_you",
                status_reason=GAVE_UP_REASON.format(error=error),
            )
        return False
    gap = RETRY_GAPS[min(job["failures"], len(RETRY_GAPS) - 1)]
    store.reschedule_job(
        job["id"], run_after=now + timedelta(seconds=gap), error=error, now=now
    )
    return True

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    db_path: str
    allowed_logins: frozenset[str]
    dev_no_auth: bool


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    logins = frozenset(
        p.strip().lower() for p in env.get("CAPTURE_ALLOWED_LOGINS", "").split(",") if p.strip()
    )
    return Settings(
        db_path=env.get("CAPTURE_DB_PATH", "/data/capture.db"),
        allowed_logins=logins,
        dev_no_auth=env.get("CAPTURE_DEV_NO_AUTH") == "1",
    )

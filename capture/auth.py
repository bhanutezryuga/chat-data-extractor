from __future__ import annotations

from .config import Settings

LOGIN_HEADER = "Tailscale-User-Login"


def is_allowed(login: str | None, settings: Settings) -> bool:
    if not login:
        return False
    return login.strip().lower() in settings.allowed_logins

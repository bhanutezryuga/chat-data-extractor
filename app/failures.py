"""Turn a raw processing-log error string into a human category + label, so the dashboard and
Telegram show *why* an item is stuck instead of just the bare NEEDS_REVIEW/FAILED status.

Best-effort text matching over the reasons pipeline.py/instagram.py/fetch.py actually log.
Order matters: more specific patterns are checked before generic ones.
"""
import re

# (pattern, category, human label) — first match wins.
_RULES = [
    (re.compile(r'Error 404|Error 410|Media not found or unavailable', re.I),
        "gone", "Post deleted or removed"),
    (re.compile(r'rate-limit reached or login required|cookies-from-browser|no Instagram cookies '
                r'configured|login expired', re.I),
        "login_expired", "Instagram session expired — re-export ig_cookies.txt"),
    (re.compile(r'Error 429|rate.?limit', re.I),
        "rate_limited", "Rate limited — should clear on its own"),
    (re.compile(r'Error 503|Service Unavailable', re.I),
        "unavailable", "Source temporarily unavailable"),
    (re.compile(r'timed out|timeout', re.I),
        "timeout", "Network timeout"),
    (re.compile(r'blocked non-public URL|blocked: not a public', re.I),
        "blocked", "Blocked by the SSRF guard (resolved to a non-public address)"),
    (re.compile(r'daily Gemini budget reached', re.I),
        "budget", "Daily Gemini budget reached — retries once the budget resets"),
    (re.compile(r'no rule matched', re.I),
        "unsupported", "Unsupported link — no rule matches this source"),
    (re.compile(r'not an Instagram post/reel URL', re.I),
        "unsupported", "Not a recognizable Instagram post/reel link"),
    (re.compile(r'video download unavailable|video disabled / no key', re.I),
        "no_content", "Video could not be analyzed (yt-dlp missing, or video analysis is off)"),
    (re.compile(r'no usable media or caption|description too thin|^chars=0$', re.I),
        "no_content", "No usable content found on the page/post"),
]


def classify(reason):
    """(category, label) for a raw processing-log reason string.
    Falls back to ("error", <trimmed raw text>) when nothing matches."""
    r = (reason or "").strip()
    for pattern, category, label in _RULES:
        if pattern.search(r):
            return category, label
    return "error", (r[:140] or "Unknown error")


# What the user can do about each category (shown in the Telegram reply for a stuck capture).
_TRY_LATER = "I'll keep it — send /retry in a little while."
_NEXT_STEP = {
    "gone": "Nothing to do — the post no longer exists. /archivegone clears these.",
    "login_expired": "Your Instagram cookie needs refreshing: re-export ig_cookies.txt, then send /retry.",
    "rate_limited": _TRY_LATER,
    "unavailable": _TRY_LATER,
    "timeout": _TRY_LATER,
    "budget": "I'll keep it — send /retry after the daily budget resets.",
    "blocked": "I can't fetch this address. Save it by hand with /note <text> if it matters.",
    "unsupported": "I can't read this kind of link. Save it by hand with /note <text> instead.",
    "no_content": "Send /retry to try again, or save it by hand with /note <text>.",
}


def next_step(category):
    """A short, concrete next step for a failure category (see classify)."""
    return _NEXT_STEP.get(category, "Send /retry to try again.")

"""Find a link in shared text and reduce it to a form that dedupes."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]\"'…"
_DROP_PARAMS = {"igsh", "igshid", "fbclid", "gclid", "si", "ref", "ref_src", "feature"}
_IG_KINDS = {"p", "reel", "reels", "tv"}


def extract_url(text: str) -> str | None:
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(_TRAILING)
    return url or None


def _host(netloc: str) -> str:
    host = netloc.rsplit("@", 1)[-1].lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        host = _host(parts.netloc)
        if not host or parts.scheme.lower() not in ("http", "https"):
            return url.strip().lower()
        segs = [s for s in parts.path.split("/") if s]
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return url.strip().lower()

    if host == "instagram.com":
        for i, seg in enumerate(segs[:-1]):
            if seg.lower() in _IG_KINDS and (i == 0 or (i == 1 and seg.lower() in ("p", "reel"))):
                return f"https://instagram.com/p/{segs[i + 1]}"
    if host == "youtu.be" and segs:
        return f"https://youtube.com/watch?v={segs[0]}"
    if host == "youtube.com":
        if len(segs) == 2 and segs[0] in ("shorts", "live"):
            return f"https://youtube.com/watch?v={segs[1]}"
        if segs == ["watch"]:
            vid = dict(query).get("v")
            if vid:
                return f"https://youtube.com/watch?v={vid}"

    kept = sorted(
        (k, v) for k, v in query
        if k.lower() not in _DROP_PARAMS and not k.lower().startswith("utm_")
    )
    out = f"https://{host}"
    if segs:
        out += "/" + "/".join(segs)
    if kept:
        out += "?" + urlencode(kept)
    return out

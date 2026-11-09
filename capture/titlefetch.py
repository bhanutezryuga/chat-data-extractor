"""Fetch a page title. Phase 1 stub: og:title or <title>, nothing smarter."""
import html
import re
import urllib.error
import urllib.parse
import urllib.request

from capture import netguard

MAX_BYTES = 512 * 1024
MAX_TITLE = 200
USER_AGENT = "Mozilla/5.0 (compatible; CaptureBot/1.0)"

_META = re.compile(r"<meta\b[^>]*>", re.I)
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.I | re.S)
_ATTR = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")


class Blocked(Exception):
    """The URL points somewhere that must not be fetched."""


def _clean(s: str) -> str | None:
    s = " ".join(html.unescape(s).split())
    return s[:MAX_TITLE] or None


def parse_title(page: str) -> str | None:
    for tag in _META.findall(page):
        attrs = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None)
                 for m in _ATTR.finditer(tag)}
        if attrs.get("property", "").lower() == "og:title" and attrs.get("content"):
            title = _clean(attrs["content"])
            if title:
                return title
    m = _TITLE.search(page)
    return _clean(m.group(1)) if m else None


_SAFE = "/%:@!$&'()*+,;=-._~?"


def to_uri(url: str) -> str:
    """Percent-encode an IRI (non-ASCII host/path/query); ASCII URIs pass through."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    netloc = host.encode("idna").decode("ascii")
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if parts.username is not None:
        user = urllib.parse.quote(parts.username, safe=_SAFE)
        if parts.password is not None:
            user += ":" + urllib.parse.quote(parts.password, safe=_SAFE)
        netloc = f"{user}@{netloc}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((
        parts.scheme, netloc,
        urllib.parse.quote(parts.path, safe=_SAFE),
        urllib.parse.quote(parts.query, safe=_SAFE),
        urllib.parse.quote(parts.fragment, safe=_SAFE)))


def fetch_title(url: str, *, timeout: float = 15) -> str | None:
    try:
        uri = to_uri(url)
    except ValueError:  # includes UnicodeError (bad IDNA host) and bad ports
        return None
    if not netguard.is_safe_public_url(uri):
        raise Blocked(url)
    try:
        req = urllib.request.Request(uri, headers={"User-Agent": USER_AGENT})
        with netguard.safe_opener().open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset()
    except netguard.BlockedRedirect as e:
        raise Blocked(url) from e
    except ValueError:  # UnicodeError is a ValueError: permanent, not retryable
        return None
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return None
        raise
    try:
        page = raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        page = raw.decode("utf-8", errors="replace")
    return parse_title(page)

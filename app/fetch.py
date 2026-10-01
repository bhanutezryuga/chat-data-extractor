"""Content acquisition. Returns (text, pdf_bytes, meta).

- pdf            -> download the file bytes (handed to Gemini natively)
- description_first (YouTube) -> oEmbed title
- description_first / readability -> HTML meta + body text
"""
import html
import json
import re
import urllib.request
from urllib.parse import urlparse, urlencode

from . import netguard

UA = {"User-Agent": "Mozilla/5.0 (chat-data-extractor)"}


def _get(url, accept="text/html", limit=20_000_000):
    if not netguard.is_safe_public_url(url):           # SSRF guard
        raise ValueError("blocked non-public URL")
    req = urllib.request.Request(url, headers={**UA, "Accept": accept})
    with netguard.safe_opener().open(req, timeout=25) as r:
        return r.headers.get("Content-Type", ""), r.read(limit)


def _strip(h):
    h = re.sub(r"(?is)<(script|style|noscript|nav|footer|header).*?</\1>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", h))).strip()


def _meta(h, *names):
    for n in names:
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)' % re.escape(n),
            h, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def fetch(url, strategy):
    meta = {"strategy": strategy, "fetched": False}
    try:
        # Instagram/TikTok serve no usable HTML to anonymous clients — use yt-dlp metadata
        # (the caption is where lists/recommendations live).
        host = urlparse(url).netloc.lower()
        if "instagram.com" in host or "tiktok.com" in host:
            from . import media
            info = media.social_metadata(url)
            if info.get("description") or info.get("title"):
                text = " ".join(p for p in [info.get("title"), info.get("description")] if p)
                meta.update(fetched=True, source="yt-dlp", uploader=info.get("uploader"),
                            is_video=info.get("is_video"))
                return (text, None, meta)
            err = info.get("error", "no metadata (Instagram login/cookies needed?)")
            if "instagram.com" in host:                # cookie-free: crawler-served og:description
                from . import instagram
                og = instagram.public_caption(url)
                if og.get("caption"):
                    meta.update(fetched=True, source="og-crawler", uploader=og.get("uploader"))
                    return (og["caption"], None, meta)
                err = f"{err}; og fallback: {og['error']}"
            meta["fetch_error"] = err
            return ("", None, meta)

        if strategy == "pdf":
            ctype, raw = _get(url, accept="application/pdf")
            slug = urlparse(url).path.rsplit("/", 1)[-1]
            meta.update(fetched=True, content_type=ctype, bytes=len(raw))
            return (f"PDF document: {slug or url}", raw, meta)

        if strategy == "description_first" and ("youtube.com" in url or "youtu.be" in url):
            try:
                _, raw = _get("https://www.youtube.com/oembed?" +
                              urlencode({"url": url, "format": "json"}),
                              accept="application/json")
                j = json.loads(raw.decode("utf-8", "replace"))
                meta.update(fetched=True, title=j.get("title"), author=j.get("author_name"))
                return (j.get("title", ""), None, meta)
            except Exception:
                pass  # fall through to generic fetch

        ctype, raw = _get(url)
        h = raw.decode("utf-8", "replace")
        title = _meta(h, "og:title", "twitter:title") or \
                (re.search(r"(?is)<title>(.*?)</title>", h) or [None, ""])[1].strip()
        desc = _meta(h, "og:description", "description", "twitter:description")
        body = _strip(h)[:4000] if strategy == "readability" else ""
        meta.update(fetched=True, title=title, og_description=desc)
        text = " ".join(p for p in [title, desc, body] if p).strip()
        return (text, None, meta)
    except Exception as e:
        meta["fetch_error"] = str(e)
        return ("", None, meta)

"""Instagram media extraction via Instagram's own web API.

yt-dlp can't pull Instagram image/carousel URLs, so for Instagram we go straight to
`i.instagram.com/api/v1/media/<id>/info/` using the logged-in `sessionid` from the
cookies file. Returns the caption + image/video URLs; the caller sends images to
Gemini vision (carousel "list" posts keep the list inside the images).

Without cookies, `public_caption` still gets the caption: Instagram serves it in
`og:description` to link-preview crawlers (no media URLs though).
"""
import html
import json
import os
import re
import urllib.error
import urllib.request

from . import config

_ABC = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_APP_ID = "936619743392459"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def is_instagram(url):
    return "instagram.com" in (url or "")


def shortcode(url):
    m = re.search(r'instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url or "")
    return m.group(1) if m else None


def _media_id(sc):
    mid = 0
    for ch in sc:
        if ch not in _ABC:
            return None
        mid = mid * 64 + _ABC.index(ch)
    return mid


def _cookie_header():
    p = config.INSTAGRAM_COOKIES
    if not p or not os.path.exists(p):
        return None
    sid = csrf = ""
    for line in open(p, encoding="utf-8", errors="replace"):
        f = line.rstrip("\n").split("\t")
        if len(f) >= 7 and f[5] == "sessionid":
            sid = f[6]
        if len(f) >= 7 and f[5] == "csrftoken":
            csrf = f[6]
    return f"sessionid={sid}; csrftoken={csrf}" if sid else None


def fetch_media(url, max_items=8):
    """Return {caption, uploader, images:[url], videos:[url]} or {error}."""
    sc = shortcode(url)
    if not sc:
        return {"error": "not an Instagram post/reel URL"}
    cookie = _cookie_header()
    if not cookie:
        return {"error": "no Instagram cookies configured (set INSTAGRAM_COOKIES)"}
    mid = _media_id(sc)
    api = f"https://i.instagram.com/api/v1/media/{mid}/info/"
    req = urllib.request.Request(api, headers={
        "User-Agent": _UA, "X-IG-App-ID": _APP_ID, "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # A dead session gets a 404 (the logged-out page) even for live posts -- only call it
        # gone if the post isn't publicly visible either.
        if e.code in (404, 410) and public_caption(url).get("caption"):
            return {"error": f"Instagram API refused the post (HTTP {e.code}) but it is still "
                             "public -> login expired"}
        return {"error": f"Instagram API: {e}"}
    except Exception as e:
        return {"error": f"Instagram API: {e}"}
    items = j.get("items") or []
    if not items:
        return {"error": "Instagram API returned no media (login expired?)"}
    it = items[0]
    caption = ((it.get("caption") or {}) or {}).get("text") or ""
    uploader = (it.get("user") or {}).get("username")
    media = it.get("carousel_media") or [it]
    images, videos = [], []
    for m in media[:max_items]:
        if m.get("video_versions"):
            videos.append(m["video_versions"][0]["url"])
        elif m.get("image_versions2", {}).get("candidates"):
            images.append(m["image_versions2"]["candidates"][0]["url"])
    return {"caption": caption, "uploader": uploader, "images": images, "videos": videos}


_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"


def _http_get(url, headers, limit=3_000_000):
    from . import netguard
    if not netguard.is_safe_public_url(url):
        raise ValueError("blocked non-public URL")
    with netguard.safe_opener().open(urllib.request.Request(url, headers=headers), timeout=25) as r:
        return r.read(limit).decode("utf-8", "replace")


def parse_og_caption(page):
    """{caption, uploader} from a crawler-served post page, or None if it has no caption."""
    m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', page or "")
    if not m:
        return None
    desc = html.unescape(m.group(1)).strip()
    # "2,345 likes, 67 comments - user on June 3, 2026: "caption"."
    p = re.match(r'(?s).*? - (\S+) on [^:]+: "(.*)"\.?$', desc)
    caption, uploader = (p.group(2), p.group(1)) if p else (desc, None)
    return {"caption": caption.strip(), "uploader": uploader} if caption.strip() else None


def public_caption(url):
    """Cookie-free caption via the link-preview crawler view. Returns {caption, uploader} or {error}."""
    sc = shortcode(url)
    if not sc:
        return {"error": "not an Instagram post/reel URL"}
    try:
        page = _http_get(f"https://www.instagram.com/p/{sc}/", {"User-Agent": _CRAWLER_UA})
    except Exception as e:
        return {"error": f"crawler fetch: {e}"}
    return parse_og_caption(page) or {"error": "no og:description (private, removed or rate-limited?)"}


def download(url, limit=20_000_000):
    from . import netguard
    if not netguard.is_safe_public_url(url):           # SSRF guard (CDN URLs are public)
        raise ValueError("blocked non-public URL")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with netguard.safe_opener().open(req, timeout=30) as r:
        return r.read(limit)

"""Instagram media extraction via Instagram's own web API.

yt-dlp can't pull Instagram image/carousel URLs, so for Instagram we go straight to
`i.instagram.com/api/v1/media/<id>/info/` using the logged-in `sessionid` from the
cookies file. Returns the caption + image/video URLs; the caller sends images to
Gemini vision (carousel "list" posts keep the list inside the images).
"""
import json
import os
import re
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


def download(url, limit=20_000_000):
    from . import netguard
    if not netguard.is_safe_public_url(url):           # SSRF guard (CDN URLs are public)
        raise ValueError("blocked non-public URL")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with netguard.safe_opener().open(req, timeout=30) as r:
        return r.read(limit)

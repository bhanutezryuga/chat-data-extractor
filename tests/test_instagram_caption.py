"""Tests for the cookie-free Instagram caption fallback: Instagram serves the full caption in
`og:description` to link-preview crawlers (facebookexternalhit UA) without a login, so when
yt-dlp gives nothing we still get the caption text.

Hermetic: temp DB, no real network (the HTTP call is stubbed at instagram._http_get). Run:
    python tests/test_instagram_caption.py
"""
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="cde_igcap_")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""
os.environ["INSTAGRAM_COOKIES"] = ""   # the whole point: no cookies
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import fetch, instagram, media   # noqa: E402

_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


# Mirrors the real crawler response: entity-encoded quotes/emoji/apostrophes, newlines inside
# the attribute, the "likes, comments - user on date:" prefix, and a trailing `".`.
PAGE = (
    '<html><head><meta property="og:title" content="Chef Asha on Instagram: &quot;Dal tadka'
    '&quot;" /><meta property="og:url" content="https://www.instagram.com/chefasha/reel/ABC123xyz/" />'
    '<meta property="og:description" content="2,345 likes, 67 comments - chefasha on June 3, 2026: '
    '&quot;Dal tadka &#x1f372;\nIngredients:\n- 1 cup toor dal\n- 2 tbsp ghee\nAsha&#x2019;s tip: '
    'bloom the cumin first.&quot;. " /></head><body></body></html>'
)
LOGIN_WALL = '<html><head><title>Instagram</title></head><body>Log in</body></html>'

print("parse_og_caption")
got = instagram.parse_og_caption(PAGE)
check("extracts the caption without the likes/date prefix",
      got and got["caption"].startswith("Dal tadka \U0001f372") and "likes" not in got["caption"],
      repr(got and got["caption"][:40]))
check("keeps multi-line ingredients and decodes entities",
      got and "- 1 cup toor dal\n- 2 tbsp ghee" in got["caption"]
      and "Asha’s tip: bloom the cumin first." in got["caption"]
      and not got["caption"].endswith('"'),
      repr(got and got["caption"][-40:]))
check("reports the uploader", got and got["uploader"] == "chefasha", repr(got))
check("returns None on a login-wall page with no og:description",
      instagram.parse_og_caption(LOGIN_WALL) is None)

print("public_caption")
calls = []


def fake_get(url, headers):
    calls.append((url, headers))
    return PAGE


instagram._http_get = fake_get
res = instagram.public_caption("https://www.instagram.com/reel/ABC123xyz/?igsh=tracking")
check("requests the canonical post URL", calls and calls[-1][0] == "https://www.instagram.com/p/ABC123xyz/",
      calls and calls[-1][0])
check("identifies as a link-preview crawler",
      calls and "facebookexternalhit" in calls[-1][1].get("User-Agent", ""))
check("sends no cookie", calls and "Cookie" not in calls[-1][1])
check("returns the caption", res.get("caption", "").startswith("Dal tadka"), repr(res))
check("rejects non-post URLs without fetching",
      "error" in instagram.public_caption("https://www.instagram.com/chefasha/") and len(calls) == 1)

instagram._http_get = lambda url, headers: LOGIN_WALL
check("login wall -> error, not an empty caption",
      "error" in instagram.public_caption("https://www.instagram.com/p/ABC123xyz/"))

print("fetch.fetch fallback (no cookies, yt-dlp unavailable)")
media.social_metadata = lambda url: {"error": "yt-dlp not installed"}
instagram._http_get = fake_get
text, pdf, meta = fetch.fetch("https://www.instagram.com/reel/ABC123xyz/", "description_first")
check("returns the caption text", "1 cup toor dal" in text, repr(text[:60]))
check("marks it fetched via the crawler fallback",
      meta.get("fetched") and meta.get("source") == "og-crawler", repr(meta))


def boom(url, headers):
    raise OSError("HTTP Error 429: Too Many Requests")


instagram._http_get = boom
text, pdf, meta = fetch.fetch("https://www.instagram.com/reel/ABC123xyz/", "description_first")
check("both fail -> empty text and a fetch_error naming both",
      text == "" and not meta.get("fetched") and "yt-dlp" in meta.get("fetch_error", "")
      and "429" in meta.get("fetch_error", ""), repr(meta))

print(f"\n{sum(_passed)}/{len(_passed)} passed")
sys.exit(0 if all(_passed) else 1)

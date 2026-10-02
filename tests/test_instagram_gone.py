"""A dead Instagram session makes the media API answer HTTP 404 (its logged-out page) for posts
that still exist. That must read as "login expired", not "Post deleted or removed" -- otherwise
the user is told to /archivegone live posts. A 404 for a post that is also not publicly
visible is still "gone".

Hermetic: temp DB, temp cookie file, no real network (urlopen and public_caption stubbed). Run:
    python tests/test_instagram_gone.py
"""
import io
import os
import sys
import tempfile
import urllib.error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="cde_iggone_")
_COOKIES = os.path.join(_TMP, "ig_cookies.txt")
with open(_COOKIES, "w", encoding="utf-8") as f:
    f.write(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tdead-session\n"
            ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tx\n")
os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""
os.environ["INSTAGRAM_COOKIES"] = _COOKIES
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import failures, instagram   # noqa: E402

_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


def api_returns(code, body=b'<html class="no-js not-logged-in ">'):
    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "Not Found", {}, io.BytesIO(body))
    instagram.urllib.request.urlopen = urlopen


URL = "https://www.instagram.com/reel/Dd6yzPvxy-O/?stkn=abc"
public_calls = []


def post_is_public(url):
    public_calls.append(url)
    return {"caption": "3 Ways to Make Aglio e Olio", "uploader": "forkthepeople"}


def post_not_public(url):
    public_calls.append(url)
    return {"error": "no og:description (private, removed or rate-limited?)"}


print("API 404 for a post that is still public")
api_returns(404)
instagram.public_caption = post_is_public
r = instagram.fetch_media(URL)
cat, label = failures.classify(r.get("error"))
check("is reported as an error", "error" in r, repr(r))
check("classifies as login_expired, not gone", cat == "login_expired", f"{cat}: {r.get('error')}")
check("checked the post's public view", public_calls == [URL], repr(public_calls))

print("API 410 for a post that is still public")
api_returns(410)
r = instagram.fetch_media(URL)
check("classifies as login_expired", failures.classify(r.get("error"))[0] == "login_expired",
      r.get("error"))

print("API 404 for a post that is not publicly visible either")
api_returns(404)
instagram.public_caption = post_not_public
r = instagram.fetch_media(URL)
check("still classifies as gone", failures.classify(r.get("error"))[0] == "gone", r.get("error"))

print("other API errors don't trigger the public check")
public_calls.clear()
api_returns(500, b"Oops, an error occurred.")
r = instagram.fetch_media(URL)
check("500 is left as-is", "500" in r.get("error", "") and public_calls == [], repr(r))

print(f"\n{sum(_passed)}/{len(_passed)} passed")
sys.exit(0 if all(_passed) else 1)

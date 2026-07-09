"""Lightweight single-user auth: a password unlocks an HMAC-signed session cookie.

Enabled only when APP_PASSWORD is set. The cookie is `ts.hmac(ts)` signed with
SESSION_SECRET (falls back to APP_PASSWORD), verified in constant time.
"""
import hashlib
import hmac
import time

from . import config

COOKIE = "session"
MAX_AGE = 30 * 24 * 3600   # 30 days


def enabled():
    return bool(config.APP_PASSWORD)


def _secret():
    return (config.SESSION_SECRET or config.APP_PASSWORD or "change-me").encode()


def make_token():
    ts = str(int(time.time()))
    sig = hmac.new(_secret(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def valid_token(tok):
    if not tok or "." not in tok:
        return False
    ts, sig = tok.split(".", 1)
    expected = hmac.new(_secret(), ts.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return (time.time() - int(ts)) <= MAX_AGE
    except ValueError:
        return False


def check_password(pw):
    return enabled() and hmac.compare_digest((pw or ""), config.APP_PASSWORD)


LOGIN_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Sign in</title>
<style>body{{font:15px system-ui;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:28px;width:300px}}
h1{{font-size:16px;margin:0 0 16px}} input{{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;
border:1px solid #334155;background:#0f172a;color:#e2e8f0;margin-bottom:12px}}
button{{width:100%;padding:10px;border:0;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer}}
.err{{color:#f87171;font-size:13px;margin-bottom:10px}}</style>
<form method=post action=/login>
<h1>Chat Data Extractor</h1>
{err}
<input type=password name=password placeholder=Password autofocus>
<button type=submit>Sign in</button>
</form>"""


def login_page(error=""):
    return LOGIN_PAGE.format(err=f'<div class="err">{error}</div>' if error else "")

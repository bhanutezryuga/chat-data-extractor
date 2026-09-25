"""Security hardening tests (isolated, no network/Gemini). Run: python tests/test_security.py"""
import os
import sys
import tempfile

os.environ["CDE_SKIP_DOTENV"] = "1"   # never inherit the real .env (#20)

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="cde_sec_"), "t.db")
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["LOGSEQ_GRAPH_DIR"] = ""       # NEVER write to a real graph from tests (isolate from .env)
os.environ["APP_PASSWORD"] = "s3cret-pw"
os.environ["SESSION_SECRET"] = "unit-test-secret"
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "111,222"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, config, netguard, telegram   # noqa: E402

_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(ok)


print("\n=== Security hardening ===\n")

# --- SSRF guard ---
for bad in ["http://127.0.0.1:8000/api/usage", "http://169.254.169.254/latest/meta-data/",
            "http://localhost:6379/", "http://10.0.0.5/", "http://192.168.1.1/",
            "file:///C:/Windows/win.ini", "ftp://example.com/x", "javascript:alert(1)"]:
    check(f"SSRF blocks {bad[:42]}", not netguard.is_safe_public_url(bad))
check("SSRF allows a normal public host", netguard.is_safe_public_url("https://en.wikipedia.org/wiki/Main_Page"))

# --- session cookie ---
tok = auth.make_token()
check("valid signed cookie verifies", auth.valid_token(tok))
check("forged cookie rejected", not auth.valid_token("9999999999.deadbeef"))
check("tampered cookie rejected", not auth.valid_token(tok[:-1] + ("0" if tok[-1] != "0" else "1")))
check("correct password accepted", auth.check_password("s3cret-pw"))
check("wrong password rejected", not auth.check_password("nope"))
check("auth is enabled when APP_PASSWORD set", auth.enabled())

# --- Telegram allowlist ---
check("allowlisted chat allowed", telegram._allowed(111) and telegram._allowed("222"))
check("foreign chat blocked", not telegram._allowed(999))

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

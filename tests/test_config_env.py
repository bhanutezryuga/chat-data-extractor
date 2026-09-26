"""Tests for test isolation from the developer's real .env (#20).

config.py loads the project-root .env with setdefault at import, so a test that doesn't override a
key silently inherits the real value (e.g. TELEGRAM_ALLOWED_CHAT_IDS). Tests opt out with
CDE_SKIP_DOTENV=1. Run:
    python tests/test_config_env.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ["CDE_SKIP_DOTENV"] = "1"
_TMP = tempfile.mkdtemp(prefix="cde_config_env_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config   # noqa: E402

_passed = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    _passed.append(bool(ok))


print("\n=== .env isolation (#20) ===\n")

env_file = Path(_TMP) / ".env"
env_file.write_text("CDE_SENTINEL_KEY=from-dotenv\n", encoding="utf-8")
os.environ.pop("CDE_SENTINEL_KEY", None)

config._load_dotenv(env_file)
check("opted out: a .env value is NOT loaded", "CDE_SENTINEL_KEY" not in os.environ,
      os.environ.get("CDE_SENTINEL_KEY"))

os.environ["CDE_SKIP_DOTENV"] = "0"
try:
    config._load_dotenv(env_file)
    check("not opted out: a .env value IS loaded", os.environ.get("CDE_SENTINEL_KEY") == "from-dotenv",
          os.environ.get("CDE_SENTINEL_KEY"))
finally:
    os.environ["CDE_SKIP_DOTENV"] = "1"
    os.environ.pop("CDE_SENTINEL_KEY", None)

# Guard: every test file must opt out BEFORE it imports the app, so no future test can silently
# pick up the developer's real settings.
tests_dir = Path(__file__).resolve().parent
missing = []
for f in sorted(tests_dir.glob("test_*.py")):
    src = f.read_text(encoding="utf-8")
    opt = src.find('os.environ["CDE_SKIP_DOTENV"] = "1"')
    imp = re.search(r"^from app import|^import app", src, re.M)
    if opt == -1 or (imp and imp.start() < opt):
        missing.append(f.name)
check("every test file opts out of .env before importing app", not missing, ", ".join(missing))

print(f"\n{sum(_passed)}/{len(_passed)} checks passed\n")
sys.exit(0 if all(_passed) else 1)

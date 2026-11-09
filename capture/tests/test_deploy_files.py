import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _find_bash():
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if git_bash.exists():
        return str(git_bash)
    return shutil.which("bash")


def test_web_published_on_localhost_only():
    text = Path("capture/compose.yaml").read_text()
    port_lines = [l.strip() for l in text.splitlines() if re.search(r'\d+:\d+"?\s*$', l) and "image" not in l]
    assert port_lines == ['- "127.0.0.1:8000:8000"']  # exactly one published port, bound to localhost


def test_env_file_is_gitignored():
    out = subprocess.run(["git", "check-ignore", "capture/capture.env"], capture_output=True, text=True)
    assert out.returncode == 0


def test_setup_script_parses():
    bash = _find_bash()
    if not bash:
        pytest.skip("no usable bash on this machine")
    assert subprocess.run([bash, "-n", "deploy/box/setup.sh"]).returncode == 0
    assert subprocess.run([bash, "-n", "deploy/box/update.sh"]).returncode == 0


def test_env_example_has_no_real_login():
    assert "CAPTURE_ALLOWED_LOGINS=you@example.com" in Path("capture/capture.env.example").read_text()


def test_box_scripts_are_executable_in_git():
    out = subprocess.run(["git", "ls-files", "-s", "deploy/box/setup.sh", "deploy/box/update.sh"],
                         capture_output=True, text=True).stdout.splitlines()
    assert len(out) == 2
    assert all(line.startswith("100755 ") for line in out)


def test_setup_creates_data_dir_for_uid_1000():
    text = Path("deploy/box/setup.sh").read_text()
    assert "install -d -o 1000 -g 1000" in text and "data-capture" in text


def test_docs_cover_phone_install_and_update():
    readme = Path("README.md").read_text(encoding="utf-8")
    guide = Path("deploy/box/BOX_SETUP.html").read_text(encoding="utf-8")
    assert "deploy/box/BOX_SETUP.html" in readme
    assert "Add to Home screen" in guide and "update.sh" in guide

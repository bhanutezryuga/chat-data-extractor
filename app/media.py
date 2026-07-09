"""Video / social acquisition via yt-dlp.

- YouTube: no download needed — Gemini ingests the URL directly (see gemini.analyze_video).
- Instagram / TikTok: login-gated. yt-dlp pulls the caption (--dump-json) and, when needed,
  downloads a small mp4. Auth comes from a cookies.txt (config.INSTAGRAM_COOKIES) or
  --cookies-from-browser (config.COOKIES_FROM_BROWSER).

yt-dlp is invoked as `python -m yt_dlp` so it works even when the .exe isn't on PATH.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from . import config

YTDLP = [sys.executable, "-m", "yt_dlp"]


def is_youtube(url):
    return "youtube.com" in url or "youtu.be" in url


def youtube_watch_url(url):
    m = re.search(r'(?:youtube\.com/shorts/|youtu\.be/|[?&]v=)([A-Za-z0-9_-]{6,})', url)
    return "https://www.youtube.com/watch?v=" + m.group(1) if m else url


def ytdlp_available():
    return importlib.util.find_spec("yt_dlp") is not None or shutil.which("yt-dlp") is not None


def _cookie_args():
    if config.INSTAGRAM_COOKIES and os.path.exists(config.INSTAGRAM_COOKIES):
        return ["--cookies", config.INSTAGRAM_COOKIES]
    if config.COOKIES_FROM_BROWSER:
        return ["--cookies-from-browser", config.COOKIES_FROM_BROWSER]
    return []


def social_metadata(url):
    """Caption/title/uploader via `yt-dlp --dump-json`. Returns a dict (with 'error' on failure)."""
    if not ytdlp_available():
        return {"error": "yt-dlp not installed"}
    try:
        proc = subprocess.run(
            YTDLP + ["--dump-json", "--no-warnings", "--no-playlist", "--skip-download",
                     "--ignore-no-formats-error"]
            + _cookie_args() + [url],
            capture_output=True, timeout=60)
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"error": proc.stderr.decode("utf-8", "replace")[-300:].strip() or "no output"}
        j = json.loads(proc.stdout.decode("utf-8", "replace").splitlines()[0])
        return {
            "title": j.get("title"),
            "description": j.get("description"),
            "uploader": j.get("uploader") or j.get("uploader_id"),
            "is_video": (j.get("vcodec") not in (None, "none")) or bool(j.get("duration")),
            "duration": j.get("duration"),
        }
    except Exception as e:
        return {"error": str(e)}


def download(url, max_mb=None):
    """Download a small single-file mp4. Returns (bytes, 'video/mp4') or (None, reason)."""
    if not ytdlp_available():
        return None, "yt-dlp not installed"
    max_mb = max_mb or config.MAX_VIDEO_MB
    tmp = tempfile.mkdtemp(prefix="cde_")
    out = os.path.join(tmp, "v.%(ext)s")
    fmt = f"best[ext=mp4][filesize<{max_mb}M]/best[ext=mp4]/best"
    try:
        proc = subprocess.run(
            YTDLP + ["-f", fmt, "-o", out, "--no-playlist", "--max-filesize", f"{max_mb}M"]
            + _cookie_args() + [url],
            capture_output=True, timeout=180)
        files = os.listdir(tmp)
        if proc.returncode != 0 or not files:
            err = (proc.stderr.decode("utf-8", "replace")[-200:] if proc.stderr else "no file")
            return None, f"yt-dlp: {err.strip() or 'too large / unsupported'}"
        with open(os.path.join(tmp, files[0]), "rb") as f:
            return f.read(), "video/mp4"
    except subprocess.TimeoutExpired:
        return None, "yt-dlp timed out"
    except Exception as e:
        return None, str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

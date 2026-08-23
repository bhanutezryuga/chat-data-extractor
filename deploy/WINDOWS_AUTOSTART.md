# Windows autostart (run the capture app 24/7)

Goal: `python -m app` starts automatically at every log on, with **no console window**, so the
Telegram poller + weekly digest keep running. Output is logged to `data\app.log`.

The app is a single Python process. `pythonw`/a hidden console means no window pops up.
Everything below you run yourself — none of it changes Windows settings on its own.

## Prerequisites (already true on this machine)
- Python 3.14 with the `py` launcher (`C:\Windows\py.exe`).
- Core app needs **no pip install** (standard library only). `yt-dlp` is optional (video download).
- Launcher: [`start-hidden.vbs`](start-hidden.vbs) — runs `py -m app` hidden, appends to `data\app.log`
  (auto-rolls the log at ~5 MB). Edit the `root` path in it if you move the project.

## Option A — Task Scheduler, via the provided script (easiest)
Run once in PowerShell (no admin needed):

```
powershell -ExecutionPolicy Bypass -File deploy\register-autostart.ps1
```

This registers a task **ChatDataExtractor** that runs `start-hidden.vbs` at log on, restarts it
if it ever exits, and never times out. Start it now without rebooting:

```
Start-ScheduledTask -TaskName ChatDataExtractor
```

Remove it any time: `Unregister-ScheduledTask -TaskName ChatDataExtractor -Confirm:$false`

## Option B — Task Scheduler, by hand (GUI)
Task Scheduler → **Create Task** (not Basic):
- **General:** name `ChatDataExtractor`; "Run only when user is logged on".
- **Triggers:** New → **At log on** → your user.
- **Actions:** New → Program/script `wscript.exe`; Add arguments
  `"C:\Users\bhanu\Projects\chat-data-extractor\deploy\start-hidden.vbs"`.
- **Settings:** allow "Run task as soon as possible after a scheduled start is missed";
  "If the task is already running: Do not start a new instance"; uncheck "Stop the task if it
  runs longer than…".

## Option C — Startup folder (simplest, no restart-on-crash)
Press `Win+R`, type `shell:startup`, Enter. Put a **shortcut** to `deploy\start-hidden.vbs`
in that folder. It launches at every log on. (No auto-restart if it crashes — that's the only
thing you give up vs. Task Scheduler.)

## Verify / operate
- Is it up? `http://127.0.0.1:8000` loads, or Telegram replies to a link.
- Logs: `Get-Content data\app.log -Wait -Tail 20`
- Stop it: end the `python.exe` running `-m app` (Task Manager), or `Stop-ScheduledTask -TaskName ChatDataExtractor`.
- **Only run one instance.** A second copy can't bind port 8000 and both would fight over the
  Telegram long-poll. Don't also launch `run.bat` while the task is running.

## Resource usage & startup burden
- **Memory:** ~27 MB resident idle (measured), typically 30–45 MB while serving. No heavy deps.
- **CPU (idle):** ~0%. The Telegram poller does one long-poll HTTPS request every ~50 s; the
  digest loop sleeps ~30 min between checks. Effectively nothing.
- **Network (idle):** the ~50 s Telegram long-poll and occasional keepalives — kilobytes.
- **Boot burden:** the task runs *at log on* (after the desktop is ready), so it does **not**
  delay boot. Startup itself is sub-second to a couple of seconds (Python import + a fast,
  idempotent SQLite migration).
- **Bursts (only when a link/PDF arrives):** a fetch + a Gemini API call; for a video, a `yt-dlp`
  subprocess and possibly downloading up to ~18 MB plus a multimodal Gemini call. Transient and
  event-driven — nothing sustained.
- **Disk:** the SQLite DB + `data\app.log` (self-rolls at ~5 MB → one `.old` backup).

# Deployment guide

Run it locally with zero setup, or put it on the internet safely. This app was built as a
single-user tool, so "on the internet" means: **your own always-on device + a login wall.**

> **Keep it on a home device (PC / Raspberry Pi / old Android phone), not a cloud VM.**
> Instagram's private API + cookies and yt-dlp are routinely blocked from datacenter IPs, so a
> cloud host will likely break those features. A home device keeps your **residential IP**.

---

## 1. Local use (no exposure)
```bash
cp .env.example .env      # add GEMINI_API_KEY, TELEGRAM_BOT_TOKEN (both optional)
python -m app             # or run.bat (Windows) / ./run.sh (Linux/macOS)
# open http://127.0.0.1:8000
```
With no `APP_PASSWORD`, the app runs **auth-off and refuses to bind to anything but localhost** —
a safety net so it can't be exposed by accident.

## 2. Lock the config (required before exposing)
Set these in `.env`:
```
APP_PASSWORD=<a strong password>            # turns on the login wall + allows non-localhost binding
SESSION_SECRET=<random hex>                 # python -c "import secrets;print(secrets.token_hex(32))"
TELEGRAM_ALLOWED_CHAT_IDS=<your chat id>    # send the bot /start to see it; locks the bot to you
COOKIE_SECURE=auto                          # auto=Secure only over HTTPS; use 'on' when always behind TLS
HOST=127.0.0.1                              # keep local; the tunnel reaches it
```
Also **rotate** any API keys/tokens you've shared anywhere.

## 3. Keep it running 24/7
Pick your host — all keep your residential IP:

- **Windows PC:** Task Scheduler → trigger *At log on* → start `run.bat` (Start in = project folder).
- **Linux / Raspberry Pi:** edit and install the systemd unit:
  ```bash
  chmod +x run.sh
  sudo cp deploy/chatextractor.service /etc/systemd/system/   # set User= and WorkingDirectory=
  sudo systemctl daemon-reload && sudo systemctl enable --now chatextractor
  ```
- **Old Android phone (Termux):** install Termux from **F-Droid** (not the Play Store) + **Termux:Boot**,
  then use `deploy/termux-boot-start.sh` as `~/.termux/boot/start.sh`. Keep the phone plugged in,
  disable battery optimization for Termux, and mind battery health (swelling risk on 24/7 charge).

On startup the app **auto-requeues** anything a previous crash left mid-processing, so nothing gets stuck.

## 4. Expose it — Cloudflare Tunnel + Access (recommended)
A public HTTPS URL with an email login wall, free TLS, no open ports, and your home IP hidden.
Needs a domain on Cloudflare (move its nameservers to the free plan).

```bash
# install cloudflared (Windows: winget install Cloudflare.cloudflared;
#                       Linux/Pi: apt install cloudflared; Termux: download the linux-arm64 binary)
cloudflared tunnel login
cloudflared tunnel create chatextractor
cloudflared tunnel route dns chatextractor chat.yourdomain.com
```
Create `~/.cloudflared/config.yml` (see `deploy/cloudflared-config.example.yml`):
```yaml
tunnel: <tunnel-id>
credentials-file: <path-to>/<tunnel-id>.json
ingress:
  - hostname: chat.yourdomain.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```
Run it (and install as a service so it survives reboots):
```bash
cloudflared tunnel run chatextractor      # then: cloudflared service install
```
**Add the login wall:** Cloudflare **Zero Trust → Access controls → Applications → Create new
application → Self-hosted and private** → hostname `chat.yourdomain.com` → policy **Allow →
Emails = your email** (add more emails to share). Identity provider: the built-in **One-time PIN**
needs no setup.

### Fallback — no domain: Tailscale
Install Tailscale on the host + your devices; reach it at the host's private `100.x` IP. Safest
and free, but private to your own signed-in devices (not a shareable public link). Or use
**Tailscale Funnel** for a public URL protected by the app's own password.

## 5. Verify
- From another network (e.g. phone on mobile data): open `https://chat.yourdomain.com` → Cloudflare
  login → app password → dashboard.
- Send the bot a link **from your allowlisted chat**; a different account is ignored.
- Confirm an **Instagram** post still extracts (proves the residential-IP path works).
- Confirm a request without login is blocked (`/api/*` → 401).

## Security model
| Layer | Stops |
|---|---|
| Cloudflare Access | strangers reaching the app; hides your IP; TLS |
| App password (HMAC session cookie) | anyone past the edge or hitting it locally |
| SSRF guard | the app being tricked into fetching internal/metadata URLs |
| Telegram allowlist | strangers driving your bot / draining your LLM quota |
| `HOST=127.0.0.1` + bind guard | accidental public exposure without a password |
| Login rate-limit | brute-forcing the password |

Keep `cloudflared`, the app, and `yt-dlp` updated; rotate `APP_PASSWORD` and API keys periodically.

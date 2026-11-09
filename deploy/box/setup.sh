#!/usr/bin/env bash
# One-time setup for the capture box (Ubuntu Server 24.04). Idempotent. Run: sudo deploy/box/setup.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y unattended-upgrades git sqlite3 docker.io docker-compose-v2

# Tailscale (official installer; safe to re-run)
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# Let the invoking user run docker without sudo
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
  usermod -aG docker "$TARGET_USER"
fi

# Old laptop as a server: keep running with the lid closed, never sleep
LOGIND=/etc/systemd/logind.conf
for key in HandleLidSwitch HandleLidSwitchExternalPower; do
  if grep -qE "^\s*#?\s*${key}=" "$LOGIND"; then
    sed -i -E "s|^\s*#?\s*${key}=.*|${key}=ignore|" "$LOGIND"
  else
    echo "${key}=ignore" >> "$LOGIND"
  fi
done
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
systemctl restart systemd-logind || true

tailscale up --hostname=capture

cat <<'MSG'

Setup done. Remaining manual steps:
  1. In the Tailscale admin console, enable MagicDNS and HTTPS certificates.
  2. Log out and back in so the docker group applies.
  3. cp capture/capture.env.example capture/capture.env, then set CAPTURE_ALLOWED_LOGINS to your Tailscale login email.
  4. docker compose -f capture/compose.yaml up -d --build
  5. sudo tailscale serve --bg 8000   (only AFTER step 1)
See deploy/box/BOX_SETUP.html for the full walkthrough.
MSG

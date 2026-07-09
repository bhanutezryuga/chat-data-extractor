#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot startup script — copy to ~/.termux/boot/start.sh (chmod +x).
# Keeps the phone awake and launches the app + Cloudflare tunnel on boot.

termux-wake-lock

cd "$HOME/chat-data-extractor" || exit 1

# app (auto-restarts any items left mid-processing on startup)
nohup python -m app > "$HOME/app.log" 2>&1 &

# cloudflare tunnel (edit the tunnel name if yours differs)
nohup "$HOME/cloudflared" tunnel --config "$HOME/.cloudflared/config.yml" run chatextractor \
      > "$HOME/cloudflared.log" 2>&1 &

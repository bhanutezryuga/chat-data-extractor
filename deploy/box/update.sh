#!/usr/bin/env bash
# Pull the latest code and rebuild the capture containers. Run from the repo root.
set -euo pipefail
git pull --ff-only && docker compose -f capture/compose.yaml up -d --build

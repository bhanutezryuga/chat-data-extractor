#!/usr/bin/env bash
# Run the Chat Data Extractor on Linux / macOS / Raspberry Pi.
# Usage:  ./run.sh   (chmod +x run.sh first)
cd "$(dirname "$0")"
exec python3 -m app

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_DATA_DIR="$ROOT/playwright/chrome-data"
DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
TARGET_URL="${1:-https://www.linkedin.com/feed/}"

mkdir -p "$USER_DATA_DIR"

open -na "Google Chrome" --args \
  --user-data-dir="$USER_DATA_DIR" \
  --remote-debugging-port="$DEBUG_PORT" \
  "$TARGET_URL"

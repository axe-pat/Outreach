#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BAD_USER_DATA_DIR="$ROOT/playwright/chrome-data"
USER_DATA_DIR="${LINKEDIN_CHROME_USER_DATA_DIR:-}"
DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
TARGET_URL="${1:-https://www.linkedin.com/feed/}"

if [[ -z "${USER_DATA_DIR}" ]]; then
  cat <<'EOF' >&2
ERROR: LINKEDIN_CHROME_USER_DATA_DIR is not set.

This launcher only works with an explicitly approved persistent Chrome profile.
It will not silently create or use Outreach/playwright/chrome-data.

Example:
  export LINKEDIN_CHROME_USER_DATA_DIR="/absolute/path/to/your/signed-in/chrome-data"
  ./scripts/launch_outreach_browser.sh
EOF
  exit 1
fi

if [[ "${USER_DATA_DIR}" == "${BAD_USER_DATA_DIR}" ]]; then
  cat <<EOF >&2
ERROR: Refusing to use the unsigned fallback profile:
  ${BAD_USER_DATA_DIR}
EOF
  exit 1
fi

if [[ ! -d "${USER_DATA_DIR}" ]]; then
  cat <<EOF >&2
ERROR: Chrome user-data-dir does not exist:
  ${USER_DATA_DIR}
EOF
  exit 1
fi

open -na "Google Chrome" --args \
  --user-data-dir="${USER_DATA_DIR}" \
  --remote-debugging-port="${DEBUG_PORT}" \
  "${TARGET_URL}"

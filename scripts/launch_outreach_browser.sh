#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_DATA_DIR="${LINKEDIN_CHROME_USER_DATA_DIR:-}"
PROFILE_NAME="${LINKEDIN_PROFILE_NAME:-}"
DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
TARGET_URL="${1:-https://www.linkedin.com/feed/}"

if [[ -z "${USER_DATA_DIR}" ]]; then
  cat <<'EOF' >&2
ERROR: LINKEDIN_CHROME_USER_DATA_DIR is not set.

This launcher only works with an explicitly approved persistent Chrome profile.
Use an absolute path to the signed-in profile you want Outreach to reuse.

Example:
  export LINKEDIN_CHROME_USER_DATA_DIR="/absolute/path/to/your/signed-in/chrome-data"
  export LINKEDIN_PROFILE_NAME="Default"
  ./scripts/launch_outreach_browser.sh
EOF
  exit 1
fi

if [[ -z "${PROFILE_NAME}" ]]; then
  cat <<'EOF' >&2
ERROR: LINKEDIN_PROFILE_NAME is not set.

Set it to the exact Chrome subprofile directory, for example:
  export LINKEDIN_PROFILE_NAME="Default"
EOF
  exit 1
fi

if [[ "${PROFILE_NAME}" == "." || "${PROFILE_NAME}" == ".." || "${PROFILE_NAME}" == *"/"* ]]; then
  echo "ERROR: LINKEDIN_PROFILE_NAME must be a Chrome subprofile directory name, not a path." >&2
  exit 1
fi

if [[ "${USER_DATA_DIR}" != /* ]]; then
  cat <<EOF >&2
ERROR: LINKEDIN_CHROME_USER_DATA_DIR must be an absolute path.
Current value:
  ${USER_DATA_DIR}
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
  --profile-directory="${PROFILE_NAME}" \
  --remote-debugging-port="${DEBUG_PORT}" \
  --enable-automation \
  --disable-extensions \
  "${TARGET_URL}"

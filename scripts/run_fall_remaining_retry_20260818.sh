#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/akshat/Desktop/Claude projects/Outreach"
cd "$REPO_DIR"
export LINKEDIN_CHROME_USER_DATA_DIR="${LINKEDIN_CHROME_USER_DATA_DIR:-$REPO_DIR/playwright/chrome-data}"
export LINKEDIN_DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
export PYTHONUNBUFFERED=1
LOG="artifacts/20260818-fall-remaining-retry.log"
: > "$LOG"
echo "RUN_START=$(date -Iseconds)" | tee -a "$LOG"
if ! curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
  echo CDP_DOWN | tee -a "$LOG"
  exit 3
fi
echo CDP_OK | tee -a "$LOG"
./.venv/bin/python main.py check-linkedin-live 2>&1 | tee -a "$LOG" | tail -8
if ! grep -qi 'session check passed' "$LOG"; then
  echo LIVE_CHECK_FAILED | tee -a "$LOG"; echo DONE | tee -a "$LOG"; exit 3
fi
echo LIVE_CHECK_OK | tee -a "$LOG"
set +e
caffeinate -dims ./.venv/bin/python -u scripts/retry_remaining_fall_invites_20260818.py 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
set -e
echo "EXIT_CODE=$RC" | tee -a "$LOG"
echo DONE | tee -a "$LOG"
exit "$RC"

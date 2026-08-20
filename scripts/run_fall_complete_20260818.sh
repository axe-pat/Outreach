#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/akshat/Desktop/Claude projects/Outreach"
cd "$REPO_DIR"
export LINKEDIN_CHROME_USER_DATA_DIR="${LINKEDIN_CHROME_USER_DATA_DIR:-$REPO_DIR/playwright/chrome-data}"
export LINKEDIN_DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
export PYTHONUNBUFFERED=1

LOG="artifacts/20260818-fall-complete-invites.log"
USER_DATA="$LINKEDIN_CHROME_USER_DATA_DIR"
: > "$LOG"
echo "RUN_START=$(date -Iseconds)" | tee -a "$LOG"
echo "MODE=reconcile_retry_fill_50" | tee -a "$LOG"
echo "KEEP_CHROME=1" | tee -a "$LOG"

if ! curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
  rm -f "$USER_DATA"/SingletonLock "$USER_DATA"/SingletonCookie "$USER_DATA"/SingletonSocket
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --user-data-dir="$USER_DATA" \
    --remote-debugging-port="$LINKEDIN_DEBUG_PORT" \
    --enable-automation --disable-extensions \
    "https://www.linkedin.com/feed/" >>/tmp/outreach-chrome-9222.log 2>&1 &
  echo "CHROME_PID=$!" | tee -a "$LOG"
fi
for i in $(seq 1 40); do
  curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null && { echo "CDP_READY at ${i}s" | tee -a "$LOG"; break; }
  sleep 1
done

./.venv/bin/python main.py check-linkedin-live 2>&1 | tee /tmp/fall-live-check-20260818b.txt | tee -a "$LOG" | tail -20
if ! grep -qi 'session check passed' /tmp/fall-live-check-20260818b.txt; then
  echo LIVE_CHECK_FAILED | tee -a "$LOG"; echo EXIT_CODE=3 | tee -a "$LOG"; echo DONE | tee -a "$LOG"; exit 3
fi
echo LIVE_CHECK_OK | tee -a "$LOG"

(
  while true; do
    sleep 45
    if grep -q '^DONE$' "$LOG" 2>/dev/null; then exit 0; fi
    if ! curl -s --max-time 1 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
      echo "$(date -Iseconds) chrome down — relaunch" | tee -a "$LOG"
      rm -f "$USER_DATA"/SingletonLock "$USER_DATA"/SingletonCookie "$USER_DATA"/SingletonSocket
      /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
        --user-data-dir="$USER_DATA" --remote-debugging-port="$LINKEDIN_DEBUG_PORT" \
        --enable-automation --disable-extensions \
        "https://www.linkedin.com/feed/" >>/tmp/outreach-chrome-9222.log 2>&1 &
      sleep 8
    fi
  done
) &
echo "CHROME_WD_PID=$!" | tee -a "$LOG"

set +e
caffeinate -dims ./.venv/bin/python -u scripts/send_fall_invites_complete_20260818.py \
  2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
set -e
echo "EXIT_CODE=$RC" | tee -a "$LOG"
echo DONE | tee -a "$LOG"

if [[ -n "${CHROME_WD_PID:-}" ]]; then
  kill "$CHROME_WD_PID" 2>/dev/null || true
fi
# Keep Outreach Chrome open so we can retry / you can see the session.
echo "TEARDOWN_SKIPPED_KEEP_CHROME=$(date -Iseconds)" | tee -a "$LOG"
exit "$RC"

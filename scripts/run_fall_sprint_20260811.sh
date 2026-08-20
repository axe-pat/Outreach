#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/akshat/Desktop/Claude projects/Outreach"
cd "$REPO_DIR"
export LINKEDIN_CHROME_USER_DATA_DIR="${LINKEDIN_CHROME_USER_DATA_DIR:-$REPO_DIR/playwright/chrome-data}"
export LINKEDIN_DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
export PYTHONUNBUFFERED=1

PLAN="artifacts/20260811-005837-fall-sprint-daily-plan.json"
LOG="artifacts/20260811-fall-sprint-terminal.log"
USER_DATA="$LINKEDIN_CHROME_USER_DATA_DIR"
if [[ "${FORCE_FRESH_LOG:-0}" == "1" ]] || [[ ! -f "$LOG" ]]; then
  : > "$LOG"
fi
echo "RUN_START=$(date -Iseconds)" | tee -a "$LOG"
echo "PLAN=$PLAN" | tee -a "$LOG"

if curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
  echo "CDP_ALREADY_UP" | tee -a "$LOG"
else
  rm -f "$USER_DATA"/SingletonLock "$USER_DATA"/SingletonCookie "$USER_DATA"/SingletonSocket
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --user-data-dir="$USER_DATA" \
    --remote-debugging-port="$LINKEDIN_DEBUG_PORT" \
    --enable-automation --disable-extensions \
    "https://www.linkedin.com/feed/" >>/tmp/outreach-chrome-9222.log 2>&1 &
  echo "CHROME_PID=$!" | tee -a "$LOG"
fi
for i in $(seq 1 25); do
  curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null && { echo "CDP_READY at ${i}s" | tee -a "$LOG"; break; }
  sleep 1
done
curl -s --max-time 3 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" | head -c 200 | tee -a "$LOG"; echo | tee -a "$LOG"

./.venv/bin/python main.py check-linkedin-live 2>&1 | tee /tmp/fall-live-check-20260811.txt | tee -a "$LOG" | tail -20
if ! grep -qi 'session check passed' /tmp/fall-live-check-20260811.txt; then
  echo LIVE_CHECK_FAILED | tee -a "$LOG"; echo EXIT_CODE=3 | tee -a "$LOG"; echo DONE | tee -a "$LOG"; exit 3
fi
echo LIVE_CHECK_OK | tee -a "$LOG"

(
  while true; do
    sleep 60
    if grep -q '^DONE$' "$LOG" 2>/dev/null; then exit 0; fi
    if ! curl -s --max-time 1 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
      echo "$(date -Iseconds) chrome down — relaunch" >>/tmp/outreach-chrome-watchdog-20260811.log
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
caffeinate -dims ./.venv/bin/python -u main.py run-track-2-daily-plan \
  --daily-plan-artifact "$PLAN" \
  --max-linkedin-invites 50 \
  --max-linkedin-followups 0 \
  --max-company-mapping 15 \
  --max-email-research 0 \
  --max-context-enrichment 0 \
  --max-email-drafts 0 \
  --max-total-actions 90 \
  --max-companies 35 \
  --execute --send-linkedin --no-send-linkedin-followups \
  --live-linkedin --no-refresh-linkedin \
  --max-invite-backfill-companies 15 \
  2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
set -e
echo "EXIT_CODE=$RC" | tee -a "$LOG"
echo DONE | tee -a "$LOG"

# Allow the Mac to sleep: stop chrome watchdog + Outreach CDP Chrome + stray caffeinate.
if [[ -n "${CHROME_WD_PID:-}" ]]; then
  kill "$CHROME_WD_PID" 2>/dev/null || true
fi
pkill -f "outreach-chrome-watchdog-20260811" 2>/dev/null || true
# Kill only the Outreach automation Chrome (9222 + this user-data-dir), not personal Chrome.
pkill -f "user-data-dir=${USER_DATA}.*--remote-debugging-port=${LINKEDIN_DEBUG_PORT}" 2>/dev/null || true
pkill -f "--user-data-dir=${USER_DATA} --remote-debugging-port=${LINKEDIN_DEBUG_PORT}" 2>/dev/null || true
# caffeinate wrapping this python exits with the pipeline; also clear orphans tied to this plan
pkill -f "caffeinate -dims ./.venv/bin/python -u main.py run-track-2-daily-plan --daily-plan-artifact ${PLAN}" 2>/dev/null || true
echo "TEARDOWN_FOR_SLEEP=$(date -Iseconds)" | tee -a "$LOG"
exit "$RC"

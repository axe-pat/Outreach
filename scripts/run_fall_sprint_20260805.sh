#!/usr/bin/env bash
# Fall sprint map+invite — 40 invites, Jobright-first, no follow-up sends.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/akshat/Desktop/Claude projects/Outreach"
cd "$REPO_DIR"

export LINKEDIN_CHROME_USER_DATA_DIR="${LINKEDIN_CHROME_USER_DATA_DIR:-$REPO_DIR/playwright/chrome-data}"
export LINKEDIN_DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
export PYTHONUNBUFFERED=1

PLAN="artifacts/20260805-004003-fall-sprint-daily-plan.json"
LOG="artifacts/20260805-fall-sprint-terminal.log"
USER_DATA="$LINKEDIN_CHROME_USER_DATA_DIR"

rm -f "$USER_DATA"/SingletonLock "$USER_DATA"/SingletonCookie "$USER_DATA"/SingletonSocket
: > "$LOG"

echo "RUN_START=$(date -Iseconds)" | tee -a "$LOG"
echo "PLAN=$PLAN" | tee -a "$LOG"

# Reuse existing CDP if healthy; otherwise launch Chrome.
if curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
  echo "CDP_ALREADY_UP" | tee -a "$LOG"
else
  rm -f "$USER_DATA"/SingletonLock "$USER_DATA"/SingletonCookie "$USER_DATA"/SingletonSocket
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --user-data-dir="$USER_DATA" \
    --remote-debugging-port="$LINKEDIN_DEBUG_PORT" \
    --enable-automation \
    --disable-extensions \
    "https://www.linkedin.com/feed/" >>/tmp/outreach-chrome-9222.log 2>&1 &
  echo "CHROME_PID=$!" | tee -a "$LOG"
fi

for i in $(seq 1 20); do
  if curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
    echo "CDP_READY at ${i}s" | tee -a "$LOG"
    break
  fi
  sleep 1
done
curl -s --max-time 3 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" | head -c 200 | tee -a "$LOG"
echo | tee -a "$LOG"

./.venv/bin/python main.py check-linkedin-live 2>&1 | tee /tmp/fall-live-check.txt | tee -a "$LOG" | tail -20
if ! grep -qi 'session check passed' /tmp/fall-live-check.txt; then
  echo "LIVE_CHECK_FAILED" | tee -a "$LOG"
  echo "EXIT_CODE=3" | tee -a "$LOG"
  echo "DONE" | tee -a "$LOG"
  exit 3
fi
echo "LIVE_CHECK_OK" | tee -a "$LOG"

# Chrome keepalive while run is alive
(
  while true; do
    sleep 60
    if ! kill -0 $$ 2>/dev/null; then
      exit 0
    fi
    if ! curl -s --max-time 1 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
      echo "$(date -Iseconds) chrome down — relaunch" >>/tmp/outreach-chrome-watchdog-20260805.log
      rm -f "$USER_DATA"/SingletonLock "$USER_DATA"/SingletonCookie "$USER_DATA"/SingletonSocket
      /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
        --user-data-dir="$USER_DATA" \
        --remote-debugging-port="$LINKEDIN_DEBUG_PORT" \
        --enable-automation \
        --disable-extensions \
        "https://www.linkedin.com/feed/" >>/tmp/outreach-chrome-9222.log 2>&1 &
      sleep 8
    fi
    if grep -q '^DONE$' "$LOG" 2>/dev/null; then
      exit 0
    fi
  done
) &
WD_PID=$!
echo "CHROME_WD_PID=$WD_PID" | tee -a "$LOG"

set +e
caffeinate -dims ./.venv/bin/python -u main.py run-track-2-daily-plan \
  --daily-plan-artifact "$PLAN" \
  --max-linkedin-invites 40 \
  --max-linkedin-followups 0 \
  --max-company-mapping 15 \
  --max-email-research 0 \
  --max-context-enrichment 0 \
  --max-email-drafts 0 \
  --max-total-actions 80 \
  --max-companies 30 \
  --execute \
  --send-linkedin \
  --no-send-linkedin-followups \
  --live-linkedin \
  --no-refresh-linkedin \
  --max-invite-backfill-companies 15 \
  2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
set -e

echo "EXIT_CODE=$RC" | tee -a "$LOG"
echo "DONE" | tee -a "$LOG"
kill "$WD_PID" 2>/dev/null || true
exit "$RC"

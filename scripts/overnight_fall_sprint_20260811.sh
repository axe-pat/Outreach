#!/usr/bin/env bash
# Overnight supervisor for fall sprint 2026-08-11 (50 invites).
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/akshat/Desktop/Claude projects/Outreach"
cd "$REPO_DIR"
export LINKEDIN_CHROME_USER_DATA_DIR="${LINKEDIN_CHROME_USER_DATA_DIR:-$REPO_DIR/playwright/chrome-data}"
export LINKEDIN_DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
export PYTHONUNBUFFERED=1

PLAN="artifacts/20260811-005837-fall-sprint-daily-plan.json"
LOG="artifacts/20260811-fall-sprint-terminal.log"
SUPER_LOG="artifacts/20260811-fall-sprint-overnight.log"
USER_DATA="$LINKEDIN_CHROME_USER_DATA_DIR"
MAX_HOURS=10
RESTARTS=0
MAX_RESTARTS=3

mkdir -p artifacts
: >> "$SUPER_LOG"
exec > >(tee -a "$SUPER_LOG") 2>&1

echo "OVERNIGHT_START=$(date -Iseconds)"
echo "PLAN=$PLAN LOG=$LOG"

caffeinate -dimsu -w $$ &
CAFFEINE_PID=$!

teardown_for_sleep() {
  echo "TEARDOWN_FOR_SLEEP_BEGIN=$(date -Iseconds)"
  # Stop keeping the machine awake
  kill "$CAFFEINE_PID" 2>/dev/null || true
  pkill -f "caffeinate -dims ./.venv/bin/python -u main.py run-track-2-daily-plan --daily-plan-artifact ${PLAN}" 2>/dev/null || true
  # Stop launchers / watchdogs for this run
  pkill -f "scripts/run_fall_sprint_20260811.sh" 2>/dev/null || true
  pkill -f "outreach-chrome-watchdog-20260811" 2>/dev/null || true
  # Quit Outreach CDP Chrome only (shared profile on 9222)
  pkill -f "--user-data-dir=${USER_DATA} --remote-debugging-port=${LINKEDIN_DEBUG_PORT}" 2>/dev/null || true
  sleep 2
  if curl -s --max-time 1 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
    local pid cmd
    pid=$(lsof -tiTCP:"${LINKEDIN_DEBUG_PORT}" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [[ -n "$pid" ]]; then
      cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
      if [[ "$cmd" == *"$USER_DATA"* ]]; then
        echo "Killing CDP listener pid=$pid"
        kill "$pid" 2>/dev/null || true
      else
        echo "Leaving unrelated CDP listener pid=$pid"
      fi
    fi
  fi
  echo "TEARDOWN_FOR_SLEEP_DONE=$(date -Iseconds) (caffeinate+watchdogs+Outreach Chrome stopped — Mac can sleep)"
}

# Only drop our own caffeinate on unexpected exit; full sleep teardown is explicit on DONE.
trap 'kill "$CAFFEINE_PID" 2>/dev/null || true' EXIT

launch_chrome() {
  if curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
    echo "CDP_OK $(date -Iseconds)"; return 0
  fi
  echo "CDP_DOWN — launching Chrome $(date -Iseconds)"
  rm -f "$USER_DATA"/SingletonLock "$USER_DATA"/SingletonCookie "$USER_DATA"/SingletonSocket
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --user-data-dir="$USER_DATA" --remote-debugging-port="$LINKEDIN_DEBUG_PORT" \
    --enable-automation --disable-extensions \
    "https://www.linkedin.com/feed/" >>/tmp/outreach-chrome-9222.log 2>&1 &
  for i in $(seq 1 40); do
    curl -s --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null && { echo "CDP_READY at ${i}s"; return 0; }
    sleep 1
  done
  echo "CDP_FAILED"; return 1
}

run_alive() { pgrep -f "run-track-2-daily-plan --daily-plan-artifact ${PLAN}" >/dev/null; }
launcher_alive() { pgrep -f "scripts/run_fall_sprint_20260811.sh" >/dev/null; }

start_run() {
  launch_chrome || return 1
  if run_alive; then echo "RUN_ALREADY_ALIVE $(date -Iseconds)"; return 0; fi
  echo "STARTING_RUN restart=${RESTARTS} $(date -Iseconds)"
  echo "" >> "$LOG"
  echo "==== SUPERVISOR_RESTART=$(date -Iseconds) restart=${RESTARTS} ====" >> "$LOG"
  open -a Terminal "$REPO_DIR/scripts/run_fall_sprint_20260811.command"
  sleep 25
  if run_alive || launcher_alive; then echo "RUN_LAUNCHED_OK"; return 0; fi
  echo "RUN_LAUNCH_FAILED — direct exec"
  (
    cd "$REPO_DIR"
    caffeinate -dims ./.venv/bin/python -u main.py run-track-2-daily-plan \
      --daily-plan-artifact "$PLAN" \
      --max-linkedin-invites 50 --max-linkedin-followups 0 --max-company-mapping 15 \
      --max-email-research 0 --max-context-enrichment 0 --max-email-drafts 0 \
      --max-total-actions 90 --max-companies 35 \
      --execute --send-linkedin --no-send-linkedin-followups \
      --live-linkedin --no-refresh-linkedin --max-invite-backfill-companies 15 \
      >>"$LOG" 2>&1
    echo "EXIT_CODE=$?" >>"$LOG"
    echo DONE >>"$LOG"
  ) &
  sleep 10
  run_alive && echo "DIRECT_RUN_OK" || echo "DIRECT_RUN_FAILED"
}

summarize_if_done() {
  echo "==== COMPLETION SUMMARY $(date -Iseconds) ===="
  rg -n 'LIVE_CHECK|Ran Track|Phases:|EXIT_CODE|^DONE$|sent_count|partial_failed|5_send' "$LOG" | tail -40 || true
  ls -lt artifacts/*20260811*track-2-daily-run.json 2>/dev/null | head -3 || true
  ./.venv/bin/python - <<'PY' || true
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import json
from outreach.config import OutreachSettings
from outreach.tracking import OutreachWorkbook
wb=OutreachWorkbook(OutreachSettings().resolved_tracking_workspace_dir)
orgs={o.organization_id:o.name for o in wb.list_organizations()}
mark=datetime(2026,8,10,19,0,tzinfo=timezone.utc)
cos=Counter(); n=0
for t in wb.list_touchpoints():
    if t.message_kind!='linkedin_invite': continue
    ok=(t.status or '').lower()=='sent' or 'invite_result=sent' in (t.notes or '')
    if not ok: continue
    ts=t.sent_at or t.recorded_at
    if not ts: continue
    dt=datetime.fromisoformat(ts.replace('Z','+00:00'))
    if dt>=mark:
        n+=1; cos[orgs.get(t.organization_id,'?')]+=1
print('invites_since_run_start', n)
print(cos.most_common(20))
runs=sorted(Path('artifacts').glob('20260811*track-2-daily-run.json'))
if runs:
    d=json.loads(runs[-1].read_text())
    print('latest_run', runs[-1].name, 'used', d.get('used'))
    for ph in d.get('phase_results') or []:
        if ph.get('phase') in {'4_contact_mapping','5_send_linkedin_invites'}:
            print(ph.get('phase'), 'status=', ph.get('status'), 'sent=', ph.get('sent_count'), 'backfill=', ph.get('backfill_sent_count'))
PY
}

if ! grep -q '^DONE$' "$LOG" 2>/dev/null; then
  start_run || true
fi

END_EPOCH=$(( $(date +%s) + MAX_HOURS * 3600 ))
DEAD_STREAK=0
while (( $(date +%s) < END_EPOCH )); do
  if grep -q '^DONE$' "$LOG" 2>/dev/null; then
    # Also require run artifact with invites if present
    echo "DETECTED_DONE $(date -Iseconds)"
    summarize_if_done
    echo "OVERNIGHT_EXIT=0"; echo "OVERNIGHT_DONE"
    teardown_for_sleep
    exit 0
  fi
  # Detect finished run artifact even if DONE missing (Aug7 failure mode)
  RUN=$(ls -t artifacts/20260811*track-2-daily-run.json 2>/dev/null | head -1 || true)
  if [[ -n "$RUN" ]] && ! run_alive; then
    if ./.venv/bin/python - <<PY
import json
from pathlib import Path
d=json.loads(Path("$RUN").read_text())
used=d.get("used") or {}
ok=int(used.get("linkedin_invites") or 0) >= 40
print("artifact", "$RUN", "invites", used.get("linkedin_invites"), "ok", ok)
raise SystemExit(0 if ok else 1)
PY
    then
      echo "DETECTED_DONE_VIA_RUN_ARTIFACT=$(date -Iseconds) run=$RUN" | tee -a "$LOG"
      echo "EXIT_CODE=0" >> "$LOG"
      echo "DONE" >> "$LOG"
      summarize_if_done
      echo "OVERNIGHT_EXIT=0"; echo "OVERNIGHT_DONE"
      teardown_for_sleep
      exit 0
    fi
  fi

  launch_chrome || true
  if run_alive || launcher_alive; then
    DEAD_STREAK=0
    echo "==== $(date -Iseconds) status=RUNNING cdp=$(curl -s --max-time 1 http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version >/dev/null && echo up || echo DOWN) ===="
    tail -6 "$LOG" 2>/dev/null | grep -v 'DeprecationWarning\|url\.parse\|Use `node' || tail -4 "$LOG"
  else
    DEAD_STREAK=$((DEAD_STREAK + 1))
    echo "==== $(date -Iseconds) status=DEAD streak=${DEAD_STREAK} ===="
    tail -20 "$LOG" 2>/dev/null || true
    if grep -q '^DONE$' "$LOG" 2>/dev/null; then continue; fi
    if (( DEAD_STREAK >= 2 )); then
      if (( RESTARTS >= MAX_RESTARTS )); then
        echo "MAX_RESTARTS_REACHED"; echo "OVERNIGHT_EXIT=2"; echo "OVERNIGHT_DONE"
        teardown_for_sleep
        exit 2
      fi
      RESTARTS=$((RESTARTS + 1)); DEAD_STREAK=0
      start_run || true
    fi
  fi
  sleep 90
done

echo "TIMEOUT_AFTER_${MAX_HOURS}H $(date -Iseconds)"
summarize_if_done
echo "OVERNIGHT_EXIT=1"; echo "OVERNIGHT_DONE"
teardown_for_sleep
exit 1

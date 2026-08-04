#!/usr/bin/env bash
# Durable babysitter: keep Track 2 running until ~40 invites land (or hard stop).
# Follow-ups are drafted/refreshed but NOT sent.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/akshat/Desktop/Claude projects/Outreach"
cd "$REPO_DIR"

export LINKEDIN_CHROME_USER_DATA_DIR="${LINKEDIN_CHROME_USER_DATA_DIR:-/Users/akshat/Desktop/Claude projects/Outreach/playwright/chrome-data}"
export LINKEDIN_PROFILE_NAME="${LINKEDIN_PROFILE_NAME:-Default}"
export LINKEDIN_DEBUG_PORT="${LINKEDIN_DEBUG_PORT:-9222}"
export PYTHONUNBUFFERED=1

TARGET_INVITES="${TARGET_INVITES:-40}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-4}"
STARTED_MARK="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"
MASTER_LOG="$LOG_DIR/e2e-40invites-watchdog-latest.log"
RUN_LOG="$LOG_DIR/e2e-40invites-watchdog-$(date +%Y%m%d-%H%M%S).log"

log() {
  local line="[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*"
  echo "$line" | tee -a "$MASTER_LOG" "$RUN_LOG"
}

ensure_cdp() {
  if curl -sS --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null; then
    return 0
  fi
  log "CDP down; launching Outreach browser"
  scripts/launch_outreach_browser.sh "https://www.linkedin.com/feed/" || true
  sleep 8
  curl -sS --max-time 5 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT}/json/version" >/dev/null
}

count_invites_since() {
  # Count invite Sent touchpoints recorded at/after STARTED_MARK.
  ./.venv/bin/python - "$STARTED_MARK" <<'PY'
import csv, sys
from datetime import datetime, timezone
from pathlib import Path

mark = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
path = Path("workspace/touchpoints.csv")
if not path.exists():
    print(0)
    raise SystemExit
count = 0
with path.open(newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        if (row.get("message_kind") or "") != "linkedin_invite":
            continue
        if (row.get("status") or "").casefold() != "sent":
            continue
        raw = (row.get("sent_at") or row.get("recorded_at") or "").strip()
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= mark:
            count += 1
print(count)
PY
}

attempt=1
log "Watchdog start. target=${TARGET_INVITES} invites since ${STARTED_MARK}. max_attempts=${MAX_ATTEMPTS}"

while (( attempt <= MAX_ATTEMPTS )); do
  sent_so_far="$(count_invites_since || echo 0)"
  remaining=$(( TARGET_INVITES - sent_so_far ))
  if (( remaining <= 0 )); then
    log "Done. sent_so_far=${sent_so_far} >= target=${TARGET_INVITES}"
    exit 0
  fi

  log "Attempt ${attempt}/${MAX_ATTEMPTS}: sent_so_far=${sent_so_far}, remaining≈${remaining}"
  ensure_cdp

  # Call Track 2 directly. Full supervised-e2e currently dies on unstaged
  # relationship-leads import before invites ever start; Track 2 is the lane
  # that actually sends.
  attempt_log="$LOG_DIR/e2e-40invites-attempt${attempt}-$(date +%Y%m%d-%H%M%S).log"
  set +e
  ./.venv/bin/python -u main.py run-track-2-daily-plan \
    --execute \
    --live-linkedin \
    --refresh-linkedin \
    --send-linkedin \
    --no-send-linkedin-followups \
    --max-total-actions 80 \
    --max-companies 50 \
    --max-linkedin-invites "$remaining" \
    --max-linkedin-followups -1 \
    --max-company-mapping 0 \
    --max-email-research 0 \
    --max-context-enrichment 0 \
    --max-email-drafts 0 \
    2>&1 | tee -a "$attempt_log" "$MASTER_LOG" "$RUN_LOG" "$LOG_DIR/e2e-40invites-latest.log"
  rc=${PIPESTATUS[0]}
  set -e
  log "Attempt ${attempt} exited rc=${rc}"

  sent_so_far="$(count_invites_since || echo 0)"
  log "After attempt ${attempt}: sent_so_far=${sent_so_far}"
  if (( sent_so_far >= TARGET_INVITES )); then
    log "Done. Hit target ${TARGET_INVITES}."
    exit 0
  fi

  attempt=$((attempt + 1))
  sleep 15
done

sent_so_far="$(count_invites_since || echo 0)"
log "Stopped after ${MAX_ATTEMPTS} attempts. sent_so_far=${sent_so_far} target=${TARGET_INVITES}"
exit 1

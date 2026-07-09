#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO_DIR="/Users/akshat/Desktop/Claude Projects/Outreach"
cd "$REPO_DIR"

if [[ -f ".env" ]]; then
  while IFS='=' read -r key value; do
    [[ -z "${key// }" || "${key:0:1}" == "#" ]] && continue
    case "$key" in
      LINKEDIN_*|PROSPEO_API_KEY|HUNTER_API_KEY|EMAIL_FINDER_*|ANTHROPIC_API_KEY|OPENAI_API_KEY)
        export "${key}=${value}"
        ;;
    esac
  done < ".env"
fi

mkdir -p logs
timestamp="$(date +%Y%m%d-%H%M%S)"
latest_log="logs/daily-supervised-e2e.log"
run_log="logs/daily-supervised-e2e-${timestamp}.log"

{
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] Starting Outreach supervised E2E"
  if ! curl -sS --max-time 2 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT:-9222}/json/version" >/dev/null; then
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] LinkedIn Chrome CDP is not reachable; launching Outreach browser"
    scripts/launch_outreach_browser.sh "https://www.linkedin.com/feed/"
    sleep 8
  fi
  if ! curl -sS --max-time 5 "http://127.0.0.1:${LINKEDIN_DEBUG_PORT:-9222}/json/version" >/dev/null; then
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] ERROR: LinkedIn Chrome CDP is still not reachable"
    exit 1
  fi
  ./.venv/bin/python main.py run-supervised-e2e \
    --execute \
    --live-linkedin \
    --refresh-linkedin \
    --send-linkedin \
    --run-resume-generator-discovery \
    --run-resume-generator-generation \
    --resume-generator-top 10 \
    --resume-generator-min-score 8.0 \
    --resume-generator-budget-mode \
    --resume-season-focus fall_ft_transition \
    --max-total-actions 24 \
    --max-companies 18 \
    --max-linkedin-invites 12 \
    --max-linkedin-followups 8 \
    --max-company-mapping 5 \
    --max-email-research 0 \
    --max-context-enrichment 8 \
    --max-email-drafts 0
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] Finished Outreach supervised E2E"
} 2>&1 | tee -a "$latest_log" "$run_log"

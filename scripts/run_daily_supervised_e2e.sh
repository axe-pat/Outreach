#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO_DIR="/Users/akshat/Desktop/Claude Projects/Outreach"
cd "$REPO_DIR"

mkdir -p logs
timestamp="$(date +%Y%m%d-%H%M%S)"
latest_log="logs/daily-supervised-e2e.log"
run_log="logs/daily-supervised-e2e-${timestamp}.log"

{
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] Starting Outreach supervised E2E"
  ./.venv/bin/python main.py run-supervised-e2e \
    --execute \
    --resume-season-focus fall_ft_transition \
    --max-total-actions 24 \
    --max-companies 18 \
    --max-linkedin-invites 12 \
    --max-linkedin-followups 8 \
    --max-company-mapping 5 \
    --max-email-research 5 \
    --max-context-enrichment 8 \
    --max-email-drafts 0
  echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] Finished Outreach supervised E2E"
} 2>&1 | tee -a "$latest_log" "$run_log"

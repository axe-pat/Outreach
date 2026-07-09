#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cat >&2 <<'MSG'
[outreach] NOTE: scripts/run_daily_supervised_e2e.sh is a compatibility shim.
[outreach] The scheduled daily runner is the ResumeGenerator LaunchAgent path.
[outreach] For an attended/manual Outreach debug run, use:
[outreach]   scripts/run_manual_supervised_e2e_debug.sh
MSG

exec "${SCRIPT_DIR}/run_manual_supervised_e2e_debug.sh" "$@"

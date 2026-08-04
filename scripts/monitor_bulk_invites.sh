#!/usr/bin/env bash
set -u
BULK_PID="${1:?bulk pid required}"
LOG="${2:-$HOME/Library/Logs/ResumeGenerator/bulk_invites_debug.log}"
MON="${3:-$HOME/Library/Logs/ResumeGenerator/bulk_invites_monitor_20260728.log}"
ROOT="/Users/akshat/Desktop/Claude projects/Outreach"
ENSURE="/Users/akshat/Desktop/Claude projects/ResumeGenerator v1/discovery/scripts/ensure_chrome_9222.sh"
CHECK="/Users/akshat/Desktop/Claude projects/ResumeGenerator v1/discovery/scripts/check_linkedin_live.sh"
PY="$ROOT/.venv/bin/python"

echo "monitor2 start $(date -Iseconds) bulk=$BULK_PID" | tee -a "$MON"
chrome_restarts=0
while kill -0 "$BULK_PID" 2>/dev/null; do
  sleep 60
  "$PY" - "$ROOT" >>"$MON" 2>&1 <<'PY'
import json, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

root = Path(sys.argv[1])
art = root / "artifacts"
ledger_path = root / "workspace" / "linkedin_invite_send_reservations.json"
batches = sorted(art.glob("20260728-*-invite-send-batch.json"))
statuses: Counter[str] = Counter()
details: list[tuple[str, str, str]] = []
sent_n = 0
for path in batches:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    for row in payload.get("results") or []:
        status = str(row.get("status") or "")
        statuses[status] += 1
        if status == "sent":
            sent_n += 1
        detail = str(row.get("detail") or "")
        lower = detail.casefold()
        if status in {"send_unknown_reserved", "preflight_failed", "send_error"} or (
            "chrome-error" in lower or "preflight" in lower
        ):
            details.append((status, str(row.get("name") or ""), detail[:160]))

reservations = json.loads(ledger_path.read_text(encoding="utf-8"))
ledger = reservations.get("reservations") or {}
unknown = sum(
    1
    for entry in ledger.values()
    if isinstance(entry, dict) and entry.get("status") == "send_unknown_reserved"
)
chromeish = sum(
    1
    for status, _name, detail in details
    if any(
        marker in detail.casefold()
        for marker in ("chrome-error", "chromewebdata", "preflight failed")
    )
)
print(
    f"{datetime.now().isoformat(timespec='seconds')} batches={len(batches)} "
    f"sent={sent_n} statuses={dict(statuses)} unk={unknown} chromeish={chromeish} "
    f"problems={len(details)}"
)
for status, name, detail in details[-6:]:
    print(f"  ! {status} {name} :: {detail}")

signal = Path("/tmp/bulk_invite_need_chrome_restart")
if chromeish >= 3 and sent_n < 8:
    signal.write_text(str(chromeish), encoding="utf-8")
elif unknown >= 5:
    signal.write_text(f"unk={unknown}", encoding="utf-8")
else:
    signal.unlink(missing_ok=True)

released = 0
now = datetime.now().astimezone().replace(microsecond=0).isoformat()
changed = False
for entry in ledger.values():
    if not isinstance(entry, dict):
        continue
    if entry.get("status") != "send_unknown_reserved":
        continue
    detail = str(entry.get("detail") or "").casefold()
    if any(
        marker in detail
        for marker in (
            "preflight failed",
            "chrome-error://",
            "chromewebdata",
            "nothing is listening",
            "could not attach to chrome",
        )
    ):
        entry["status"] = "preflight_failed"
        entry["reconciliation_required"] = False
        entry["detail"] = (
            "Hot-released mid-run (preflight before send). "
            + str(entry.get("detail") or "")[:200]
        )
        entry["updated_at"] = now
        released += 1
        changed = True
if changed:
    reservations["updated_at"] = now
    ledger_path.write_text(
        json.dumps(reservations, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  HOT_RELEASED_PREFLIGHT={released}")
PY

  if [[ -f /tmp/bulk_invite_need_chrome_restart ]]; then
    reason="$(cat /tmp/bulk_invite_need_chrome_restart)"
    echo "$(date -Iseconds) RESTART_CHROME ${reason}" | tee -a "$MON"
    "$ENSURE" https://www.linkedin.com/feed/ >>"$MON" 2>&1 || true
    "$CHECK" >>"$MON" 2>&1 || true
    chrome_restarts=$((chrome_restarts + 1))
    rm -f /tmp/bulk_invite_need_chrome_restart
    echo "$(date -Iseconds) chrome_restarts=${chrome_restarts}" | tee -a "$MON"
  fi
done

echo "monitor2 end $(date -Iseconds)" | tee -a "$MON"
"$PY" - "$ROOT" >>"$MON" 2>&1 <<'PY'
import json
from collections import Counter
from pathlib import Path
import sys

root = Path(sys.argv[1])
art = root / "artifacts"
statuses: Counter[str] = Counter()
sent: list[str] = []
for path in sorted(art.glob("20260728-*-invite-send-batch.json")):
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload.get("results") or []:
        status = str(row.get("status") or "")
        statuses[status] += 1
        if status == "sent":
            sent.append(str(row.get("name") or ""))
print("FINAL_BATCH_STATUSES", dict(statuses))
print("FINAL_SENT_COUNT", len(sent))
print("SENT", sent)
runs = sorted(art.glob("20260728-*-track-2-daily-run.json"))
if runs:
    payload = json.loads(runs[-1].read_text(encoding="utf-8"))
    for phase in payload.get("phase_results") or []:
        if phase.get("phase") == "5_send_linkedin_invites":
            print(
                "INVITE_PHASE",
                phase.get("status"),
                "sent",
                phase.get("sent_count"),
                "budget",
                phase.get("budget"),
                "remaining",
                phase.get("remaining_budget"),
            )
PY

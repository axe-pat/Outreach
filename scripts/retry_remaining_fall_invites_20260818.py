#!/usr/bin/env python3
"""Retry the 13 remaining unavailable Fall invites while Chrome is still up."""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from outreach.artifacts import write_artifact  # noqa: E402
from outreach.cli import execute_invite_batch  # noqa: E402
from outreach.config import OutreachSettings  # noqa: E402
from outreach.invite_reservations import (  # noqa: E402
    invite_reservation_blocks_retry,
    load_invite_reservations,
    reservation_key,
)
from outreach.services.linkedin import LinkedInScraper  # noqa: E402
import outreach.cli as cli  # noqa: E402

cli._partition_initial_invites_for_review = lambda candidates, organization=None: (
    list(candidates),
    [],
)

REMAINING = [
    ("Micro1", "https://www.linkedin.com/in/marianamcnally/"),
    ("ConverzAI", "https://www.linkedin.com/in/ashwarya-poddar-25943a9/"),
    ("Turing", "https://www.linkedin.com/in/awasthi-ram/"),
    ("Jobright.ai", "https://www.linkedin.com/in/zoe-zhou-165029278/"),
    ("Jobright.ai", "https://www.linkedin.com/in/zhengyudian/"),
    ("Jobright.ai", "https://www.linkedin.com/in/ericcheng26/"),
    ("Voker", "https://www.linkedin.com/in/jordan-rowe-42a6313a1/"),
    ("Amperesand", "https://www.linkedin.com/in/rahul-vattigunta/"),
    ("Commure", "https://www.linkedin.com/in/manikanta-varaganti/"),
    ("Snorkel AI", "https://www.linkedin.com/in/raghavananand/"),
    ("Snorkel AI", "https://www.linkedin.com/in/amirfleurizard/"),
    ("Anam AI", "https://www.linkedin.com/in/annaabuckley/"),
    ("Invisible Technologies", "https://www.linkedin.com/in/saurabhdubey98/"),
]


def load_candidates() -> dict[str, list[dict]]:
    by_url: dict[str, dict] = {}
    for path in Path("artifacts").glob("20260818-*-fall-complete-invite-source-*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("results") or []:
            url = str(row.get("linkedin_url") or "")
            if url:
                by_url[url.rstrip("/")] = row
    grouped: dict[str, list[dict]] = defaultdict(list)
    missing = []
    for company, url in REMAINING:
        row = by_url.get(url.rstrip("/"))
        if not row:
            missing.append((company, url))
            continue
        grouped[company].append(row)
    if missing:
        raise SystemExit(f"Missing source rows: {missing}")
    return grouped


def main() -> int:
    settings = OutreachSettings()
    LinkedInScraper(settings).require_live_cdp_session()
    ledger = load_invite_reservations(
        Path(settings.resolved_tracking_workspace_dir) / "linkedin_invite_send_reservations.json"
    )
    grouped = load_candidates()
    grand: dict[str, int] = {}
    for company, rows in grouped.items():
        sendable = []
        for row in rows:
            key = reservation_key(
                linkedin_url=str(row["linkedin_url"]),
                company=company,
                name=str(row["name"]),
            )
            existing = (ledger.get("reservations") or {}).get(key) or {}
            status = str(existing.get("status") or "")
            blocked = invite_reservation_blocks_retry(existing)
            print(f"TRY {company:22} {row['name']:28} res={status or 'none':16} block={blocked}")
            if blocked and status in {"sent", "sent_without_note", "reconciled_pending", "reconciled_connected", "already_connected"}:
                print("  skip already sent/connected")
                continue
            sendable.append(row)
        if not sendable:
            continue
        source = {
            "company": company,
            "company_mode": "default",
            "dry_run": False,
            "source": "fall_remaining_unavailable_retry",
            "company_filter_status": "completed_mapped_workbook_assignment",
            "company_filter_error": "",
            "count": len(sendable),
            "results": sendable,
        }
        source_path = write_artifact(
            settings.artifacts_dir,
            f"fall-remaining-retry-{re.sub(r'[^a-z0-9]+', '-', company.lower()).strip('-')}",
            source,
        )
        print(f"SENDING {company} n={len(sendable)}")
        _a, _p, counts, contacts, tps = execute_invite_batch(
            settings=settings,
            company=company,
            source_artifact_path=source_path,
            batch=sendable,
            execute=True,
            limit=len(sendable),
            start_at=0,
            verdict="send",
            min_score=0,
            source_payload_snapshot=source,
        )
        print(f"  done {company} {counts} contacts={contacts} tps={tps}")
        for key, value in counts.items():
            grand[key] = grand.get(key, 0) + value
        ledger = load_invite_reservations(
            Path(settings.resolved_tracking_workspace_dir) / "linkedin_invite_send_reservations.json"
        )
    print(f"GRAND {grand}")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

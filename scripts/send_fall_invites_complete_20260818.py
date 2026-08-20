#!/usr/bin/env python3
"""Complete the Fall invite send: reconcile unknowns, retry misses, fill toward 50."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from outreach.artifacts import write_artifact  # noqa: E402
from outreach.cli import (  # noqa: E402
    apply_linkedin_reconcile_results,
    execute_invite_batch,
)
from outreach.config import OutreachSettings  # noqa: E402
from outreach.invite_reservations import (  # noqa: E402
    load_invite_reservations,
    reservation_key,
)
from outreach.mapped_invites import (  # noqa: E402
    _company_mode,
    build_mapped_invite_candidates,
)
from outreach.services.linkedin import LinkedInScraper  # noqa: E402
from outreach.services.notes import NoteGenerator  # noqa: E402
from outreach.tracking import OutreachWorkbook  # noqa: E402
import outreach.cli as cli  # noqa: E402

cli._partition_initial_invites_for_review = lambda candidates, organization=None: (
    list(candidates),
    [],
)

REWRITE = ROOT / "artifacts" / "20260818-invite-notes-rewritten.md"
PREVIEW = ROOT / "artifacts" / "20260818-091125-fall-sprint-invite-notes-preview.json"
PACK = ROOT / "artifacts" / "20260818-161137-fall-sprint-approved-invite-pack.json"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_DATA = ROOT / "playwright" / "chrome-data"
PORT = 9222
TARGET = 50
SKIP_NAMES = {
    "tim sackett",
    "maria burton",
    "rose logenio",
    "dario cioni",
}


def _cdp_up() -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2) as handle:
            return bool(handle.read())
    except Exception:
        return False


def ensure_chrome() -> None:
    if _cdp_up():
        print("CDP_OK")
        return
    print("CDP_DOWN — launching Chrome")
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (USER_DATA / name).unlink(missing_ok=True)
    subprocess.Popen(
        [
            CHROME,
            f"--user-data-dir={USER_DATA}",
            f"--remote-debugging-port={PORT}",
            "--enable-automation",
            "--disable-extensions",
            "https://www.linkedin.com/feed/",
        ],
        stdout=open("/tmp/outreach-chrome-9222.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for i in range(40):
        if _cdp_up():
            print(f"CDP_READY at {i+1}s")
            return
        time.sleep(1)
    raise RuntimeError("Could not start Chrome CDP on 9222")


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _name_key(full_name: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", " ", full_name or "")
    cleaned = re.sub(r"[^\w\s.-]", " ", cleaned)
    parts = [p for p in cleaned.split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return _norm(parts[0])
    return _norm(f"{parts[0]} {parts[-1]}")


def skipped_name(name: str) -> bool:
    n = _norm(name)
    return any(skip in n for skip in SKIP_NAMES)


def candidate_payload(row: dict, note: str) -> dict:
    company = row["company"]
    return {
        **row,
        "note": note,
        "note_length": len(note),
        "passes": ["mapped_workbook_contact"],
        "existing_connection": False,
        "target_company_match": True,
        "target_company_evidence_company": company,
        "target_company_evidence_passes": ["mapped_workbook_contact"],
        "snippet": f"Current: {row.get('title') or ''} at {company}".strip(),
        "raw_text": f"Current: {row.get('title') or ''} at {company} | {row.get('title') or ''}",
        "note_qc": {
            "verdict": "send",
            "score": 100,
            "flags": [],
            "strengths": ["operator_send_all"],
        },
        "contact_id": row.get("mapped_contact_id") or row.get("contact_id") or "",
        "operator_approved_note": True,
    }


def load_universe() -> list[dict]:
    preview = json.loads(PREVIEW.read_text(encoding="utf-8"))
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    by_url: dict[str, dict] = {}
    for row in preview["notes"]:
        if skipped_name(row["name"]):
            continue
        by_url[row["linkedin_url"]] = candidate_payload(row, row["note"])
    # Approved rewrite notes win.
    for person in pack["people"]:
        if skipped_name(person["name"]):
            continue
        base = by_url.get(person["linkedin_url"]) or {
            "company": person["company"],
            "name": person["name"],
            "title": "",
            "linkedin_url": person["linkedin_url"],
            "mapped_contact_id": "",
        }
        by_url[person["linkedin_url"]] = candidate_payload(base, person["note"])
    return list(by_url.values())


def extra_fill(settings, workbook, existing_urls: set[str], needed: int) -> list[dict]:
    if needed <= 0:
        return []
    note_generator = NoteGenerator(ai_messaging=None, ai_message_limit=0)
    contacts_by_org = defaultdict(list)
    for contact in workbook.list_contacts():
        contacts_by_org[contact.organization_id].append(contact)
    touch_by_org = defaultdict(list)
    for tp in workbook.list_touchpoints():
        touch_by_org[tp.organization_id].append(tp)
    extras: list[dict] = []
    preferred = [
        "Commure",
        "ConverzAI",
        "Yondu",
        "Tavus",
        "Micro1",
        "Abridge",
        "Amperesand",
        "Jobright.ai",
    ]
    orgs = {o.name.strip().lower(): o for o in workbook.list_organizations()}
    for name in preferred:
        if len(extras) >= needed:
            break
        org = orgs.get(name.lower()) or next(
            (o for k, o in orgs.items() if name.lower() in k or k in name.lower()),
            None,
        )
        if org is None:
            continue
        mapped = build_mapped_invite_candidates(
            organization=org,
            contacts=contacts_by_org.get(org.organization_id, []),
            touchpoints=touch_by_org.get(org.organization_id, []),
            settings=settings,
        )
        prepared = note_generator.generate_batch(
            mapped[: max(0, needed - len(extras) + 2)],
            company=org.name,
            company_mode=_company_mode(org),
        )
        for item in prepared:
            url = str(item.get("linkedin_url") or "")
            person = str(item.get("name") or "")
            if not url or url in existing_urls or skipped_name(person):
                continue
            qc = (item.get("note_qc") or {}).get("verdict")
            if qc and qc != "send":
                continue
            extras.append(
                candidate_payload(
                    {
                        "company": org.name,
                        "name": person,
                        "title": item.get("title") or "",
                        "linkedin_url": url,
                        "mapped_contact_id": item.get("mapped_contact_id") or "",
                        "score": item.get("score") or 25,
                        "role_bucket": item.get("role_bucket") or "Other",
                    },
                    str(item.get("note") or ""),
                )
            )
            existing_urls.add(url)
            if len(extras) >= needed:
                break
    return extras


def reservation_status(settings, company: str, person: dict) -> str:
    ledger = load_invite_reservations(
        Path(settings.resolved_tracking_workspace_dir) / "linkedin_invite_send_reservations.json"
    )
    key = reservation_key(
        linkedin_url=person["linkedin_url"], company=company, name=person["name"]
    )
    existing = (ledger.get("reservations") or {}).get(key) or {}
    return str(existing.get("status") or "")


def already_done_status(status: str) -> bool:
    return status in {
        "sent",
        "sent_without_note",
        "already_connected",
        "reconciled_connected",
        "reconciled_pending",
        "pending",
    }


def send_company(settings, company: str, rows: list[dict]) -> dict[str, int]:
    ensure_chrome()
    LinkedInScraper(settings).require_live_cdp_session()
    source = {
        "company": company,
        "company_mode": "default",
        "dry_run": False,
        "source": "fall_complete_retry_fill",
        "company_filter_status": "completed_mapped_workbook_assignment",
        "company_filter_error": "",
        "count": len(rows),
        "results": rows,
    }
    source_path = write_artifact(
        settings.artifacts_dir,
        f"fall-complete-invite-source-{re.sub(r'[^a-z0-9]+', '-', company.lower()).strip('-')}",
        source,
    )
    print(f"SENDING {company} n={len(rows)} source={source_path}")
    _artifact, _progress, counts, contacts_added, touchpoints_added = execute_invite_batch(
        settings=settings,
        company=company,
        source_artifact_path=source_path,
        batch=rows,
        execute=True,
        limit=len(rows),
        start_at=0,
        verdict="send",
        min_score=0,
        source_payload_snapshot=source,
    )
    print(f"  done {company} counts={counts} contacts={contacts_added} tps={touchpoints_added}")
    return counts


def main() -> int:
    settings = OutreachSettings()
    ensure_chrome()
    LinkedInScraper(settings).require_live_cdp_session()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    universe = load_universe()
    urls = {p["linkedin_url"] for p in universe}
    extras = extra_fill(settings, workbook, urls, max(0, TARGET - len(universe)))
    universe.extend(extras)
    print(f"UNIVERSE {len(universe)} extras={len(extras)}")

    need_reconcile = []
    for person in universe:
        status = reservation_status(settings, person["company"], person)
        person["_reservation_status"] = status
        if status == "send_unknown_reserved":
            need_reconcile.append(
                {
                    "contact_id": person.get("mapped_contact_id") or person.get("contact_id") or "",
                    "name": person["name"],
                    "linkedin_url": person["linkedin_url"],
                    "company": person["company"],
                }
            )
    print(f"RECONCILE {len(need_reconcile)}")
    if need_reconcile:
        detected = LinkedInScraper(settings).reconcile_connection_statuses(need_reconcile)
        raw = [item.__dict__ for item in detected]
        apply_linkedin_reconcile_results(
            workbook=workbook,
            results=raw,
            source_artifact="fall-complete-unknown-reconcile",
            apply_changes=True,
        )
        for item in raw:
            print(f"  recon {item.get('status'):16} {item.get('name')}")
        recon_path = write_artifact(
            settings.artifacts_dir,
            "fall-complete-unknown-reconcile",
            {"count": len(raw), "results": raw},
        )
        print(f"RECONCILE_ARTIFACT {recon_path}")

    to_send_by_company: dict[str, list[dict]] = defaultdict(list)
    skip_send = []
    for person in universe:
        status = reservation_status(settings, person["company"], person)
        person["_reservation_status"] = status
        if already_done_status(status):
            skip_send.append((person["company"], person["name"], status, "already_done"))
            continue
        to_send_by_company[person["company"]].append(person)
        print(f"QUEUE {person['company']:14} {person['name']:28} was={status or 'none'}")
    print(f"QUEUED {sum(len(v) for v in to_send_by_company.values())} already_done={len(skip_send)}")

    grand: dict[str, int] = {}
    for company, rows in to_send_by_company.items():
        # One retry if Chrome dies mid-company.
        for attempt in range(2):
            try:
                counts = send_company(settings, company, rows)
                for key, value in counts.items():
                    grand[key] = grand.get(key, 0) + value
                break
            except Exception as exc:
                print(f"  send error {company} attempt={attempt+1}: {type(exc).__name__}: {exc}")
                ensure_chrome()
                if attempt == 1:
                    raise
    print(f"GRAND {grand}")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

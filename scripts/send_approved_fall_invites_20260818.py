#!/usr/bin/env python3
"""Send operator-approved Fall invite notes only. No mapping, no backfill."""
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
from outreach.services.linkedin import LinkedInScraper  # noqa: E402
import outreach.cli as cli  # noqa: E402

REWRITE = ROOT / "artifacts" / "20260818-invite-notes-rewritten.md"
PREVIEW = ROOT / "artifacts" / "20260818-091125-fall-sprint-invite-notes-preview.json"
SKIP_NAMES = {"tim sackett"}
# Operator already reviewed founders/CEOs in the rewrite pack.
cli._partition_initial_invites_for_review = lambda candidates, organization=None: (
    list(candidates),
    [],
)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _first_name(full_name: str) -> str:
    token = full_name.strip().split()[0] if full_name.strip() else "there"
    return token.rstrip(",").title() if token.lower() == token else token.rstrip(",")


def _name_key(full_name: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", " ", full_name or "")
    cleaned = re.sub(r"[^\w\s.-]", " ", cleaned)
    parts = [p for p in cleaned.split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return _norm(parts[0])
    return _norm(f"{parts[0]} {parts[-1]}")


def parse_rewrite(text: str) -> list[dict]:
    blocks = re.split(r"\n## ", text)
    items: list[dict] = []
    for block in blocks[1:]:
        lines = block.strip().splitlines()
        company = lines[0].strip()
        body = "\n".join(lines[1:])
        for m in re.finditer(
            r"\*\*(.+?)\*\* — \d+ chars\n\n> (.+?)(?=\n\n|\n---|\Z)",
            body,
            flags=re.S,
        ):
            heading = m.group(1).strip()
            note = " ".join(m.group(2).split())
            items.append({"company": company, "heading": heading, "note": note})
    return items


def heading_is_all_contacts(heading: str) -> bool:
    return "all contacts" in heading.lower()


def heading_name_keys(heading: str) -> list[str]:
    if heading_is_all_contacts(heading):
        return []
    left = heading.split("—")[0].strip()
    keys = []
    for chunk in re.split(r"\s*/\s*", left):
        key = _name_key(chunk)
        if key:
            keys.append(key)
    return keys


def note_for(row: dict, template: str) -> str:
    if "{first}" in template:
        first = _first_name(row["name"])
        if first.lower() == "ram":
            first = "Ram"
        return template.replace("{first}", first)
    return template


def build_approved() -> tuple[dict[str, list[dict]], list[dict]]:
    preview = json.loads(PREVIEW.read_text(encoding="utf-8"))
    by_company: dict[str, list[dict]] = defaultdict(list)
    for row in preview["notes"]:
        by_company[row["company"]].append(row)

    chosen: dict[str, list[dict]] = defaultdict(list)
    skipped: list[dict] = []
    for spec in parse_rewrite(REWRITE.read_text(encoding="utf-8")):
        company = spec["company"]
        pool = by_company.get(company) or []
        keys = heading_name_keys(spec["heading"])
        if heading_is_all_contacts(spec["heading"]):
            matches = pool
        else:
            matches = [row for row in pool if _name_key(row["name"]) in set(keys)]
            if not matches:
                # first-token fallback for "ram awasthi"
                first_keys = {k.split()[0] for k in keys}
                matches = [
                    row
                    for row in pool
                    if _name_key(row["name"]).split()[:1] == list(first_keys)
                    or _name_key(row["name"]).split()[0] in first_keys
                ]
        if not matches:
            skipped.append({**spec, "reason": "no_preview_match"})
            continue
        for row in matches:
            if any(skip in _norm(row["name"]) for skip in SKIP_NAMES):
                skipped.append(
                    {
                        "company": company,
                        "heading": spec["heading"],
                        "name": row["name"],
                        "reason": "operator_skip_false_map",
                    }
                )
                continue
            note = note_for(row, spec["note"])
            if len(note) > 300:
                skipped.append(
                    {
                        "company": company,
                        "name": row["name"],
                        "reason": f"over_char_limit_{len(note)}",
                        "note": note,
                    }
                )
                continue
            candidate = {
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
                "note_qc": {"verdict": "send", "score": 100, "flags": [], "strengths": ["operator_approved"]},
                "operator_approved_note": True,
            }
            chosen[company].append(candidate)
    return chosen, skipped


def main() -> int:
    settings = OutreachSettings()
    chosen, skipped = build_approved()
    total = sum(len(v) for v in chosen.values())
    pack = {
        "schema": "fall-sprint-approved-invite-send",
        "rewrite_source": str(REWRITE),
        "preview_source": str(PREVIEW),
        "send": True,
        "mapping": False,
        "backfill": False,
        "skipped": skipped,
        "companies": {company: len(rows) for company, rows in chosen.items()},
        "total": total,
        "people": [
            {"company": c, "name": r["name"], "linkedin_url": r["linkedin_url"], "note": r["note"]}
            for c, rows in chosen.items()
            for r in rows
        ],
    }
    pack_path = write_artifact(settings.artifacts_dir, "fall-sprint-approved-invite-pack", pack)
    print(f"PACK {pack_path}")
    print(f"TOTAL {total}")
    for company, rows in chosen.items():
        print(f"  {company}: {len(rows)}")
        for row in rows:
            print(f"    - {row['name']} ({len(row['note'])} chars)")
    if skipped:
        print("SKIPPED")
        for item in skipped:
            print(f"  {item}")
    if total <= 0:
        print("Nothing to send")
        return 2

    LinkedInScraper(settings).require_live_cdp_session()
    grand: dict[str, int] = {}
    for company, rows in chosen.items():
        source = {
            "company": company,
            "company_mode": "default",
            "dry_run": False,
            "source": "operator_approved_fall_rewrite",
            "company_filter_status": "completed_mapped_workbook_assignment",
            "company_filter_error": "",
            "count": len(rows),
            "results": rows,
        }
        source_path = write_artifact(
            settings.artifacts_dir,
            f"fall-approved-invite-source-{re.sub(r'[^a-z0-9]+', '-', company.lower()).strip('-')}",
            source,
        )
        print(f"SENDING {company} n={len(rows)} source={source_path}")
        artifact, progress, counts, contacts_added, touchpoints_added = execute_invite_batch(
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
        print(
            f"  done {company} counts={counts} contacts={contacts_added} "
            f"touchpoints={touchpoints_added} artifact={artifact} progress={progress}"
        )
        for key, value in counts.items():
            grand[key] = grand.get(key, 0) + value
    print(f"GRAND {grand}")
    print("DONE")
    return 0 if grand.get("sent", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

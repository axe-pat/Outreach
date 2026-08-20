#!/usr/bin/env python3
"""Audit original invite notes on a backlog or across the full sent ledger.

Outputs:
1. repeated invite templates
2. focus-area claims that the organization description does not support
3. contacts whose follow-up would inherit a false premise
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from outreach.cli import extract_description_from_notes, parse_notes_metadata  # noqa: E402
from outreach.tracking import OutreachWorkbook  # noqa: E402

FOCUS_CLAIM = re.compile(
    r"especially (?:its|their) work in ([^.,;!?\n]+)",
    re.I,
)

# Synonyms / near-neighbors that count as supported when they appear in the
# org description or tags.  Keep this conservative: the point is false
# premises, not soft thematic stretch.
FOCUS_ALIASES: dict[str, set[str]] = {
    "recruiting workflows": {
        "recruit", "hiring", "interview", "talent", "ats", "candidate", "hr",
        "staffing", "workforce",
    },
    "robotics": {"robot", "robotics", "autonom", "hardware", "embodied"},
    "marketplace operations": {
        "marketplace", "two-sided", "gig", "labor marketplace", "platform economy",
    },
    "developer security": {
        "security", "devtools", "developer", "appsec", "vulnerability", "secure",
        "code security", "snyk",
    },
    "healthcare workflows": {
        "health", "clinical", "medical", "patient", "care", "hospital", "pharma",
    },
    "observability and reliability": {
        "observab", "monitor", "reliab", "telemetry", "incident", "sre", "uptime",
    },
    "subscription billing": {
        "billing", "subscription", "payments", "invoice", "revenue", "fintech",
    },
}


def fingerprint(note: str) -> str:
    text = note or ""
    text = re.sub(r"\bHi [A-Z][\w'’\-]+,? ?", "Hi NAME, ", text)
    text = re.sub(r"\bHey [A-Z][\w'’\-]+,? ?", "Hey NAME, ", text)
    text = re.sub(
        r"interested in [^.,]{1,80}, especially",
        "interested in COMPANY, especially",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(?:PM(?:/product)?|product|BizOps/Strategy) roles? at [^.,]{1,80}",
        "ROLE at COMPANY",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"exploring [^.,]{1,100} at [^.,]{1,80}",
        "exploring ROLE at COMPANY",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text[:200]


def org_corpus(row: dict) -> str:
    notes = row.get("organization_notes") or ""
    metadata = parse_notes_metadata(notes)
    description = extract_description_from_notes(notes)
    tags = metadata.get("tags", "")
    story = metadata.get("story_fit_reason", "")
    return " ".join([description, tags, story, notes]).casefold()


def claim_supported(claim: str, corpus: str) -> tuple[bool, str]:
    key = claim.casefold().strip()
    aliases = FOCUS_ALIASES.get(key)
    if aliases is None:
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]{4,}", key)
            if token not in {"work", "with", "from"}
        ]
        hits = [token for token in tokens if token in corpus]
        return (bool(hits), f"literal_hits={hits}" if hits else "no_literal_support")
    hits = [alias for alias in sorted(aliases) if alias in corpus]
    return (bool(hits), f"alias_hits={hits}" if hits else "no_alias_support")


def all_sent_invite_rows(workspace: Path) -> list[dict[str, object]]:
    workbook = OutreachWorkbook(workspace)
    contacts = {item.contact_id: item for item in workbook.list_contacts()}
    organizations = {
        item.organization_id: item for item in workbook.list_organizations()
    }
    rows: list[dict[str, object]] = []
    for invite in workbook.list_touchpoints():
        if invite.message_kind.strip().casefold() != "linkedin_invite":
            continue
        if invite.status.strip().casefold() not in {"sent", "delivered", "completed"}:
            continue
        note = (invite.message_text or "").strip()
        if not note:
            continue
        contact = contacts.get(invite.contact_id)
        organization_id = (
            contact.organization_id if contact is not None else invite.organization_id
        )
        organization = organizations.get(organization_id)
        rows.append(
            {
                "touchpoint_id": invite.touchpoint_id,
                "contact_id": invite.contact_id,
                "name": contact.full_name if contact is not None else "",
                "company": organization.name if organization is not None else "",
                "title": contact.title if contact is not None else "",
                "status": contact.status if contact is not None else "",
                "band": "",
                "followups_sent": None,
                "original_invite_note": note,
                "invite_sent_at": invite.sent_at or invite.recorded_at,
                "organization_notes": organization.notes if organization is not None else "",
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--backlog")
    source.add_argument(
        "--all-invites",
        action="store_true",
        help="audit every confirmed-Sent invite in the workbook ledger",
    )
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--out-prefix", default="")
    args = parser.parse_args()

    if args.all_invites:
        workspace = Path(args.workspace)
        if not workspace.is_absolute():
            workspace = REPO / workspace
        rows = all_sent_invite_rows(workspace)
        source_label = str(workspace / "touchpoints.csv")
        scope_label = "All historical sent"
        default_stem = "all-sent-invite-audit"
    else:
        backlog = json.loads(Path(args.backlog).read_text(encoding="utf-8"))
        rows = backlog.get("results") or []
        source_label = args.backlog
        scope_label = "Accepted-silent"
        default_stem = "accepted-silent-invite-audit"

    templates: Counter[str] = Counter()
    template_examples: dict[str, str] = {}
    focus_counts: Counter[str] = Counter()
    unsupported: list[dict[str, object]] = []
    supported_focus: list[dict[str, object]] = []
    missing_notes = 0

    for row in rows:
        note = (row.get("original_invite_note") or "").strip()
        if not note:
            missing_notes += 1
            continue
        fp = fingerprint(note)
        templates[fp] += 1
        template_examples.setdefault(fp, note)

        match = FOCUS_CLAIM.search(note)
        if not match:
            continue
        claim = match.group(1).strip()
        focus_counts[claim.casefold()] += 1
        corpus = org_corpus(row)
        ok, evidence = claim_supported(claim, corpus)
        payload = {
            "touchpoint_id": row.get("touchpoint_id"),
            "contact_id": row.get("contact_id"),
            "name": row.get("name"),
            "company": row.get("company"),
            "title": row.get("title"),
            "band": row.get("band"),
            "followups_sent": row.get("followups_sent"),
            "claimed_focus": claim,
            "invite_note": note,
            "org_description": extract_description_from_notes(
                row.get("organization_notes") or ""
            ),
            "org_tags": parse_notes_metadata(row.get("organization_notes") or "").get(
                "tags", ""
            ),
            "evidence": evidence,
        }
        if ok:
            supported_focus.append(payload)
        else:
            unsupported.append(payload)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    prefix = args.out_prefix or f"artifacts/{stamp}-{default_stem}"
    prefix_path = Path(prefix)
    if not prefix_path.is_absolute():
        prefix_path = REPO / prefix_path
    prefix_path.parent.mkdir(parents=True, exist_ok=True)

    by_claim: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in unsupported:
        by_claim[str(item["claimed_focus"]).casefold()].append(item)

    unsupported_people = {
        str(item.get("contact_id") or item.get("touchpoint_id") or "")
        for item in unsupported
    }
    supported_people = {
        str(item.get("contact_id") or item.get("touchpoint_id") or "")
        for item in supported_focus
    }
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "source": source_label,
        "scope": scope_label,
        "total_rows": len(rows),
        "unique_people": len(
            {
                str(row.get("contact_id") or row.get("touchpoint_id") or "")
                for row in rows
            }
        ),
        "missing_invite_notes": missing_notes,
        "focus_claim_count": sum(focus_counts.values()),
        "unsupported_focus_count": len(unsupported),
        "unsupported_people_count": len(unsupported_people),
        "supported_focus_count": len(supported_focus),
        "supported_people_count": len(supported_people),
        "focus_frequency": [
            {"claim": claim, "count": count}
            for claim, count in focus_counts.most_common()
        ],
        "template_frequency": [
            {
                "count": count,
                "fingerprint": fp,
                "example": template_examples[fp],
            }
            for fp, count in templates.most_common(40)
        ],
        "unsupported_focus": unsupported,
        "supported_focus": supported_focus,
    }
    Path(f"{prefix_path}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        f"# {scope_label} invite-note audit",
        "",
        f"Rows: **{len(rows)}** · missing notes: **{missing_notes}**",
        f"Focus claims (`especially its/their work in …`): **{sum(focus_counts.values())}**",
        (
            "Unsupported against org description/tags: "
            f"**{len(unsupported_people)} people** ({len(unsupported)} invite rows)"
        ),
        "",
        "## Focus-claim frequency",
        "",
    ]
    for claim, count in focus_counts.most_common():
        lines.append(f"- **{count}** — {claim}")
    lines += ["", "## Unsupported focus claims (priority)", ""]
    for claim, items in sorted(by_claim.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"### {claim} ({len(items)})")
        lines.append("")
        for item in items:
            lines.append(
                f"- **{item['name']} — {item['company']}** | band={item['band']} | "
                f"followups={item['followups_sent']}"
            )
            lines.append(f"  - Invite: {item['invite_note']}")
            lines.append(
                f"  - Org description: {item['org_description'] or '(none)'}"
            )
            if item["org_tags"]:
                lines.append(f"  - Tags: {item['org_tags']}")
        lines.append("")
    lines += ["## Top invite templates", ""]
    for item in payload["template_frequency"][:20]:
        lines.append(f"### {item['count']}×")
        lines.append("")
        lines.append("```")
        lines.append(item["example"])
        lines.append("```")
        lines.append("")
    Path(f"{prefix_path}.md").write_text("\n".join(lines), encoding="utf-8")

    print(
        json.dumps(
            {
                "total_rows": len(rows),
                "missing_invite_notes": missing_notes,
                "focus_claim_count": sum(focus_counts.values()),
                "unsupported_focus_count": len(unsupported),
                "unsupported_people_count": len(unsupported_people),
                "top_unsupported": [
                    {"claim": claim, "count": len(items)}
                    for claim, items in sorted(
                        by_claim.items(), key=lambda kv: (-len(kv[1]), kv[0])
                    )[:10]
                ],
            },
            indent=2,
        )
    )
    print(f"wrote {prefix_path}.md and {prefix_path}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

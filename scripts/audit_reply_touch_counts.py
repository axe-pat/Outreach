#!/usr/bin/env python3
"""Report tracker-backed follow-up counts before the accepted-silent lane runs."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from outreach.reply_engine.touches import (  # noqa: E402
    DEFAULT_TOUCH_CAP,
    is_outbound_followup_touch,
    outbound_followup_touch_counts,
)
from outreach.tracking import OutreachWorkbook  # noqa: E402


INBOUND_REPLY_KINDS = frozenset(
    {"linkedin_reply", "inbound_reply", "reply"}
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    workbook = OutreachWorkbook(REPO / args.workspace)
    contacts = workbook.list_contacts()
    organizations = {
        organization.organization_id: organization.name
        for organization in workbook.list_organizations()
    }
    touchpoints = workbook.list_touchpoints()
    counts = outbound_followup_touch_counts(touchpoints)
    replied_contact_ids = {
        touchpoint.contact_id
        for touchpoint in touchpoints
        if touchpoint.contact_id
        and touchpoint.message_kind.strip().casefold() in INBOUND_REPLY_KINDS
    }
    accepted_silent = [
        contact
        for contact in contacts
        if contact.status.strip().casefold() == "connected"
        and contact.contact_id not in replied_contact_ids
    ]

    all_distribution = Counter(counts.get(contact.contact_id, 0) for contact in contacts)
    silent_distribution = Counter(
        counts.get(contact.contact_id, 0) for contact in accepted_silent
    )
    accepted_silent_ids = {contact.contact_id for contact in accepted_silent}
    silent_touch_kind_distribution = Counter(
        touchpoint.message_kind
        for touchpoint in touchpoints
        if touchpoint.contact_id in accepted_silent_ids
        and is_outbound_followup_touch(touchpoint)
    )
    cap_rows = sorted(
        [
            contact
            for contact in accepted_silent
            if counts.get(contact.contact_id, 0) >= DEFAULT_TOUCH_CAP
        ],
        key=lambda contact: (
            -counts.get(contact.contact_id, 0),
            contact.full_name.casefold(),
        ),
    )

    lines = [
        "# Reply-engine touch-count audit",
        "",
        "Tracker read only: no contact, touchpoint, or draft was changed.",
        "",
        f"All contacts: **{len(contacts)}**",
        f"Accepted-silent contacts: **{len(accepted_silent)}**",
        f"Accepted-silent at or above the {DEFAULT_TOUCH_CAP}-touch cap: "
        f"**{len(cap_rows)}**",
        "",
        "## Accepted-silent distribution",
        "",
    ]
    lines.extend(
        f"- {touch_count} prior follow-up(s): **{count}**"
        for touch_count, count in sorted(silent_distribution.items())
    )
    lines.extend(["", "### Counted touchpoint kinds", ""])
    lines.extend(
        f"- `{message_kind}`: **{count}**"
        for message_kind, count in silent_touch_kind_distribution.most_common()
    )
    lines.extend(["", "## All-contact distribution", ""])
    lines.extend(
        f"- {touch_count} prior follow-up(s): **{count}**"
        for touch_count, count in sorted(all_distribution.items())
    )
    lines.extend(["", "## Accepted-silent contacts at the cap", ""])
    if not cap_rows:
        lines.append("None.")
    for contact in cap_rows:
        lines.extend(
            [
                f"### {contact.full_name} — "
                f"{organizations.get(contact.organization_id, contact.organization_id)}",
                f"- Contact: `{contact.contact_id}`",
                f"- Prior outbound follow-ups: **{counts[contact.contact_id]}**",
                f"- LinkedIn: {contact.linkedin_url or '(missing)' }",
                "",
            ]
        )

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = (
        Path(args.out)
        if args.out
        else REPO / "artifacts" / f"{stamp}-reply-touch-count-audit.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"accepted_silent={len(accepted_silent)}")
    print(f"at_cap={len(cap_rows)}")
    print(
        "distribution="
        + ",".join(
            f"{touch_count}:{count}"
            for touch_count, count in sorted(silent_distribution.items())
        )
    )
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

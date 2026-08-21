#!/usr/bin/env python3
"""Build the connected-but-silent LinkedIn lane from the workbook ledger.

This script is deliberately standalone.  It does not change the workbook and
does not modify or wrap the reply engine.  Its JSON output is accepted directly
by ``scripts/run_reply_engine.py --backlog``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from outreach.cadence import (  # noqa: E402
    LINKEDIN_FOLLOWUP_KINDS,
    REPLY_KINDS,
    SENT_STATUSES,
)
from outreach.reply_engine.reopen import evaluate_reopen_conditions  # noqa: E402
from outreach.reply_engine.touches import inbound_probably_missing  # noqa: E402
from outreach.tracking import (  # noqa: E402
    OpportunityRecord,
    OrganizationRecord,
    OutreachWorkbook,
    TouchpointRecord,
)

CONNECTED_STATUSES = {"connected", "accepted"}
WARM_STATUS = "warm"
DO_NOT_CONTACT_STATUSES = {"do not contact", "do_not_contact", "closed_hard"}
REPLY_STATUSES = {"replied", "responded"}
INVITE_KINDS = {"linkedin_invite", "invite", "connection_invite"}


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def parse_timestamp(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_sent(touchpoint: TouchpointRecord) -> bool:
    return normalized(touchpoint.status) in {normalized(value) for value in SENT_STATUSES}


def has_logged_reply(touchpoints: Iterable[TouchpointRecord]) -> bool:
    for touchpoint in touchpoints:
        kind = normalized(touchpoint.message_kind).replace(" ", "_")
        status = normalized(touchpoint.status)
        if kind in REPLY_KINDS or status in REPLY_STATUSES:
            return True
    return False


def followups_sent(touchpoints: Iterable[TouchpointRecord]) -> int:
    """Doc-aligned count: automated follow-ups only, not hand-sends."""

    return sum(
        1
        for touchpoint in touchpoints
        if is_sent(touchpoint)
        and normalized(touchpoint.message_kind).replace(" ", "_") == "linkedin_followup"
    )


def manual_outbounds(touchpoints: Iterable[TouchpointRecord]) -> list[TouchpointRecord]:
    return [
        touchpoint
        for touchpoint in touchpoints
        if is_sent(touchpoint)
        and normalized(touchpoint.message_kind).replace(" ", "_")
        == "linkedin_manual_message"
        and (touchpoint.message_text or "").strip()
    ]


def sent_followup_touchpoints(
    touchpoints: Iterable[TouchpointRecord],
) -> list[TouchpointRecord]:
    return [
        touchpoint
        for touchpoint in touchpoints
        if is_sent(touchpoint)
        and normalized(touchpoint.message_kind).replace(" ", "_")
        in LINKEDIN_FOLLOWUP_KINDS
        and (touchpoint.message_text or "").strip()
    ]


def build_message_window(
    *,
    invite_note: str,
    outbound: list[TouchpointRecord],
) -> list[dict[str, str]]:
    """Invite plus any prior outbound so the engine sees YOU_REPLIED_LAST.

    Without the outbound rows, already-touched silent accepts stay NO_CONTEXT
    and rule 11 re-drafts them.  Including them is selection hygiene, not a
    touch-cap implementation.
    """

    window: list[dict[str, str]] = []
    if invite_note:
        window.append(
            {
                "sender": "You",
                "message": invite_note,
                "timestamp_text": "",
                "source": "original_invite",
            }
        )
    ordered = sorted(
        outbound,
        key=lambda item: parse_timestamp(item.sent_at or item.recorded_at)
        or datetime.max.replace(tzinfo=UTC),
    )
    for item in ordered:
        window.append(
            {
                "sender": "You",
                "message": (item.message_text or "").strip(),
                "timestamp_text": item.sent_at or item.recorded_at or "",
                "source": normalized(item.message_kind).replace(" ", "_"),
            }
        )
    return window


def original_invite(touchpoints: Iterable[TouchpointRecord]) -> TouchpointRecord | None:
    candidates = [
        touchpoint
        for touchpoint in touchpoints
        if normalized(touchpoint.message_kind).replace(" ", "_") in INVITE_KINDS
        and (touchpoint.message_text or "").strip()
    ]
    candidates.sort(
        key=lambda item: (
            0 if is_sent(item) else 1,
            parse_timestamp(item.sent_at or item.recorded_at)
            or datetime.max.replace(tzinfo=UTC),
        )
    )
    return candidates[0] if candidates else None


def has_sent_invite(touchpoints: Iterable[TouchpointRecord]) -> bool:
    return any(
        normalized(touchpoint.message_kind).replace(" ", "_") in INVITE_KINDS
        and is_sent(touchpoint)
        for touchpoint in touchpoints
    )


def has_prior_outbound_linkedin(
    touchpoints: Iterable[TouchpointRecord],
) -> bool:
    """A follow-up requires evidence that Akshat already messaged them."""

    values = list(touchpoints)
    return (
        has_sent_invite(values)
        or bool(sent_followup_touchpoints(values))
        or bool(manual_outbounds(values))
    )


def _first_outreach_review_row(
    contact,
    organization: OrganizationRecord | None,
    *,
    reason: str = "no prior outbound LinkedIn message",
) -> dict[str, str]:
    notes = (contact.notes or "").casefold()
    company = organization.name if organization else ""
    if reason == "live LinkedIn thread not verified":
        disposition = (
            "identity hold; exclude from follow-ups until a live thread is verified"
        )
    elif "suppress follow up permanently" in notes:
        disposition = "permanently suppressed; retain for relationship context only"
    elif normalized(company) == "fivetran":
        disposition = "Akshat is handling this company manually"
    else:
        disposition = "manual relationship review before any first message"
    return {
        "contact_id": contact.contact_id,
        "organization_id": contact.organization_id,
        "name": contact.full_name,
        "company": company,
        "title": contact.title,
        "status": contact.status,
        "linkedin_url": contact.linkedin_url,
        "target_lists": contact.target_lists,
        "disposition": disposition,
        "reason": reason,
    }


def render_first_outreach_review(rows: list[dict[str, str]]) -> str:
    """Render first-message contacts without generating any copy."""

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("company") or "(unbound)"].append(row)

    lines = [
        "# Warm / never-invited contacts — manual first-message review",
        "",
        "These contacts have no prior outbound LinkedIn message. They are not follow-ups, "
        "no copy was generated, and none is included in a follow-up send count.",
        "",
        f"**Total moved out of the follow-up engine: {len(rows)}**",
        "",
    ]
    for company in sorted(grouped, key=str.casefold):
        contacts = sorted(grouped[company], key=lambda item: item["name"].casefold())
        lines.extend([f"## Filed under {company} ({len(contacts)})", ""])
        for row in contacts:
            lines.append(f"### {row['name']}")
            lines.append(f"- **Visible title:** {row.get('title') or 'missing title'}")
            lines.append(f"- **Disposition:** {row['disposition']}")
            if row.get("target_lists"):
                lines.append(f"- **Target lists:** {row['target_lists']}")
            if row.get("linkedin_url"):
                lines.append(f"- **LinkedIn:** {row['linkedin_url']}")
            lines.append("")
    return "\n".join(lines)


def write_first_outreach_review(
    rows: list[dict[str, str]],
    output: Path,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_first_outreach_review(rows), encoding="utf-8")


def fall_bands(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            normalized(row.get("company", "")): row
            for row in csv.DictReader(handle)
            if normalized(row.get("company", ""))
        }


def band_for(
    organization: OrganizationRecord | None,
    bands: dict[str, dict[str, str]],
) -> tuple[str, float]:
    if organization is None:
        return "", 0.0
    match = bands.get(normalized(organization.name), {})
    band = str(match.get("band") or "").strip()
    try:
        score = float(match.get("fall_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    if not band and "fall_sprint" in normalized(organization.target_lists):
        band = "fall_sprint"
    return band, score


def opportunity_payload(opportunity: OpportunityRecord) -> dict[str, object]:
    return opportunity.model_dump(mode="json")


def state_invite_notes(workspace: Path) -> dict[str, list[tuple[str, str]]]:
    """Recover original invites captured from LinkedIn but missing in touchpoints."""

    path = workspace / "linkedin_message_state.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    notes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for state in (payload.get("thread_states") or {}).values():
        if not isinstance(state, dict):
            continue
        name = normalized(str(state.get("name") or ""))
        if not name:
            continue
        for message in state.get("message_window") or []:
            if not isinstance(message, dict):
                continue
            if str(message.get("source") or "") != "original_invite":
                continue
            text = str(message.get("message") or "").strip()
            if text:
                notes[name].append((text, str(state.get("first_seen_at") or "")))
                break
    return notes


def build_backlog(
    *,
    workspace: Path,
    pursuit_season: str,
) -> dict[str, object]:
    workbook = OutreachWorkbook(workspace)
    contacts = workbook.list_contacts()
    organizations = workbook.list_organizations()
    opportunities = workbook.list_opportunities()
    touchpoints = workbook.list_touchpoints()

    org_by_id = {item.organization_id: item for item in organizations}
    opportunities_by_org: dict[str, list[OpportunityRecord]] = defaultdict(list)
    for opportunity in opportunities:
        opportunities_by_org[opportunity.organization_id].append(opportunity)
    touchpoints_by_contact: dict[str, list[TouchpointRecord]] = defaultdict(list)
    for touchpoint in touchpoints:
        if touchpoint.contact_id:
            touchpoints_by_contact[touchpoint.contact_id].append(touchpoint)

    reopen = {
        item.contact_id: item
        for item in evaluate_reopen_conditions(
            contacts=contacts,
            organizations=organizations,
            opportunities=opportunities,
            pursuit_season=pursuit_season,
        )
    }
    bands = fall_bands(workspace / "fall_sprint_targets.csv")
    state_notes = state_invite_notes(workspace)

    exclusions: Counter[str] = Counter()
    rows: list[dict[str, object]] = []
    targeted_repull: list[dict[str, object]] = []
    first_outreach_review: list[dict[str, str]] = []
    for contact in contacts:
        status = normalized(contact.status)
        notes = normalized(contact.notes)
        contact_touchpoints = touchpoints_by_contact.get(contact.contact_id, [])
        organization = org_by_id.get(contact.organization_id)

        if status not in CONNECTED_STATUSES and status != WARM_STATUS:
            exclusions["not_connected_status"] += 1
            continue
        if status in DO_NOT_CONTACT_STATUSES or "do not contact" in notes:
            exclusions["do_not_contact"] += 1
            continue
        if "parked" in notes or "suppress follow up" in notes:
            exclusions["parked_or_suppress_notes"] += 1
            continue
        if "followup live thread unverified" in notes:
            first_outreach_review.append(
                _first_outreach_review_row(
                    contact,
                    organization,
                    reason="live LinkedIn thread not verified",
                )
            )
            exclusions["live_thread_unverified"] += 1
            continue

        # First-message contacts are not follow-ups, regardless of a stale
        # Connected status.  Only a genuinely sent invite/follow-up/manual
        # message is outbound evidence; Prepared, Unavailable, Unknown
        # reserved, and synthetic acceptance rows do not qualify.
        if not has_prior_outbound_linkedin(contact_touchpoints):
            first_outreach_review.append(
                _first_outreach_review_row(contact, organization)
            )
            exclusions["no_prior_outbound_linkedin"] += 1
            continue
        missing_inbound_evidence = inbound_probably_missing(contact_touchpoints)
        if missing_inbound_evidence is not None:
            exclusions["inbound_probably_missing"] += 1
            targeted_repull.append(
                {
                    "contact_id": contact.contact_id,
                    "organization_id": contact.organization_id,
                    "name": contact.full_name,
                    "linkedin_url": contact.linkedin_url,
                    "responsive_outbound": missing_inbound_evidence.message_text,
                    "outbound_at": (
                        missing_inbound_evidence.sent_at
                        or missing_inbound_evidence.recorded_at
                    ),
                    "reason": "inbound_probably_missing",
                    "requested_capture": "deep_thread_repull",
                }
            )
            continue
        if has_logged_reply(contact_touchpoints):
            exclusions["logged_reply"] += 1
            continue

        reopen_assessment = reopen.get(contact.contact_id)
        if contact.reopen_condition.strip():
            if reopen_assessment is None or reopen_assessment.status != "reopen_candidate":
                exclusions["parked_unmet_reopen"] += 1
                continue

        # The gate above proves that some LinkedIn outbound was really sent.
        # Prefer the sent invite as the opening context, while retaining older
        # recovery behavior for contacts whose verified outbound was a later
        # follow-up or manual message.
        invite = original_invite(contact_touchpoints)
        invite_note = (invite.message_text or "").strip() if invite else ""
        invite_at = (invite.sent_at or invite.recorded_at) if invite else ""
        invite_evidence_status = invite.status if invite else ""
        if not invite_note:
            captured = state_notes.get(normalized(contact.full_name), [])
            unique_notes = {note for note, _ in captured}
            if len(unique_notes) == 1:
                invite_note, invite_at = captured[0]
                invite_evidence_status = "linkedin_state_recovered"
                exclusions["invite_note_recovered_from_state"] += 1
            else:
                exclusions["missing_invite_note"] += 1
                invite_evidence_status = "missing"

        band, fall_score = band_for(organization, bands)
        followup_count = followups_sent(contact_touchpoints)
        manuals = manual_outbounds(contact_touchpoints)
        outbound = [*sent_followup_touchpoints(contact_touchpoints), *manuals]
        window = build_message_window(invite_note=invite_note, outbound=outbound)
        if not window:
            first_outreach_review.append(
                _first_outreach_review_row(contact, organization)
            )
            exclusions["no_prior_outbound_linkedin"] += 1
            continue
        org_opportunities = sorted(
            opportunities_by_org.get(contact.organization_id, []),
            key=lambda item: (item.discovered_at or "", item.title),
            reverse=True,
        )

        rows.append(
            {
                "contact_id": contact.contact_id,
                "segment": "accepted_silent",
                "relationship_context": "accepted_silent",
                "organization_id": contact.organization_id,
                "name": contact.full_name,
                "title": contact.title,
                "contact_type": contact.contact_type,
                "linkedin_url": contact.linkedin_url,
                "status": contact.status,
                "company": organization.name if organization else "",
                "organization_type": (
                    organization.organization_type.value if organization else ""
                ),
                "organization_notes": organization.notes if organization else "",
                "band": band,
                "fall_score": fall_score,
                "opportunities": [
                    opportunity_payload(item) for item in org_opportunities
                ],
                "original_invite_note": invite_note,
                "invite_date": invite_at,
                "invite_evidence_status": invite_evidence_status,
                "prior_outbound_verified": True,
                "followups_sent": followup_count,
                "manual_outbound_count": len(manuals),
                "reopen_condition": contact.reopen_condition,
                # Prior outbound belongs in the window so already-touched
                # silent accepts resolve to YOU_REPLIED_LAST instead of
                # getting a second cold ask.
                "message_window": window,
                "capture_confidence": "full",
                "captured_message_count": len(window),
                "expected_message_count": len(window),
            }
        )

    rows.sort(
        key=lambda row: (
            int(row["followups_sent"]),
            -float(row["fall_score"]),
            str(row["company"]).casefold(),
            str(row["name"]).casefold(),
        )
    )
    by_followups = Counter(int(row["followups_sent"]) for row in rows)
    by_band = Counter(str(row["band"] or "(none)") for row in rows)
    by_manual = Counter(int(row["manual_outbound_count"]) for row in rows)
    by_segment = Counter(str(row["segment"]) for row in rows)
    first_outreach_review.sort(
        key=lambda row: (row["company"].casefold(), row["name"].casefold())
    )

    return {
        "created_at": datetime.now(UTC).isoformat(),
        "source": "workspace ledger after live LinkedIn reconcile",
        "selection": (
            "connected/accepted/Warm with a genuinely sent prior LinkedIn message; "
            "no logged reply; not do-not-contact; "
            "not parked/suppress notes; not parked with unmet reopen condition"
        ),
        "pursuit_season": pursuit_season,
        "count": len(rows),
        "targeted_repull": sorted(
            targeted_repull,
            key=lambda row: str(row["name"]).casefold(),
        ),
        "first_outreach_review": first_outreach_review,
        "summary": {
            "moved_to_first_outreach_review": len(first_outreach_review),
            "by_followups_sent": dict(sorted(by_followups.items())),
            "by_manual_outbound_count": dict(sorted(by_manual.items())),
            "at_or_over_two_touch_cap": sum(
                int(row["followups_sent"]) >= 2 for row in rows
            ),
            "already_touched_outbound": sum(
                1
                for row in rows
                if int(row["followups_sent"]) > 0 or int(row["manual_outbound_count"]) > 0
            ),
            "by_band": dict(by_band.most_common()),
            "by_segment": dict(sorted(by_segment.items())),
            "exclusions": dict(exclusions),
        },
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--season", default="fall")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    workspace = REPO / args.workspace
    payload = build_backlog(workspace=workspace, pursuit_season=args.season)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = (
        Path(args.output)
        if args.output
        else REPO / "artifacts" / f"{stamp}-accepted-silent-backlog.json"
    )
    if not output.is_absolute():
        output = REPO / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    repull_output = output.with_name(f"{output.stem}-targeted-repull.json")
    repull_payload = {
        "created_at": payload["created_at"],
        "source_backlog": str(output),
        "count": len(payload["targeted_repull"]),
        "results": payload["targeted_repull"],
    }
    repull_output.write_text(json.dumps(repull_payload, indent=2), encoding="utf-8")

    first_outreach_output = output.with_name(
        f"{output.stem}-warm-never-invited-manual-review.md"
    )
    write_first_outreach_review(
        list(payload["first_outreach_review"]),
        first_outreach_output,
    )

    print(json.dumps(payload["summary"], indent=2))
    print(f"count={payload['count']} artifact={output.relative_to(REPO)}")
    print(
        f"targeted_repull={repull_payload['count']} "
        f"artifact={repull_output.relative_to(REPO)}"
    )
    print(
        f"first_outreach_review={len(payload['first_outreach_review'])} "
        f"artifact={first_outreach_output.relative_to(REPO)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

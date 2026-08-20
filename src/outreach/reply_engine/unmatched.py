"""Classify, report, and safely ingest unmatched LinkedIn conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachWorkbook,
    SourceKind,
    stable_suffix,
)
from .org_identity import title_employer


NOISE = "noise"
NEEDS_CONTACT_ROW = "needs_contact_row"

_SPONSORED = re.compile(r"\bsponsored\b|\bpromoted\b|sponsored inmail", re.I)
_INMAIL = re.compile(r"\binmail\b", re.I)
_BROADCAST = re.compile(
    r"product hunt|\bupvote\b|we (?:just )?launched|would love your support|"
    r"subscribe|newsletter|webinar|quick favor|check out our launch|sent a post",
    re.I,
)
_RECRUITER_SPAM = re.compile(
    r"career opportunity\s*[–—:-]|urgent(?:ly)? hiring|immediate opening|"
    r"your profile (?:is|was) shortlisted",
    re.I,
)
_PAID_RESEARCH = re.compile(
    r"paid research|compensated (?:study|interview|research)|research study|"
    r"earn \$?\d+|honorarium",
    re.I,
)
_COMPANY_FROM_MESSAGE = (
    re.compile(r"\bmy company,?\s+([A-Z][A-Za-z0-9&.'-]{1,40})(?=[\s,.!?]|$)", re.I),
    re.compile(r"\b([A-Z][A-Za-z0-9&.'-]{1,40})\s+hires\b"),
    re.compile(
        r"\b(?:at|with)\s+([A-Z][A-Za-z0-9&.'-]{1,40}"
        r"(?:\s+[A-Z][A-Za-z0-9&.'-]{1,40}){0,3})(?=[,.!?]|$)"
    ),
    re.compile(r"\binterested in\s+([A-Z][A-Za-z0-9&.'-]{1,40})(?=[\s,.!?]|$)"),
    re.compile(
        r"\bapplying to (?:the )?([A-Z][A-Za-z0-9&.'-]{1,40})\s+"
        r"(?:PM|Product)\b"
    ),
)


@dataclass
class UnmatchedAssessment:
    thread_id: str
    name: str
    latest_message: str
    classification: str
    reason: str
    title: str = ""
    company: str = ""
    linkedin_url: str = ""
    thread_url: str = ""
    organization_id: str = ""
    contact_id: str = ""
    row_status: str = "not_created"


def classify_unmatched_thread(thread: dict) -> UnmatchedAssessment:
    latest = str(thread.get("latest_message") or thread.get("message_text") or "").strip()
    name = str(thread.get("name") or "").strip()
    sender = str(thread.get("last_sender") or "").strip()
    combined = f"{name} {sender} {latest}"

    if _SPONSORED.search(combined):
        classification, reason = NOISE, "sponsored message"
    elif _INMAIL.search(combined):
        classification, reason = NOISE, "InMail without a matched relationship"
    elif _BROADCAST.search(combined):
        classification, reason = NOISE, "broadcast or launch promotion"
    elif _PAID_RESEARCH.search(combined):
        classification, reason = NOISE, "paid research solicitation"
    elif _RECRUITER_SPAM.search(combined):
        classification, reason = NOISE, "generic recruiter spam"
    else:
        classification, reason = NEEDS_CONTACT_ROW, "human thread requires workbook identity"

    title = str(thread.get("title") or "").strip()
    company = str(thread.get("company") or "").strip() or title_employer(title)
    if not company:
        for pattern in _COMPANY_FROM_MESSAGE:
            match = pattern.search(latest)
            if match:
                company = match.group(1).strip(" .,;:!?")
                break

    return UnmatchedAssessment(
        thread_id=str(thread.get("thread_id") or "").strip(),
        name=name,
        latest_message=latest,
        classification=classification,
        reason=reason,
        title=title,
        company=company,
        linkedin_url=str(thread.get("linkedin_url") or "").strip(),
        thread_url=str(thread.get("thread_url") or "").strip(),
    )


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def ingest_unmatched_threads(
    workbook: OutreachWorkbook,
    assessments: list[UnmatchedAssessment],
    *,
    create_rows: bool,
) -> list[UnmatchedAssessment]:
    """Create identity rows, but never draft in the same pass."""

    organizations = workbook.list_organizations()
    org_by_name = {_normalized(item.name): item for item in organizations if item.name}
    org_ids = {item.organization_id for item in organizations}

    for assessment in assessments:
        if assessment.classification != NEEDS_CONTACT_ROW:
            assessment.row_status = "classified_noise"
            continue
        if not create_rows:
            assessment.row_status = "would_create"
            continue

        company_name = assessment.company or f"Unresolved organization — {assessment.name}"
        existing_org = org_by_name.get(_normalized(company_name))
        if existing_org is None:
            organization_id = workbook.make_organization_id(company_name)
            if organization_id in org_ids:
                organization_id = f"{organization_id}-{stable_suffix(assessment.thread_id or assessment.name, 6)}"
            existing_org, org_created = workbook.upsert_organization(
                OrganizationRecord(
                    organization_id=organization_id,
                    name=company_name,
                    organization_type=OrganizationType.COMPANY,
                    target_lists="linkedin;unmatched_thread",
                    status="Needs identity review",
                    source_kind=SourceKind.LINKEDIN,
                    source_url=assessment.thread_url,
                    notes=(
                        "Created from an unmatched LinkedIn conversation. "
                        "Verify organization identity and context before drafting."
                    ),
                )
            )
            org_by_name[_normalized(company_name)] = existing_org
            org_ids.add(existing_org.organization_id)
        else:
            org_created = False

        contact_id = workbook.make_contact_id(
            existing_org.organization_id,
            assessment.name or "Unknown LinkedIn sender",
            linkedin_url=assessment.linkedin_url,
        )
        contact, contact_created = workbook.upsert_contact(
            ContactRecord(
                contact_id=contact_id,
                organization_id=existing_org.organization_id,
                full_name=assessment.name or "Unknown LinkedIn sender",
                title=assessment.title,
                target_lists="linkedin;unmatched_thread",
                status="Needs context",
                linkedin_url=assessment.linkedin_url,
                source_kind=SourceKind.LINKEDIN,
                source_url=assessment.thread_url,
                notes=(
                    f"Created from unmatched thread {assessment.thread_id}. "
                    "Do not draft in the ingestion pass; verify profile and organization context first."
                ),
            )
        )
        assessment.organization_id = existing_org.organization_id
        assessment.contact_id = contact.contact_id
        assessment.row_status = (
            "created_org_and_contact"
            if org_created and contact_created
            else "created_contact"
            if contact_created
            else "already_present"
        )

    return assessments


def render_unmatched_report(assessments: list[UnmatchedAssessment]) -> str:
    needs = sum(item.classification == NEEDS_CONTACT_ROW for item in assessments)
    noise = sum(item.classification == NOISE for item in assessments)
    lines = [
        "# Unmatched LinkedIn threads",
        "",
        f"Threads: **{len(assessments)}** · needs contact row: **{needs}** · noise: **{noise}**",
        "",
        "No thread in this report was drafted in the same pass.",
        "",
    ]
    for item in assessments:
        lines.extend(
            [
                f"## {item.name or 'Unknown sender'}",
                f"- Classification: `{item.classification}` — {item.reason}",
                f"- Row status: `{item.row_status}`",
                f"- Company signal: {item.company or '(not present in captured thread)' }",
                f"- Contact: {item.contact_id or '(none)'}",
                f"- Thread: {item.thread_url or item.thread_id or '(unknown)'}",
                f"- Latest: {item.latest_message or '(empty)'}",
                "",
            ]
        )
    return "\n".join(lines)


def process_unmatched_threads(
    *,
    workbook: OutreachWorkbook,
    unmatched_threads: list[dict],
    artifacts_dir: Path,
    create_rows: bool,
) -> tuple[Path, list[UnmatchedAssessment]]:
    assessments = [classify_unmatched_thread(thread) for thread in unmatched_threads]
    ingest_unmatched_threads(workbook, assessments, create_rows=create_rows)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = artifacts_dir / f"{stamp}-unmatched-threads.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_unmatched_report(assessments), encoding="utf-8")
    return output, assessments

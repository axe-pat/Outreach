from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from outreach.company_enrichment import format_notes_parts, parse_notes_parts
from outreach.tracking import (
    ContactRecord,
    DiscoverySourceRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachChannel,
    OutreachWorkbook,
    SourceKind,
)


DEFAULT_RELATIONSHIP_LEADS_PATH = Path("workspace/relationship_leads.csv")
DEFAULT_PEOPLEGROVE_USC_LEADS_PATH = Path("workspace/relationship_leads_peoplegrove_usc.csv")
DEFAULT_RECENT_MBA_PM_LEADS_PATH = Path("workspace/relationship_leads_recent_mba_pm.csv")

RELATIONSHIP_LEAD_FIELDS = [
    "source_type",
    "full_name",
    "company",
    "title",
    "linkedin_url",
    "email",
    "company_website",
    "company_linkedin_url",
    "location",
    "school",
    "program",
    "grad_year",
    "relationship_signal",
    "contact_type",
    "priority",
    "target_lists",
    "tags",
    "source_url",
    "notes",
]

RELATIONSHIP_SOURCE_PRESETS: dict[str, dict[str, str | list[str]]] = {
    "peoplegrove_usc": {
        "path": str(DEFAULT_PEOPLEGROVE_USC_LEADS_PATH),
        "source_type": "peoplegrove",
        "target_lists": "peoplegrove;usc-network;usc-founder;usc-operator",
        "tags": "usc,peoplegrove,warm-network",
        "priority": "medium",
        "instructions": [
            "Use this for PeopleGrove/Trojan Network/USC manual captures.",
            "Prioritize founders, C-suite, product leaders, operators, recruiters, and startup-adjacent USC people.",
            "Paste one person per row. company and full_name are required; LinkedIn URL is strongly preferred.",
        ],
    },
    "recent_mba_pm": {
        "path": str(DEFAULT_RECENT_MBA_PM_LEADS_PATH),
        "source_type": "recent_mba_pm",
        "target_lists": "recent-mba-pm;mba-network;product-transition",
        "tags": "recent-mba-pm,mba,product",
        "priority": "medium",
        "instructions": [
            "Use this for recent MBA grads who moved into PM/product/product strategy.",
            "Best rows include school/program/grad_year plus current company/title.",
            "This is a relationship lead lane, not an account-power lane; keep the ask mentorship/routing oriented.",
        ],
    },
}


@dataclass(frozen=True)
class RelationshipLead:
    source_type: str
    full_name: str
    company: str
    title: str = ""
    linkedin_url: str = ""
    email: str = ""
    company_website: str = ""
    company_linkedin_url: str = ""
    location: str = ""
    school: str = ""
    program: str = ""
    grad_year: str = ""
    relationship_signal: str = ""
    contact_type: str = ""
    priority: str = ""
    target_lists: str = ""
    tags: str = ""
    source_url: str = ""
    notes: str = ""


def relationship_source_preset(source_key: str) -> dict[str, str | list[str]]:
    normalized = _normalized_source_type(source_key).replace("-", "_")
    return RELATIONSHIP_SOURCE_PRESETS.get(normalized, {})


def relationship_source_default_path(source_key: str) -> Path:
    preset = relationship_source_preset(source_key)
    raw_path = str(preset.get("path") or DEFAULT_RELATIONSHIP_LEADS_PATH)
    return Path(raw_path)


def ensure_relationship_leads_template(
    path: Path = DEFAULT_RELATIONSHIP_LEADS_PATH,
    *,
    source_key: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if source_key:
            _write_relationship_source_readme(path, source_key)
        return path
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RELATIONSHIP_LEAD_FIELDS)
        writer.writeheader()
    if source_key:
        _write_relationship_source_readme(path, source_key)
    return path


def load_relationship_leads(
    source_path: Path,
    *,
    default_source_type: str = "",
    default_target_lists: str = "",
    default_tags: str = "",
    default_priority: str = "",
) -> list[RelationshipLead]:
    if not source_path.exists():
        raise FileNotFoundError(f"Relationship leads file not found: {source_path}")
    with source_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [
            RelationshipLead(
                source_type=_clean(row.get("source_type")) or default_source_type or "manual_relationship_lead",
                full_name=_clean(row.get("full_name")),
                company=_clean(row.get("company")),
                title=_clean(row.get("title")),
                linkedin_url=_clean(row.get("linkedin_url")),
                email=_clean(row.get("email")),
                company_website=_clean(row.get("company_website")),
                company_linkedin_url=_clean(row.get("company_linkedin_url")),
                location=_clean(row.get("location")),
                school=_clean(row.get("school")),
                program=_clean(row.get("program")),
                grad_year=_clean(row.get("grad_year")),
                relationship_signal=_clean(row.get("relationship_signal")),
                contact_type=_clean(row.get("contact_type")),
                priority=_clean(row.get("priority")) or default_priority,
                target_lists=_merge_semicolon(_clean(row.get("target_lists")), default_target_lists),
                tags=_merge_csv(_clean(row.get("tags")), default_tags),
                source_url=_clean(row.get("source_url")),
                notes=_clean(row.get("notes")),
            )
            for row in reader
        ]


def import_relationship_leads(
    workbook_dir: Path,
    *,
    source_path: Path = DEFAULT_RELATIONSHIP_LEADS_PATH,
    source_key: str = "",
    execute: bool = False,
) -> dict[str, int | str]:
    workbook = OutreachWorkbook(workbook_dir)
    workbook.initialize()
    source_path = source_path.resolve()
    preset = relationship_source_preset(source_key)
    leads = load_relationship_leads(
        source_path,
        default_source_type=str(preset.get("source_type") or ""),
        default_target_lists=str(preset.get("target_lists") or ""),
        default_tags=str(preset.get("tags") or ""),
        default_priority=str(preset.get("priority") or ""),
    )
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    source_id = workbook.make_source_id("relationship-leads", str(source_path))

    if execute:
        workbook.upsert_source(
            DiscoverySourceRecord(
                source_id=source_id,
                label="Relationship lead import",
                source_kind=SourceKind.MANUAL,
                base_url=str(source_path),
                extraction_method="curated_csv_import",
                owner="outreach-engine",
                last_run_at=now,
                notes="One-time/manual import path for PeopleGrove, recent MBA PM, USC founder, and other relationship leads.",
            )
        )

    organizations_added = 0
    organizations_updated = 0
    contacts_added = 0
    contacts_updated = 0
    skipped = 0
    org_by_id = {item.organization_id: item for item in workbook.list_organizations()}
    contact_by_id = {item.contact_id: item for item in workbook.list_contacts()}

    for lead in leads:
        if not lead.full_name or not lead.company:
            skipped += 1
            continue
        org_id = workbook.make_organization_id(lead.company)
        existing_org = org_by_id.get(org_id)
        org_target_lists = _organization_target_lists(lead)
        org_notes = _organization_notes(lead)
        if not execute:
            if existing_org:
                organizations_updated += 1
            else:
                organizations_added += 1
            contact_id = workbook.make_contact_id(org_id, lead.full_name, lead.linkedin_url, lead.email)
            if contact_id in contact_by_id:
                contacts_updated += 1
            else:
                contacts_added += 1
            continue

        if existing_org:
            updates = _organization_updates(existing_org, lead, org_target_lists, org_notes, now)
            if updates:
                updated = workbook.update_organization(org_id, **updates)
                if updated is not None:
                    existing_org = updated
                    org_by_id[org_id] = updated
                    organizations_updated += 1
        else:
            existing_org, created = workbook.upsert_organization(
                OrganizationRecord(
                    organization_id=org_id,
                    name=lead.company,
                    organization_type=OrganizationType.COMPANY,
                    target_lists=org_target_lists,
                    status="Relationship lead",
                    city=lead.location,
                    website=lead.company_website,
                    linkedin_url=lead.company_linkedin_url,
                    source_kind=_source_kind_for_lead(lead.source_type),
                    source_url=lead.source_url or lead.company_linkedin_url or lead.company_website,
                    discovered_at=now,
                    last_updated_at=now,
                    notes=org_notes,
                )
            )
            org_by_id[org_id] = existing_org
            if created:
                organizations_added += 1

        contact_id = workbook.make_contact_id(org_id, lead.full_name, lead.linkedin_url, lead.email)
        existing_contact = contact_by_id.get(contact_id)
        contact_target_lists = _contact_target_lists(lead)
        contact_notes = _contact_notes(lead)
        if existing_contact:
            updates = _contact_updates(existing_contact, lead, contact_target_lists, contact_notes)
            if updates:
                updated = workbook.update_contact(contact_id, **updates)
                if updated is not None:
                    contact_by_id[contact_id] = updated
                    contacts_updated += 1
            continue

        contact, created = workbook.upsert_contact(
            ContactRecord(
                contact_id=contact_id,
                organization_id=org_id,
                full_name=lead.full_name,
                title=lead.title,
                contact_type=lead.contact_type or _infer_contact_type(lead),
                target_lists=contact_target_lists,
                preferred_channel=OutreachChannel.EMAIL if lead.email else OutreachChannel.LINKEDIN,
                status="Discovered",
                linkedin_url=lead.linkedin_url,
                email=lead.email,
                source_kind=_source_kind_for_lead(lead.source_type),
                source_url=lead.source_url or lead.linkedin_url,
                discovered_at=now,
                notes=contact_notes,
            )
        )
        contact_by_id[contact.contact_id] = contact
        if created:
            contacts_added += 1

    return {
        "source_path": str(source_path),
        "source_key": source_key,
        "count": len(leads),
        "organizations_added": organizations_added,
        "organizations_updated": organizations_updated,
        "contacts_added": contacts_added,
        "contacts_updated": contacts_updated,
        "skipped": skipped,
    }


def _write_relationship_source_readme(path: Path, source_key: str) -> None:
    preset = relationship_source_preset(source_key)
    if not preset:
        return
    readme_path = path.with_suffix(".md")
    instructions = [str(item) for item in preset.get("instructions", []) if str(item).strip()]
    lines = [
        f"# {source_key} relationship lead capture",
        "",
        f"CSV: `{path.name}`",
        "",
        "Required columns:",
        "",
        "- `full_name`",
        "- `company`",
        "",
        "Recommended columns:",
        "",
        "- `title`",
        "- `linkedin_url`",
        "- `company_website` or `company_linkedin_url`",
        "- `school`, `program`, `grad_year`",
        "- `relationship_signal`",
        "- `notes`",
        "",
        "Defaults applied during import when a row leaves fields blank:",
        "",
        f"- `source_type`: `{preset.get('source_type', '')}`",
        f"- `target_lists`: `{preset.get('target_lists', '')}`",
        f"- `tags`: `{preset.get('tags', '')}`",
        f"- `priority`: `{preset.get('priority', '')}`",
        "",
        "Capture rules:",
        "",
    ]
    lines.extend(f"- {item}" for item in instructions)
    lines.append("")
    readme_path.write_text("\n".join(lines), encoding="utf-8")


def _organization_updates(
    existing: OrganizationRecord,
    lead: RelationshipLead,
    target_lists: str,
    notes: str,
    now: str,
) -> dict[str, str]:
    updates: dict[str, str] = {}
    merged_target_lists = _merge_semicolon(existing.target_lists, target_lists)
    if merged_target_lists != existing.target_lists:
        updates["target_lists"] = merged_target_lists
    if not existing.city and lead.location:
        updates["city"] = lead.location
    if not existing.website and lead.company_website:
        updates["website"] = lead.company_website
    if not existing.linkedin_url and lead.company_linkedin_url:
        updates["linkedin_url"] = lead.company_linkedin_url
    if not existing.source_url and (lead.source_url or lead.company_linkedin_url or lead.company_website):
        updates["source_url"] = lead.source_url or lead.company_linkedin_url or lead.company_website
    merged_notes = _merge_notes(existing.notes, notes)
    if merged_notes != existing.notes:
        updates["notes"] = merged_notes
    if updates:
        updates["last_updated_at"] = now
    return updates


def _contact_updates(
    existing: ContactRecord,
    lead: RelationshipLead,
    target_lists: str,
    notes: str,
) -> dict[str, str]:
    updates: dict[str, str] = {}
    merged_target_lists = _merge_semicolon(existing.target_lists, target_lists)
    if merged_target_lists != existing.target_lists:
        updates["target_lists"] = merged_target_lists
    if not existing.title and lead.title:
        updates["title"] = lead.title
    if not existing.contact_type and (lead.contact_type or _infer_contact_type(lead)):
        updates["contact_type"] = lead.contact_type or _infer_contact_type(lead)
    if not existing.linkedin_url and lead.linkedin_url:
        updates["linkedin_url"] = lead.linkedin_url
    if not existing.email and lead.email:
        updates["email"] = lead.email
        updates["preferred_channel"] = OutreachChannel.EMAIL.value
    if not existing.source_url and (lead.source_url or lead.linkedin_url):
        updates["source_url"] = lead.source_url or lead.linkedin_url
    merged_notes = _merge_notes(existing.notes, notes)
    if merged_notes != existing.notes:
        updates["notes"] = merged_notes
    return updates


def _organization_target_lists(lead: RelationshipLead) -> str:
    parts = ["track-2", "relationship", "relationship-leads", _normalized_source_type(lead.source_type)]
    if lead.target_lists:
        parts.append(lead.target_lists)
    if lead.priority:
        parts.append(f"priority-{lead.priority.lower()}")
    return _merge_semicolon(*parts)


def _contact_target_lists(lead: RelationshipLead) -> str:
    parts = ["track-2", "relationship-leads", _normalized_source_type(lead.source_type)]
    if lead.target_lists:
        parts.append(lead.target_lists)
    if lead.school:
        parts.append(_school_tag(lead.school))
    if lead.program:
        parts.append(_program_tag(lead.program))
    if lead.priority:
        parts.append(f"priority-{lead.priority.lower()}")
    return _merge_semicolon(*parts)


def _organization_notes(lead: RelationshipLead) -> str:
    _, metadata = parse_notes_parts("")
    metadata["seed_source"] = "relationship_leads"
    metadata["relationship_source_type"] = lead.source_type
    metadata["relationship_signal"] = lead.relationship_signal
    metadata["lead_priority"] = lead.priority
    metadata["tags"] = lead.tags
    metadata["context_source"] = "relationship_lead_import"
    metadata["context_confidence"] = "manual_seed"
    metadata["context_evidence_url"] = lead.source_url or lead.company_linkedin_url or lead.company_website
    return format_notes_parts(["Relationship lead imported"], metadata)


def _contact_notes(lead: RelationshipLead) -> str:
    _, metadata = parse_notes_parts("")
    metadata["seed_source"] = "relationship_leads"
    metadata["relationship_source_type"] = lead.source_type
    metadata["relationship_signal"] = lead.relationship_signal
    metadata["school"] = lead.school
    metadata["program"] = lead.program
    metadata["grad_year"] = lead.grad_year
    metadata["lead_priority"] = lead.priority
    metadata["lead_notes"] = lead.notes
    return format_notes_parts(["Relationship lead contact"], metadata)


def _merge_notes(existing: str, incoming: str) -> str:
    existing_freeform, existing_metadata = parse_notes_parts(existing)
    incoming_freeform, incoming_metadata = parse_notes_parts(incoming)
    merged_metadata = {**incoming_metadata, **existing_metadata}
    if incoming_metadata.get("tags") or existing_metadata.get("tags"):
        merged_metadata["tags"] = _merge_csv(existing_metadata.get("tags", ""), incoming_metadata.get("tags", ""))
    merged_freeform = [*existing_freeform]
    for item in incoming_freeform:
        if item and item not in merged_freeform:
            merged_freeform.append(item)
    return format_notes_parts(merged_freeform or incoming_freeform, merged_metadata)


def _source_kind_for_lead(source_type: str) -> SourceKind:
    normalized = _normalized_source_type(source_type)
    if normalized in {"peoplegrove", "handshake", "usc", "usc-founder", "usc-alumni"}:
        return SourceKind.UNIVERSITY_DIRECTORY
    if normalized in {"linkedin", "recent-mba-pm", "linkedin-recent-mba-pm"}:
        return SourceKind.LINKEDIN
    if normalized in {"email", "warm-email"}:
        return SourceKind.EMAIL
    return SourceKind.MANUAL


def _infer_contact_type(lead: RelationshipLead) -> str:
    text = " ".join([lead.title, lead.relationship_signal]).lower()
    if any(token in text for token in ["founder", "co-founder", "cofounder", "ceo"]):
        return "Founder"
    if any(token in text for token in ["cto", "chief technology", "engineering"]):
        return "Engineering"
    if any(token in text for token in ["product", "pm", "chief product"]):
        return "Product"
    if any(token in text for token in ["recruiter", "talent", "campus"]):
        return "Recruiter"
    if "mba" in text:
        return "MBA"
    return ""


def _normalized_source_type(value: str) -> str:
    return "-".join(_clean(value).lower().replace("_", "-").split()) or "manual-relationship-lead"


def _school_tag(value: str) -> str:
    return f"school-{_normalized_source_type(value)}"


def _program_tag(value: str) -> str:
    return f"program-{_normalized_source_type(value)}"


def _merge_semicolon(*values: str) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        for item in (value or "").split(";"):
            clean = item.strip()
            normalized = clean.lower()
            if not clean or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(clean)
    return ";".join(merged)


def _merge_csv(*values: str) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        for item in (value or "").split(","):
            clean = item.strip()
            normalized = clean.lower()
            if not clean or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(clean)
    return ",".join(merged)


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())

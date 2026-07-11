from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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
    "source_record_id",
    "capture_batch",
    "captured_at",
    "captured_by",
]

RELATIONSHIP_LEAD_STAGE_FIELDS = [
    "batch_id",
    "row_id",
    "source_row_number",
    "row_fingerprint",
    "dedupe_key",
    "validation_status",
    "validation_issues",
    "review_status",
    "reviewed_by",
    "reviewed_at",
    "review_notes",
    *RELATIONSHIP_LEAD_FIELDS,
]

REQUIRED_RELATIONSHIP_LEAD_FIELDS = {"full_name", "company"}
REQUIRED_RELATIONSHIP_STAGE_FIELDS = {
    "batch_id",
    "row_id",
    "source_row_number",
    "row_fingerprint",
    "validation_status",
    "review_status",
}
VALID_REVIEW_STATUSES = {"pending", "approved", "rejected"}
FINAL_REVIEW_STATUSES = {"approved", "rejected"}
RELATIONSHIP_DECISION_SCHEMA_VERSION = 1
VALID_PRIORITIES = {"low", "medium", "high"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

RELATIONSHIP_SOURCE_PRESETS: dict[str, dict[str, str | list[str]]] = {
    "peoplegrove_usc": {
        "path": str(DEFAULT_PEOPLEGROVE_USC_LEADS_PATH),
        "source_type": "peoplegrove",
        # Keep source-wide defaults generic. Role-specific lists belong to each
        # curated row; applying founder/operator here polluted every staged
        # PeopleGrove contact regardless of their actual role.
        "target_lists": "peoplegrove;usc-network",
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
    source_record_id: str = ""
    capture_batch: str = ""
    captured_at: str = ""
    captured_by: str = ""
    batch_id: str = ""
    row_id: str = ""
    source_row_number: int = 0
    row_fingerprint: str = ""


@dataclass(frozen=True)
class RelationshipLeadIssue:
    row_number: int
    code: str
    message: str
    duplicate_of_row: int = 0

    def as_dict(self) -> dict[str, int | str]:
        payload: dict[str, int | str] = {
            "row_number": self.row_number,
            "code": self.code,
            "message": self.message,
        }
        if self.duplicate_of_row:
            payload["duplicate_of_row"] = self.duplicate_of_row
        return payload


class RelationshipLeadValidationError(ValueError):
    """Raised when relationship lead source or staged data is malformed."""


class RelationshipLeadReviewError(ValueError):
    """Raised when an execute import has not passed the explicit review gate."""


class RelationshipLeadConflictError(ValueError):
    """Raised when an approved lead conflicts with an existing workbook identity."""


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
        fieldnames, rows = _read_relationship_csv(path)
        if not rows and fieldnames != RELATIONSHIP_LEAD_FIELDS:
            _write_csv_atomic(path, RELATIONSHIP_LEAD_FIELDS, [])
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
    fieldnames, rows = _read_relationship_csv(source_path)
    missing = REQUIRED_RELATIONSHIP_LEAD_FIELDS - set(fieldnames)
    if missing:
        raise RelationshipLeadValidationError(
            f"Relationship lead CSV is missing required columns: {', '.join(sorted(missing))}"
        )
    leads: list[RelationshipLead] = []
    for source_row_number, row in rows:
        if not any(_clean(row.get(field)) for field in RELATIONSHIP_LEAD_FIELDS):
            continue
        lead = RelationshipLead(
            source_type=(
                _clean(row.get("source_type"))
                or default_source_type
                or "manual_relationship_lead"
            ),
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
            target_lists=_merge_semicolon(
                _clean(row.get("target_lists")), default_target_lists
            ),
            tags=_merge_csv(_clean(row.get("tags")), default_tags),
            source_url=_clean(row.get("source_url")),
            notes=_clean(row.get("notes")),
            source_record_id=_clean(row.get("source_record_id")),
            capture_batch=_clean(row.get("capture_batch")),
            captured_at=_clean(row.get("captured_at")),
            captured_by=_clean(row.get("captured_by")),
            batch_id=_clean(row.get("batch_id")),
            row_id=_clean(row.get("row_id")),
            source_row_number=_positive_int(row.get("source_row_number")) or source_row_number,
            row_fingerprint=_clean(row.get("row_fingerprint")),
        )
        leads.append(_normalize_relationship_lead(lead))
    return leads


def relationship_leads_staged_path(source_path: Path) -> Path:
    return source_path.with_name(f"{source_path.stem}.staged.csv")


def relationship_leads_manifest_path(staged_path: Path) -> Path:
    return staged_path.with_suffix(".manifest.json")


def relationship_leads_review_manifest_path(staged_path: Path) -> Path:
    return staged_path.with_suffix(".review.json")


def validate_relationship_leads(leads: list[RelationshipLead]) -> list[RelationshipLeadIssue]:
    issues: list[RelationshipLeadIssue] = []
    seen_identity_tokens: dict[str, int] = {}
    current_year = datetime.now(UTC).year

    for lead in leads:
        row_number = lead.source_row_number
        if not lead.full_name:
            issues.append(RelationshipLeadIssue(row_number, "missing_full_name", "full_name is required"))
        if not lead.company:
            issues.append(RelationshipLeadIssue(row_number, "missing_company", "company is required"))
        if not (lead.linkedin_url or lead.source_url or lead.source_record_id):
            issues.append(
                RelationshipLeadIssue(
                    row_number,
                    "missing_provenance",
                    "provide linkedin_url, source_url, or source_record_id",
                )
            )
        if _normalized_source_type(lead.source_type) == "peoplegrove" and not (
            lead.source_url or lead.source_record_id
        ):
            issues.append(
                RelationshipLeadIssue(
                    row_number,
                    "missing_peoplegrove_record",
                    "PeopleGrove rows require source_url or source_record_id",
                )
            )
        if lead.email and not EMAIL_PATTERN.fullmatch(lead.email):
            issues.append(RelationshipLeadIssue(row_number, "invalid_email", "email is malformed"))
        for field_name in ("linkedin_url", "company_website", "company_linkedin_url", "source_url"):
            value = getattr(lead, field_name)
            if value and not _is_http_url(value):
                issues.append(
                    RelationshipLeadIssue(
                        row_number,
                        f"invalid_{field_name}",
                        f"{field_name} must be an absolute http(s) URL",
                    )
                )
        if lead.linkedin_url and _is_http_url(lead.linkedin_url) and not _is_linkedin_person_url(
            lead.linkedin_url
        ):
            issues.append(
                RelationshipLeadIssue(
                    row_number,
                    "invalid_linkedin_person_url",
                    "linkedin_url must be a LinkedIn /in/ or /pub/ profile URL",
                )
            )
        if lead.company_linkedin_url and _is_http_url(lead.company_linkedin_url) and not _is_linkedin_url(
            lead.company_linkedin_url
        ):
            issues.append(
                RelationshipLeadIssue(
                    row_number,
                    "invalid_company_linkedin_url",
                    "company_linkedin_url must use linkedin.com",
                )
            )
        if lead.priority and lead.priority.lower() not in VALID_PRIORITIES:
            issues.append(
                RelationshipLeadIssue(
                    row_number,
                    "invalid_priority",
                    f"priority must be one of {', '.join(sorted(VALID_PRIORITIES))}",
                )
            )
        if lead.grad_year and (
            not lead.grad_year.isdigit()
            or len(lead.grad_year) != 4
            or not 1950 <= int(lead.grad_year) <= current_year + 3
        ):
            issues.append(
                RelationshipLeadIssue(
                    row_number,
                    "invalid_grad_year",
                    f"grad_year must be a four-digit year from 1950 to {current_year + 3}",
                )
            )
        if lead.captured_at and not _is_iso_datetime(lead.captured_at):
            issues.append(
                RelationshipLeadIssue(
                    row_number,
                    "invalid_captured_at",
                    "captured_at must be an ISO-8601 timestamp",
                )
            )

        duplicate_rows = {
            seen_identity_tokens[token]
            for token in _relationship_identity_tokens(lead)
            if token in seen_identity_tokens
        }
        if duplicate_rows:
            duplicate_of = min(duplicate_rows)
            issues.append(
                RelationshipLeadIssue(
                    row_number,
                    "duplicate_row",
                    f"duplicates source row {duplicate_of}",
                    duplicate_of_row=duplicate_of,
                )
            )
        else:
            for token in _relationship_identity_tokens(lead):
                seen_identity_tokens[token] = row_number
    return issues


def stage_relationship_leads(
    source_path: Path,
    *,
    staged_path: Path | None = None,
    source_key: str = "",
) -> dict[str, object]:
    source_path = source_path.resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Relationship leads file not found: {source_path}")
    staged_path = (staged_path or relationship_leads_staged_path(source_path)).resolve()
    if staged_path == source_path:
        raise RelationshipLeadValidationError("staged_path must differ from source_path")
    preset = relationship_source_preset(source_key)
    leads = load_relationship_leads(
        source_path,
        default_source_type=str(preset.get("source_type") or ""),
        default_target_lists=str(preset.get("target_lists") or ""),
        default_tags=str(preset.get("tags") or ""),
        default_priority=str(preset.get("priority") or ""),
    )
    source_sha256 = _file_sha256(source_path)
    batch_label = _normalized_source_type(source_key or "relationship")
    batch_id = f"rel-{batch_label}-{source_sha256[:12]}"
    staged_at = _utc_now()
    prepared_leads = [
        replace(
            lead,
            batch_id=batch_id,
            row_id=f"{batch_id}-r{lead.source_row_number:04d}",
            capture_batch=lead.capture_batch or batch_id,
            captured_at=lead.captured_at or staged_at,
        )
        for lead in leads
    ]
    issues = validate_relationship_leads(prepared_leads)
    issues_by_row: dict[int, list[RelationshipLeadIssue]] = {}
    for issue in issues:
        issues_by_row.setdefault(issue.row_number, []).append(issue)

    staged_rows: list[dict[str, object]] = []
    for lead in prepared_leads:
        row_issues = issues_by_row.get(lead.source_row_number, [])
        fingerprint = _relationship_lead_fingerprint(lead)
        staged_lead = replace(lead, row_fingerprint=fingerprint)
        staged_rows.append(
            {
                "batch_id": staged_lead.batch_id,
                "row_id": staged_lead.row_id,
                "source_row_number": staged_lead.source_row_number,
                "row_fingerprint": fingerprint,
                "dedupe_key": _relationship_dedupe_key(staged_lead),
                "validation_status": "blocked" if row_issues else "ready",
                "validation_issues": "; ".join(
                    f"{issue.code}: {issue.message}" for issue in row_issues
                ),
                "review_status": "pending",
                "reviewed_by": "",
                "reviewed_at": "",
                "review_notes": "",
                **_relationship_lead_csv_payload(staged_lead),
            }
        )

    _write_csv_atomic(staged_path, RELATIONSHIP_LEAD_STAGE_FIELDS, staged_rows)
    manifest_path = relationship_leads_manifest_path(staged_path)
    duplicate_rows = sum(issue.code == "duplicate_row" for issue in issues)
    blocked_rows = len({issue.row_number for issue in issues})
    manifest = {
        "schema_version": 1,
        "batch_id": batch_id,
        "source_key": source_key,
        "source_path": str(source_path),
        "source_sha256": source_sha256,
        "staged_path": str(staged_path),
        "staged_at": staged_at,
        "rows_total": len(prepared_leads),
        "rows_ready": len(prepared_leads) - blocked_rows,
        "rows_blocked": blocked_rows,
        "duplicate_rows": duplicate_rows,
        "issues": [issue.as_dict() for issue in issues],
    }
    _write_json_atomic(manifest_path, manifest)
    review_manifest_path = relationship_leads_review_manifest_path(staged_path)
    if review_manifest_path.exists():
        review_manifest_path.unlink()
    return {
        **manifest,
        "manifest_path": str(manifest_path),
        "pending_review": len(prepared_leads),
    }


def review_staged_relationship_leads(
    staged_path: Path,
    *,
    reviewer: str,
    approve_row_ids: tuple[str, ...] = (),
    reject_row_ids: tuple[str, ...] = (),
    approve_all_ready: bool = False,
    reject_all_blocked: bool = False,
    decision_artifact_path: Path | None = None,
    override_finalized: bool = False,
    review_notes: str = "",
) -> dict[str, object]:
    staged_path = staged_path.resolve()
    reviewer = _clean(reviewer)
    if not reviewer:
        raise RelationshipLeadReviewError("reviewer is required")
    fieldnames, numbered_rows = _read_relationship_csv(staged_path)
    missing = REQUIRED_RELATIONSHIP_STAGE_FIELDS - set(fieldnames)
    if missing:
        raise RelationshipLeadReviewError(
            "Not a staged relationship lead file; missing columns: "
            + ", ".join(sorted(missing))
        )
    rows = [row for _, row in numbered_rows]
    stage_manifest = _assert_stage_manifest(staged_path, rows)
    review_manifest_path = relationship_leads_review_manifest_path(staged_path)
    if decision_artifact_path is None and review_manifest_path.exists():
        try:
            existing_review_manifest = json.loads(
                review_manifest_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            raise RelationshipLeadReviewError(
                f"Existing review manifest is unreadable: {review_manifest_path}"
            ) from exc
        if isinstance(existing_review_manifest, dict) and isinstance(
            existing_review_manifest.get("decision_artifact"), dict
        ):
            raise RelationshipLeadReviewError(
                "This review is bound to a complete decision artifact; provide a new "
                "decision artifact to update or reseal it"
            )
    rows_by_id = {_clean(row.get("row_id")): row for row in rows}
    approve = {_clean(item) for item in approve_row_ids if _clean(item)}
    reject = {_clean(item) for item in reject_row_ids if _clean(item)}
    decision_binding: dict[str, object] | None = None
    if decision_artifact_path is not None:
        if approve or reject or approve_all_ready or reject_all_blocked:
            raise RelationshipLeadReviewError(
                "A decision artifact is a complete decision set; do not combine it with "
                "row selectors or bulk review flags"
            )
        approve, reject, decision_binding = _load_relationship_decision_artifact(
            decision_artifact_path,
            rows=rows,
            stage_manifest=stage_manifest,
            expected_staged_sha256=_file_sha256(staged_path),
        )
    else:
        if approve_all_ready:
            approve.update(
                row_id
                for row_id, row in rows_by_id.items()
                if _clean(row.get("validation_status")) == "ready"
                and _clean(row.get("review_status")).lower() == "pending"
            )
        if reject_all_blocked:
            reject.update(
                row_id
                for row_id, row in rows_by_id.items()
                if _clean(row.get("validation_status")) == "blocked"
                and _clean(row.get("review_status")).lower() == "pending"
            )
    if not approve and not reject:
        raise RelationshipLeadReviewError("select at least one row to approve or reject")
    overlap = approve & reject
    if overlap:
        raise RelationshipLeadReviewError(
            "Rows cannot be both approved and rejected: " + ", ".join(sorted(overlap))
        )
    unknown = (approve | reject) - set(rows_by_id)
    if unknown:
        raise RelationshipLeadReviewError("Unknown row IDs: " + ", ".join(sorted(unknown)))

    desired_statuses = {
        **{row_id: "approved" for row_id in approve},
        **{row_id: "rejected" for row_id in reject},
    }
    finalized_overrides: list[str] = []
    for row_id, desired_status in desired_statuses.items():
        row = rows_by_id[row_id]
        lead = _relationship_lead_from_staged_row(row)
        actual_fingerprint = _relationship_lead_fingerprint(lead)
        if actual_fingerprint != _clean(row.get("row_fingerprint")):
            raise RelationshipLeadReviewError(
                f"Row {row_id} changed after staging; restage before reviewing"
            )
        if desired_status == "approved" and _clean(row.get("validation_status")) != "ready":
            raise RelationshipLeadReviewError(
                f"Row {row_id} is validation-blocked and cannot be approved"
            )
        current_status = _clean(row.get("review_status")).lower()
        if (
            current_status in FINAL_REVIEW_STATUSES
            and current_status != desired_status
        ):
            if not override_finalized:
                raise RelationshipLeadReviewError(
                    f"Row {row_id} is already {current_status}; pass override_finalized "
                    f"to change it to {desired_status}"
                )
            finalized_overrides.append(row_id)

    reviewed_at = _utc_now()
    for row_id in approve | reject:
        row = rows_by_id[row_id]
        desired_status = desired_statuses[row_id]
        current_status = _clean(row.get("review_status")).lower()
        needs_provenance = not (
            _clean(row.get("reviewed_by")) and _clean(row.get("reviewed_at"))
        )
        if current_status != desired_status or needs_provenance or review_notes:
            row["review_status"] = desired_status
            row["reviewed_by"] = reviewer
            row["reviewed_at"] = reviewed_at
            if review_notes:
                row["review_notes"] = _clean(review_notes)

    _write_csv_atomic(staged_path, RELATIONSHIP_LEAD_STAGE_FIELDS, rows)
    counts = _relationship_review_counts(rows)
    review_manifest = {
        "schema_version": 2,
        "staged_path": str(staged_path),
        "staged_sha256": _file_sha256(staged_path),
        "reviewed_at": reviewed_at,
        "reviewer": reviewer,
        "finalized_override_count": len(finalized_overrides),
        "finalized_override_row_ids": sorted(finalized_overrides),
        **counts,
    }
    if decision_binding is not None:
        review_manifest["decision_artifact"] = decision_binding
    _write_json_atomic(review_manifest_path, review_manifest)
    return {**review_manifest, "review_manifest_path": str(review_manifest_path)}


def import_relationship_leads(
    workbook_dir: Path,
    *,
    source_path: Path = DEFAULT_RELATIONSHIP_LEADS_PATH,
    source_key: str = "",
    execute: bool = False,
) -> dict[str, object]:
    source_path = source_path.resolve()
    preset = relationship_source_preset(source_key)
    fieldnames, numbered_rows = _read_relationship_csv(source_path)
    all_leads = load_relationship_leads(
        source_path,
        default_source_type=str(preset.get("source_type") or ""),
        default_target_lists=str(preset.get("target_lists") or ""),
        default_tags=str(preset.get("tags") or ""),
        default_priority=str(preset.get("priority") or ""),
    )
    staged = REQUIRED_RELATIONSHIP_STAGE_FIELDS.issubset(set(fieldnames))
    rows = [row for _, row in numbered_rows]
    validation_issues = validate_relationship_leads(all_leads)
    issue_rows = {issue.row_number for issue in validation_issues}
    review_counts = {
        "rows_approved": 0,
        "rows_rejected": 0,
        "rows_pending": len(all_leads),
    }

    if execute and not staged:
        raise RelationshipLeadReviewError(
            "Execute import requires a staged and reviewed CSV. Run stage-relationship-leads, "
            "review-relationship-leads, then import the staged file."
        )
    if execute and not all_leads:
        raise RelationshipLeadReviewError(
            "Execute import requires a non-empty staged relationship lead batch"
        )

    if staged:
        review_counts = _relationship_review_counts(rows)
        statuses = {
            _clean(row.get("row_id")): _clean(row.get("review_status")).lower()
            for row in rows
        }
        leads = [lead for lead in all_leads if statuses.get(lead.row_id) == "approved"]
        if execute:
            _assert_relationship_review_gate(source_path, rows, all_leads)
    else:
        leads = [lead for lead in all_leads if lead.source_row_number not in issue_rows]

    workbook = OutreachWorkbook(workbook_dir)
    organizations = workbook.list_organizations()
    contacts = workbook.list_contacts()
    conflicts = _relationship_workbook_conflicts(workbook, leads, organizations, contacts)
    if conflicts and execute:
        raise RelationshipLeadConflictError(
            "Approved relationship leads conflict with existing workbook identities: "
            + "; ".join(conflicts)
        )
    conflict_rows = {
        int(item.split("row ", 1)[1].split(":", 1)[0])
        for item in conflicts
        if "row " in item and ":" in item
    }
    importable_leads = [lead for lead in leads if lead.source_row_number not in conflict_rows]

    batch_ids = {lead.batch_id for lead in all_leads if lead.batch_id}
    batch_id = next(iter(batch_ids)) if len(batch_ids) == 1 else ""
    source_token = batch_id or _file_sha256(source_path)[:12]
    source_id = workbook.make_source_id(f"relationship-leads-{source_token}")
    now = _utc_now()

    if execute and importable_leads:
        workbook.initialize()
        source_kind = _batch_source_kind(importable_leads)
        workbook.upsert_source(
            DiscoverySourceRecord(
                source_id=source_id,
                label=f"Relationship lead import {source_token}",
                source_kind=source_kind,
                base_url=str(source_path),
                extraction_method="reviewed_curated_csv_import",
                owner="outreach-engine",
                last_run_at=now,
                notes=(
                    "One-time/low-frequency reviewed relationship lead batch"
                    f" | source_key={source_key or 'manual'}"
                    f" | batch_id={batch_id}"
                    f" | staged_sha256={_file_sha256(source_path)}"
                ),
            )
        )

    organizations_added = 0
    organizations_updated = 0
    organizations_unchanged = 0
    contacts_added = 0
    contacts_updated = 0
    contacts_unchanged = 0
    org_by_id = {item.organization_id: item for item in organizations}
    contact_by_id = {item.contact_id: item for item in contacts}
    planned_org_ids: set[str] = set()

    for lead in importable_leads:
        org_matches = _matching_organizations(workbook, lead, list(org_by_id.values()))
        existing_org = org_matches[0] if org_matches else None
        org_id = existing_org.organization_id if existing_org else workbook.make_organization_id(lead.company)
        org_target_lists = _organization_target_lists(lead)
        org_notes = _organization_notes(lead)
        if not execute:
            if existing_org:
                if _organization_updates(existing_org, lead, org_target_lists, org_notes, now):
                    organizations_updated += 1
                else:
                    organizations_unchanged += 1
            else:
                if org_id not in planned_org_ids:
                    organizations_added += 1
                    planned_org_ids.add(org_id)
            contact_matches = _matching_contacts(lead, org_id, list(contact_by_id.values()))
            if contact_matches:
                existing_contact = contact_matches[0]
                if _contact_updates(
                    existing_contact,
                    lead,
                    _contact_target_lists(lead),
                    _contact_notes(lead),
                ):
                    contacts_updated += 1
                else:
                    contacts_unchanged += 1
            else:
                contacts_added += 1
            continue

        if existing_org:
            updates = _organization_updates(existing_org, lead, org_target_lists, org_notes, now)
            if updates:
                updated = workbook.update_organization(existing_org.organization_id, **updates)
                if updated is not None:
                    existing_org = updated
                    org_by_id[existing_org.organization_id] = updated
                    organizations_updated += 1
            else:
                organizations_unchanged += 1
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

        contact_matches = _matching_contacts(lead, org_id, list(contact_by_id.values()))
        existing_contact = contact_matches[0] if contact_matches else None
        contact_target_lists = _contact_target_lists(lead)
        contact_notes = _contact_notes(lead)
        if existing_contact:
            updates = _contact_updates(existing_contact, lead, contact_target_lists, contact_notes)
            if updates:
                updated = workbook.update_contact(existing_contact.contact_id, **updates)
                if updated is not None:
                    contact_by_id[existing_contact.contact_id] = updated
                    contacts_updated += 1
            else:
                contacts_unchanged += 1
            continue

        contact_id = workbook.make_contact_id(org_id, lead.full_name, lead.linkedin_url, lead.email)
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
        "source_id": source_id,
        "batch_id": batch_id,
        "staged": staged,
        "review_required": bool(all_leads) and not staged,
        "count": len(all_leads),
        "rows_selected": len(importable_leads),
        **review_counts,
        "validation_issue_count": len(validation_issues),
        "validation_issues": [issue.as_dict() for issue in validation_issues],
        "workbook_conflicts": conflicts,
        "organizations_added": organizations_added,
        "organizations_updated": organizations_updated,
        "organizations_unchanged": organizations_unchanged,
        "contacts_added": contacts_added,
        "contacts_updated": contacts_updated,
        "contacts_unchanged": contacts_unchanged,
        "skipped": len(all_leads) - len(importable_leads),
        "execute": execute,
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
        "- `source_url` or `source_record_id` (required provenance for PeopleGrove)",
        "- `capture_batch`, `captured_at`, `captured_by`",
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
    lines.extend(
        [
            "",
            "Review workflow:",
            "",
            f"1. Stage: `python main.py stage-relationship-leads --source-path {path}`",
            "2. Inspect the staged CSV and its validation issues.",
            "3. Record explicit row decisions, or pass a complete SHA-bound decision JSON to `review-relationship-leads --decision-artifact`.",
            "4. Import the reviewed staged CSV with `import-relationship-leads --execute`.",
            "",
            "Bulk review flags affect pending rows only; changing a finalized decision requires `--override-finalized`.",
            "Raw, missing, empty, or unreviewed capture CSVs cannot be executed directly.",
            "",
        ]
    )
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
    metadata["relationship_import_batch"] = lead.batch_id or lead.capture_batch
    metadata["relationship_source_record_id"] = lead.source_record_id
    metadata["relationship_captured_at"] = lead.captured_at
    metadata["relationship_captured_by"] = lead.captured_by
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
    metadata["relationship_import_batch"] = lead.batch_id or lead.capture_batch
    metadata["relationship_import_row"] = lead.row_id
    metadata["relationship_row_fingerprint"] = lead.row_fingerprint
    metadata["relationship_source_record_id"] = lead.source_record_id
    metadata["relationship_evidence_url"] = lead.source_url or lead.linkedin_url
    metadata["relationship_captured_at"] = lead.captured_at
    metadata["relationship_captured_by"] = lead.captured_by
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


def _read_relationship_csv(path: Path) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    if not path.exists():
        raise FileNotFoundError(f"Relationship leads file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        raw_fieldnames = list(reader.fieldnames or [])
        fieldnames = [str(name or "").strip() for name in raw_fieldnames]
        if len(fieldnames) != len(set(fieldnames)):
            raise RelationshipLeadValidationError("Relationship lead CSV has duplicate columns")
        rows: list[tuple[int, dict[str, str]]] = []
        for row_number, raw_row in enumerate(reader, start=2):
            normalized_row = {
                normalized_name: str(raw_row.get(raw_name) or "")
                for raw_name, normalized_name in zip(raw_fieldnames, fieldnames, strict=True)
            }
            rows.append((row_number, normalized_row))
    return fieldnames, rows


def _relationship_lead_from_staged_row(row: dict[str, str]) -> RelationshipLead:
    lead = RelationshipLead(
        source_type=_clean(row.get("source_type")) or "manual_relationship_lead",
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
        priority=_clean(row.get("priority")),
        target_lists=_clean(row.get("target_lists")),
        tags=_clean(row.get("tags")),
        source_url=_clean(row.get("source_url")),
        notes=_clean(row.get("notes")),
        source_record_id=_clean(row.get("source_record_id")),
        capture_batch=_clean(row.get("capture_batch")),
        captured_at=_clean(row.get("captured_at")),
        captured_by=_clean(row.get("captured_by")),
        batch_id=_clean(row.get("batch_id")),
        row_id=_clean(row.get("row_id")),
        source_row_number=_positive_int(row.get("source_row_number")),
        row_fingerprint=_clean(row.get("row_fingerprint")),
    )
    return _normalize_relationship_lead(lead)


def _normalize_relationship_lead(lead: RelationshipLead) -> RelationshipLead:
    linkedin_url = lead.linkedin_url
    if linkedin_url and _is_http_url(linkedin_url) and _is_linkedin_url(linkedin_url):
        linkedin_url = _canonical_linkedin_url(linkedin_url)
    company_linkedin_url = lead.company_linkedin_url
    if company_linkedin_url and _is_http_url(company_linkedin_url) and _is_linkedin_url(
        company_linkedin_url
    ):
        company_linkedin_url = _canonical_linkedin_url(company_linkedin_url)
    return replace(
        lead,
        linkedin_url=linkedin_url,
        email=lead.email.lower(),
        company_linkedin_url=company_linkedin_url,
        priority=lead.priority.lower(),
    )


def _relationship_lead_csv_payload(lead: RelationshipLead) -> dict[str, str]:
    return {field: str(getattr(lead, field) or "") for field in RELATIONSHIP_LEAD_FIELDS}


def _relationship_lead_fingerprint(lead: RelationshipLead) -> str:
    payload = _relationship_lead_csv_payload(lead)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _relationship_identity_tokens(lead: RelationshipLead) -> set[str]:
    tokens: set[str] = set()
    if lead.linkedin_url:
        tokens.add(f"linkedin:{_canonical_url_identity(lead.linkedin_url)}")
    if lead.email:
        tokens.add(f"email:{lead.email.strip().lower()}")
    if lead.full_name and lead.company:
        tokens.add(
            f"name-company:{_identity_text(lead.full_name)}|{_identity_text(lead.company)}"
        )
    return tokens


def _relationship_dedupe_key(lead: RelationshipLead) -> str:
    tokens = _relationship_identity_tokens(lead)
    for prefix in ("linkedin:", "email:", "name-company:"):
        for token in sorted(tokens):
            if token.startswith(prefix):
                return token
    return f"row:{lead.source_row_number}"


def _relationship_review_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    statuses = [_clean(row.get("review_status")).lower() for row in rows]
    return {
        "rows_approved": statuses.count("approved"),
        "rows_rejected": statuses.count("rejected"),
        "rows_pending": len(statuses)
        - statuses.count("approved")
        - statuses.count("rejected"),
    }


def _load_relationship_decision_artifact(
    path: Path,
    *,
    rows: list[dict[str, str]],
    stage_manifest: dict[str, object],
    expected_staged_sha256: str,
) -> tuple[set[str], set[str], dict[str, object]]:
    path = path.resolve()
    if not path.is_file():
        raise RelationshipLeadReviewError(
            f"Relationship review decision artifact not found: {path}"
        )
    payload = _load_json_object_without_duplicate_keys(path)
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != RELATIONSHIP_DECISION_SCHEMA_VERSION
    ):
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact schema_version must be 1"
        )

    def row_ids(field: str) -> set[str]:
        raw = payload.get(field)
        if not isinstance(raw, list):
            raise RelationshipLeadReviewError(
                f"Relationship review decision artifact {field} must be a JSON list"
            )
        values: list[str] = []
        for value in raw:
            if not isinstance(value, str) or not _clean(value):
                raise RelationshipLeadReviewError(
                    f"Relationship review decision artifact {field} contains a blank or non-string row ID"
                )
            values.append(_clean(value))
        if len(values) != len(set(values)):
            raise RelationshipLeadReviewError(
                f"Relationship review decision artifact {field} contains duplicate row IDs"
            )
        return set(values)

    def count(field: str) -> int:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RelationshipLeadReviewError(
                f"Relationship review decision artifact {field} must be a non-negative integer"
            )
        return value

    approved = row_ids("approved_row_ids")
    rejected = row_ids("rejected_row_ids")
    if approved & rejected:
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact assigns rows to both approved and rejected"
        )

    staged_row_ids = [_clean(row.get("row_id")) for row in rows]
    if any(not row_id for row_id in staged_row_ids) or len(staged_row_ids) != len(
        set(staged_row_ids)
    ):
        raise RelationshipLeadReviewError(
            "Staged relationship rows must have unique, non-empty row IDs"
        )
    staged_row_id_set = set(staged_row_ids)
    decided = approved | rejected
    if decided != staged_row_id_set:
        missing = sorted(staged_row_id_set - decided)
        unknown = sorted(decided - staged_row_id_set)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact must partition every staged row exactly once"
            + (": " + "; ".join(details) if details else "")
        )

    rows_total = count("rows_total")
    rows_approved = count("rows_approved")
    rows_rejected = count("rows_rejected")
    if rows_total != len(rows) or rows_approved != len(approved) or rows_rejected != len(
        rejected
    ):
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact counts do not match its row-ID partition"
        )
    if rows_approved + rows_rejected != rows_total:
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact counts are not a complete partition"
        )

    batch_id = _clean(payload.get("batch_id"))
    source_sha256 = _clean(payload.get("source_sha256"))
    staged_sha256 = _clean(payload.get("staged_sha256"))
    if batch_id != _clean(stage_manifest.get("batch_id")):
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact batch_id does not match the stage manifest"
        )
    if source_sha256 != _clean(stage_manifest.get("source_sha256")):
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact source_sha256 does not match the original capture"
        )
    if staged_sha256 != expected_staged_sha256:
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact staged_sha256 does not match the staged CSV it was prepared for"
        )

    binding: dict[str, object] = {
        "path": str(path),
        "sha256": _file_sha256(path),
        "schema_version": schema_version,
        "batch_id": batch_id,
        "source_sha256": source_sha256,
        "staged_sha256": staged_sha256,
        "rows_total": rows_total,
        "rows_approved": rows_approved,
        "rows_rejected": rows_rejected,
    }
    return approved, rejected, binding


def _load_json_object_without_duplicate_keys(path: Path) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise RelationshipLeadReviewError(
                    f"Relationship review decision artifact contains duplicate JSON key {key!r}"
                )
            payload[key] = value
        return payload

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise RelationshipLeadReviewError(
            f"Relationship review decision artifact is invalid JSON: {path}"
        ) from exc
    except OSError as exc:
        raise RelationshipLeadReviewError(
            f"Relationship review decision artifact is unreadable: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise RelationshipLeadReviewError(
            "Relationship review decision artifact must contain one JSON object"
        )
    return payload


def _assert_stage_manifest(staged_path: Path, rows: list[dict[str, str]]) -> dict[str, object]:
    manifest_path = relationship_leads_manifest_path(staged_path)
    if not manifest_path.exists():
        raise RelationshipLeadReviewError(f"Stage manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RelationshipLeadReviewError(f"Stage manifest is unreadable: {manifest_path}") from exc
    batch_ids = {_clean(row.get("batch_id")) for row in rows if _clean(row.get("batch_id"))}
    if len(batch_ids) != 1 or next(iter(batch_ids), "") != _clean(manifest.get("batch_id")):
        raise RelationshipLeadReviewError("Staged rows do not match the stage manifest batch_id")
    source_path = Path(str(manifest.get("source_path") or ""))
    if not source_path.exists():
        raise RelationshipLeadReviewError(f"Original capture CSV is missing: {source_path}")
    if _file_sha256(source_path) != _clean(manifest.get("source_sha256")):
        raise RelationshipLeadReviewError(
            "Original capture CSV changed after staging; restage before reviewing/importing"
        )
    return manifest


def _assert_relationship_review_gate(
    staged_path: Path,
    rows: list[dict[str, str]],
    leads: list[RelationshipLead],
) -> None:
    stage_manifest = _assert_stage_manifest(staged_path, rows)
    review_manifest_path = relationship_leads_review_manifest_path(staged_path)
    if not review_manifest_path.exists():
        raise RelationshipLeadReviewError(
            f"Review manifest not found: {review_manifest_path}; record explicit review decisions first"
        )
    try:
        review_manifest = json.loads(review_manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RelationshipLeadReviewError(
            f"Review manifest is unreadable: {review_manifest_path}"
        ) from exc
    if _clean(review_manifest.get("staged_sha256")) != _file_sha256(staged_path):
        raise RelationshipLeadReviewError(
            "Staged CSV changed after review; restage or record review decisions again"
        )
    _assert_relationship_decision_artifact_binding(
        review_manifest,
        stage_manifest=stage_manifest,
        rows=rows,
    )

    issues = validate_relationship_leads(leads)
    issue_rows = {issue.row_number for issue in issues}
    errors: list[str] = []
    leads_by_row_id = {lead.row_id: lead for lead in leads}
    for row in rows:
        row_id = _clean(row.get("row_id"))
        status = _clean(row.get("review_status")).lower()
        validation_status = _clean(row.get("validation_status")).lower()
        lead = leads_by_row_id.get(row_id)
        if lead is None:
            errors.append(f"unknown staged row {row_id or '(blank)'}")
            continue
        if status not in {"approved", "rejected"}:
            errors.append(f"{row_id} is still pending review")
        reviewed_at = _clean(row.get("reviewed_at"))
        if not _clean(row.get("reviewed_by")) or not reviewed_at:
            errors.append(f"{row_id} lacks reviewer provenance")
        elif not _is_iso_datetime(reviewed_at):
            errors.append(f"{row_id} has an invalid reviewed_at timestamp")
        expected_validation = "blocked" if lead.source_row_number in issue_rows else "ready"
        if validation_status != expected_validation:
            errors.append(f"{row_id} validation status no longer matches its data")
        if status == "approved" and expected_validation != "ready":
            errors.append(f"{row_id} is approved despite validation errors")
        if _relationship_lead_fingerprint(lead) != _clean(row.get("row_fingerprint")):
            errors.append(f"{row_id} changed after staging")
    if errors:
        raise RelationshipLeadReviewError("Review gate blocked import: " + "; ".join(errors))


def _assert_relationship_decision_artifact_binding(
    review_manifest: dict[str, object],
    *,
    stage_manifest: dict[str, object],
    rows: list[dict[str, str]],
) -> None:
    raw_binding = review_manifest.get("decision_artifact")
    if raw_binding is None:
        return
    if not isinstance(raw_binding, dict):
        raise RelationshipLeadReviewError(
            "Review manifest decision_artifact binding is malformed"
        )
    decision_path = Path(_clean(raw_binding.get("path")))
    expected_staged_sha256 = _clean(raw_binding.get("staged_sha256"))
    if not decision_path.is_absolute() or not expected_staged_sha256:
        raise RelationshipLeadReviewError(
            "Review manifest decision_artifact binding lacks an absolute path or staged SHA"
        )
    approved, rejected, actual_binding = _load_relationship_decision_artifact(
        decision_path,
        rows=rows,
        stage_manifest=stage_manifest,
        expected_staged_sha256=expected_staged_sha256,
    )
    for key in (
        "path",
        "sha256",
        "schema_version",
        "batch_id",
        "source_sha256",
        "staged_sha256",
        "rows_total",
        "rows_approved",
        "rows_rejected",
    ):
        if raw_binding.get(key) != actual_binding.get(key):
            raise RelationshipLeadReviewError(
                f"Review manifest decision_artifact binding changed for {key}"
            )
    actual_approved = {
        _clean(row.get("row_id"))
        for row in rows
        if _clean(row.get("review_status")).lower() == "approved"
    }
    actual_rejected = {
        _clean(row.get("row_id"))
        for row in rows
        if _clean(row.get("review_status")).lower() == "rejected"
    }
    if actual_approved != approved or actual_rejected != rejected:
        raise RelationshipLeadReviewError(
            "Staged review decisions do not match the bound decision artifact"
        )


def _relationship_workbook_conflicts(
    workbook: OutreachWorkbook,
    leads: list[RelationshipLead],
    organizations: list[OrganizationRecord],
    contacts: list[ContactRecord],
) -> list[str]:
    conflicts: list[str] = []
    for lead in leads:
        org_matches = _matching_organizations(workbook, lead, organizations)
        if len(org_matches) > 1:
            conflicts.append(
                f"row {lead.source_row_number}: company matches multiple organizations "
                + ",".join(item.organization_id for item in org_matches)
            )
            continue
        org_id = (
            org_matches[0].organization_id
            if org_matches
            else workbook.make_organization_id(lead.company)
        )
        contact_matches = _matching_contacts(lead, org_id, contacts)
        if len(contact_matches) > 1:
            conflicts.append(
                f"row {lead.source_row_number}: person matches multiple contacts "
                + ",".join(item.contact_id for item in contact_matches)
            )
        elif contact_matches and contact_matches[0].organization_id != org_id:
            conflicts.append(
                f"row {lead.source_row_number}: person locator already belongs to "
                f"{contact_matches[0].organization_id}"
            )
    return conflicts


def _matching_organizations(
    workbook: OutreachWorkbook,
    lead: RelationshipLead,
    organizations: list[OrganizationRecord],
) -> list[OrganizationRecord]:
    expected_id = workbook.make_organization_id(lead.company)
    company_name = _identity_text(lead.company)
    website = _canonical_url_identity(lead.company_website) if lead.company_website else ""
    linkedin = (
        _canonical_url_identity(lead.company_linkedin_url)
        if lead.company_linkedin_url
        else ""
    )
    matches: dict[str, OrganizationRecord] = {}
    for organization in organizations:
        if (
            organization.organization_id == expected_id
            or _identity_text(organization.name) == company_name
            or (
                website
                and organization.website
                and _canonical_url_identity(organization.website) == website
            )
            or (
                linkedin
                and organization.linkedin_url
                and _canonical_url_identity(organization.linkedin_url) == linkedin
            )
        ):
            matches[organization.organization_id] = organization
    return list(matches.values())


def _matching_contacts(
    lead: RelationshipLead,
    organization_id: str,
    contacts: list[ContactRecord],
) -> list[ContactRecord]:
    linkedin = _canonical_url_identity(lead.linkedin_url) if lead.linkedin_url else ""
    email = lead.email.strip().lower()
    full_name = _identity_text(lead.full_name)
    matches: dict[str, ContactRecord] = {}
    for contact in contacts:
        if (
            (
                linkedin
                and contact.linkedin_url
                and _canonical_url_identity(contact.linkedin_url) == linkedin
            )
            or (email and contact.email and contact.email.strip().lower() == email)
            or (
                contact.organization_id == organization_id
                and _identity_text(contact.full_name) == full_name
            )
        ):
            matches[contact.contact_id] = contact
    return list(matches.values())


def _batch_source_kind(leads: list[RelationshipLead]) -> SourceKind:
    source_kinds = {_source_kind_for_lead(lead.source_type) for lead in leads}
    return next(iter(source_kinds)) if len(source_kinds) == 1 else SourceKind.MANUAL


def _is_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def _is_linkedin_url(value: str) -> bool:
    try:
        hostname = (urlsplit(value).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "linkedin.com" or hostname.endswith(".linkedin.com")


def _is_linkedin_person_url(value: str) -> bool:
    if not _is_linkedin_url(value):
        return False
    path = urlsplit(value).path.lower().rstrip("/")
    return path.startswith("/in/") or path.startswith("/pub/")


def _canonical_linkedin_url(value: str) -> str:
    parts = urlsplit(value)
    hostname = (parts.hostname or "linkedin.com").lower()
    if hostname == "linkedin.com" or hostname.endswith(".linkedin.com"):
        hostname = "www.linkedin.com"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", hostname, path, "", ""))


def _canonical_url_identity(value: str) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value.strip().lower()
    hostname = (parts.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    path = parts.path.rstrip("/").lower()
    return f"{hostname}{path}"


def _identity_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_csv_atomic(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, object] | dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    with temporary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary_path.replace(path)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _is_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _positive_int(value: object) -> int:
    try:
        parsed = int(str(value or "0"))
    except ValueError:
        return 0
    return parsed if parsed > 0 else 0


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
    return f"school-{_target_list_slug(value)}"


def _program_tag(value: str) -> str:
    return f"program-{_target_list_slug(value)}"


def _target_list_slug(value: str) -> str:
    """Return one delimiter-safe target-list token for human-entered labels."""

    return re.sub(r"[^a-z0-9]+", "-", _clean(value).casefold()).strip("-")


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

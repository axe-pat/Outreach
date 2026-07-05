from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from outreach.company_enrichment import format_notes_parts, parse_notes_parts
from outreach.tracking import DiscoverySourceRecord, OrganizationRecord, OrganizationType, OutreachWorkbook, SourceKind


DEFAULT_STORY_FIT_TARGETS_PATH = Path("workspace/story_fit_targets.csv")

STORY_FIT_TARGET_FIELDS = [
    "company",
    "website",
    "story_cluster",
    "story_angle",
    "tags",
    "description",
    "why_this_company",
    "why_you_have_a_case",
    "profile_evidence",
    "target_roles",
    "priority",
    "organization_type",
    "team_size",
    "city",
    "source_url",
    "verification_status",
]


@dataclass(frozen=True)
class StoryFitTarget:
    company: str
    website: str = ""
    story_cluster: str = ""
    story_angle: str = ""
    tags: str = ""
    description: str = ""
    why_this_company: str = ""
    why_you_have_a_case: str = ""
    profile_evidence: str = ""
    target_roles: str = ""
    priority: str = "wishlist"
    organization_type: str = OrganizationType.COMPANY.value
    team_size: str = ""
    city: str = ""
    source_url: str = ""
    verification_status: str = "manual_seed"


def import_story_fit_targets(
    workbook_dir: Path,
    *,
    source_path: Path = DEFAULT_STORY_FIT_TARGETS_PATH,
    execute: bool = False,
) -> dict[str, int | str]:
    workbook = OutreachWorkbook(workbook_dir)
    workbook.initialize()
    source_path = source_path.resolve()
    targets = load_story_fit_targets(source_path)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    source_id = workbook.make_source_id("story-fit-targets", str(source_path))

    added = 0
    updated = 0
    skipped = 0
    existing_by_id = {item.organization_id: item for item in workbook.list_organizations()}

    if execute:
        workbook.upsert_source(
            DiscoverySourceRecord(
                source_id=source_id,
                label="Story-fit target catalog",
                source_kind=SourceKind.MANUAL,
                base_url=str(source_path),
                extraction_method="curated_csv_import",
                owner="outreach-engine",
                last_run_at=now,
                notes="Companies selected because Akshat has a real pitch, not because a role was posted.",
            )
        )

    for target in targets:
        if not target.company:
            skipped += 1
            continue
        org_id = workbook.make_organization_id(target.company)
        existing = existing_by_id.get(org_id)
        notes = _story_fit_notes(target)
        target_lists = _story_fit_target_lists(target)

        if not execute:
            if existing:
                updated += 1
            else:
                added += 1
            continue

        if existing:
            freeform, metadata = parse_notes_parts(existing.notes)
            _, story_metadata = parse_notes_parts(notes)
            merged_metadata = {**metadata, **story_metadata}
            merged_metadata["tags"] = _merge_csv(metadata.get("tags", ""), story_metadata.get("tags", ""))
            if metadata.get("context_confidence") == "external_verified":
                for key in (
                    "context_source",
                    "context_confidence",
                    "context_evidence_url",
                    "context_enriched_at",
                    "context_refresh_after",
                    "prestige_signals",
                    "prestige_evidence_url",
                    "description",
                    "team_size",
                ):
                    if metadata.get(key):
                        merged_metadata[key] = metadata[key]
            workbook.update_organization(
                org_id,
                target_lists=_merge_semicolon(existing.target_lists, target_lists),
                status=existing.status or "Story-fit target",
                city=existing.city or target.city,
                website=existing.website or target.website,
                source_kind=getattr(existing.source_kind, "value", existing.source_kind) or SourceKind.MANUAL.value,
                source_url=existing.source_url or target.source_url or target.website,
                notes=format_notes_parts(freeform or ["Story-fit target"], merged_metadata),
                last_updated_at=now,
            )
            updated += 1
            continue

        workbook.upsert_organization(
            OrganizationRecord(
                organization_id=org_id,
                name=target.company,
                organization_type=_organization_type(target.organization_type),
                target_lists=target_lists,
                status="Story-fit target",
                city=target.city,
                website=target.website,
                source_kind=SourceKind.MANUAL,
                source_url=target.source_url or target.website,
                discovered_at=now,
                last_updated_at=now,
                notes=notes,
            )
        )
        added += 1

    return {
        "source_path": str(source_path),
        "count": len(targets),
        "added": added,
        "updated": updated,
        "skipped": skipped,
    }


def load_story_fit_targets(source_path: Path) -> list[StoryFitTarget]:
    if not source_path.exists():
        raise FileNotFoundError(f"Story-fit targets file not found: {source_path}")
    with source_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [
            StoryFitTarget(
                company=_clean(row.get("company")),
                website=_clean(row.get("website")),
                story_cluster=_clean(row.get("story_cluster")),
                story_angle=_clean(row.get("story_angle")) or _clean(row.get("story_cluster")),
                tags=_clean(row.get("tags")),
                description=_clean(row.get("description")),
                why_this_company=_clean(row.get("why_this_company")) or _clean(row.get("why_you_have_a_case")),
                why_you_have_a_case=_clean(row.get("why_you_have_a_case")) or _clean(row.get("why_this_company")),
                profile_evidence=_clean(row.get("profile_evidence")),
                target_roles=_clean(row.get("target_roles")),
                priority=_clean(row.get("priority")) or "wishlist",
                organization_type=_clean(row.get("organization_type")) or OrganizationType.COMPANY.value,
                team_size=_clean(row.get("team_size")),
                city=_clean(row.get("city")),
                source_url=_clean(row.get("source_url")),
                verification_status=_clean(row.get("verification_status")) or "manual_seed",
            )
            for row in reader
        ]


def _story_fit_target_lists(target: StoryFitTarget) -> str:
    parts = ["story-fit", "track-2", "relationship"]
    if target.story_cluster:
        parts.append(target.story_cluster)
    if target.priority in {"priority", "core", "dream", "tier-a"}:
        parts.append(target.priority)
    elif target.priority:
        parts.append("wishlist")
    return _merge_semicolon(*parts)


def _story_fit_notes(target: StoryFitTarget) -> str:
    _, metadata = parse_notes_parts("")
    why_this_company = target.why_this_company or target.why_you_have_a_case
    story_angle = target.story_angle or target.story_cluster
    priority = target.priority
    metadata["source"] = "story_fit_targets"
    metadata["seed_source"] = "story_fit_targets"
    metadata["context_source"] = "manual_story_fit_catalog"
    metadata["context_confidence"] = target.verification_status or "manual_seed"
    metadata["context_evidence_url"] = target.source_url or target.website
    metadata["why_this_company"] = why_this_company
    metadata["story_angle"] = story_angle
    metadata["priority"] = priority
    metadata["story_cluster"] = target.story_cluster
    metadata["story_fit_reason"] = why_this_company
    metadata["profile_evidence"] = target.profile_evidence
    metadata["target_roles"] = target.target_roles
    metadata["manual_priority"] = priority
    metadata["tags"] = target.tags
    metadata["description"] = target.description
    metadata["team_size"] = target.team_size
    return format_notes_parts(["Story-fit target"], metadata)


def _organization_type(value: str) -> OrganizationType:
    normalized = value.strip().lower()
    for item in OrganizationType:
        if item.value == normalized:
            return item
    return OrganizationType.COMPANY


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
    return " ".join(str(value or "").split()).strip()

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def stable_suffix(value: str, length: int = 10) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


class OrganizationType(str, Enum):
    COMPANY = "company"
    STARTUP = "startup"
    INCUBATOR = "incubator"
    ACCELERATOR = "accelerator"
    HACKER_HOUSE = "hacker_house"
    UNIVERSITY = "university"
    RESEARCH_LAB = "research_lab"
    COMMUNITY = "community"
    INVESTOR = "investor"
    OTHER = "other"


class OpportunityType(str, Enum):
    INTERNSHIP = "internship"
    FULL_TIME = "full_time"
    RESEARCH = "research"
    RESIDENCY = "residency"
    FELLOWSHIP = "fellowship"
    PROJECT = "project"
    ENTREPRENEURSHIP = "entrepreneurship"
    NETWORKING = "networking"
    OTHER = "other"


class SourceKind(str, Enum):
    MANUAL = "manual"
    LINKEDIN = "linkedin"
    LINKEDIN_JOB = "linkedin_job"
    JOB_BOARD = "job_board"
    STARTUP_DIRECTORY = "startup_directory"
    YC_DIRECTORY = "yc_directory"
    UNIVERSITY_DIRECTORY = "university_directory"
    X = "x"
    EMAIL = "email"
    OTHER = "other"


class OutreachChannel(str, Enum):
    LINKEDIN = "linkedin"
    EMAIL = "email"
    X = "x"
    PHONE = "phone"
    REFERRAL = "referral"
    OTHER = "other"


class OrganizationRecord(BaseModel):
    organization_id: str
    name: str
    organization_type: OrganizationType = OrganizationType.COMPANY
    target_lists: str = ""
    status: str = "New"
    city: str = ""
    website: str = ""
    linkedin_url: str = ""
    source_kind: SourceKind = SourceKind.MANUAL
    source_url: str = ""
    discovered_at: str = Field(default_factory=utc_now_iso)
    last_updated_at: str = Field(default_factory=utc_now_iso)
    notes: str = ""


class OpportunityRecord(BaseModel):
    opportunity_id: str
    organization_id: str
    title: str
    opportunity_type: OpportunityType = OpportunityType.OTHER
    target_lists: str = ""
    location: str = ""
    status: str = "Discovered"
    source_kind: SourceKind = SourceKind.MANUAL
    source_url: str = ""
    discovered_at: str = Field(default_factory=utc_now_iso)
    compensation_hint: str = ""
    notes: str = ""


class ContactRecord(BaseModel):
    contact_id: str
    organization_id: str
    full_name: str
    title: str = ""
    contact_type: str = ""
    target_lists: str = ""
    preferred_channel: OutreachChannel = OutreachChannel.LINKEDIN
    status: str = "Discovered"
    linkedin_url: str = ""
    email: str = ""
    source_kind: SourceKind = SourceKind.MANUAL
    source_url: str = ""
    discovered_at: str = Field(default_factory=utc_now_iso)
    last_contacted_at: str = ""
    notes: str = ""


class TouchpointRecord(BaseModel):
    touchpoint_id: str
    organization_id: str
    contact_id: str = ""
    channel: OutreachChannel = OutreachChannel.LINKEDIN
    status: str = "Draft"
    message_kind: str = "outreach"
    message_text: str
    recorded_at: str = Field(default_factory=utc_now_iso)
    sent_at: str = ""
    source_artifact: str = ""
    notes: str = ""


class DiscoverySourceRecord(BaseModel):
    source_id: str
    label: str
    source_kind: SourceKind = SourceKind.MANUAL
    base_url: str = ""
    extraction_method: str = ""
    owner: str = ""
    last_run_at: str = ""
    notes: str = ""


class LinkedInImportSummary(BaseModel):
    organization_id: str
    contacts_added: int
    touchpoints_added: int
    source_id: str


class OutreachWorkbook:
    TABLE_MODELS = {
        "organizations": OrganizationRecord,
        "opportunities": OpportunityRecord,
        "contacts": ContactRecord,
        "touchpoints": TouchpointRecord,
        "sources": DiscoverySourceRecord,
    }

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def initialize(self) -> dict[str, Path]:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        paths: dict[str, Path] = {}
        for table_name in self.TABLE_MODELS:
            target = self.table_path(table_name)
            if not target.exists():
                with target.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=self._fieldnames(table_name))
                    writer.writeheader()
            paths[table_name] = target
        return paths

    def summary_counts(self) -> dict[str, int]:
        self.initialize()
        return {table_name: len(self._read_rows(table_name)) for table_name in self.TABLE_MODELS}

    def table_path(self, table_name: str) -> Path:
        return self.base_dir / f"{table_name}.csv"

    def make_organization_id(self, name: str) -> str:
        return f"org-{slugify(name)}"

    def make_contact_id(
        self,
        organization_id: str,
        full_name: str,
        linkedin_url: str = "",
        email: str = "",
    ) -> str:
        seed = linkedin_url or email or full_name
        return f"ct-{slugify(organization_id)}-{slugify(seed)}"

    def make_opportunity_id(self, organization_id: str, title: str, source_url: str = "") -> str:
        suffix = stable_suffix(f"{organization_id}|{title}|{source_url}", length=6)
        return f"opp-{slugify(organization_id)}-{slugify(title)}-{suffix}"

    def make_source_id(self, label: str, base_url: str = "") -> str:
        seed = label or base_url or "source"
        return f"src-{slugify(seed)}"

    def make_touchpoint_id(
        self,
        organization_id: str,
        contact_id: str,
        channel: str,
        message_text: str,
        source_artifact: str = "",
    ) -> str:
        seed = "|".join([organization_id, contact_id, channel, message_text.strip(), source_artifact])
        return f"tp-{stable_suffix(seed, length=12)}"

    def upsert_organization(self, record: OrganizationRecord) -> tuple[OrganizationRecord, bool]:
        return self._create_or_get(
            "organizations",
            record,
            lambda row: self._same_organization(row, record),
        )

    def upsert_opportunity(self, record: OpportunityRecord) -> tuple[OpportunityRecord, bool]:
        return self._create_or_get(
            "opportunities",
            record,
            lambda row: row.get("opportunity_id") == record.opportunity_id
            or (
                row.get("organization_id") == record.organization_id
                and self._normalize(row.get("title")) == self._normalize(record.title)
                and self._normalize(row.get("source_url")) == self._normalize(record.source_url)
            ),
        )

    def upsert_contact(self, record: ContactRecord) -> tuple[ContactRecord, bool]:
        return self._create_or_get(
            "contacts",
            record,
            lambda row: self._same_contact(row, record),
        )

    def append_touchpoint(self, record: TouchpointRecord) -> tuple[TouchpointRecord, bool]:
        return self._create_or_get(
            "touchpoints",
            record,
            lambda row: row.get("touchpoint_id") == record.touchpoint_id,
        )

    def upsert_source(self, record: DiscoverySourceRecord) -> tuple[DiscoverySourceRecord, bool]:
        return self._create_or_get(
            "sources",
            record,
            lambda row: row.get("source_id") == record.source_id
            or self._normalize(row.get("label")) == self._normalize(record.label),
        )

    def import_linkedin_artifact(
        self,
        artifact_path: Path,
        target_lists: str = "referrals;linkedin",
        organization_type: OrganizationType = OrganizationType.COMPANY,
        touchpoint_status: str = "Draft",
    ) -> LinkedInImportSummary:
        self.initialize()
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        company = str(payload.get("company") or "").strip()
        if not company:
            raise ValueError("Artifact does not include a company field.")

        source_url = self._first_nonempty_pass_value(payload.get("pass_summaries", []), "final_url")
        discovered_at = self._artifact_timestamp_or_now(artifact_path)
        organization, _ = self.upsert_organization(
            OrganizationRecord(
                organization_id=self.make_organization_id(company),
                name=company,
                organization_type=organization_type,
                target_lists=target_lists,
                status="Researching",
                source_kind=SourceKind.LINKEDIN,
                source_url=source_url,
                discovered_at=discovered_at,
                last_updated_at=discovered_at,
                notes="Imported from LinkedIn outreach artifact.",
            )
        )

        source, _ = self.upsert_source(
            DiscoverySourceRecord(
                source_id=self.make_source_id(f"linkedin-{company}"),
                label=f"LinkedIn people search for {company}",
                source_kind=SourceKind.LINKEDIN,
                base_url="https://www.linkedin.com/search/results/people/",
                extraction_method="artifact_import",
                owner="outreach-engine",
                last_run_at=discovered_at,
                notes=f"Imported from {artifact_path.name}",
            )
        )

        contacts_added = 0
        touchpoints_added = 0
        for candidate in payload.get("results", []):
            full_name = str(candidate.get("name") or "").strip()
            if not full_name:
                continue

            linkedin_url = str(candidate.get("linkedin_url") or "").strip()
            contact, created = self.upsert_contact(
                ContactRecord(
                    contact_id=self.make_contact_id(
                        organization.organization_id,
                        full_name,
                        linkedin_url=linkedin_url,
                        email=str(candidate.get("email") or "").strip(),
                    ),
                    organization_id=organization.organization_id,
                    full_name=full_name,
                    title=str(candidate.get("title") or "").strip(),
                    contact_type=str(candidate.get("role_bucket") or "").strip(),
                    target_lists=target_lists,
                    preferred_channel=OutreachChannel.LINKEDIN,
                    status="Warm" if candidate.get("existing_connection") else "Queued",
                    linkedin_url=linkedin_url,
                    source_kind=SourceKind.LINKEDIN,
                    source_url=source_url,
                    discovered_at=discovered_at,
                    notes=self._contact_notes(candidate),
                )
            )
            if created:
                contacts_added += 1

            note_text = str(candidate.get("note") or "").strip()
            if not note_text:
                continue

            touchpoint, created = self.append_touchpoint(
                TouchpointRecord(
                    touchpoint_id=self.make_touchpoint_id(
                        organization.organization_id,
                        contact.contact_id,
                        OutreachChannel.LINKEDIN.value,
                        note_text,
                        source_artifact=str(artifact_path),
                    ),
                    organization_id=organization.organization_id,
                    contact_id=contact.contact_id,
                    channel=OutreachChannel.LINKEDIN,
                    status=touchpoint_status,
                    message_kind=str(candidate.get("note_family") or "linkedin_note"),
                    message_text=note_text,
                    recorded_at=discovered_at,
                    source_artifact=str(artifact_path),
                    notes=self._touchpoint_notes(candidate),
                )
            )
            if created:
                touchpoints_added += 1

        return LinkedInImportSummary(
            organization_id=organization.organization_id,
            contacts_added=contacts_added,
            touchpoints_added=touchpoints_added,
            source_id=source.source_id,
        )

    def _create_or_get(
        self,
        table_name: str,
        record: BaseModel,
        matcher: Any,
    ) -> tuple[Any, bool]:
        self.initialize()
        model_cls = self.TABLE_MODELS[table_name]
        rows = self._read_rows(table_name)
        for row in rows:
            if matcher(row):
                return model_cls(**row), False
        rows.append(self._serialize_model(record))
        self._write_rows(table_name, rows)
        return record, True

    def _read_rows(self, table_name: str) -> list[dict[str, str]]:
        path = self.table_path(table_name)
        if not path.exists():
            return []
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            return list(reader)

    def _write_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        path = self.table_path(table_name)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self._fieldnames(table_name))
            writer.writeheader()
            writer.writerows(rows)

    def _fieldnames(self, table_name: str) -> list[str]:
        return list(self.TABLE_MODELS[table_name].model_fields.keys())

    def _serialize_model(self, record: BaseModel) -> dict[str, str]:
        payload = record.model_dump(mode="json")
        return {key: "" if value is None else str(value) for key, value in payload.items()}

    def _normalize(self, value: str | None) -> str:
        return (value or "").strip().lower()

    def _same_organization(self, row: dict[str, str], record: OrganizationRecord) -> bool:
        if row.get("organization_id") == record.organization_id:
            return True
        if self._normalize(row.get("website")) and self._normalize(row.get("website")) == self._normalize(record.website):
            return True
        if self._normalize(row.get("linkedin_url")) and self._normalize(row.get("linkedin_url")) == self._normalize(record.linkedin_url):
            return True
        return self._normalize(row.get("name")) == self._normalize(record.name)

    def _same_contact(self, row: dict[str, str], record: ContactRecord) -> bool:
        if row.get("contact_id") == record.contact_id:
            return True
        if self._normalize(row.get("linkedin_url")) and self._normalize(row.get("linkedin_url")) == self._normalize(record.linkedin_url):
            return True
        if self._normalize(row.get("email")) and self._normalize(row.get("email")) == self._normalize(record.email):
            return True
        return (
            row.get("organization_id") == record.organization_id
            and self._normalize(row.get("full_name")) == self._normalize(record.full_name)
        )

    def _artifact_timestamp_or_now(self, artifact_path: Path) -> str:
        prefix = artifact_path.stem.split("-", 2)
        if len(prefix) >= 2 and len(prefix[0]) == 8 and len(prefix[1]) == 6:
            stamp = f"{prefix[0]}{prefix[1]}"
            try:
                parsed = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
                return parsed.replace(microsecond=0).isoformat()
            except ValueError:
                pass
        return utc_now_iso()

    def _first_nonempty_pass_value(self, pass_summaries: Any, field_name: str) -> str:
        if not isinstance(pass_summaries, list):
            return ""
        for item in pass_summaries:
            if isinstance(item, dict):
                value = str(item.get(field_name) or "").strip()
                if value:
                    return value
        return ""

    def _contact_notes(self, candidate: dict[str, Any]) -> str:
        fragments: list[str] = []
        passes = candidate.get("passes") or []
        if passes:
            fragments.append(f"passes={','.join(str(item) for item in passes)}")
        if candidate.get("tier"):
            fragments.append(f"tier={candidate['tier']}")
        if candidate.get("priority_bucket"):
            fragments.append(f"priority={candidate['priority_bucket']}")
        triggers = candidate.get("triggers") or []
        if triggers:
            fragments.append(f"triggers={','.join(str(item) for item in triggers)}")
        return " | ".join(fragments)

    def _touchpoint_notes(self, candidate: dict[str, Any]) -> str:
        note_qc = candidate.get("note_qc") or {}
        if not isinstance(note_qc, dict):
            return ""
        verdict = str(note_qc.get("verdict") or "").strip()
        score = str(note_qc.get("score") or "").strip()
        flags = note_qc.get("flags") or []
        parts = []
        if verdict:
            parts.append(f"verdict={verdict}")
        if score:
            parts.append(f"score={score}")
        if flags:
            parts.append(f"flags={','.join(str(item) for item in flags)}")
        return " | ".join(parts)

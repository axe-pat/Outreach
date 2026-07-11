from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from outreach.company_enrichment import parse_notes_parts
from outreach.relationship_leads import RELATIONSHIP_LEAD_FIELDS


DEFAULT_PEOPLEGROVE_CURATED_PATH = Path(
    "workspace/relationship_leads_peoplegrove_curated.csv"
)
DEFAULT_MINIMUM_PEOPLEGROVE_SCORE = 70

_AT_COMPANY_PATTERN = re.compile(
    r"^(?P<title>[^|\u2022;]+?)\s+(?:at|@)\s+(?P<company>[^|\u2022;]+?)"
    r"(?:\s*(?:\||\u2022|;)\s*.+)?$",
    re.IGNORECASE,
)
_FOUNDER_COMPANY_PATTERN = re.compile(
    r"^(?P<title>[^|\u2022;]*(?:co[ -]?founder|founder|chief executive officer|\bceo\b)"
    r"[^|\u2022;]*?)"
    r"\s*(?:\||\u2014|\u2013|\s-\s|,)\s*(?P<company>[^|\u2022;]+?)"
    r"(?:\s*(?:\||\u2022|;)\s*.+)?$",
    re.IGNORECASE,
)
_STUDENT_TITLE_PATTERN = re.compile(
    r"\b(?:student|intern(?:ship)?|mba candidate|masters? candidate|"
    r"graduate assistant|research assistant|teaching assistant|undergraduate|"
    r"summer analyst)\b",
    re.IGNORECASE,
)
_JOB_SEEKER_PATTERN = re.compile(
    r"(?:\bopen to work\b|\bjob seeker\b|\bseeking\b|\blooking for\b.*"
    r"\b(?:role|job|opportunit)|\baspiring\b|\btransitioning into\b)",
    re.IGNORECASE,
)
_NON_CURRENT_PATTERN = re.compile(
    r"^(?:incoming|former|previous|ex[- ])\b|\b(?:formerly|worked at|retired)\b",
    re.IGNORECASE,
)
_DESCRIPTIVE_TITLE_TAGLINE_PATTERN = re.compile(
    r":\s*(?:spearheading|driving|building|helping|transforming|empowering|"
    r"creating|innovating)\b",
    re.IGNORECASE,
)
_LEADERSHIP_PATTERN = re.compile(
    r"\b(?:senior|sr\.?|principal|group|lead|leader|head|director|"
    r"senior vice president|vice president|svp|evp|vp|chief|general manager|gm)\b",
    re.IGNORECASE,
)

_COMPANY_BLOCKLIST = {
    "a company",
    "confidential",
    "freelance",
    "independent",
    "investor",
    "life coach",
    "llc",
    "mba",
    "multiple companies",
    "private",
    "product lead",
    "self employed",
    "self-employed",
    "startup",
    "startups",
    "stealth",
    "stealth mode",
    "stealth startup",
    "tech",
    "technology",
    "various",
}

# Attach subsidiaries and legacy brands to the canonical account already used
# by the Outreach workbook. The exact source headline remains in notes.
_CANONICAL_COMPANY_ALIASES = {
    "apollomed-nasdaq-ameh": "ApolloMed",
    "amazon-web-services": "Amazon",
    "amazon-web-services-aws": "Amazon",
    "aws": "Amazon",
    "facebook": "Meta",
    "snowflake": "Snowflake",
    "the-boeing-company": "Boeing",
    "youtube": "Google",
}

_COMPANY_DESCRIPTION_PATTERN = re.compile(
    r"(?:\b(?:years? experience|usc alumni|mba candidate|retired|formerly|worked at)\b|"
    r"^(?:ex[- ]|former\b)|@.+@|"
    r"\b(?:co-owner|co[ -]?founder|product lead|life coach|career coach|"
    r"orthopedic surgeon|strategic advisor|consulting executive)\b|"
    r",\s+(?:an?\s+.+\s+company|an?\s+.+\s+fund\b|"
    r"(?:a\s+)?(?:cre|private equity|business development)\b))",
    re.IGNORECASE,
)
_POSSIBLE_LOW_ALIGNMENT_PATTERN = re.compile(
    r"\b(?:artist|arts|brew(?:ing)?|caregiving|career coach|church|clinical|"
    r"creative consultant|film|holistic defense|marketing|mortgage|music|"
    r"nonprofit|psychotherapist|recordings?|social work|speech therapy|"
    r"theatre|transit authority)\b",
    re.IGNORECASE,
)
_IRRELEVANT_PRODUCT_TITLE_PATTERN = re.compile(
    r"\b(?:food safety|product design(?:er)?|product engineer(?:ing)?|"
    r"product marketing|scrum product owner)\b|\bproduct manager\b.*\bmarketing\b",
    re.IGNORECASE,
)
_IRRELEVANT_PROGRAM_TITLE_PATTERN = re.compile(
    r"\b(?:collaborative care|holistic defense|incentives program|land planner|"
    r"social services?|talent development program)\b",
    re.IGNORECASE,
)
_IRRELEVANT_FOUNDER_CONTEXT_PATTERN = re.compile(
    r"(?:\b(?:brand strategist|corporate gerontologist|creative consultant|"
    r"psychotherapist)\b|\b(?:arts|brand studio|caregiving club|hollywood resumes|"
    r"brew(?:ing)?|marketing|mayor's fund|media group|productions?|recordings?|speech therapy|"
    r"studios?)\b|[a-z]*artists?\b)",
    re.IGNORECASE,
)


class PeopleGroveCurationError(ValueError):
    """Raised when a browser capture cannot be safely curated."""


class _EnrichmentDecisionFields(TypedDict, total=False):
    enrichment_title: str
    enrichment_company: str
    enrichment_date_range: str
    enrichment_location: str
    enrichment_mapping_key: str
    enrichment_source_record_id: str
    enrichment_source_url: str
    enrichment_captured_at: str
    enrichment_captured_by: str
    enrichment_artifact_sha256: str


@dataclass(frozen=True)
class PeopleGroveProfile:
    input_index: int
    full_name: str
    headline: str
    program: str
    grad_year: str
    member_type: str
    source_url: str
    source_record_id: str
    queries: tuple[str, ...]
    labels: tuple[str, ...]


@dataclass(frozen=True)
class PeopleGroveCurrentRole:
    title: str
    company: str
    date_range: str = ""
    location: str = ""


@dataclass(frozen=True)
class PeopleGroveProfileEnrichment:
    mapping_key: str
    source_record_id: str
    source_url: str
    current_roles: tuple[PeopleGroveCurrentRole, ...]
    captured_at: str
    captured_by: str
    artifact_sha256: str


@dataclass(frozen=True)
class PeopleGroveEnrichmentArtifact:
    path: Path
    sha256: str
    source_capture_sha256: str
    captured_at: str
    captured_by: str
    records: tuple[PeopleGroveProfileEnrichment, ...]
    by_input_index: dict[int, PeopleGroveProfileEnrichment]


@dataclass(frozen=True)
class RoleClassification:
    category: str
    base_score: int
    contact_type: str
    target_list: str
    role_level: str


@dataclass(frozen=True)
class CurationDecision:
    profile: PeopleGroveProfile
    accepted: bool
    reason: str
    category: str = ""
    score: int = 0
    title: str = ""
    company: str = ""
    role_level: str = ""
    lead: dict[str, str] | None = None
    duplicate_of_input_index: int | None = None
    original_reason: str = ""
    company_fit_signal: str = ""
    review_flags: tuple[str, ...] = ()
    role_source: str = ""
    enrichment_title: str = ""
    enrichment_company: str = ""
    enrichment_date_range: str = ""
    enrichment_location: str = ""
    enrichment_mapping_key: str = ""
    enrichment_source_record_id: str = ""
    enrichment_source_url: str = ""
    enrichment_captured_at: str = ""
    enrichment_captured_by: str = ""
    enrichment_artifact_sha256: str = ""

    def audit_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "input_index": self.profile.input_index,
            "full_name": self.profile.full_name,
            "headline": self.profile.headline,
            "source_record_id": self.profile.source_record_id,
            "source_url": self.profile.source_url,
            "accepted": self.accepted,
            "reason": self.reason,
            "category": self.category,
            "score": self.score,
            "title": self.title,
            "company": self.company,
            "role_level": self.role_level,
            "role_source": self.role_source,
            "enrichment_title": self.enrichment_title,
            "enrichment_company": self.enrichment_company,
            "company_fit_signal": self.company_fit_signal,
            "review_flags": list(self.review_flags),
            "program": self.profile.program,
            "grad_year": self.profile.grad_year,
            "member_type": self.profile.member_type,
            "queries": list(self.profile.queries),
            "labels": list(self.profile.labels),
            "enrichment_date_range": self.enrichment_date_range,
            "enrichment_location": self.enrichment_location,
            "enrichment_mapping_key": self.enrichment_mapping_key,
            "enrichment_source_record_id": self.enrichment_source_record_id,
            "enrichment_source_url": self.enrichment_source_url,
            "enrichment_captured_at": self.enrichment_captured_at,
            "enrichment_captured_by": self.enrichment_captured_by,
            "enrichment_artifact_sha256": self.enrichment_artifact_sha256,
        }
        if self.duplicate_of_input_index is not None:
            payload["duplicate_of_input_index"] = self.duplicate_of_input_index
        if self.original_reason:
            payload["original_reason"] = self.original_reason
        return payload


@dataclass(frozen=True)
class ExistingPeopleGroveKeys:
    source_record_ids: frozenset[str] = frozenset()
    source_urls: frozenset[str] = frozenset()
    person_companies: frozenset[str] = frozenset()
    known_company_names: frozenset[str] = frozenset()


def curate_peoplegrove_capture(
    input_path: Path,
    *,
    output_path: Path = DEFAULT_PEOPLEGROVE_CURATED_PATH,
    summary_path: Path | None = None,
    enrichment_path: Path | None = None,
    workspace: Path | None = None,
    capture_batch: str = "",
    captured_by: str = "codex-peoplegrove-browser-capture",
    minimum_score: int = DEFAULT_MINIMUM_PEOPLEGROVE_SCORE,
) -> dict[str, object]:
    """Turn a browser-captured PeopleGrove JSON array into a review-gated lead CSV.

    This only writes the curated source CSV and its audit summary. It never imports
    contacts or changes the Outreach workbook.
    """
    if not 0 <= minimum_score <= 100:
        raise PeopleGroveCurationError("minimum_score must be between 0 and 100")
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    summary_path = (summary_path or output_path.with_suffix(".summary.json")).resolve()
    enrichment_path = enrichment_path.resolve() if enrichment_path is not None else None
    distinct_paths = {input_path, output_path, summary_path}
    if len(distinct_paths) != 3 or enrichment_path in distinct_paths:
        raise PeopleGroveCurationError(
            "input, enrichment, curated CSV, and summary paths must differ"
        )

    profiles = load_peoplegrove_capture(input_path)
    enrichment = (
        load_peoplegrove_enrichment(
            enrichment_path,
            capture_path=input_path,
            profiles=profiles,
        )
        if enrichment_path is not None
        else None
    )
    generated_at = datetime.now(UTC).isoformat()
    capture_batch = _clean(capture_batch) or (
        f"peoplegrove-{generated_at[:10]}-{_short_file_hash(input_path)}"
    )
    captured_by = _clean(captured_by) or "codex-peoplegrove-browser-capture"
    existing = load_existing_peoplegrove_keys(workspace) if workspace is not None else None

    preliminary = [
        _evaluate_profile(
            profile,
            captured_at=generated_at,
            capture_batch=capture_batch,
            captured_by=captured_by,
            minimum_score=minimum_score,
            enrichment=(
                enrichment.by_input_index.get(profile.input_index)
                if enrichment is not None
                else None
            ),
        )
        for profile in profiles
    ]
    decisions = _deduplicate_capture(preliminary)
    if existing is not None:
        decisions = [_dedupe_against_workspace(decision, existing) for decision in decisions]
    decisions = [_annotate_company_fit(decision, existing) for decision in decisions]

    accepted = [decision for decision in decisions if decision.accepted and decision.lead]
    rows = [decision.lead or {} for decision in accepted]
    _write_csv_atomic(output_path, RELATIONSHIP_LEAD_FIELDS, rows)

    reason_counts = Counter(
        decision.reason for decision in decisions if not decision.accepted
    )
    category_counts = Counter(
        decision.category for decision in accepted if decision.category
    )
    priority_counts = Counter(
        str((decision.lead or {}).get("priority") or "") for decision in accepted
    )
    query_coverage: dict[str, dict[str, int]] = {}
    for decision in decisions:
        for query in decision.profile.queries:
            counts = query_coverage.setdefault(
                query,
                {"rows_input": 0, "rows_accepted": 0, "rows_rejected": 0},
            )
            counts["rows_input"] += 1
            counts["rows_accepted" if decision.accepted else "rows_rejected"] += 1
    input_member_types = Counter(profile.member_type or "unknown" for profile in profiles)
    accepted_member_types = Counter(
        decision.profile.member_type or "unknown" for decision in accepted
    )
    review_flag_counts = Counter(
        flag for decision in accepted for flag in decision.review_flags
    )
    company_fit_counts = Counter(
        decision.company_fit_signal or "unclassified" for decision in accepted
    )
    summary: dict[str, object] = {
        "schema_version": 1,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "summary_path": str(summary_path),
        "enrichment_path": str(enrichment.path) if enrichment is not None else "",
        "enrichment_sha256": enrichment.sha256 if enrichment is not None else "",
        "enrichment_source_capture_sha256": (
            enrichment.source_capture_sha256 if enrichment is not None else ""
        ),
        "enrichment_captured_at": enrichment.captured_at if enrichment is not None else "",
        "enrichment_captured_by": enrichment.captured_by if enrichment is not None else "",
        "enrichment_records": len(enrichment.records) if enrichment is not None else 0,
        "enrichment_capture_rows_matched": (
            len(enrichment.by_input_index) if enrichment is not None else 0
        ),
        "roles_selected_from_enrichment": sum(
            decision.role_source == "peoplegrove_career_journey_enrichment"
            for decision in decisions
        ),
        "accepted_from_enrichment": sum(
            decision.accepted
            and decision.role_source == "peoplegrove_career_journey_enrichment"
            for decision in decisions
        ),
        "workspace_dedupe_path": str(workspace.resolve()) if workspace is not None else "",
        "generated_at": generated_at,
        "capture_batch": capture_batch,
        "captured_by": captured_by,
        "minimum_score": minimum_score,
        "rows_input": len(profiles),
        "rows_accepted": len(accepted),
        "rows_rejected": len(decisions) - len(accepted),
        "rejection_reasons": dict(sorted(reason_counts.items())),
        "accepted_categories": dict(sorted(category_counts.items())),
        "accepted_priorities": dict(sorted(priority_counts.items())),
        "query_coverage": dict(sorted(query_coverage.items())),
        "input_member_types": dict(sorted(input_member_types.items())),
        "accepted_member_types": dict(sorted(accepted_member_types.items())),
        "accepted_company_fit_signals": dict(sorted(company_fit_counts.items())),
        "accepted_review_flags": dict(sorted(review_flag_counts.items())),
        "rows_with_source_record_id": sum(bool(profile.source_record_id) for profile in profiles),
        "rows_with_source_url": sum(bool(profile.source_url) for profile in profiles),
        "manual_company_fit_review_required": True,
        "next_gate": "stage, inspect, and explicitly review rows before import",
        "decisions": [decision.audit_payload() for decision in decisions],
    }
    _write_json_atomic(summary_path, summary)
    return summary


def load_peoplegrove_capture(path: Path) -> list[PeopleGroveProfile]:
    if not path.exists():
        raise FileNotFoundError(f"PeopleGrove capture not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PeopleGroveCurationError(f"PeopleGrove capture is invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise PeopleGroveCurationError("PeopleGrove capture must be a JSON array")

    profiles: list[PeopleGroveProfile] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise PeopleGroveCurationError(
                f"PeopleGrove capture item {index} must be a JSON object"
            )
        profiles.append(
            PeopleGroveProfile(
                input_index=index,
                full_name=_clean(item.get("full_name")),
                headline=_clean(item.get("headline")),
                program=_clean(item.get("program")),
                grad_year=_clean(item.get("grad_year")),
                member_type=_clean(item.get("member_type")),
                source_url=_clean(item.get("source_url")),
                source_record_id=_clean(item.get("source_record_id")),
                queries=_string_tuple(item.get("queries")),
                labels=_string_tuple(item.get("labels")),
            )
        )
    return profiles


def load_peoplegrove_enrichment(
    path: Path,
    *,
    capture_path: Path,
    profiles: list[PeopleGroveProfile],
) -> PeopleGroveEnrichmentArtifact:
    """Load a capture-bound career-journey mapping without guessing identities."""

    if not path.exists():
        raise FileNotFoundError(f"PeopleGrove enrichment not found: {path}")
    payload = _load_json_without_duplicate_keys(path)
    if not isinstance(payload, dict):
        raise PeopleGroveCurationError("PeopleGrove enrichment must be a JSON object")
    expected_top_level = {
        "schema_version",
        "source_capture_sha256",
        "captured_at",
        "captured_by",
        "profiles",
    }
    if set(payload) != expected_top_level:
        missing = sorted(expected_top_level - set(payload))
        extra = sorted(set(payload) - expected_top_level)
        raise PeopleGroveCurationError(
            "PeopleGrove enrichment top-level schema mismatch"
            f"; missing={missing}; extra={extra}"
        )
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise PeopleGroveCurationError(
            "PeopleGrove enrichment schema_version must be integer 1"
        )

    source_capture_sha256 = _strict_string(
        payload.get("source_capture_sha256"),
        field="source_capture_sha256",
        required=True,
    ).lower()
    if not re.fullmatch(r"[a-f0-9]{64}", source_capture_sha256):
        raise PeopleGroveCurationError(
            "PeopleGrove enrichment source_capture_sha256 must be a SHA-256 hex digest"
        )
    actual_capture_sha256 = _file_sha256(capture_path)
    if source_capture_sha256 != actual_capture_sha256:
        raise PeopleGroveCurationError(
            "PeopleGrove enrichment is bound to a different capture SHA-256"
        )
    captured_at = _strict_string(
        payload.get("captured_at"), field="captured_at", required=True
    )
    if not _is_timezone_aware_iso_datetime(captured_at):
        raise PeopleGroveCurationError(
            "PeopleGrove enrichment captured_at must be a timezone-aware ISO-8601 timestamp"
        )
    captured_by = _strict_string(
        payload.get("captured_by"), field="captured_by", required=True
    )
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise PeopleGroveCurationError(
            "PeopleGrove enrichment profiles must be an object keyed by source identity"
        )

    artifact_sha256 = _file_sha256(path)
    records: list[PeopleGroveProfileEnrichment] = []
    seen_record_ids: dict[str, str] = {}
    seen_urls: dict[str, str] = {}
    for raw_key, raw_record in raw_profiles.items():
        mapping_key = _strict_string(raw_key, field="profile mapping key", required=True)
        if not isinstance(raw_record, dict):
            raise PeopleGroveCurationError(
                f"PeopleGrove enrichment profile {mapping_key!r} must be an object"
            )
        expected_profile_fields = {"source_record_id", "source_url", "current_roles"}
        if set(raw_record) != expected_profile_fields:
            missing = sorted(expected_profile_fields - set(raw_record))
            extra = sorted(set(raw_record) - expected_profile_fields)
            raise PeopleGroveCurationError(
                f"PeopleGrove enrichment profile {mapping_key!r} schema mismatch"
                f"; missing={missing}; extra={extra}"
            )
        source_record_id = _strict_string(
            raw_record.get("source_record_id"),
            field=f"profiles[{mapping_key}].source_record_id",
        )
        raw_source_url = _strict_string(
            raw_record.get("source_url"),
            field=f"profiles[{mapping_key}].source_url",
        )
        source_url = _canonical_url(raw_source_url)
        if raw_source_url and not source_url:
            raise PeopleGroveCurationError(
                f"PeopleGrove enrichment profile {mapping_key!r} has invalid source_url"
            )
        if not source_record_id and not source_url:
            raise PeopleGroveCurationError(
                f"PeopleGrove enrichment profile {mapping_key!r} requires source_record_id or source_url"
            )
        _validate_enrichment_mapping_key(
            mapping_key,
            source_record_id=source_record_id,
            source_url=source_url,
        )

        if source_record_id:
            previous_key = seen_record_ids.get(source_record_id)
            if previous_key is not None:
                raise PeopleGroveCurationError(
                    "Duplicate PeopleGrove enrichment source_record_id "
                    f"{source_record_id!r} in {previous_key!r} and {mapping_key!r}"
                )
            seen_record_ids[source_record_id] = mapping_key
        if source_url:
            previous_key = seen_urls.get(source_url)
            if previous_key is not None:
                raise PeopleGroveCurationError(
                    "Duplicate PeopleGrove enrichment source_url "
                    f"{source_url!r} in {previous_key!r} and {mapping_key!r}"
                )
            seen_urls[source_url] = mapping_key

        raw_roles = raw_record.get("current_roles")
        if not isinstance(raw_roles, list) or not raw_roles:
            raise PeopleGroveCurationError(
                f"PeopleGrove enrichment profile {mapping_key!r} current_roles must be a non-empty list"
            )
        roles = tuple(
            _parse_enriched_current_role(
                raw_role,
                mapping_key=mapping_key,
                role_index=role_index,
            )
            for role_index, raw_role in enumerate(raw_roles)
        )
        records.append(
            PeopleGroveProfileEnrichment(
                mapping_key=mapping_key,
                source_record_id=source_record_id,
                source_url=source_url,
                current_roles=roles,
                captured_at=captured_at,
                captured_by=captured_by,
                artifact_sha256=artifact_sha256,
            )
        )

    capture_by_id: dict[str, set[int]] = {}
    capture_by_url: dict[str, set[int]] = {}
    capture_by_input_index = {profile.input_index: profile for profile in profiles}
    for profile in profiles:
        record_id_token = _clean(profile.source_record_id)
        source_url = _canonical_url(profile.source_url)
        if record_id_token:
            capture_by_id.setdefault(record_id_token, set()).add(profile.input_index)
        if source_url:
            capture_by_url.setdefault(source_url, set()).add(profile.input_index)

    by_input_index: dict[int, PeopleGroveProfileEnrichment] = {}
    for record in records:
        matches: set[int] | None = None
        record_id_token = _clean(record.source_record_id)
        if record_id_token:
            id_matches = capture_by_id.get(record_id_token)
            if not id_matches:
                raise PeopleGroveCurationError(
                    "PeopleGrove enrichment references unknown source_record_id "
                    f"{record.source_record_id!r}"
                )
            matches = set(id_matches)
        if record.source_url:
            url_matches = capture_by_url.get(record.source_url)
            if not url_matches:
                raise PeopleGroveCurationError(
                    "PeopleGrove enrichment references unknown source_url "
                    f"{record.source_url!r}"
                )
            matches = set(url_matches) if matches is None else matches.intersection(url_matches)
        if not matches:
            raise PeopleGroveCurationError(
                f"PeopleGrove enrichment identity mismatch for {record.mapping_key!r}"
            )
        matched_identities = {
            (
                _clean(capture_by_input_index[input_index].source_record_id),
                _canonical_url(capture_by_input_index[input_index].source_url),
            )
            for input_index in matches
        }
        if len(matched_identities) != 1:
            raise PeopleGroveCurationError(
                "PeopleGrove enrichment identity is ambiguous across capture rows for "
                f"{record.mapping_key!r}"
            )
        for input_index in matches:
            previous = by_input_index.get(input_index)
            if previous is not None:
                raise PeopleGroveCurationError(
                    "Multiple PeopleGrove enrichment mappings resolve to capture row "
                    f"{input_index}: {previous.mapping_key!r}, {record.mapping_key!r}"
                )
            by_input_index[input_index] = record

    return PeopleGroveEnrichmentArtifact(
        path=path,
        sha256=artifact_sha256,
        source_capture_sha256=source_capture_sha256,
        captured_at=captured_at,
        captured_by=captured_by,
        records=tuple(records),
        by_input_index=by_input_index,
    )


def _parse_enriched_current_role(
    value: object,
    *,
    mapping_key: str,
    role_index: int,
) -> PeopleGroveCurrentRole:
    if not isinstance(value, dict):
        raise PeopleGroveCurationError(
            f"PeopleGrove enrichment {mapping_key!r} current_roles[{role_index}] must be an object"
        )
    expected_fields = {"title", "company", "date_range", "location"}
    if set(value) != expected_fields:
        missing = sorted(expected_fields - set(value))
        extra = sorted(set(value) - expected_fields)
        raise PeopleGroveCurationError(
            f"PeopleGrove enrichment {mapping_key!r} current_roles[{role_index}] schema mismatch"
            f"; missing={missing}; extra={extra}"
        )
    title = _strict_string(
        value.get("title"),
        field=f"profiles[{mapping_key}].current_roles[{role_index}].title",
        required=True,
    )
    company = _strict_string(
        value.get("company"),
        field=f"profiles[{mapping_key}].current_roles[{role_index}].company",
        required=True,
    )
    if not _company_is_confident(company):
        raise PeopleGroveCurationError(
            f"PeopleGrove enrichment {mapping_key!r} current_roles[{role_index}] has invalid company"
        )
    return PeopleGroveCurrentRole(
        title=title,
        company=company,
        date_range=_strict_string(
            value.get("date_range"),
            field=f"profiles[{mapping_key}].current_roles[{role_index}].date_range",
        ),
        location=_strict_string(
            value.get("location"),
            field=f"profiles[{mapping_key}].current_roles[{role_index}].location",
        ),
    )


def _validate_enrichment_mapping_key(
    mapping_key: str,
    *,
    source_record_id: str,
    source_url: str,
) -> None:
    canonical_key_url = _canonical_url(mapping_key)
    if canonical_key_url:
        if not source_url or canonical_key_url != source_url:
            raise PeopleGroveCurationError(
                f"PeopleGrove enrichment URL key {mapping_key!r} does not match source_url"
            )
        return
    if not source_record_id or mapping_key != source_record_id:
        raise PeopleGroveCurationError(
            f"PeopleGrove enrichment key {mapping_key!r} does not match source_record_id"
        )


def parse_current_title_company(headline: str) -> tuple[str, str] | None:
    """Parse only explicit current-role headline shapes, never infer a company."""
    cleaned = _clean(headline)
    if not cleaned:
        return None
    match = _AT_COMPANY_PATTERN.fullmatch(cleaned)
    if match is None:
        match = _FOUNDER_COMPANY_PATTERN.fullmatch(cleaned)
    if match is None:
        return None
    title = _clean(match.group("title")).strip("-|,\u2013\u2014 ")
    company = _clean(match.group("company")).strip("-|,\u2013\u2014 ")
    company = _CANONICAL_COMPANY_ALIASES.get(_normalized_token(company), company)
    if (
        not title
        or _DESCRIPTIVE_TITLE_TAGLINE_PATTERN.search(title)
        or not _company_is_confident(company)
    ):
        return None
    return title, company


def classify_peoplegrove_title(title: str) -> RoleClassification | None:
    normalized = _normalized_text(title)
    leadership = bool(_LEADERSHIP_PATTERN.search(normalized))
    if re.search(r"\b(?:co[ -]?founder|founder)\b", normalized):
        return RoleClassification("founder_c_suite", 92, "Founder", "usc-founder", "executive")
    if re.search(r"\b(?:deputy\s+)?chief of staff\b", normalized):
        return RoleClassification(
            "venture_startup_operator",
            80,
            "Operator",
            "usc-startup-operator",
            "leadership",
        )
    if re.search(r"\b(?:ceo|coo|cpo|cso|cto|cmo|cro|cfo)\b", normalized) or re.search(
        r"\bchief\s+[a-z& /-]+\s+officer\b", normalized
    ):
        return RoleClassification("founder_c_suite", 88, "Executive", "usc-executive", "executive")
    if re.search(
        r"\b(?:recruiter|recruiting|talent acquisition|talent partner|"
        r"talent lead|people partner|university recruiting|campus recruiting)\b",
        normalized,
    ):
        return RoleClassification(
            "recruiting_talent", 72, "Recruiting", "usc-recruiting", "leadership" if leadership else "core"
        )
    if re.search(
        r"\b(?:product manager|product management|product lead|product leader|"
        r"product director|product strategy|product strategist|product operations|"
        r"product ops|product owner|head of product|product executive|"
        r"product\s*(?:&|and)\s*(?:strategy|growth))\b",
        normalized,
    ) or re.fullmatch(r"product", normalized) or re.search(
        r"\b(?:svp|evp|vp|senior vice president|vice president)[, ]+"
        r"(?:of )?product\b(?=\s*(?:$|(?:&|and)\s*(?:strategy|growth)\b))",
        normalized,
    ):
        return RoleClassification(
            "product_product_strategy",
            86 if leadership else 76,
            "Product",
            "usc-product",
            "leadership" if leadership else "core",
        )
    if re.search(
        r"\b(?:business operations|bizops|strategy\s*(?:&|and)\s*operations|"
        r"operations\s*(?:&|and)\s*strategy|strategic operations|corporate strategy|"
        r"strategy lead|strategy manager|"
        r"strategy director|strategic initiatives)\b",
        normalized,
    ):
        return RoleClassification(
            "bizops_strategy",
            82 if leadership else 72,
            "Operator",
            "usc-bizops-strategy",
            "leadership" if leadership else "core",
        )
    if re.search(
        r"\b(?:venture partner|operating partner|general partner|"
        r"venture capitalist|venture investor|angel investor|investor|"
        r"entrepreneur in residence|startup operator|"
        r"founder'?s office|chief of staff)\b",
        normalized,
    ) or re.match(r"^venture capital\b", normalized) or (
        "marketing" not in normalized
        and re.search(
            r"\b(?:head|director|lead|vp|vice president)\s+(?:of\s+)?"
            r"(?:growth|go-to-market|gtm|partnerships|business development)\b",
            normalized,
        )
    ):
        return RoleClassification(
            "venture_startup_operator", 80, "Operator", "usc-startup-operator", "leadership"
        )
    if re.search(
        r"\b(?:program manager|program management|program lead|operations lead|"
        r"operations manager|operations director)\b",
        normalized,
    ) or re.search(
        r"\b(?:head|director|lead|vp|vice president|manager)\s+(?:of\s+)?operations\b",
        normalized,
    ) or re.search(r"\bgeneral manager\b", normalized):
        return RoleClassification(
            "program_operations_leadership",
            76 if leadership else 68,
            "Operator",
            "usc-program-operations",
            "leadership" if leadership else "core",
        )
    return None


def load_existing_peoplegrove_keys(workspace: Path) -> ExistingPeopleGroveKeys:
    workspace = workspace.resolve()
    contacts_path = workspace / "contacts.csv"
    if not contacts_path.exists():
        raise PeopleGroveCurationError(
            f"Workspace dedupe requested but contacts.csv is missing: {contacts_path}"
        )

    organization_names: dict[str, str] = {}
    known_company_names: set[str] = set()
    organizations_path = workspace / "organizations.csv"
    if organizations_path.exists():
        with organizations_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                name = _clean(row.get("name"))
                organization_names[_clean(row.get("organization_id"))] = name
                company_token = _normalized_token(name)
                if company_token:
                    known_company_names.add(company_token)

    source_record_ids: set[str] = set()
    source_urls: set[str] = set()
    person_companies: set[str] = set()
    with contacts_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            _, metadata = parse_notes_parts(_clean(row.get("notes")))
            source_record_id = _normalized_token(
                metadata.get("relationship_source_record_id", "")
            )
            if source_record_id:
                source_record_ids.add(source_record_id)
            source_url = _canonical_url(
                metadata.get("relationship_evidence_url", "") or row.get("source_url", "")
            )
            if source_url:
                source_urls.add(source_url)
            company = organization_names.get(_clean(row.get("organization_id")), "")
            identity = _person_company_key(_clean(row.get("full_name")), company)
            if identity:
                person_companies.add(identity)
    return ExistingPeopleGroveKeys(
        source_record_ids=frozenset(source_record_ids),
        source_urls=frozenset(source_urls),
        person_companies=frozenset(person_companies),
        known_company_names=frozenset(known_company_names),
    )


def _evaluate_profile(
    profile: PeopleGroveProfile,
    *,
    captured_at: str,
    capture_batch: str,
    captured_by: str,
    minimum_score: int,
    enrichment: PeopleGroveProfileEnrichment | None = None,
) -> CurationDecision:
    if not profile.full_name:
        return CurationDecision(profile, False, "missing_full_name")
    if not (profile.source_record_id or profile.source_url):
        return CurationDecision(profile, False, "missing_provenance")
    if profile.source_url and not _canonical_url(profile.source_url):
        return CurationDecision(profile, False, "invalid_source_url")

    member_type = _normalized_text(profile.member_type)
    if member_type in {"student", "current student", "undergraduate", "graduate student"}:
        return CurationDecision(profile, False, "student_or_intern")
    if profile.headline and _JOB_SEEKER_PATTERN.search(profile.headline):
        return CurationDecision(profile, False, "job_seeker")

    parsed = parse_current_title_company(profile.headline) if profile.headline else None
    role_source = "card_headline"
    selected_enrichment_role: PeopleGroveCurrentRole | None = None
    classification: RoleClassification | None = None
    if parsed is None:
        if enrichment is None:
            return CurationDecision(
                profile,
                False,
                "missing_headline"
                if not profile.headline
                else "unparseable_current_role_company",
            )
        selected = _select_enriched_current_role(enrichment)
        if selected is None:
            return CurationDecision(
                profile,
                False,
                "no_eligible_enriched_current_role",
                role_source="peoplegrove_career_journey_enrichment_no_eligible_role",
                **_enrichment_decision_fields(enrichment),
            )
        selected_enrichment_role, classification = selected
        title = selected_enrichment_role.title
        company = _CANONICAL_COMPANY_ALIASES.get(
            _normalized_token(selected_enrichment_role.company),
            selected_enrichment_role.company,
        )
        role_source = "peoplegrove_career_journey_enrichment"
    else:
        title, company = parsed
    if _STUDENT_TITLE_PATTERN.search(title):
        return CurationDecision(
            profile,
            False,
            "student_or_intern",
            title=title,
            company=company,
            role_source=role_source,
            **_enrichment_decision_fields(enrichment, selected_enrichment_role),
        )
    if _JOB_SEEKER_PATTERN.search(title):
        return CurationDecision(profile, False, "job_seeker", title=title, company=company)
    if _NON_CURRENT_PATTERN.search(title):
        return CurationDecision(profile, False, "non_current_role", title=title, company=company)

    classification = classification or classify_peoplegrove_title(title)
    if classification is None:
        return CurationDecision(
            profile, False, "no_high_signal_role", title=title, company=company
        )
    if _role_context_is_irrelevant(classification, title, company):
        return CurationDecision(
            profile,
            False,
            "irrelevant_role_context",
            category=classification.category,
            score=classification.base_score,
            title=title,
            company=company,
            role_level=classification.role_level,
            role_source=role_source,
            **_enrichment_decision_fields(enrichment, selected_enrichment_role),
        )

    evidence = " ".join(
        [profile.program, profile.member_type, *profile.queries, *profile.labels]
    ).lower()
    score = classification.base_score
    if any(term in evidence for term in ("usc", "trojan", "marshall")):
        score += 5
    if any(term in member_type for term in ("alum", "mentor")):
        score += 3
    if profile.program:
        score += 2
    grad_year = _valid_grad_year(profile.grad_year)
    if grad_year:
        score += 2
    score = min(score, 100)
    if score < minimum_score:
        return CurationDecision(
            profile,
            False,
            "below_score_threshold",
            category=classification.category,
            score=score,
            title=title,
            company=company,
            role_level=classification.role_level,
            role_source=role_source,
            **_enrichment_decision_fields(enrichment, selected_enrichment_role),
        )

    priority = "high" if score >= 88 else "medium" if score >= 75 else "low"
    source_url = _canonical_url(profile.source_url)
    relationship_parts = ["USC PeopleGrove/Trojan Network"]
    if profile.program:
        relationship_parts.append(profile.program)
    if grad_year:
        relationship_parts.append(f"class of {grad_year}")
    if profile.member_type:
        relationship_parts.append(profile.member_type)
    notes_parts = [
        f"peoplegrove_category={classification.category}",
        f"peoplegrove_score={score}",
        f"peoplegrove_role_level={classification.role_level}",
        f"captured_headline={profile.headline}",
    ]
    if selected_enrichment_role is not None and enrichment is not None:
        notes_parts.extend(
            [
                "peoplegrove_role_source=career_journey_enrichment",
                f"enrichment_mapping_key={enrichment.mapping_key}",
                f"enrichment_artifact_sha256={enrichment.artifact_sha256}",
                f"enrichment_source_record_id={enrichment.source_record_id}",
                f"enrichment_source_url={enrichment.source_url}",
                f"enrichment_captured_at={enrichment.captured_at}",
                f"enrichment_captured_by={enrichment.captured_by}",
                f"enrichment_current_role_title={selected_enrichment_role.title}",
                f"enrichment_current_role_company={selected_enrichment_role.company}",
                f"enrichment_current_role_date_range={selected_enrichment_role.date_range}",
                f"enrichment_current_role_location={selected_enrichment_role.location}",
            ]
        )
    if profile.queries:
        notes_parts.append(f"capture_queries={'; '.join(profile.queries)}")
    if profile.labels:
        notes_parts.append(f"capture_labels={'; '.join(profile.labels)}")
    # Browser-card labels are presentation state (for example "Message",
    # "currently offline", or "+ 1") rather than durable relationship
    # attributes. Preserve them in capture notes for auditability, but never
    # promote them into tracker tags.
    tags = ["peoplegrove", "usc", "trojan-network", "warm-network", classification.category]
    lead = {field: "" for field in RELATIONSHIP_LEAD_FIELDS}
    lead.update(
        {
            "source_type": "peoplegrove",
            "full_name": profile.full_name,
            "company": company,
            "title": title,
            "school": "University of Southern California",
            "program": profile.program,
            "grad_year": grad_year,
            "relationship_signal": "; ".join(relationship_parts),
            "contact_type": classification.contact_type,
            "priority": priority,
            "target_lists": ";".join(
                ["peoplegrove", "usc-network", classification.target_list]
            ),
            "tags": ",".join(dict.fromkeys(tags)),
            "source_url": source_url,
            "notes": " | ".join(notes_parts),
            "source_record_id": profile.source_record_id,
            "capture_batch": capture_batch,
            "captured_at": captured_at,
            "captured_by": captured_by,
        }
    )
    return CurationDecision(
        profile,
        True,
        "accepted",
        category=classification.category,
        score=score,
        title=title,
        company=company,
        role_level=classification.role_level,
        lead=lead,
        role_source=role_source,
        **_enrichment_decision_fields(enrichment, selected_enrichment_role),
    )


def _select_enriched_current_role(
    enrichment: PeopleGroveProfileEnrichment,
) -> tuple[PeopleGroveCurrentRole, RoleClassification] | None:
    """Choose only from explicit current roles; never derive a role or employer."""

    eligible: list[tuple[int, int, PeopleGroveCurrentRole, RoleClassification]] = []
    for index, role in enumerate(enrichment.current_roles):
        if (
            _STUDENT_TITLE_PATTERN.search(role.title)
            or _JOB_SEEKER_PATTERN.search(role.title)
            or _NON_CURRENT_PATTERN.search(role.title)
        ):
            continue
        classification = classify_peoplegrove_title(role.title)
        if classification is None or _role_context_is_irrelevant(
            classification, role.title, role.company
        ):
            continue
        eligible.append((classification.base_score, -index, role, classification))
    if not eligible:
        return None
    _, _, role, classification = max(eligible, key=lambda item: (item[0], item[1]))
    return role, classification


def _enrichment_decision_fields(
    enrichment: PeopleGroveProfileEnrichment | None,
    role: PeopleGroveCurrentRole | None = None,
) -> _EnrichmentDecisionFields:
    if enrichment is None:
        return {}
    return {
        "enrichment_title": role.title if role is not None else "",
        "enrichment_company": role.company if role is not None else "",
        "enrichment_date_range": role.date_range if role is not None else "",
        "enrichment_location": role.location if role is not None else "",
        "enrichment_mapping_key": enrichment.mapping_key,
        "enrichment_source_record_id": enrichment.source_record_id,
        "enrichment_source_url": enrichment.source_url,
        "enrichment_captured_at": enrichment.captured_at,
        "enrichment_captured_by": enrichment.captured_by,
        "enrichment_artifact_sha256": enrichment.artifact_sha256,
    }


def _deduplicate_capture(decisions: list[CurationDecision]) -> list[CurationDecision]:
    if not decisions:
        return []
    parent = list(range(len(decisions)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    seen_ids: dict[str, int] = {}
    seen_urls: dict[str, int] = {}
    for index, decision in enumerate(decisions):
        source_id = _normalized_token(decision.profile.source_record_id)
        source_url = _canonical_url(decision.profile.source_url)
        if source_id:
            if source_id in seen_ids:
                union(index, seen_ids[source_id])
            else:
                seen_ids[source_id] = index
        if source_url:
            if source_url in seen_urls:
                union(index, seen_urls[source_url])
            else:
                seen_urls[source_url] = index

    groups: dict[int, list[int]] = {}
    for index in range(len(decisions)):
        groups.setdefault(find(index), []).append(index)

    result = list(decisions)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        winner_index = max(
            indexes,
            key=lambda index: (
                int(decisions[index].accepted),
                decisions[index].score,
                int(bool(decisions[index].profile.source_url)),
                -decisions[index].profile.input_index,
            ),
        )
        winner = decisions[winner_index]
        for index in indexes:
            if index == winner_index:
                continue
            decision = decisions[index]
            duplicate_reason = _duplicate_reason(decision.profile, winner.profile)
            result[index] = replace(
                decision,
                accepted=False,
                reason=duplicate_reason,
                lead=None,
                duplicate_of_input_index=winner.profile.input_index,
                original_reason=decision.reason if decision.reason != "accepted" else "",
            )

    seen_people: dict[str, int] = {}
    accepted_indexes = sorted(
        (index for index, decision in enumerate(result) if decision.accepted),
        key=lambda index: (-result[index].score, result[index].profile.input_index),
    )
    for index in accepted_indexes:
        decision = result[index]
        identity = _person_company_key(decision.profile.full_name, decision.company)
        if not identity:
            continue
        if identity in seen_people:
            winner = result[seen_people[identity]]
            result[index] = replace(
                decision,
                accepted=False,
                reason="duplicate_person_company",
                lead=None,
                duplicate_of_input_index=winner.profile.input_index,
                original_reason="accepted",
            )
        else:
            seen_people[identity] = index
    return result


def _dedupe_against_workspace(
    decision: CurationDecision,
    existing: ExistingPeopleGroveKeys,
) -> CurationDecision:
    if not decision.accepted:
        return decision
    source_id = _normalized_token(decision.profile.source_record_id)
    source_url = _canonical_url(decision.profile.source_url)
    identity = _person_company_key(decision.profile.full_name, decision.company)
    reason = ""
    if source_id and source_id in existing.source_record_ids:
        reason = "already_in_workspace_source_record"
    elif source_url and source_url in existing.source_urls:
        reason = "already_in_workspace_source_url"
    elif identity and identity in existing.person_companies:
        reason = "already_in_workspace_person_company"
    if not reason:
        return decision
    return replace(decision, accepted=False, reason=reason, lead=None, original_reason="accepted")


def _annotate_company_fit(
    decision: CurationDecision,
    existing: ExistingPeopleGroveKeys | None,
) -> CurationDecision:
    if not decision.accepted:
        return decision
    company_token = _normalized_token(decision.company)
    known_company = bool(
        existing is not None and company_token in existing.known_company_names
    )
    company_fit_signal = (
        "existing_outreach_company" if known_company else "unverified_company_fit"
    )
    flags = ["manual_company_fit_review"]
    if not known_company:
        flags.append("company_not_in_outreach_universe")
    context = " ".join([decision.title, decision.company])
    if _POSSIBLE_LOW_ALIGNMENT_PATTERN.search(context):
        flags.append("possible_low_alignment_industry")
    if decision.category == "recruiting_talent" and re.search(
        r"\b(?:associate|coordinator|junior)\b", decision.title, re.IGNORECASE
    ):
        flags.append("junior_routing_contact")
    if (
        decision.category == "program_operations_leadership"
        and decision.role_level != "leadership"
    ):
        flags.append("non_leadership_program_role")
    if decision.company.count(",") >= 2 or len(decision.company.split()) > 7:
        flags.append("complex_company_string")

    lead = dict(decision.lead or {})
    notes = _clean(lead.get("notes"))
    fit_note = (
        f"company_fit_signal={company_fit_signal} | "
        f"review_flags={'; '.join(dict.fromkeys(flags))}"
    )
    lead["notes"] = f"{notes} | {fit_note}" if notes else fit_note
    return replace(
        decision,
        company_fit_signal=company_fit_signal,
        review_flags=tuple(dict.fromkeys(flags)),
        lead=lead,
    )


def _duplicate_reason(profile: PeopleGroveProfile, winner: PeopleGroveProfile) -> str:
    source_id = _normalized_token(profile.source_record_id)
    winner_id = _normalized_token(winner.source_record_id)
    if source_id and source_id == winner_id:
        return "duplicate_source_record_id"
    source_url = _canonical_url(profile.source_url)
    winner_url = _canonical_url(winner.source_url)
    if source_url and source_url == winner_url:
        return "duplicate_source_url"
    return "duplicate_capture_record"


def _role_context_is_irrelevant(
    classification: RoleClassification,
    title: str,
    company: str,
) -> bool:
    if classification.category == "product_product_strategy":
        return bool(_IRRELEVANT_PRODUCT_TITLE_PATTERN.search(title))
    if classification.category == "program_operations_leadership":
        return bool(_IRRELEVANT_PROGRAM_TITLE_PATTERN.search(title))
    if classification.category == "recruiting_talent":
        return bool(
            re.search(r"\bmortgage loan originator\b", title, re.IGNORECASE)
            or re.search(r"\btransit authority\b", company, re.IGNORECASE)
        )
    if classification.category == "venture_startup_operator":
        return bool(
            re.search(r"\btalent agent\b", title, re.IGNORECASE)
            or re.search(r"\bgrowth marketing\b", title, re.IGNORECASE)
        )
    if classification.category == "founder_c_suite":
        return bool(_IRRELEVANT_FOUNDER_CONTEXT_PATTERN.search(f"{title} {company}"))
    return False


def _company_is_confident(company: str) -> bool:
    normalized = _normalized_text(company).strip(".,")
    if not normalized or normalized in _COMPANY_BLOCKLIST:
        return False
    if len(company) < 2 or len(company) > 100 or len(company.split()) > 12:
        return False
    if not re.search(r"[a-z]", company, re.IGNORECASE):
        return False
    if re.search(r"https?://|\b(?:open to work|seeking|looking for)\b", company, re.IGNORECASE):
        return False
    if _COMPANY_DESCRIPTION_PATTERN.search(company):
        return False
    if re.search(
        r"\b(?:product manager|engineer|consultant|founder|student|intern|speaker|"
        r"builder|advisor)\b",
        normalized,
    ):
        return False
    return True


def _valid_grad_year(value: str) -> str:
    cleaned = _clean(value)
    if not re.fullmatch(r"\d{4}", cleaned):
        return ""
    year = int(cleaned)
    current_year = datetime.now(UTC).year
    return cleaned if 1950 <= year <= current_year + 3 else ""


def _person_company_key(full_name: str, company: str) -> str:
    name_token = _normalized_token(full_name)
    company_token = _normalized_token(company)
    return f"{name_token}|{company_token}" if name_token and company_token else ""


def _canonical_url(value: object) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    host = parts.netloc.lower()
    query = ""
    if "peoplegrove.com" in host:
        identity_query = [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=False)
            if key.lower() in {"userprofile", "profileid", "userid", "memberid"}
        ]
        query = urlencode(sorted(identity_query, key=lambda pair: pair[0].lower()))
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def _normalized_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _clean(value).lower()).strip("-")


def _normalized_text(value: object) -> str:
    return re.sub(r"\s+", " ", _clean(value).lower()).strip()


def _tag_token(value: object) -> str:
    return _normalized_token(value)[:60]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return (_clean(value),) if _clean(value) else ()
    return tuple(dict.fromkeys(_clean(item) for item in candidates if _clean(item)))


def _load_json_without_duplicate_keys(path: Path) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PeopleGroveCurationError(
                    f"PeopleGrove enrichment contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise PeopleGroveCurationError(
            f"PeopleGrove enrichment is invalid JSON: {exc}"
        ) from exc


def _strict_string(value: object, *, field: str, required: bool = False) -> str:
    if not isinstance(value, str):
        raise PeopleGroveCurationError(
            f"PeopleGrove enrichment field {field} must be a string"
        )
    cleaned = _clean(value)
    if required and not cleaned:
        raise PeopleGroveCurationError(
            f"PeopleGrove enrichment field {field} must not be blank"
        )
    return cleaned


def _is_timezone_aware_iso_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split()).strip()


def _short_file_hash(path: Path) -> str:
    return _file_sha256(path)[:12]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _temporary_path(path)
    try:
        with temp_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _temporary_path(path)
    try:
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _temporary_path(path: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    return Path(raw_path)

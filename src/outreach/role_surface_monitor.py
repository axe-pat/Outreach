from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from pydantic import BaseModel, Field, field_validator


SCHEMA_VERSION = "1.0"


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class RoleFamily(str, Enum):
    PRODUCT_PM = "product_pm"
    PRODUCT_STRATEGY = "product_strategy"
    BIZOPS_STRATEGY = "bizops_strategy"
    PROGRAM_OPERATIONS = "program_operations"
    GROWTH_ADJACENT = "growth_adjacent"
    OTHER = "other"


MONITORED_ROLE_FAMILIES = (
    RoleFamily.PRODUCT_PM,
    RoleFamily.PRODUCT_STRATEGY,
    RoleFamily.BIZOPS_STRATEGY,
    RoleFamily.PROGRAM_OPERATIONS,
    RoleFamily.GROWTH_ADJACENT,
)

ROLE_FAMILY_LABELS = {
    RoleFamily.PRODUCT_PM: "Product / PM",
    RoleFamily.PRODUCT_STRATEGY: "Product Strategy",
    RoleFamily.BIZOPS_STRATEGY: "BizOps / Strategy",
    RoleFamily.PROGRAM_OPERATIONS: "Program / Operations",
    RoleFamily.GROWTH_ADJACENT: "Narrow Growth-adjacent",
    RoleFamily.OTHER: "Other / unclassified",
}


class RoleStage(str, Enum):
    DISCOVERED = "discovered"
    SCORED = "scored"
    SURFACED = "surfaced"
    ACTED = "acted"


ROLE_STAGE_ORDER = {
    RoleStage.DISCOVERED: 0,
    RoleStage.SCORED: 1,
    RoleStage.SURFACED: 2,
    RoleStage.ACTED: 3,
}


class SourceRunStatus(str, Enum):
    RAN = "ran"
    SKIPPED = "skipped"
    FAILED = "failed"
    NOT_REPORTED = "not_reported"


class CoverageStatus(str, Enum):
    MET = "met"
    MISSED = "missed"
    NO_SOURCE_RAN = "no_source_ran"


class RoleObservation(BaseModel):
    run_id: str
    source: str
    title: str
    company: str
    stage: RoleStage = RoleStage.DISCOVERED
    source_url: str = ""
    location: str = ""
    external_role_id: str = ""
    observed_at: str = Field(default_factory=utc_now_iso)

    @field_validator("run_id", "source", "title", "company")
    @classmethod
    def require_identity_fields(cls, value: str) -> str:
        value = _clean(value)
        if not value:
            raise ValueError("run_id, source, title, and company are required")
        return value

    @field_validator("source_url", "location", "external_role_id", "observed_at")
    @classmethod
    def clean_optional_fields(cls, value: str) -> str:
        return _clean(value)


class SourceRun(BaseModel):
    run_id: str
    source: str
    status: SourceRunStatus
    reason: str = ""
    artifact: str = ""

    @field_validator("run_id", "source")
    @classmethod
    def require_source_identity(cls, value: str) -> str:
        value = _clean(value)
        if not value:
            raise ValueError("run_id and source are required")
        return value

    @field_validator("reason", "artifact")
    @classmethod
    def clean_source_fields(cls, value: str) -> str:
        return _clean(value)


class CoverageFloor(BaseModel):
    minimum_discovered: int = Field(default=1, ge=0)
    minimum_surfaced: int = Field(default=0, ge=0)


DEFAULT_COVERAGE_FLOORS: dict[RoleFamily, CoverageFloor] = {
    RoleFamily.PRODUCT_PM: CoverageFloor(minimum_discovered=1, minimum_surfaced=1),
    RoleFamily.PRODUCT_STRATEGY: CoverageFloor(minimum_discovered=1),
    RoleFamily.BIZOPS_STRATEGY: CoverageFloor(minimum_discovered=1),
    RoleFamily.PROGRAM_OPERATIONS: CoverageFloor(minimum_discovered=1),
    RoleFamily.GROWTH_ADJACENT: CoverageFloor(minimum_discovered=1),
}


class RoleClassification(BaseModel):
    family: RoleFamily
    matched_rule: str


class FamilyCoverage(BaseModel):
    family: RoleFamily
    label: str
    lane: str
    discovered: int = 0
    scored: int = 0
    surfaced: int = 0
    acted: int = 0
    unique_companies: int = 0
    minimum_discovered: int = 0
    minimum_surfaced: int = 0
    discovered_gap: int = 0
    surfaced_gap: int = 0
    coverage_status: CoverageStatus


class SourceCoverage(BaseModel):
    source: str
    status: SourceRunStatus
    reason: str = ""
    artifact: str = ""
    observations: int = 0
    unique_roles: int = 0
    discovered: int = 0
    scored: int = 0
    surfaced: int = 0
    acted: int = 0
    monitored_roles: int = 0
    unclassified_roles: int = 0


class SourceFamilyCoverage(BaseModel):
    source: str
    source_status: SourceRunStatus
    family: RoleFamily
    label: str
    discovered: int = 0
    scored: int = 0
    surfaced: int = 0
    acted: int = 0
    unique_companies: int = 0


class UnclassifiedRole(BaseModel):
    title: str
    company: str
    sources: list[str]


class RoleSurfaceReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    generated_at: str
    summary: dict[str, object]
    summary_text: str
    family_coverage: list[FamilyCoverage]
    source_coverage: list[SourceCoverage]
    source_family_coverage: list[SourceFamilyCoverage]
    unclassified_roles: list[UnclassifiedRole]
    warnings: list[str]


@dataclass(frozen=True)
class RoleSurfaceArtifacts:
    report_json: Path
    family_csv: Path
    source_csv: Path
    source_family_csv: Path
    unclassified_csv: Path


@dataclass
class _UniqueRole:
    title: str
    company: str
    family: RoleFamily
    stage: RoleStage
    sources: set[str]


_PRODUCT_STRATEGY_RULES = (
    (re.compile(r"\bproduct\s+strateg(?:y|ic)\b"), "product strategy"),
    (re.compile(r"\bproduct\s+strategist\b"), "product strategist"),
    (re.compile(r"\bstrateg(?:y|ic)\b.*\bproduct\b"), "strategy plus product"),
)

_PRODUCT_PM_RULES = (
    (re.compile(r"\b(?:growth\s+product|product\s+growth)\b"), "growth product"),
    (re.compile(r"\bproduct\s+(?:ops|operations)\b"), "product operations"),
    (re.compile(r"\bproduct\s+management\b"), "product management"),
    (re.compile(r"\bproduct\s+manager\b"), "product manager"),
    (re.compile(r"\bproduct\s+owner\b"), "product owner"),
    (re.compile(r"\bproduct\s+lead(?:er)?\b"), "product lead"),
    (re.compile(r"\bhead\s+of\s+product\b"), "head of product"),
    (re.compile(r"\b(?:director|vp|vice president)\b.*\bproduct\b"), "product leadership"),
    (re.compile(r"\bproduct\b.*\b(?:director|lead)\b"), "product leadership"),
)

_BIZOPS_STRATEGY_RULES = (
    (re.compile(r"\bbiz\s*ops\b"), "bizops"),
    (re.compile(r"\bbusiness\s+operations\b"), "business operations"),
    (re.compile(r"\bstrategy\s+(?:and|&)\s+operations\b"), "strategy and operations"),
    (re.compile(r"\bstrategic\s+operations\b"), "strategic operations"),
    (re.compile(r"\b(?:business|corporate|company)\s+strategy\b"), "business strategy"),
    (re.compile(r"\bstrategic\s+initiatives\b"), "strategic initiatives"),
    (re.compile(r"\bchief\s+of\s+staff\b"), "chief of staff"),
)

_PROGRAM_OPERATIONS_EXCLUSIONS = re.compile(
    r"\b(?:sales|revenue|warehouse|retail|manufacturing|clinical|people|hr|"
    r"human resources|finance|accounting|legal)\b"
)
_PROGRAM_OPERATIONS_RULES = (
    (re.compile(r"\bprogram\s+manager\b"), "program manager"),
    (re.compile(r"\bprogram\s+management\b"), "program management"),
    (re.compile(r"\boperations\s+(?:manager|lead|director|analyst|associate)\b"), "operations"),
    (re.compile(r"\b(?:head|vp|vice president)\s+of\s+operations\b"), "operations leadership"),
    (re.compile(r"\boperational\s+excellence\b"), "operational excellence"),
    (re.compile(r"\blaunch\s+operations\b"), "launch operations"),
)

_GROWTH_EXCLUSIONS = re.compile(
    r"\b(?:sales|account executive|business development|demand gen(?:eration)?|seo|"
    r"paid media|marketing)\b"
)
_GROWTH_RULES = (
    (re.compile(r"\bgrowth\s+strateg(?:y|ic)\b"), "growth strategy"),
    (re.compile(r"\bgrowth\s+(?:ops|operations)\b"), "growth operations"),
    (
        re.compile(r"\b(?:strateg(?:y|ic)|ops|operations)\s*(?:and|&)\s*growth\b"),
        "strategy or operations and growth",
    ),
    (
        re.compile(r"\bgrowth\s*(?:and|&)\s*(?:strateg(?:y|ic)|ops|operations)\b"),
        "growth and strategy or operations",
    ),
    (
        re.compile(r"\buser\s+growth\s+(?:strateg(?:y|ic)|ops|operations|project)\b"),
        "user growth strategy or operations",
    ),
)


def classify_role_title(title: str) -> RoleClassification:
    normalized = _normalize_title(title)
    for pattern, label in _PRODUCT_STRATEGY_RULES:
        if pattern.search(normalized):
            return RoleClassification(family=RoleFamily.PRODUCT_STRATEGY, matched_rule=label)
    for pattern, label in _PRODUCT_PM_RULES:
        if pattern.search(normalized):
            return RoleClassification(family=RoleFamily.PRODUCT_PM, matched_rule=label)
    for pattern, label in _BIZOPS_STRATEGY_RULES:
        if pattern.search(normalized):
            return RoleClassification(family=RoleFamily.BIZOPS_STRATEGY, matched_rule=label)
    if not _GROWTH_EXCLUSIONS.search(normalized):
        for pattern, label in _GROWTH_RULES:
            if pattern.search(normalized):
                return RoleClassification(family=RoleFamily.GROWTH_ADJACENT, matched_rule=label)
    if not _PROGRAM_OPERATIONS_EXCLUSIONS.search(normalized):
        for pattern, label in _PROGRAM_OPERATIONS_RULES:
            if pattern.search(normalized):
                return RoleClassification(
                    family=RoleFamily.PROGRAM_OPERATIONS,
                    matched_rule=label,
                )
    return RoleClassification(family=RoleFamily.OTHER, matched_rule="no monitored-family rule")


def build_role_surface_report(
    *,
    run_id: str,
    observations: Iterable[RoleObservation],
    source_runs: Iterable[SourceRun],
    coverage_floors: Mapping[RoleFamily, CoverageFloor] | None = None,
    generated_at: str | None = None,
) -> RoleSurfaceReport:
    """Build one-run coverage across the primary Product lane and adjacent guardrails."""

    run_id = _clean(run_id)
    observation_list = list(observations)
    source_run_list = list(source_runs)
    _validate_single_run(run_id, observation_list, source_run_list)
    source_run_map = _source_run_map(source_run_list)
    observed_sources = {item.source for item in observation_list}
    for source in sorted(observed_sources - set(source_run_map)):
        source_run_map[source] = SourceRun(
            run_id=run_id,
            source=source,
            status=SourceRunStatus.NOT_REPORTED,
            reason="Role observations exist, but this source did not report an explicit run status.",
        )

    unique_roles = _unique_roles(observation_list)
    unique_by_source = _unique_roles_by_source(observation_list)
    any_source_ran = any(item.status == SourceRunStatus.RAN for item in source_run_map.values())
    floors = {**DEFAULT_COVERAGE_FLOORS, **(coverage_floors or {})}
    family_coverage = [
        _family_coverage(family, unique_roles, floors.get(family), any_source_ran)
        for family in (*MONITORED_ROLE_FAMILIES, RoleFamily.OTHER)
    ]
    source_coverage = [
        _source_coverage(source_run, observation_list, unique_by_source)
        for source_run in sorted(source_run_map.values(), key=lambda item: item.source.casefold())
    ]
    source_family_coverage = [
        _source_family_coverage(source_run, family, unique_by_source)
        for source_run in sorted(source_run_map.values(), key=lambda item: item.source.casefold())
        for family in (*MONITORED_ROLE_FAMILIES, RoleFamily.OTHER)
    ]
    unclassified_roles = _unclassified_roles(unique_roles)
    warnings = _coverage_warnings(family_coverage, source_coverage)
    summary = _report_summary(
        observation_list,
        unique_roles,
        family_coverage,
        source_coverage,
    )
    summary_text = concise_role_surface_summary(summary)
    return RoleSurfaceReport(
        run_id=run_id,
        generated_at=generated_at or utc_now_iso(),
        summary=summary,
        summary_text=summary_text,
        family_coverage=family_coverage,
        source_coverage=source_coverage,
        source_family_coverage=source_family_coverage,
        unclassified_roles=unclassified_roles,
        warnings=warnings,
    )


def write_role_surface_artifacts(
    output_dir: Path,
    report: RoleSurfaceReport,
) -> RoleSurfaceArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_json = output_dir / "role_surface_report.json"
    family_csv = output_dir / "role_surface_by_family.csv"
    source_csv = output_dir / "role_surface_by_source.csv"
    source_family_csv = output_dir / "role_surface_by_source_family.csv"
    unclassified_csv = output_dir / "role_surface_unclassified.csv"

    report_json.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    _write_models_csv(family_csv, report.family_coverage)
    _write_models_csv(source_csv, report.source_coverage)
    _write_models_csv(source_family_csv, report.source_family_coverage)
    _write_models_csv(unclassified_csv, report.unclassified_roles)
    return RoleSurfaceArtifacts(
        report_json=report_json,
        family_csv=family_csv,
        source_csv=source_csv,
        source_family_csv=source_family_csv,
        unclassified_csv=unclassified_csv,
    )


def concise_role_surface_summary(summary: Mapping[str, object]) -> str:
    raw_gaps = summary.get("families_below_floor", [])
    gaps = raw_gaps if isinstance(raw_gaps, (list, tuple, set)) else []
    gap_text = ", ".join(str(item) for item in gaps) if gaps else "none"
    return (
        "Role surface: "
        f"{summary.get('unique_roles', 0)} unique roles from "
        f"{summary.get('sources_ran', 0)} ran sources; "
        f"Product/PM {summary.get('primary_product_roles', 0)}, "
        f"adjacent {summary.get('adjacent_roles', 0)}, "
        f"unclassified {summary.get('unclassified_roles', 0)}. "
        f"Coverage gaps: {gap_text}."
    )


def _validate_single_run(
    run_id: str,
    observations: list[RoleObservation],
    source_runs: list[SourceRun],
) -> None:
    if not run_id:
        raise ValueError("run_id is required")
    mismatches = {item.run_id for item in observations if item.run_id != run_id}
    mismatches.update(item.run_id for item in source_runs if item.run_id != run_id)
    if mismatches:
        mismatch_text = ", ".join(sorted(mismatches))
        raise ValueError(
            f"Role-surface inputs must be scoped to run {run_id!r}; found {mismatch_text}."
        )


def _source_run_map(source_runs: list[SourceRun]) -> dict[str, SourceRun]:
    output: dict[str, SourceRun] = {}
    for source_run in source_runs:
        if source_run.source in output:
            raise ValueError(f"Duplicate source run status for {source_run.source!r}.")
        output[source_run.source] = source_run
    return output


def _unique_roles(observations: list[RoleObservation]) -> dict[str, _UniqueRole]:
    output: dict[str, _UniqueRole] = {}
    for observation in observations:
        key = _role_key(observation)
        classification = classify_role_title(observation.title)
        existing = output.get(key)
        if existing is None:
            output[key] = _UniqueRole(
                title=observation.title,
                company=observation.company,
                family=classification.family,
                stage=observation.stage,
                sources={observation.source},
            )
            continue
        existing.sources.add(observation.source)
        if ROLE_STAGE_ORDER[observation.stage] > ROLE_STAGE_ORDER[existing.stage]:
            existing.stage = observation.stage
    return output


def _unique_roles_by_source(
    observations: list[RoleObservation],
) -> dict[tuple[str, str], _UniqueRole]:
    output: dict[tuple[str, str], _UniqueRole] = {}
    for observation in observations:
        key = (observation.source, _role_key(observation))
        classification = classify_role_title(observation.title)
        existing = output.get(key)
        if existing is None:
            output[key] = _UniqueRole(
                title=observation.title,
                company=observation.company,
                family=classification.family,
                stage=observation.stage,
                sources={observation.source},
            )
            continue
        if ROLE_STAGE_ORDER[observation.stage] > ROLE_STAGE_ORDER[existing.stage]:
            existing.stage = observation.stage
    return output


def _family_coverage(
    family: RoleFamily,
    unique_roles: dict[str, _UniqueRole],
    floor: CoverageFloor | None,
    any_source_ran: bool,
) -> FamilyCoverage:
    roles = [item for item in unique_roles.values() if item.family == family]
    discovered, scored, surfaced, acted = _stage_counts(roles)
    if family == RoleFamily.OTHER:
        floor = CoverageFloor(minimum_discovered=0, minimum_surfaced=0)
    floor = floor or CoverageFloor(minimum_discovered=0, minimum_surfaced=0)
    discovered_gap = max(0, floor.minimum_discovered - discovered)
    surfaced_gap = max(0, floor.minimum_surfaced - surfaced)
    if not any_source_ran:
        status = CoverageStatus.NO_SOURCE_RAN
    elif discovered_gap or surfaced_gap:
        status = CoverageStatus.MISSED
    else:
        status = CoverageStatus.MET
    return FamilyCoverage(
        family=family,
        label=ROLE_FAMILY_LABELS[family],
        lane=(
            "primary"
            if family == RoleFamily.PRODUCT_PM
            else "guardrail"
            if family != RoleFamily.OTHER
            else "audit"
        ),
        discovered=discovered,
        scored=scored,
        surfaced=surfaced,
        acted=acted,
        unique_companies=len({item.company.casefold() for item in roles}),
        minimum_discovered=floor.minimum_discovered,
        minimum_surfaced=floor.minimum_surfaced,
        discovered_gap=discovered_gap,
        surfaced_gap=surfaced_gap,
        coverage_status=status,
    )


def _source_coverage(
    source_run: SourceRun,
    observations: list[RoleObservation],
    unique_by_source: dict[tuple[str, str], _UniqueRole],
) -> SourceCoverage:
    source_observations = [item for item in observations if item.source == source_run.source]
    roles = [
        value for (source, _), value in unique_by_source.items() if source == source_run.source
    ]
    discovered, scored, surfaced, acted = _stage_counts(roles)
    monitored = sum(item.family != RoleFamily.OTHER for item in roles)
    return SourceCoverage(
        source=source_run.source,
        status=source_run.status,
        reason=source_run.reason,
        artifact=source_run.artifact,
        observations=len(source_observations),
        unique_roles=len(roles),
        discovered=discovered,
        scored=scored,
        surfaced=surfaced,
        acted=acted,
        monitored_roles=monitored,
        unclassified_roles=len(roles) - monitored,
    )


def _source_family_coverage(
    source_run: SourceRun,
    family: RoleFamily,
    unique_by_source: dict[tuple[str, str], _UniqueRole],
) -> SourceFamilyCoverage:
    roles = [
        value
        for (source, _), value in unique_by_source.items()
        if source == source_run.source and value.family == family
    ]
    discovered, scored, surfaced, acted = _stage_counts(roles)
    return SourceFamilyCoverage(
        source=source_run.source,
        source_status=source_run.status,
        family=family,
        label=ROLE_FAMILY_LABELS[family],
        discovered=discovered,
        scored=scored,
        surfaced=surfaced,
        acted=acted,
        unique_companies=len({item.company.casefold() for item in roles}),
    )


def _stage_counts(roles: Iterable[_UniqueRole]) -> tuple[int, int, int, int]:
    role_list = list(roles)
    discovered = len(role_list)
    scored = sum(
        ROLE_STAGE_ORDER[item.stage] >= ROLE_STAGE_ORDER[RoleStage.SCORED]
        for item in role_list
    )
    surfaced = sum(
        ROLE_STAGE_ORDER[item.stage] >= ROLE_STAGE_ORDER[RoleStage.SURFACED]
        for item in role_list
    )
    acted = sum(item.stage == RoleStage.ACTED for item in role_list)
    return discovered, scored, surfaced, acted


def _unclassified_roles(
    unique_roles: dict[str, _UniqueRole],
) -> list[UnclassifiedRole]:
    return sorted(
        (
            UnclassifiedRole(
                title=item.title,
                company=item.company,
                sources=sorted(item.sources),
            )
            for item in unique_roles.values()
            if item.family == RoleFamily.OTHER
        ),
        key=lambda item: (item.company.casefold(), item.title.casefold()),
    )


def _coverage_warnings(
    families: list[FamilyCoverage],
    sources: list[SourceCoverage],
) -> list[str]:
    warnings: list[str] = []
    if not any(source.status == SourceRunStatus.RAN for source in sources):
        warnings.append("No source was explicitly recorded as ran; coverage cannot be judged.")
    for source in sources:
        if source.status == SourceRunStatus.NOT_REPORTED:
            warnings.append(
                f"{source.source}: observations exist but the source did not report a run status."
            )
    for family in families:
        if family.family != RoleFamily.OTHER and family.coverage_status == CoverageStatus.MISSED:
            warnings.append(
                f"{family.label}: below floor "
                f"(discovered {family.discovered}/{family.minimum_discovered}, "
                f"surfaced {family.surfaced}/{family.minimum_surfaced})."
            )
    return warnings


def _report_summary(
    observations: list[RoleObservation],
    unique_roles: dict[str, _UniqueRole],
    families: list[FamilyCoverage],
    sources: list[SourceCoverage],
) -> dict[str, object]:
    family_map = {item.family: item for item in families}
    adjacent_roles = sum(
        family_map[family].discovered
        for family in MONITORED_ROLE_FAMILIES
        if family != RoleFamily.PRODUCT_PM
    )
    return {
        "observations": len(observations),
        "unique_roles": len(unique_roles),
        "unique_companies": len(
            {item.company.casefold() for item in unique_roles.values()}
        ),
        "primary_product_roles": family_map[RoleFamily.PRODUCT_PM].discovered,
        "adjacent_roles": adjacent_roles,
        "unclassified_roles": family_map[RoleFamily.OTHER].discovered,
        "sources_ran": sum(item.status == SourceRunStatus.RAN for item in sources),
        "sources_skipped": sum(item.status == SourceRunStatus.SKIPPED for item in sources),
        "sources_failed": sum(item.status == SourceRunStatus.FAILED for item in sources),
        "sources_not_reported": sum(
            item.status == SourceRunStatus.NOT_REPORTED for item in sources
        ),
        "families_below_floor": [
            item.label
            for item in families
            if item.family != RoleFamily.OTHER and item.coverage_status == CoverageStatus.MISSED
        ],
    }


def _write_models_csv(path: Path, rows: Iterable[BaseModel]) -> None:
    row_list = list(rows)
    if not row_list:
        path.write_text("", encoding="utf-8")
        return
    dict_rows = [row.model_dump(mode="json") for row in row_list]
    fieldnames = list(dict_rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in dict_rows:
            writer.writerow(
                {
                    key: ";".join(value) if isinstance(value, list) else value
                    for key, value in row.items()
                }
            )


def _role_key(observation: RoleObservation) -> str:
    return "|".join(
        [
            _normalize_identity(observation.company),
            _normalize_title(observation.title),
            _normalize_identity(observation.location),
        ]
    )


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9&]+", " ", value.casefold()).strip()


def _normalize_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()

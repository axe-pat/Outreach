from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from outreach.role_surface_monitor import (
    ROLE_FAMILY_LABELS,
    RoleFamily,
    classify_role_title,
)
from outreach.tracking import ContactRecord, OrganizationRecord, OrganizationType, OutreachWorkbook


SCHEMA_VERSION = "1.1"

# This queue is a planning boundary, not a send boundary. "ready_for_next_stage"
# means the item can move to the existing draft/review flow; it never authorizes a send.
READY = "ready_for_next_stage"
REVIEW_REQUIRED = "human_review_required"
BUFFERED = "buffered"

WARM_CONTACT_STATUSES = {"accepted", "connected", "warm", "replied", "responded"}
STRONG_WARM_CONTACT_STATUSES = {"warm", "replied", "responded"}
STARTUP_TARGET_TAGS = {
    "startup",
    "yc",
    "built_in",
    "builtin",
    "company_watchlist",
    "company-watchlist",
}
STRATEGIC_ACCOUNT_TAGS = {"strategic", "wishlist", "dream"}
ADJACENT_ROLE_FAMILIES = {
    RoleFamily.PRODUCT_STRATEGY,
    RoleFamily.BIZOPS_STRATEGY,
    RoleFamily.PROGRAM_OPERATIONS,
    RoleFamily.GROWTH_ADJACENT,
}
STRONG_ADJACENT_MIN_FIT_SCORE = 7.0
STRONG_ROLE_BUCKETS = {
    "application_plus_outreach",
    "application_only",
    "scored_application_selected",
}

ACTION_PRIORITY = {
    "application_plus_outreach": 100,
    "follow_up_warm_contact": 95,
    "follow_up_existing_outreach": 90,
    "application_only": 85,
    "application_research": 82,
    "warm_company_outreach": 80,
    "company_outreach": 70,
    "application_review": 60,
    "research_company": 40,
    "role_watch": 25,
}

# ResumeGenerator already performs source-specific scoring and gating. This map
# normalizes its output buckets instead of reimplementing that policy here.
BUCKET_ACTIONS = {
    "application_plus_outreach": "application_plus_outreach",
    "application_only": "application_only",
    "scored_application_selected": "application_review",
    "unscored_coverage_candidates": "application_review",
    "outreach_only_today": "company_outreach",
    "relationship_buffer": "research_company",
    "follow_up": "follow_up_existing_outreach",
}


class DiscoveryProvenance(BaseModel):
    source_name: str
    source_type: str
    source_run_id: str
    source_url: str = ""
    observed_at: str = ""
    context: str = ""


class SharedRole(BaseModel):
    role_id: str
    title: str
    location: str = ""
    status: str = ""
    fit_score: float | None = None
    source: str = ""
    source_url: str = ""
    queue_bucket: str = ""
    role_family: str = ""
    role_family_label: str = ""
    classification_rule: str = ""
    decision: str = ""
    write_gate: str = ""
    strong_adjacent_role: bool = False


class WarmContact(BaseModel):
    contact_id: str
    full_name: str
    title: str = ""
    status: str
    linkedin_url: str = ""
    email: str = ""
    last_contacted_at: str = ""


class SharedDailyQueueItem(BaseModel):
    queue_id: str
    company: str
    normalized_company: str
    organization_id: str = ""
    organization_type: str = ""
    website: str = ""
    company_url: str = ""
    city: str = ""
    target_lists: list[str] = Field(default_factory=list)
    primary_action: str
    recommended_actions: list[str]
    gate: str
    priority_score: float
    priority_reasons: list[str]
    roles: list[SharedRole] = Field(default_factory=list)
    warm_contacts: list[WarmContact] = Field(default_factory=list)
    role_watch_state: str = ""
    review_state: str = ""
    source_types: list[str] = Field(default_factory=list)
    provenance: list[DiscoveryProvenance] = Field(default_factory=list)


class SharedDailyQueue(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    generated_at: str
    scope: str = "run_scoped_resume_generator_queue"
    inputs: dict[str, str]
    source_coverage: dict[str, dict[str, object]]
    summary: dict[str, object]
    items: list[SharedDailyQueueItem]


@dataclass(frozen=True)
class SharedQueueArtifacts:
    run_json: Path
    run_csv: Path
    current_json: Path
    current_csv: Path


@dataclass
class _CompanyAccumulator:
    normalized_company: str
    company_names: list[str] = field(default_factory=list)
    organization_id: str = ""
    organization_type: str = ""
    website: str = ""
    company_url: str = ""
    city: str = ""
    target_lists: set[str] = field(default_factory=set)
    actions: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    roles: dict[str, SharedRole] = field(default_factory=dict)
    warm_contacts: dict[str, WarmContact] = field(default_factory=dict)
    review_state: str = ""
    source_types: set[str] = field(default_factory=set)
    provenance: dict[tuple[str, ...], DiscoveryProvenance] = field(default_factory=dict)
    evidence_scores: list[float] = field(default_factory=list)
    observation_count: int = 0
    strategic_role_watch: bool = False
    adjacent_role_trigger_ids: set[str] = field(default_factory=set)

    def add_provenance(self, value: DiscoveryProvenance) -> None:
        key = (
            value.source_type.casefold(),
            value.source_name.casefold(),
            value.source_run_id,
            _normalize_url(value.source_url),
            value.context.casefold(),
        )
        self.provenance.setdefault(key, value)
        self.source_types.add(value.source_type)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_company_name(value: str) -> str:
    """Return a conservative identity key shared by both recruiting repos."""

    cleaned = (
        str(value or "")
        .casefold()
        .replace("&", " and ")
        .replace("™", "")
        .replace("®", "")
    )
    tokens = re.findall(r"[a-z0-9]+", cleaned)
    while tokens and tokens[-1] in {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "company",
        "co",
    }:
        tokens.pop()
    return "".join(tokens)


def resolve_run_scoped_action_queue(
    *,
    nightly_summary_path: Path | None = None,
    action_queue_path: Path | None = None,
) -> tuple[Path, dict[str, object], str, dict[str, str]]:
    """Resolve one exact ResumeGenerator queue without any latest-file fallback."""

    if nightly_summary_path is None and action_queue_path is None:
        raise ValueError("Provide an exact nightly summary or an exact action queue path.")

    summary: dict[str, object] = {}
    inputs: dict[str, str] = {}
    expected_queue: Path | None = None
    run_id = ""
    if nightly_summary_path is not None:
        summary = _load_json_object(nightly_summary_path)
        status = _clean(summary.get("action_queue_status"))
        if status != "current_run":
            raise ValueError(
                "Nightly summary does not mark its action queue as current_run; "
                f"got {status or 'missing'} from {nightly_summary_path}."
            )
        expected_value = _clean(summary.get("action_queue"))
        if not expected_value:
            raise ValueError(f"Nightly summary has no action_queue: {nightly_summary_path}")
        expected_queue = _artifact_path(expected_value, relative_to=nightly_summary_path.parent)
        inputs["nightly_summary"] = str(nightly_summary_path)
        source_metrics_value = _clean(summary.get("source_metrics"))
        if source_metrics_value:
            source_metrics_path = _artifact_path(
                source_metrics_value,
                relative_to=nightly_summary_path.parent,
            )
            inputs["source_metrics"] = str(source_metrics_path)
            if source_metrics_path.exists():
                metrics = _load_json_object(source_metrics_path)
                action_ref = metrics.get("action_queue")
                metric_artifact = (
                    _clean(action_ref.get("artifact"))
                    if isinstance(action_ref, dict)
                    else ""
                )
                if metric_artifact:
                    metric_queue = _artifact_path(
                        metric_artifact,
                        relative_to=source_metrics_path.parent,
                    )
                    if not _same_path(metric_queue, expected_queue):
                        raise ValueError(
                            "Nightly summary and source metrics reference different action queues: "
                            f"{expected_queue} != {metric_queue}"
                        )
        run_id = nightly_summary_path.stem.removesuffix("-nightly-pipeline-summary")

    selected_queue = action_queue_path or expected_queue
    if selected_queue is None:
        raise ValueError("Could not resolve an action queue path.")
    selected_queue = Path(selected_queue).expanduser().resolve(strict=False)
    if expected_queue is not None and not _same_path(selected_queue, expected_queue):
        raise ValueError(
            "The explicitly supplied action queue does not match the nightly summary: "
            f"{selected_queue} != {expected_queue}"
        )
    if not selected_queue.exists():
        raise FileNotFoundError(f"Action queue does not exist: {selected_queue}")

    payload = _load_json_object(selected_queue)
    inputs["resume_generator_action_queue"] = str(selected_queue)
    queue_inputs = payload.get("inputs")
    if isinstance(queue_inputs, dict):
        for key in ("source_breadth", "startup_source_report", "current_apply_queue"):
            value = _clean(queue_inputs.get(key))
            if value:
                inputs[f"resume_generator_{key}"] = value
    run_id = run_id or selected_queue.stem.removesuffix("-daily-action-queue")
    return selected_queue, payload, run_id, inputs


def build_shared_daily_queue(
    *,
    action_queue_payload: dict[str, object],
    run_id: str,
    action_queue_path: Path,
    workspace: Path | None = None,
    watchlist_path: Path | None = None,
    include_warm_targets: bool = True,
    warm_startups_only: bool = True,
    include_relationship_buffer: bool = True,
    limit: int | None = None,
    generated_at: str | None = None,
    extra_inputs: dict[str, str] | None = None,
) -> SharedDailyQueue:
    """Normalize and merge application, company, and warm-network candidates."""

    accumulators: dict[str, _CompanyAccumulator] = {}
    bucket_counts: Counter[str] = Counter()
    source_observation_counts: Counter[str] = Counter()
    action_observations = 0
    startup_observations = 0

    for bucket, action in BUCKET_ACTIONS.items():
        if bucket == "relationship_buffer" and not include_relationship_buffer:
            continue
        rows = _dict_rows(action_queue_payload.get(bucket))
        # Older reports expose this bucket under score_for_application.
        if bucket == "unscored_coverage_candidates" and not rows:
            rows = _dict_rows(action_queue_payload.get("score_for_application"))
        for row in rows:
            company = _first(row, "company", "organization_name", "company_name")
            normalized = normalize_company_name(company)
            if not normalized:
                continue
            accumulator = accumulators.setdefault(
                normalized,
                _CompanyAccumulator(normalized_company=normalized),
            )
            accumulator.company_names.append(company)
            accumulator.actions.add(action)
            accumulator.observation_count += 1
            action_observations += 1
            bucket_counts[bucket] += 1
            source = _first(row, "source", "lane_source") or bucket
            source_type = _source_type(source, bucket)
            source_observation_counts[source_type] += 1
            if source_type == "startup_company_source":
                startup_observations += 1
            role = _add_role(accumulator, row, bucket=bucket, source=source)
            role_context = ""
            if role is not None:
                role_context = (
                    f"; role={role.title}; role_family={role.role_family}; "
                    f"strong_adjacent={str(role.strong_adjacent_role).lower()}"
                )
            accumulator.add_provenance(
                DiscoveryProvenance(
                    source_name=source,
                    source_type=source_type,
                    source_run_id=run_id,
                    source_url=_first(row, "url", "company_url", "source_item_url"),
                    observed_at=_clean(action_queue_payload.get("generated_at")),
                    context=f"ResumeGenerator daily queue bucket={bucket}{role_context}",
                )
            )
            accumulator.reasons.extend(_string_list(row.get("reasons")))
            accumulator.reasons.extend(_derived_row_reasons(row, bucket))
            score = _first_float(row, "fit_score", "relationship_score")
            if score is not None:
                accumulator.evidence_scores.append(score)
            accumulator.company_url = accumulator.company_url or _first(
                row,
                "company_url",
                "source_item_url",
            )
            accumulator.city = accumulator.city or _first(row, "city", "location")

    workbook_stats = {
        "status": "skipped",
        "organizations_scanned": 0,
        "warm_contacts_scanned": 0,
        "warm_company_observations": 0,
        "strategic_accounts_watched": 0,
    }
    if workspace is not None and workspace.exists():
        workbook = OutreachWorkbook(workspace)
        organizations = workbook.list_organizations()
        contacts = workbook.list_contacts()
        workbook_stats = _merge_workbook_state(
            accumulators,
            organizations=organizations,
            contacts=contacts,
            run_id=run_id,
            workspace=workspace,
            include_warm_targets=include_warm_targets,
            warm_startups_only=warm_startups_only,
        )

    role_watch_stats: dict[str, object] = {
        "status": workbook_stats.get("status", "skipped"),
        "artifact": str(workspace or ""),
        "accounts_watched": workbook_stats.get("strategic_accounts_watched", 0),
        "candidate_rows_scanned": 0,
        "candidate_rows_added": 0,
        "triggered_accounts": sum(
            1 for accumulator in accumulators.values() if accumulator.adjacent_role_trigger_ids
        ),
    }
    if workbook_stats.get("status") == "loaded":
        candidate_stats = _merge_strategic_role_candidates(
            accumulators,
            action_queue_payload=action_queue_payload,
            run_id=run_id,
        )
        candidate_rows_added = int(candidate_stats["candidate_rows_added"])
        action_observations += candidate_rows_added
        bucket_counts["scored_application_not_selected"] += candidate_rows_added
        source_observation_counts["resume_generator_role"] += candidate_rows_added
        role_watch_stats.update(candidate_stats)
        role_watch_stats["triggered_accounts"] = sum(
            1 for accumulator in accumulators.values() if accumulator.adjacent_role_trigger_ids
        )

    watchlist_stats: dict[str, object] = {
        "status": "not_found" if watchlist_path is not None else "skipped",
        "approved_entries": 0,
        "observations": 0,
    }
    if watchlist_path is not None and watchlist_path.exists():
        watchlist_stats = _merge_approved_watchlist(
            accumulators,
            path=watchlist_path,
            run_id=run_id,
        )

    all_items = [_finalize(accumulator) for accumulator in accumulators.values()]
    all_items.sort(
        key=lambda item: (
            item.priority_score,
            ACTION_PRIORITY.get(item.primary_action, 0),
            item.company.casefold(),
        ),
        reverse=True,
    )
    total_before_limit = len(all_items)
    if limit is not None and limit > 0:
        all_items = all_items[:limit]

    action_counts = Counter(item.primary_action for item in all_items)
    gate_counts = Counter(item.gate for item in all_items)
    role_watch_counts = Counter(
        item.role_watch_state for item in all_items if item.role_watch_state
    )
    source_type_counts = Counter(
        source_type for item in all_items for source_type in item.source_types
    )
    inputs = {
        "resume_generator_action_queue": str(action_queue_path),
        "outreach_workspace": str(workspace or ""),
        "company_watchlist": str(watchlist_path or ""),
        **(extra_inputs or {}),
    }
    source_coverage: dict[str, dict[str, object]] = {
        "resume_generator_daily_action_queue": {
            "status": "loaded",
            "artifact": str(action_queue_path),
            "observations": action_observations,
            "bucket_counts": dict(sorted(bucket_counts.items())),
        },
        "resume_generator_roles": {
            "status": (
                "loaded" if source_observation_counts["resume_generator_role"] else "zero"
            ),
            "artifact": str(action_queue_path),
            "observations": source_observation_counts["resume_generator_role"],
        },
        "yc_builtin_company_sources": {
            "status": "loaded" if startup_observations else "zero",
            "artifact": _queue_input(action_queue_payload, "startup_source_report"),
            "observations": startup_observations,
        },
        "outreach_warm_companies": workbook_stats,
        "strategic_account_role_watch": role_watch_stats,
        "approved_company_watchlist": {
            **watchlist_stats,
            "artifact": str(watchlist_path or ""),
        },
    }
    summary: dict[str, object] = {
        "observations_received": sum(
            accumulator.observation_count for accumulator in accumulators.values()
        ),
        "unique_companies_before_limit": total_before_limit,
        "items_returned": len(all_items),
        "items_suppressed_by_limit": total_before_limit - len(all_items),
        "duplicates_merged": max(
            0,
            sum(accumulator.observation_count for accumulator in accumulators.values())
            - total_before_limit,
        ),
        "primary_action_counts": dict(sorted(action_counts.items())),
        "gate_counts": dict(sorted(gate_counts.items())),
        "role_watch_state_counts": dict(sorted(role_watch_counts.items())),
        "source_type_counts": dict(sorted(source_type_counts.items())),
    }
    scope_parts = ["run_scoped_resume_generator_queue"]
    if workspace is not None and workspace.exists():
        scope_parts.append("outreach_workspace_snapshot")
    if watchlist_path is not None and watchlist_path.exists():
        scope_parts.append("approved_watchlist_snapshot")
    return SharedDailyQueue(
        run_id=run_id,
        generated_at=generated_at or utc_now_iso(),
        scope=" + ".join(scope_parts),
        inputs=inputs,
        source_coverage=source_coverage,
        summary=summary,
        items=all_items,
    )


def write_shared_daily_queue(
    output_dir: Path,
    queue: SharedDailyQueue,
) -> SharedQueueArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _artifact_stamp(queue.run_id, queue.generated_at)
    run_json = output_dir / f"{stamp}-shared-daily-queue.json"
    run_csv = output_dir / f"{stamp}-shared-daily-queue.csv"
    current_json = output_dir / "shared_daily_queue.json"
    current_csv = output_dir / "shared_daily_queue.csv"
    payload = queue.model_dump(mode="json")
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    _write_queue_csv(run_csv, queue)
    _atomic_write_text(run_json, serialized)
    # Publish the convenience CSV first and JSON last. Each file carries the
    # run id, so readers can detect the brief cross-file handoff window.
    _write_queue_csv(current_csv, queue)
    _atomic_write_text(current_json, serialized)
    return SharedQueueArtifacts(
        run_json=run_json,
        run_csv=run_csv,
        current_json=current_json,
        current_csv=current_csv,
    )


def _merge_workbook_state(
    accumulators: dict[str, _CompanyAccumulator],
    *,
    organizations: Iterable[OrganizationRecord],
    contacts: Iterable[ContactRecord],
    run_id: str,
    workspace: Path,
    include_warm_targets: bool,
    warm_startups_only: bool,
) -> dict[str, object]:
    organization_list = list(organizations)
    organization_by_id = {item.organization_id: item for item in organization_list}
    organization_by_name = {
        normalize_company_name(item.name): item
        for item in organization_list
        if normalize_company_name(item.name)
    }
    warm_by_org: dict[str, list[ContactRecord]] = defaultdict(list)
    warm_count = 0
    for contact in contacts:
        if _normalize_status(contact.status) not in WARM_CONTACT_STATUSES:
            continue
        warm_count += 1
        warm_by_org[contact.organization_id].append(contact)

    # Enrich ResumeGenerator observations with stable Outreach entity metadata.
    for normalized, accumulator in list(accumulators.items()):
        organization = organization_by_name.get(normalized)
        if organization is not None:
            _merge_organization(accumulator, organization)

    # Strategic companies remain company-level tracker rows even when no live
    # PM opening exists. Represent that durable state as a low-priority watch
    # task in the shared queue rather than creating another mutable tracker.
    strategic_accounts_watched = 0
    for organization in organization_list:
        if not _is_strategic_account(organization):
            continue
        normalized = normalize_company_name(organization.name)
        if not normalized:
            continue
        accumulator = accumulators.setdefault(
            normalized,
            _CompanyAccumulator(normalized_company=normalized),
        )
        accumulator.company_names.append(organization.name)
        _merge_organization(accumulator, organization)
        accumulator.strategic_role_watch = True
        accumulator.actions.add("role_watch")
        accumulator.observation_count += 1
        strategic_accounts_watched += 1
        accumulator.reasons.append("strategic_account_role_watch")
        accumulator.add_provenance(
            DiscoveryProvenance(
                source_name="Outreach strategic account tracker",
                source_type="outreach_strategic_account",
                source_run_id=run_id,
                source_url=(
                    organization.source_url
                    or organization.linkedin_url
                    or organization.website
                ),
                observed_at=organization.last_updated_at,
                context=(
                    "Company-level strategic account remains active while the "
                    "exact ResumeGenerator run is watched for strong adjacent roles"
                ),
            )
        )
        _refresh_strategic_role_watch(accumulator)

    warm_observations = 0
    if include_warm_targets:
        for organization_id, warm_contacts in warm_by_org.items():
            organization = organization_by_id.get(organization_id)
            if organization is None:
                continue
            if warm_startups_only and not _is_startup_target(organization):
                continue
            normalized = normalize_company_name(organization.name)
            if not normalized:
                continue
            accumulator = accumulators.setdefault(
                normalized,
                _CompanyAccumulator(normalized_company=normalized),
            )
            accumulator.company_names.append(organization.name)
            _merge_organization(accumulator, organization)
            statuses = {_normalize_status(contact.status) for contact in warm_contacts}
            accumulator.actions.add(
                "follow_up_warm_contact"
                if statuses.intersection(STRONG_WARM_CONTACT_STATUSES)
                else "warm_company_outreach"
            )
            accumulator.observation_count += 1
            warm_observations += 1
            accumulator.add_provenance(
                DiscoveryProvenance(
                    source_name="Outreach relationship tracker",
                    source_type="outreach_warm_network",
                    source_run_id=run_id,
                    source_url=organization.source_url or organization.linkedin_url,
                    observed_at=organization.last_updated_at,
                    context=f"{len(warm_contacts)} warm/connected contact(s)",
                )
            )
            for contact in warm_contacts:
                accumulator.warm_contacts[contact.contact_id] = WarmContact(
                    contact_id=contact.contact_id,
                    full_name=contact.full_name,
                    title=contact.title,
                    status=contact.status,
                    linkedin_url=contact.linkedin_url,
                    email=contact.email,
                    last_contacted_at=contact.last_contacted_at,
                )
            accumulator.reasons.append(
                f"warm_network={len(warm_contacts)} contact(s): "
                + ", ".join(sorted(statuses))
            )

    return {
        "status": "loaded",
        "artifact": str(workspace),
        "organizations_scanned": len(organization_list),
        "warm_contacts_scanned": warm_count,
        "warm_company_observations": warm_observations,
        "strategic_accounts_watched": strategic_accounts_watched,
        "strategic_adjacent_role_triggers": sum(
            1 for accumulator in accumulators.values() if accumulator.adjacent_role_trigger_ids
        ),
        "warm_startups_only": warm_startups_only,
    }


def _merge_approved_watchlist(
    accumulators: dict[str, _CompanyAccumulator],
    *,
    path: Path,
    run_id: str,
) -> dict[str, object]:
    payload = _load_json_object(path)
    entries = _dict_rows(payload.get("entries"))
    approved_entries = 0
    observations = 0
    watchlist_run_id = _clean(payload.get("run_id")) or run_id
    for entry in entries:
        if _normalize_status(entry.get("review_state")) != "approved":
            continue
        company = _first(entry, "company_name", "company")
        normalized = normalize_company_name(company)
        if not normalized:
            continue
        approved_entries += 1
        observations += 1
        accumulator = accumulators.setdefault(
            normalized,
            _CompanyAccumulator(normalized_company=normalized),
        )
        accumulator.company_names.append(company)
        accumulator.website = accumulator.website or _first(entry, "website")
        accumulator.company_url = accumulator.company_url or _first(
            entry,
            "linkedin_company_url",
            "website",
        )
        accumulator.review_state = "approved"
        accumulator.actions.add("company_outreach")
        accumulator.observation_count += 1
        rubric_total = _as_float(entry.get("rubric_total"))
        if rubric_total is not None:
            accumulator.evidence_scores.append(rubric_total)
            accumulator.reasons.append(f"approved_watchlist_rubric={rubric_total:g}/15")
        raw_provenance = _dict_rows(entry.get("provenance"))
        if raw_provenance:
            for provenance in raw_provenance:
                accumulator.add_provenance(
                    DiscoveryProvenance(
                        source_name=_first(provenance, "source_name") or "Approved company watchlist",
                        source_type=_first(provenance, "source_type") or "approved_company_watchlist",
                        source_run_id=_first(provenance, "source_run_id") or watchlist_run_id,
                        source_url=_first(provenance, "source_url"),
                        observed_at=_first(provenance, "observed_at"),
                        context=_first(provenance, "context"),
                    )
                )
        accumulator.add_provenance(
            DiscoveryProvenance(
                source_name="Approved company watchlist",
                source_type="approved_company_watchlist",
                source_run_id=watchlist_run_id,
                source_url=str(path),
                observed_at=_clean(payload.get("generated_at")),
                context="Human approved and rubric-qualified company candidate",
            )
        )
    return {
        "status": "loaded",
        "approved_entries": approved_entries,
        "observations": observations,
    }


def _merge_organization(
    accumulator: _CompanyAccumulator,
    organization: OrganizationRecord,
) -> None:
    accumulator.organization_id = organization.organization_id
    accumulator.organization_type = organization.organization_type.value
    accumulator.website = organization.website or accumulator.website
    accumulator.company_url = (
        organization.linkedin_url
        or organization.source_url
        or accumulator.company_url
    )
    accumulator.city = organization.city or accumulator.city
    accumulator.target_lists.update(_tags(organization.target_lists))


def _merge_strategic_role_candidates(
    accumulators: dict[str, _CompanyAccumulator],
    *,
    action_queue_payload: dict[str, object],
    run_id: str,
) -> dict[str, object]:
    """Recover only strong adjacent roles omitted from the normal queue surface."""

    bucket = "scored_application_not_selected"
    rows = _dict_rows(action_queue_payload.get(bucket))
    added = 0
    for row in rows:
        company = _first(row, "company", "organization_name", "company_name")
        accumulator = accumulators.get(normalize_company_name(company))
        if accumulator is None or not accumulator.strategic_role_watch:
            continue
        if not _is_strong_adjacent_role(row, bucket=bucket):
            continue
        source = _first(row, "source", "lane_source") or bucket
        role = _add_role(accumulator, row, bucket=bucket, source=source)
        if role is None:
            continue
        accumulator.company_names.append(company)
        accumulator.observation_count += 1
        added += 1
        if role.fit_score is not None:
            accumulator.evidence_scores.append(role.fit_score)
        accumulator.reasons.extend(_string_list(row.get("reasons")))
        accumulator.reasons.append(
            f"role_watch_recovered={role.role_family_label}: {role.title}"
        )
        accumulator.add_provenance(
            DiscoveryProvenance(
                source_name=source,
                source_type="resume_generator_role",
                source_run_id=run_id,
                source_url=role.source_url,
                observed_at=_clean(action_queue_payload.get("generated_at")),
                context=(
                    f"Strategic role watch bucket={bucket}; role={role.title}; "
                    f"role_family={role.role_family}; fit_score={role.fit_score}; "
                    f"decision={role.decision}; write_gate={role.write_gate}"
                ),
            )
        )
        _refresh_strategic_role_watch(accumulator)
    return {
        "candidate_rows_scanned": len(rows),
        "candidate_rows_added": added,
    }


def _add_role(
    accumulator: _CompanyAccumulator,
    row: dict[str, Any],
    *,
    bucket: str,
    source: str,
) -> SharedRole | None:
    title = _first(row, "role_title", "title", "job_title")
    if not title:
        return None
    source_url = _first(row, "url", "source_url", "job_url")
    role_key = _normalize_url(source_url) or re.sub(r"\W+", "", title.casefold())
    role_id_seed = source_url or f"{accumulator.normalized_company}|{title}"
    classification = classify_role_title(title)
    role = SharedRole(
        role_id="role-" + hashlib.sha1(role_id_seed.encode("utf-8")).hexdigest()[:12],
        title=title,
        location=_first(row, "location"),
        status=_first(row, "status", "existing_status", "post_score_status"),
        fit_score=_first_float(row, "fit_score", "existing_fit_score"),
        source=source,
        source_url=source_url,
        queue_bucket=bucket,
        role_family=classification.family.value,
        role_family_label=ROLE_FAMILY_LABELS[classification.family],
        classification_rule=classification.matched_rule,
        decision=_first(row, "decision"),
        write_gate=_first(row, "write_gate"),
        strong_adjacent_role=_is_strong_adjacent_role(row, bucket=bucket),
    )
    existing = accumulator.roles.get(role_key)
    if existing is None or (role.fit_score or -1) > (existing.fit_score or -1):
        accumulator.roles[role_key] = role
        return role
    return existing


def _is_strong_adjacent_role(row: dict[str, Any], *, bucket: str) -> bool:
    title = _first(row, "role_title", "title", "job_title")
    source_url = _first(row, "url", "source_url", "job_url")
    if not title or not source_url:
        return False
    classification = classify_role_title(title)
    if classification.family not in ADJACENT_ROLE_FAMILIES:
        return False
    if bucket in STRONG_ROLE_BUCKETS:
        return True
    if bucket != "scored_application_not_selected":
        return False
    reasons = {value.casefold() for value in _string_list(row.get("reasons"))}
    if any("blocklisted_company" in value for value in reasons):
        return False
    return bool(
        _normalize_status(row.get("decision")) == "proceed"
        and _normalize_status(row.get("write_gate")) == "accepted"
        and (_first_float(row, "fit_score") or 0) >= STRONG_ADJACENT_MIN_FIT_SCORE
    )


def _refresh_strategic_role_watch(accumulator: _CompanyAccumulator) -> None:
    if not accumulator.strategic_role_watch:
        return
    strong_roles = [
        role for role in accumulator.roles.values() if role.strong_adjacent_role
    ]
    if not strong_roles:
        return
    accumulator.actions.add("application_research")
    for role in strong_roles:
        accumulator.adjacent_role_trigger_ids.add(role.role_id)
        accumulator.reasons.append(
            f"strong_adjacent_role={role.role_family_label}: {role.title}"
        )


def _finalize(accumulator: _CompanyAccumulator) -> SharedDailyQueueItem:
    actions = sorted(
        accumulator.actions,
        key=lambda action: (ACTION_PRIORITY.get(action, 0), action),
        reverse=True,
    )
    primary_action = actions[0] if actions else "research_company"
    if primary_action in {"research_company", "role_watch"}:
        gate = BUFFERED
    elif primary_action in {
        "application_research",
        "application_review",
        "follow_up_existing_outreach",
    }:
        gate = REVIEW_REQUIRED
    elif (
        primary_action == "company_outreach"
        and accumulator.review_state != "approved"
        and not accumulator.warm_contacts
    ):
        gate = REVIEW_REQUIRED
    else:
        gate = READY

    company = _preferred_company_name(accumulator.company_names)
    roles = sorted(
        accumulator.roles.values(),
        key=lambda role: (role.fit_score if role.fit_score is not None else -1, role.title.casefold()),
        reverse=True,
    )
    warm_contacts = sorted(
        accumulator.warm_contacts.values(),
        key=lambda contact: (
            _warm_contact_weight(contact.status),
            contact.last_contacted_at,
            contact.full_name.casefold(),
        ),
        reverse=True,
    )
    priority_score, generated_reasons = _priority_score(
        primary_action=primary_action,
        evidence_scores=accumulator.evidence_scores,
        warm_contacts=warm_contacts,
        source_types=accumulator.source_types,
        approved=accumulator.review_state == "approved",
    )
    reasons = _dedupe_strings([*generated_reasons, *accumulator.reasons])
    provenance = sorted(
        accumulator.provenance.values(),
        key=lambda item: (item.observed_at, item.source_type, item.source_name, item.source_url),
    )
    return SharedDailyQueueItem(
        queue_id="shared-" + hashlib.sha1(accumulator.normalized_company.encode()).hexdigest()[:12],
        company=company,
        normalized_company=accumulator.normalized_company,
        organization_id=accumulator.organization_id,
        organization_type=accumulator.organization_type,
        website=accumulator.website,
        company_url=accumulator.company_url,
        city=accumulator.city,
        target_lists=sorted(accumulator.target_lists),
        primary_action=primary_action,
        recommended_actions=actions,
        gate=gate,
        priority_score=priority_score,
        priority_reasons=reasons,
        roles=roles,
        warm_contacts=warm_contacts,
        role_watch_state=(
            "triggered"
            if accumulator.adjacent_role_trigger_ids
            else ("watching" if accumulator.strategic_role_watch else "")
        ),
        review_state=accumulator.review_state,
        source_types=sorted(accumulator.source_types),
        provenance=provenance,
    )


def _priority_score(
    *,
    primary_action: str,
    evidence_scores: list[float],
    warm_contacts: list[WarmContact],
    source_types: set[str],
    approved: bool,
) -> tuple[float, list[str]]:
    base = float(ACTION_PRIORITY.get(primary_action, 0))
    reasons = [f"action={primary_action}"]
    evidence_bonus = min(max(evidence_scores, default=0.0), 15.0)
    if evidence_bonus:
        base += evidence_bonus
        reasons.append(f"best_fit_or_relationship_score={evidence_bonus:g}")
    warm_bonus = max((_warm_contact_weight(item.status) for item in warm_contacts), default=0)
    if warm_bonus:
        base += warm_bonus
        reasons.append(f"warm_contact_bonus={warm_bonus}")
    cross_source_bonus = min(max(len(source_types) - 1, 0) * 2, 6)
    if cross_source_bonus:
        base += cross_source_bonus
        reasons.append(f"cross_source_bonus={cross_source_bonus}")
    if approved:
        base += 5
        reasons.append("human_approved_watchlist")
    return round(base, 2), reasons


def _write_queue_csv(path: Path, queue: SharedDailyQueue) -> None:
    fields = [
        "run_id",
        "generated_at",
        "scope",
        "rank",
        "queue_id",
        "company",
        "organization_id",
        "primary_action",
        "recommended_actions",
        "gate",
        "priority_score",
        "review_state",
        "role_watch_state",
        "role_count",
        "top_roles",
        "role_provenance_json",
        "warm_contact_count",
        "warm_contacts",
        "source_types",
        "target_lists",
        "website",
        "company_url",
        "priority_reasons",
        "provenance_json",
    ]
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    for rank, item in enumerate(queue.items, start=1):
        writer.writerow(
            {
                "run_id": queue.run_id,
                "generated_at": queue.generated_at,
                "scope": queue.scope,
                "rank": rank,
                "queue_id": item.queue_id,
                "company": item.company,
                "organization_id": item.organization_id,
                "primary_action": item.primary_action,
                "recommended_actions": ";".join(item.recommended_actions),
                "gate": item.gate,
                "priority_score": item.priority_score,
                "review_state": item.review_state,
                "role_watch_state": item.role_watch_state,
                "role_count": len(item.roles),
                "top_roles": " | ".join(role.title for role in item.roles[:3]),
                "role_provenance_json": json.dumps(
                    [value.model_dump(mode="json") for value in item.roles],
                    separators=(",", ":"),
                ),
                "warm_contact_count": len(item.warm_contacts),
                "warm_contacts": " | ".join(
                    f"{contact.full_name} ({contact.status})"
                    for contact in item.warm_contacts[:5]
                ),
                "source_types": ";".join(item.source_types),
                "target_lists": ";".join(item.target_lists),
                "website": item.website,
                "company_url": item.company_url,
                "priority_reasons": " | ".join(item.priority_reasons),
                "provenance_json": json.dumps(
                    [value.model_dump(mode="json") for value in item.provenance],
                    separators=(",", ":"),
                ),
            }
        )
    _atomic_write_text(path, handle.getvalue())


def _atomic_write_text(path: Path, content: str) -> None:
    """Publish complete queue artifacts without exposing truncated files."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _source_type(source: str, bucket: str) -> str:
    normalized = source.casefold()
    if normalized == "current_apply_queue" or bucket in {
        "application_plus_outreach",
        "application_only",
        "scored_application_selected",
        "unscored_coverage_candidates",
    }:
        return "resume_generator_role"
    if normalized.startswith("startup_org:") or any(
        token in normalized for token in ("yc_", "builtin_")
    ):
        return "startup_company_source"
    return "resume_generator_daily_queue"


def _derived_row_reasons(row: dict[str, Any], bucket: str) -> list[str]:
    values = [f"resume_queue_bucket={bucket}"]
    relationship_score = _as_float(row.get("relationship_score"))
    if relationship_score is not None:
        values.append(f"relationship_score={relationship_score:g}")
    recommended_action = _clean(row.get("recommended_action"))
    if recommended_action:
        values.append(f"upstream_action={recommended_action}")
    return values


def _is_startup_target(organization: OrganizationRecord) -> bool:
    if organization.organization_type in {
        OrganizationType.STARTUP,
        OrganizationType.ACCELERATOR,
        OrganizationType.INCUBATOR,
        OrganizationType.HACKER_HOUSE,
    }:
        return True
    return bool(_tags(organization.target_lists).intersection(STARTUP_TARGET_TAGS))


def _is_strategic_account(organization: OrganizationRecord) -> bool:
    if _tags(organization.target_lists).intersection(STRATEGIC_ACCOUNT_TAGS):
        return True
    if _normalize_status(organization.status) == "strategic target":
        return True
    return "seed_source=built_in_strategic_accounts" in organization.notes.casefold()


def _warm_contact_weight(status: str) -> int:
    return {
        "replied": 10,
        "responded": 10,
        "warm": 8,
        "accepted": 5,
        "connected": 5,
    }.get(_normalize_status(status), 0)


def _preferred_company_name(names: Iterable[str]) -> str:
    candidates = _dedupe_strings(names)
    if not candidates:
        return "Unknown"
    return sorted(candidates, key=lambda value: (len(value), value.casefold()))[0]


def _artifact_stamp(run_id: str, generated_at: str) -> str:
    match = re.search(r"(20\d{6})[-T_]?([0-2]\d[0-5]\d[0-5]\d)", run_id)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    try:
        value = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        value = datetime.now(UTC)
    return value.strftime("%Y%m%d-%H%M%S")


def _queue_input(payload: dict[str, object], key: str) -> str:
    inputs = payload.get("inputs")
    return _clean(inputs.get(key)) if isinstance(inputs, dict) else ""


def _artifact_path(value: str, *, relative_to: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else relative_to / path


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return payload


def _dict_rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_status(value: object) -> str:
    return _clean(value).casefold()


def _normalize_url(value: object) -> str:
    return _clean(value).casefold().rstrip("/")


def _first(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean(row.get(key))
        if value:
            return value
    return ""


def _as_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _as_float(row.get(key))
        if value is not None:
            return value
    return None


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    cleaned = _clean(value)
    return [cleaned] if cleaned else []


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    unique: dict[str, str] = {}
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            unique.setdefault(cleaned.casefold(), cleaned)
    return list(unique.values())


def _tags(value: str) -> set[str]:
    return {
        tag.strip().casefold()
        for tag in re.split(r"[;,]", value or "")
        if tag.strip()
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build one run-stamped queue from an exact ResumeGenerator action queue "
            "plus YC/Built In, warm Outreach, and approved-watchlist snapshots."
        )
    )
    parser.add_argument("--nightly-summary", type=Path)
    parser.add_argument("--action-queue", type=Path)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    parser.add_argument("--watchlist", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("workspace/shared_discovery"),
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--no-warm-targets", action="store_true")
    parser.add_argument("--all-warm-companies", action="store_true")
    parser.add_argument("--no-relationship-buffer", action="store_true")
    args = parser.parse_args(argv)
    if args.nightly_summary is None and args.action_queue is None:
        parser.error("one of --nightly-summary or --action-queue is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    action_path, payload, resolved_run_id, run_inputs = resolve_run_scoped_action_queue(
        nightly_summary_path=args.nightly_summary,
        action_queue_path=args.action_queue,
    )
    workspace = args.workspace.expanduser().resolve(strict=False)
    output_dir = args.output_dir.expanduser().resolve(strict=False)
    watchlist = (
        args.watchlist.expanduser().resolve(strict=False)
        if args.watchlist is not None
        else None
    )
    if watchlist is None:
        default_watchlist = workspace / "company_discovery" / "company_watchlist.json"
        watchlist = default_watchlist if default_watchlist.exists() else None
    queue = build_shared_daily_queue(
        action_queue_payload=payload,
        run_id=args.run_id or resolved_run_id,
        action_queue_path=action_path,
        workspace=workspace,
        watchlist_path=watchlist,
        include_warm_targets=not args.no_warm_targets,
        warm_startups_only=not args.all_warm_companies,
        include_relationship_buffer=not args.no_relationship_buffer,
        limit=args.limit,
        extra_inputs=run_inputs,
    )
    artifacts = write_shared_daily_queue(output_dir, queue)
    print(f"Shared queue: {len(queue.items)} companies")
    print(f"Actions: {queue.summary.get('primary_action_counts', {})}")
    print(f"JSON: {artifacts.run_json}")
    print(f"CSV: {artifacts.run_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

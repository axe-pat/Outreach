from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from outreach.company_watchlist import (
    CandidateCompanySignal,
    CandidateProvenance,
    CompanyFitRubric,
    RubricDimension,
)
from outreach.linkedin_signals import normalize_extracted_company
from outreach.role_surface_monitor import (
    RoleObservation,
    RoleStage,
    SourceRun,
    SourceRunStatus,
)


def company_signals_from_feed_ledger(
    path: Path,
    *,
    run_id: str,
    known_companies: Iterable[str] = (),
    signal_ids: Iterable[str] | None = None,
    observed_at: str = "",
) -> list[CandidateCompanySignal]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    signals: list[CandidateCompanySignal] = []
    known_names = [item.strip() for item in known_companies if item.strip()]
    known = {item.casefold() for item in known_names}
    known_by_token = {_company_token(item): item for item in known_names}
    selected_ids = (
        {item.strip() for item in signal_ids if item.strip()}
        if signal_ids is not None
        else None
    )
    for row in rows:
        signal_id = str(row.get("signal_id") or "").strip()
        if selected_ids is not None and signal_id not in selected_ids:
            continue
        if selected_ids is None and observed_at and not _observed_in_snapshot(row, observed_at):
            continue
        post_text = str(row.get("post_text") or "")
        company = normalize_extracted_company(
            str(row.get("company") or ""),
            post_text,
        )
        company = _company_page_identity(
            company,
            author_name=str(row.get("author_name") or ""),
            company_url=str(row.get("company_url") or ""),
            known_by_token=known_by_token,
        )
        kinds = {item.strip() for item in str(row.get("signal_kinds") or "").split(";") if item.strip()}
        disposition = str(row.get("review_disposition") or "pending").strip()
        if not company or disposition == "dismissed":
            continue
        if company.casefold() in known:
            continue
        if disposition != "company_candidate" and not kinds.intersection({"company_discovery", "startup_discovery"}):
            continue
        text = " ".join(
            str(row.get(key) or "")
            for key in ("post_text", "context", "relevance_reason", "signal_kinds")
        )
        rubric = _feed_rubric(text, kinds)
        first_seen_at = str(row.get("first_seen_at") or row.get("last_seen_at") or "")
        provenance_run_id = run_id
        if selected_ids is None and not observed_at and first_seen_at:
            provenance_run_id = f"linkedin-feed:{first_seen_at}"
        signals.append(
            CandidateCompanySignal(
                company_name=company,
                linkedin_company_url=str(row.get("company_url") or ""),
                description=post_text or str(row.get("context") or ""),
                rubric=rubric,
                provenance=[
                    CandidateProvenance(
                        source_name="LinkedIn home feed",
                        source_type="linkedin_home_feed",
                        source_run_id=provenance_run_id,
                        source_url=str(
                            row.get("post_url")
                            or row.get("company_url")
                            or row.get("author_url")
                            or ""
                        ),
                        observed_at=str(row.get("last_seen_at") or row.get("first_seen_at") or ""),
                        signal_type=";".join(sorted(kinds)) or "company_discovery",
                        author_or_actor=str(row.get("author_name") or ""),
                        context=str(row.get("post_text") or row.get("context") or ""),
                    )
                ],
            )
        )
    return signals


def _company_page_identity(
    company: str,
    *,
    author_name: str,
    company_url: str,
    known_by_token: dict[str, str],
) -> str:
    """Prefer a LinkedIn company-page identity when DOM extraction used a person."""

    match = re.search(r"linkedin\.com/company/([^/?#]+)", company_url, re.IGNORECASE)
    if match is None:
        return company
    slug = match.group(1)
    slug_token = _company_token(slug)
    company_token = _company_token(company)
    author_token = _company_token(author_name)
    if not slug_token or company_token == slug_token:
        return company
    if company_token and company_token != author_token:
        return company
    known_name = known_by_token.get(slug_token)
    if known_name:
        return known_name
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", slug) if part)


def _company_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def company_signals_from_source_metrics(
    source_metrics_path: Path,
    *,
    run_id: str,
    known_companies: Iterable[str] = (),
) -> list[CandidateCompanySignal]:
    """Turn this run's independent startup discovery into reviewable companies."""

    metrics = _load_json(source_metrics_path)
    sources = metrics.get("sources") if isinstance(metrics.get("sources"), dict) else {}
    report_ref = (
        metrics.get("startup_source_report")
        if isinstance(metrics.get("startup_source_report"), dict)
        else {}
    )
    artifact_value = str((report_ref or {}).get("artifact") or "")
    report = _load_json(Path(artifact_value)) if artifact_value else {}
    known = {item.strip().casefold() for item in known_companies if item.strip()}
    observed_at = str(report.get("generated_at") or metrics.get("generated_at") or "")
    signals: list[CandidateCompanySignal] = []
    relationship_rows = _nested_items(report, "relationship_lane")
    run_started_at = str(metrics.get("run_started_at") or "")
    if run_started_at:
        relationship_rows = [
            row
            for row in relationship_rows
            if _relationship_item_is_current(row, report, run_started_at)
        ]
    relationship_status = _metric_status(sources, "startup_relationship")
    startup_apply_status = _metric_status(sources, "startup_apply")
    lanes = (
        (
            "startup_relationship",
            relationship_rows if relationship_status in {"", "ran"} else [],
        ),
        (
            "startup_apply",
            _nested_items(report, "startup_apply")
            if startup_apply_status in {"", "ran"}
            else [],
        ),
    )
    for lane_name, rows in lanes:
        for row in rows:
            company = _first(row, "organization_name", "company", "company_name")
            if not company or company.casefold() in known:
                continue
            source_id = _first(row, "source_id", "source") or lane_name
            source_url = _first(
                row,
                "source_item_url",
                "company_url",
                "url",
                "job_url",
                "jobs_url",
            )
            description = _startup_context(row)
            signals.append(
                CandidateCompanySignal(
                    company_name=company,
                    website=_first(row, "website"),
                    description=description,
                    rubric=_startup_rubric(row, description),
                    provenance=[
                        CandidateProvenance(
                            source_name=source_id,
                            source_type=lane_name,
                            source_run_id=run_id,
                            source_url=source_url,
                            observed_at=observed_at,
                            signal_type=str(row.get("verdict") or lane_name),
                            context=description,
                        )
                    ],
                )
            )
    return signals


def _observed_in_snapshot(row: dict[str, str], observed_at: str) -> bool:
    if str(row.get("last_seen_at") or "") == observed_at:
        return True
    try:
        history = json.loads(str(row.get("observation_history_json") or "[]"))
    except json.JSONDecodeError:
        return False
    return isinstance(history, list) and observed_at in history


def role_inputs_from_source_metrics(
    source_metrics_path: Path,
    *,
    run_id: str,
) -> tuple[list[RoleObservation], list[SourceRun]]:
    payload = _load_json(source_metrics_path)
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    observations: list[RoleObservation] = []
    source_runs: list[SourceRun] = []
    for source, metric in sources.items():
        metric = metric if isinstance(metric, dict) else {}
        status_value = str(metric.get("status") or "not_reported")
        status = _source_status(status_value)
        details = metric.get("details") if isinstance(metric.get("details"), dict) else {}
        artifact_paths = _artifact_paths(details)
        source_runs.append(
            SourceRun(
                run_id=run_id,
                source=str(source),
                status=status,
                reason=str(metric.get("reason") or ""),
                artifact=str(artifact_paths[0][0]) if artifact_paths else "",
            )
        )
        for artifact_path, stage in artifact_paths:
            for row in _artifact_role_rows(artifact_path):
                title = _first(row, "title", "job_title", "role_title", "position")
                company = _first(row, "company", "company_name", "organization")
                if not title or not company:
                    continue
                observations.append(
                    RoleObservation(
                        run_id=run_id,
                        source=str(source),
                        title=title,
                        company=company,
                        stage=_stage_for_row(row, stage),
                        source_url=_first(row, "url", "job_url", "source_url", "linkedin_url"),
                        location=_first(row, "location", "job_location"),
                        external_role_id=_first(row, "id", "job_id", "row_id"),
                    )
                )

    action_queue = payload.get("action_queue") if isinstance(payload.get("action_queue"), dict) else {}
    action_artifact_value = str((action_queue or {}).get("artifact") or "")
    action_artifact = (
        _repair_path(Path(action_artifact_value)) if action_artifact_value else None
    )
    if action_artifact is not None and action_artifact.exists() and action_artifact.is_file():
        source_runs.append(SourceRun(run_id=run_id, source="action_queue", status=SourceRunStatus.RAN, artifact=str(action_artifact)))
        for row in _artifact_role_rows(action_artifact):
            title = _first(row, "title", "job_title", "role_title", "position")
            company = _first(row, "company", "company_name", "organization")
            if title and company:
                observations.append(
                    RoleObservation(
                        run_id=run_id,
                        source="action_queue",
                        title=title,
                        company=company,
                        stage=RoleStage.SURFACED,
                        source_url=_first(row, "url", "job_url", "source_url", "linkedin_url"),
                        location=_first(row, "location", "job_location"),
                    )
                )
    return observations, _dedupe_source_runs(source_runs)


def _feed_rubric(text: str, kinds: set[str]) -> CompanyFitRubric:
    lower = text.lower()
    domain_groups = {
        "data/platform": ("data", "platform", "developer", "api", "infrastructure", "observability"),
        "ai/workflow": (" ai ", "agent", "automation", "workflow", "machine learning"),
        "marketplace/operations": ("marketplace", "logistics", "fleet", "mobility", "operations"),
        "fintech": ("fintech", "payments", "billing", "finance", "banking"),
        "health": ("health", "clinical", "provider", "patient"),
        "robotics": ("robot", "hardware", "autonomous", "manufacturing"),
    }
    matched = [name for name, tokens in domain_groups.items() if any(token in lower for token in tokens)]
    domain_score = min(3, len(matched) + (1 if matched else 0))
    story_score = 2 if matched else 1
    growth_score = 3 if {"funding", "hiring", "launch"}.intersection(kinds) else 2 if "startup_discovery" in kinds else 1
    role_score = 3 if "job" in kinds and any(token in lower for token in ("product", "strategy", "operations", "program", "growth")) else 2 if {"job", "hiring"}.intersection(kinds) else 1
    return CompanyFitRubric(
        domain_fit=RubricDimension(score=domain_score, evidence=", ".join(matched) or "needs domain research"),
        technical_mba_story=RubricDimension(score=story_score, evidence=("credible story bridge: " + ", ".join(matched)) if matched else "needs story research"),
        geography_remote=RubricDimension(score=1, evidence="feed signal does not establish location; review required"),
        growth_quality=RubricDimension(score=growth_score, evidence="feed signals: " + ", ".join(sorted(kinds))),
        role_surface=RubricDimension(score=role_score, evidence="feed signals: " + ", ".join(sorted(kinds))),
    )


def _startup_rubric(row: dict[str, object], context: str) -> CompanyFitRubric:
    lower = context.casefold()
    target_domain_tokens = (
        " ai",
        "agent",
        "data",
        "developer",
        "platform",
        "workflow",
        "enterprise",
        "marketplace",
        "fintech",
        "health",
        "robot",
        "mobility",
        "operations",
    )
    domain_matches = sorted({token.strip() for token in target_domain_tokens if token in lower})
    domain_score = 3 if domain_matches else 1
    story_score = 3 if any(token in lower for token in (" ai", "data", "developer", "technical", "platform")) else 2
    location = _first(row, "location", "city").casefold()
    if any(token in location for token in ("san francisco", "los angeles", "new york", "remote")):
        geography_score = 3
    elif location:
        geography_score = 2
    else:
        geography_score = 1
    growth_signals = [
        signal
        for signal in ("yc-backed", "active hiring", "recent yc batch", "small-team", "venture")
        if signal in lower
    ]
    growth_score = 3 if growth_signals else 2
    role_score = 2 if any(token in lower for token in ("hiring", "jobs", "product", "strategy", "operations", "growth")) else 1
    return CompanyFitRubric(
        domain_fit=RubricDimension(
            score=domain_score,
            evidence=", ".join(domain_matches) or "startup source; domain needs human review",
        ),
        technical_mba_story=RubricDimension(
            score=story_score,
            evidence="technical/operator story signals from startup description and tags",
        ),
        geography_remote=RubricDimension(
            score=geography_score,
            evidence=location or "location needs human review",
        ),
        growth_quality=RubricDimension(
            score=growth_score,
            evidence=", ".join(growth_signals) or "curated startup-source quality signal",
        ),
        role_surface=RubricDimension(
            score=role_score,
            evidence="hiring/role evidence in source" if role_score == 2 else "role surface needs research",
        ),
    )


def _nested_items(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    lane = payload.get(key) if isinstance(payload.get(key), dict) else {}
    items = (lane or {}).get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _startup_context(row: dict[str, object]) -> str:
    reasons = row.get("reasons") if isinstance(row.get("reasons"), list) else []
    tags = row.get("tags") if isinstance(row.get("tags"), list) else []
    parts = [
        _first(row, "description", "signal_title", "role_title", "title"),
        "; ".join(str(item) for item in reasons if str(item).strip()),
        "tags: " + ", ".join(str(item) for item in tags if str(item).strip()) if tags else "",
        "team: " + _first(row, "team_size") if _first(row, "team_size") else "",
        "batch: " + _first(row, "batch") if _first(row, "batch") else "",
        "location: " + _first(row, "location", "city") if _first(row, "location", "city") else "",
    ]
    return " | ".join(part for part in parts if part)


def _artifact_paths(details: dict[str, object]) -> list[tuple[Path, RoleStage]]:
    result: list[tuple[Path, RoleStage]] = []
    for key, value in details.items():
        if "artifact" not in key or not isinstance(value, str) or not value:
            continue
        path = _repair_path(Path(value))
        if not path.exists():
            continue
        stage = RoleStage.SCORED if "scored" in key else RoleStage.DISCOVERED
        result.append((path, stage))
    return result


def _artifact_role_rows(path: Path) -> list[dict[str, object]]:
    payload = _load_json(path)
    rows: list[dict[str, object]] = []
    named_keys = {"jobs", "results", "items", "selected", "candidates"}
    for key in named_keys:
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    for key, value in payload.items():
        if key in named_keys:
            continue
        if not isinstance(value, list):
            continue
        if value and all(isinstance(item, dict) for item in value):
            rows.extend(item for item in value if isinstance(item, dict))
    return rows


def _stage_for_row(row: dict[str, object], default: RoleStage) -> RoleStage:
    status = _first(row, "status", "decision", "verdict").lower()
    if status in {"applied", "sent", "acted"}:
        return RoleStage.ACTED
    if status in {"proceed", "queued", "selected", "surfaced"}:
        return RoleStage.SURFACED
    return default


def _source_status(value: str) -> SourceRunStatus:
    normalized = value.strip().lower()
    if normalized == "ran":
        return SourceRunStatus.RAN
    if normalized == "skipped":
        return SourceRunStatus.SKIPPED
    if normalized in {"failed", "timeout", "timed_out"}:
        return SourceRunStatus.FAILED
    return SourceRunStatus.NOT_REPORTED


def _dedupe_source_runs(items: Iterable[SourceRun]) -> list[SourceRun]:
    result: dict[str, SourceRun] = {}
    for item in items:
        result[item.source] = item
    return list(result.values())


def _first(row: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _load_json(path: Path) -> dict[str, object]:
    path = _repair_path(path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _repair_path(path: Path) -> Path:
    if path.exists():
        return path
    repaired = Path(str(path).replace("/Claude projects/", "/Claude Projects/"))
    return repaired if repaired.exists() else path


def _metric_status(sources: object, key: str) -> str:
    if not isinstance(sources, dict):
        return ""
    metric = sources.get(key)
    return str(metric.get("status") or "").strip().lower() if isinstance(metric, dict) else ""


def _relationship_item_is_current(
    row: dict[str, object],
    report: dict[str, object],
    run_started_at: str,
) -> bool:
    lane = report.get("relationship_lane")
    artifacts = lane.get("artifacts") if isinstance(lane, dict) else {}
    source_id = _first(row, "source_id", "source")
    metadata = artifacts.get(source_id) if isinstance(artifacts, dict) else {}
    artifact_value = str(metadata.get("artifact") or "") if isinstance(metadata, dict) else ""
    if not artifact_value:
        return False
    artifact = _repair_path(Path(artifact_value))
    if not artifact.exists() or not artifact.is_file():
        return False
    try:
        started = datetime.fromisoformat(run_started_at.replace("Z", "+00:00"))
        started_epoch = started.timestamp()
    except ValueError:
        return False
    return artifact.stat().st_mtime >= started_epoch

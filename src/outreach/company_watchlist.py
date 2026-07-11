from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator


SCHEMA_VERSION = "1.0"
PROMOTION_SCORE_THRESHOLD = 10
RUBRIC_DIMENSIONS = (
    "domain_fit",
    "technical_mba_story",
    "geography_remote",
    "growth_quality",
    "role_surface",
)

RUBRIC_GUIDANCE = {
    "scale": {
        "0": "No evidence or a material mismatch.",
        "1": "Plausible but weak or still needs research.",
        "2": "Clear positive evidence.",
        "3": "Exceptional fit with specific evidence.",
    },
    "dimensions": {
        "domain_fit": "Fit with target domains, products, and problem spaces.",
        "technical_mba_story": (
            "Strength of the credible engineering plus MBA/product/operator story."
        ),
        "geography_remote": "US geography, relocation, or remote-work viability.",
        "growth_quality": "Company quality, trajectory, team, funding, or product momentum.",
        "role_surface": (
            "Plausible Product/PM or adjacent Product Strategy, BizOps/Strategy, "
            "Program/Ops, or narrow Growth role surface."
        ),
    },
    "promotion_rule": (
        f"Recommend promotion at {PROMOTION_SCORE_THRESHOLD}/15 or above only when every "
        "dimension has at least some evidence; a human approval is still required."
    ),
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class ReviewState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    NEEDS_RESEARCH = "needs_research"
    REJECTED = "rejected"


class PromotionRecommendation(str, Enum):
    PROMOTE = "promote"
    RESEARCH = "research"
    PASS = "pass"


class RubricDimension(BaseModel):
    score: int = Field(default=0, ge=0, le=3)
    evidence: str = ""

    @field_validator("evidence")
    @classmethod
    def clean_evidence(cls, value: str) -> str:
        return _clean(value)


class CompanyFitRubric(BaseModel):
    domain_fit: RubricDimension = Field(default_factory=RubricDimension)
    technical_mba_story: RubricDimension = Field(default_factory=RubricDimension)
    geography_remote: RubricDimension = Field(default_factory=RubricDimension)
    growth_quality: RubricDimension = Field(default_factory=RubricDimension)
    role_surface: RubricDimension = Field(default_factory=RubricDimension)

    @property
    def total(self) -> int:
        return sum(getattr(self, dimension).score for dimension in RUBRIC_DIMENSIONS)


class CandidateProvenance(BaseModel):
    source_name: str
    source_type: str
    source_run_id: str
    source_url: str = ""
    observed_at: str = Field(default_factory=utc_now_iso)
    signal_type: str = "company_discovery"
    author_or_actor: str = ""
    context: str = ""

    @field_validator("source_name", "source_type", "source_run_id")
    @classmethod
    def require_provenance_identity(cls, value: str) -> str:
        value = _clean(value)
        if not value:
            raise ValueError("source_name, source_type, and source_run_id are required")
        return value

    @field_validator("source_url", "observed_at", "signal_type", "author_or_actor", "context")
    @classmethod
    def clean_optional_provenance(cls, value: str) -> str:
        return _clean(value)


class CandidateCompanySignal(BaseModel):
    company_name: str
    website: str = ""
    linkedin_company_url: str = ""
    description: str = ""
    rubric: CompanyFitRubric = Field(default_factory=CompanyFitRubric)
    provenance: list[CandidateProvenance] = Field(min_length=1)

    @field_validator("company_name")
    @classmethod
    def require_company_name(cls, value: str) -> str:
        value = _clean(value)
        if not value:
            raise ValueError("company_name is required")
        return value

    @field_validator("website", "linkedin_company_url", "description")
    @classmethod
    def clean_company_fields(cls, value: str) -> str:
        return _clean(value)


class CompanyReviewDecision(BaseModel):
    candidate_id: str = ""
    company_name: str = ""
    website: str = ""
    review_state: ReviewState = ReviewState.PENDING
    reviewer: str = ""
    reviewed_at: str = ""
    reviewer_notes: str = ""

    @field_validator(
        "candidate_id", "company_name", "website", "reviewer", "reviewed_at", "reviewer_notes"
    )
    @classmethod
    def clean_review_fields(cls, value: str) -> str:
        return _clean(value)


class CandidateCompany(BaseModel):
    candidate_id: str
    company_name: str
    website: str = ""
    linkedin_company_url: str = ""
    description: str = ""
    rubric: CompanyFitRubric
    rubric_total: int
    recommendation: PromotionRecommendation
    recommendation_reasons: list[str] = Field(default_factory=list)
    provenance: list[CandidateProvenance]
    first_seen_at: str = ""
    last_seen_at: str = ""
    review_state: ReviewState = ReviewState.PENDING
    reviewer: str = ""
    reviewed_at: str = ""
    reviewer_notes: str = ""
    watchlist_eligible: bool = False


class CompanyWatchlistEntry(BaseModel):
    candidate_id: str
    company_name: str
    website: str = ""
    linkedin_company_url: str = ""
    description: str = ""
    rubric: CompanyFitRubric
    rubric_total: int
    review_state: ReviewState
    reviewer: str = ""
    reviewed_at: str = ""
    reviewer_notes: str = ""
    provenance: list[CandidateProvenance]
    promoted_at: str


@dataclass(frozen=True)
class CompanyDiscoveryArtifacts:
    payload_json: Path
    review_queue_csv: Path
    watchlist_json: Path
    watchlist_csv: Path
    summary_json: Path


def build_candidate_review_queue(
    signals: Iterable[CandidateCompanySignal],
    *,
    review_decisions: Iterable[CompanyReviewDecision] = (),
) -> list[CandidateCompany]:
    """Dedupe discovery signals, score them, and apply explicit human review decisions."""

    candidates = [_candidate_from_group(group) for group in _dedupe_signal_groups(list(signals))]
    decisions = list(review_decisions)
    return sorted(
        (_apply_review(candidate, _find_review(candidate, decisions)) for candidate in candidates),
        key=lambda item: (-item.rubric_total, item.company_name.casefold()),
    )


def build_company_watchlist(
    candidates: Iterable[CandidateCompany],
    *,
    promoted_at: str | None = None,
) -> list[CompanyWatchlistEntry]:
    """Promote only high-fit candidates that a human explicitly approved."""

    timestamp = promoted_at or utc_now_iso()
    return [
        CompanyWatchlistEntry(
            candidate_id=candidate.candidate_id,
            company_name=candidate.company_name,
            website=candidate.website,
            linkedin_company_url=candidate.linkedin_company_url,
            description=candidate.description,
            rubric=candidate.rubric,
            rubric_total=candidate.rubric_total,
            review_state=candidate.review_state,
            reviewer=candidate.reviewer,
            reviewed_at=candidate.reviewed_at,
            reviewer_notes=candidate.reviewer_notes,
            provenance=candidate.provenance,
            promoted_at=timestamp,
        )
        for candidate in candidates
        if candidate.watchlist_eligible
    ]


def load_company_review_decisions(path: Path) -> list[CompanyReviewDecision]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        decisions: list[CompanyReviewDecision] = []
        for row in rows:
            state = _clean(row.get("review_state")) or ReviewState.PENDING.value
            decisions.append(
                CompanyReviewDecision(
                    candidate_id=_clean(row.get("candidate_id")),
                    company_name=_clean(row.get("company_name")),
                    website=_clean(row.get("website")),
                    review_state=ReviewState(state),
                    reviewer=_clean(row.get("reviewer")),
                    reviewed_at=_clean(row.get("reviewed_at")),
                    reviewer_notes=_clean(row.get("reviewer_notes")),
                )
            )
    return decisions


def write_company_discovery_artifacts(
    output_dir: Path,
    *,
    run_id: str,
    signals: Iterable[CandidateCompanySignal],
    review_decisions: Iterable[CompanyReviewDecision] = (),
    generated_at: str | None = None,
) -> CompanyDiscoveryArtifacts:
    """Write a reusable JSON payload, editable review CSV, and approved watchlist artifacts."""

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at or utc_now_iso()
    signal_list = list(signals)
    decision_list = list(review_decisions)
    candidates = build_candidate_review_queue(signal_list, review_decisions=decision_list)
    fresh_watchlist = build_company_watchlist(candidates, promoted_at=timestamp)

    payload_json = output_dir / "company_discovery_candidates.json"
    review_queue_csv = output_dir / "company_discovery_review.csv"
    watchlist_json = output_dir / "company_watchlist.json"
    watchlist_csv = output_dir / "company_watchlist.csv"
    summary_json = output_dir / "company_discovery_summary.json"
    previous_watchlist = _load_existing_watchlist_entries(watchlist_json)
    watchlist = _merge_cumulative_watchlist(
        fresh=fresh_watchlist,
        previous=previous_watchlist,
        current_candidates=candidates,
        review_decisions=decision_list,
    )
    summary = company_discovery_summary(signal_list, candidates, watchlist)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _clean(run_id),
        "generated_at": timestamp,
        "rubric_guidance": RUBRIC_GUIDANCE,
        "summary": summary,
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
    }
    payload_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_candidate_csv(review_queue_csv, candidates)
    watchlist_json.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": _clean(run_id),
                "generated_at": timestamp,
                "entries": [entry.model_dump(mode="json") for entry in watchlist],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_watchlist_csv(watchlist_csv, watchlist)
    summary_json.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": _clean(run_id),
                "generated_at": timestamp,
                **summary,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return CompanyDiscoveryArtifacts(
        payload_json=payload_json,
        review_queue_csv=review_queue_csv,
        watchlist_json=watchlist_json,
        watchlist_csv=watchlist_csv,
        summary_json=summary_json,
    )


def _load_existing_watchlist_entries(path: Path) -> list[CompanyWatchlistEntry]:
    """Load the durable approved set without silently discarding corrupt state."""

    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Existing company watchlist is unreadable: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ValueError(f"Existing company watchlist has invalid entries: {path}")
    try:
        return [CompanyWatchlistEntry.model_validate(item) for item in payload["entries"]]
    except ValueError as exc:
        raise ValueError(f"Existing company watchlist contains an invalid entry: {path}") from exc


def _merge_cumulative_watchlist(
    *,
    fresh: list[CompanyWatchlistEntry],
    previous: list[CompanyWatchlistEntry],
    current_candidates: list[CandidateCompany],
    review_decisions: list[CompanyReviewDecision],
) -> list[CompanyWatchlistEntry]:
    """Keep prior approvals when known-company filtering removes their signals.

    A candidate present in the current rebuild is authoritative: if it is no
    longer eligible, its previous watchlist entry is removed. An explicit
    non-approved decision is also authoritative even when the candidate's
    signals are absent from this rebuild.
    """

    fresh_by_company = {
        _normalize_company_name(entry.company_name): entry for entry in fresh
    }
    current_companies = {
        _normalize_company_name(candidate.company_name) for candidate in current_candidates
    }
    merged = dict(fresh_by_company)
    for entry in previous:
        company_key = _normalize_company_name(entry.company_name)
        if not company_key or company_key in merged or company_key in current_companies:
            continue
        decision = _watchlist_entry_review_decision(entry, review_decisions)
        if decision is not None and decision.review_state != ReviewState.APPROVED:
            continue
        merged[company_key] = entry
    return sorted(merged.values(), key=lambda entry: entry.company_name.casefold())


def _watchlist_entry_review_decision(
    entry: CompanyWatchlistEntry,
    decisions: list[CompanyReviewDecision],
) -> CompanyReviewDecision | None:
    for decision in decisions:
        if decision.candidate_id and decision.candidate_id == entry.candidate_id:
            return decision
    entry_domain = _website_domain(entry.website)
    if entry_domain:
        for decision in decisions:
            if _website_domain(decision.website) == entry_domain:
                return decision
    entry_name = _normalize_company_name(entry.company_name)
    return next(
        (
            decision
            for decision in decisions
            if decision.company_name
            and _normalize_company_name(decision.company_name) == entry_name
        ),
        None,
    )


def company_discovery_summary(
    signals: Iterable[CandidateCompanySignal],
    candidates: Iterable[CandidateCompany],
    watchlist: Iterable[CompanyWatchlistEntry],
) -> dict[str, object]:
    signal_list = list(signals)
    candidate_list = list(candidates)
    watchlist_entries = list(watchlist)
    source_counts = Counter(
        provenance.source_type
        for signal in signal_list
        for provenance in signal.provenance
    )
    return {
        "signals_received": len(signal_list),
        "unique_candidates": len(candidate_list),
        "duplicates_merged": max(0, len(signal_list) - len(candidate_list)),
        "recommended_for_promotion": sum(
            item.recommendation == PromotionRecommendation.PROMOTE for item in candidate_list
        ),
        "pending_review": sum(item.review_state == ReviewState.PENDING for item in candidate_list),
        "needs_research": sum(
            item.review_state == ReviewState.NEEDS_RESEARCH for item in candidate_list
        ),
        "approved": sum(item.review_state == ReviewState.APPROVED for item in candidate_list),
        "rejected": sum(item.review_state == ReviewState.REJECTED for item in candidate_list),
        "approved_but_below_rubric": sum(
            item.review_state == ReviewState.APPROVED
            and item.recommendation != PromotionRecommendation.PROMOTE
            for item in candidate_list
        ),
        "promoted_to_watchlist": len(watchlist_entries),
        "source_signal_counts": dict(sorted(source_counts.items())),
    }


def concise_company_discovery_summary(summary: dict[str, object]) -> str:
    return (
        "Company discovery: "
        f"{summary.get('signals_received', 0)} signals -> "
        f"{summary.get('unique_candidates', 0)} unique candidates; "
        f"{summary.get('recommended_for_promotion', 0)} rubric-qualified, "
        f"{summary.get('pending_review', 0)} pending review, "
        f"{summary.get('promoted_to_watchlist', 0)} approved and promoted."
    )


def _dedupe_signal_groups(
    signals: list[CandidateCompanySignal],
) -> list[list[CandidateCompanySignal]]:
    if not signals:
        return []
    parents = list(range(len(signals)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    identity_owner: dict[str, int] = {}
    for index, signal in enumerate(signals):
        for key in _signal_identity_keys(signal):
            if key in identity_owner:
                union(index, identity_owner[key])
            else:
                identity_owner[key] = index

    groups: dict[int, list[CandidateCompanySignal]] = {}
    for index, signal in enumerate(signals):
        groups.setdefault(find(index), []).append(signal)
    return list(groups.values())


def _signal_identity_keys(signal: CandidateCompanySignal) -> set[str]:
    keys = {f"name:{_normalize_company_name(signal.company_name)}"}
    domain = _website_domain(signal.website)
    if domain:
        keys.add(f"domain:{domain}")
    linkedin_url = _normalize_url(signal.linkedin_company_url)
    if linkedin_url:
        keys.add(f"linkedin:{linkedin_url}")
    return keys


def _candidate_from_group(group: list[CandidateCompanySignal]) -> CandidateCompany:
    ordered = sorted(
        group,
        key=lambda signal: (
            _normalize_company_name(signal.company_name),
            _website_domain(signal.website),
            _normalize_url(signal.linkedin_company_url),
        ),
    )
    company_name = ordered[0].company_name
    website = next((item.website for item in ordered if item.website), "")
    linkedin_url = next(
        (item.linkedin_company_url for item in ordered if item.linkedin_company_url), ""
    )
    rubric = _merge_rubrics(item.rubric for item in ordered)
    recommendation, reasons = _recommend(rubric)
    provenance = _merge_provenance(ordered)
    observed_times = sorted(item.observed_at for item in provenance if item.observed_at)
    return CandidateCompany(
        candidate_id=_candidate_id(company_name, website, linkedin_url),
        company_name=company_name,
        website=website,
        linkedin_company_url=linkedin_url,
        description=_merge_text(item.description for item in ordered),
        rubric=rubric,
        rubric_total=rubric.total,
        recommendation=recommendation,
        recommendation_reasons=reasons,
        provenance=provenance,
        first_seen_at=observed_times[0] if observed_times else "",
        last_seen_at=observed_times[-1] if observed_times else "",
    )


def _merge_rubrics(rubrics: Iterable[CompanyFitRubric]) -> CompanyFitRubric:
    rubric_list = list(rubrics)
    merged: dict[str, RubricDimension] = {}
    for dimension in RUBRIC_DIMENSIONS:
        values = [getattr(rubric, dimension) for rubric in rubric_list]
        merged[dimension] = RubricDimension(
            score=max((value.score for value in values), default=0),
            evidence=_merge_text(value.evidence for value in values),
        )
    return CompanyFitRubric(**merged)


def _merge_provenance(group: Iterable[CandidateCompanySignal]) -> list[CandidateProvenance]:
    unique: dict[tuple[str, ...], CandidateProvenance] = {}
    for signal in group:
        for provenance in signal.provenance:
            key = (
                provenance.source_name.casefold(),
                provenance.source_type.casefold(),
                provenance.source_run_id,
                _normalize_url(provenance.source_url),
                provenance.signal_type.casefold(),
                provenance.context.casefold(),
            )
            unique.setdefault(key, provenance)
    return sorted(
        unique.values(),
        key=lambda item: (item.observed_at, item.source_type, item.source_name, item.source_url),
    )


def _recommend(
    rubric: CompanyFitRubric,
) -> tuple[PromotionRecommendation, list[str]]:
    missing_dimensions = [
        dimension for dimension in RUBRIC_DIMENSIONS if getattr(rubric, dimension).score == 0
    ]
    if rubric.total >= PROMOTION_SCORE_THRESHOLD and not missing_dimensions:
        return PromotionRecommendation.PROMOTE, [
            f"Rubric total {rubric.total}/15 meets the {PROMOTION_SCORE_THRESHOLD}-point threshold.",
            "Every rubric dimension has positive evidence.",
        ]
    if missing_dimensions:
        readable = ", ".join(dimension.replace("_", " ") for dimension in missing_dimensions)
        return PromotionRecommendation.RESEARCH, [
            f"Missing positive evidence for: {readable}.",
            f"Current rubric total is {rubric.total}/15.",
        ]
    if rubric.total >= 7:
        return PromotionRecommendation.RESEARCH, [
            f"Rubric total {rubric.total}/15 is promising but below the promotion threshold."
        ]
    return PromotionRecommendation.PASS, [
        f"Rubric total {rubric.total}/15 is below the research and promotion bands."
    ]


def _find_review(
    candidate: CandidateCompany,
    decisions: list[CompanyReviewDecision],
) -> CompanyReviewDecision | None:
    for decision in decisions:
        if decision.candidate_id and decision.candidate_id == candidate.candidate_id:
            return decision
    candidate_domain = _website_domain(candidate.website)
    if candidate_domain:
        for decision in decisions:
            if _website_domain(decision.website) == candidate_domain:
                return decision
    normalized_name = _normalize_company_name(candidate.company_name)
    for decision in decisions:
        if decision.company_name and _normalize_company_name(decision.company_name) == normalized_name:
            return decision
    return None


def _apply_review(
    candidate: CandidateCompany,
    decision: CompanyReviewDecision | None,
) -> CandidateCompany:
    if decision is None:
        return candidate
    updates = {
        "review_state": decision.review_state,
        "reviewer": decision.reviewer,
        "reviewed_at": decision.reviewed_at,
        "reviewer_notes": decision.reviewer_notes,
        "watchlist_eligible": (
            decision.review_state == ReviewState.APPROVED
            and candidate.recommendation == PromotionRecommendation.PROMOTE
        ),
    }
    return candidate.model_copy(update=updates)


def _candidate_id(company_name: str, website: str, linkedin_url: str) -> str:
    identity = _website_domain(website) or _normalize_url(linkedin_url)
    identity = identity or _normalize_company_name(company_name)
    slug = re.sub(r"[^a-z0-9]+", "-", company_name.casefold()).strip("-") or "company"
    suffix = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    return f"candidate-{slug}-{suffix}"


def _write_candidate_csv(path: Path, candidates: Iterable[CandidateCompany]) -> None:
    fieldnames = [
        "candidate_id",
        "company_name",
        "website",
        "linkedin_company_url",
        "description",
        "rubric_total",
        "recommendation",
        "recommendation_reasons",
        *[field for dimension in RUBRIC_DIMENSIONS for field in (f"{dimension}_score", f"{dimension}_evidence")],
        "review_state",
        "reviewer",
        "reviewed_at",
        "reviewer_notes",
        "watchlist_eligible",
        "first_seen_at",
        "last_seen_at",
        "source_types",
        "source_urls",
        "provenance_json",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            row: dict[str, object] = {
                "candidate_id": candidate.candidate_id,
                "company_name": candidate.company_name,
                "website": candidate.website,
                "linkedin_company_url": candidate.linkedin_company_url,
                "description": candidate.description,
                "rubric_total": candidate.rubric_total,
                "recommendation": candidate.recommendation.value,
                "recommendation_reasons": " | ".join(candidate.recommendation_reasons),
                "review_state": candidate.review_state.value,
                "reviewer": candidate.reviewer,
                "reviewed_at": candidate.reviewed_at,
                "reviewer_notes": candidate.reviewer_notes,
                "watchlist_eligible": str(candidate.watchlist_eligible).lower(),
                "first_seen_at": candidate.first_seen_at,
                "last_seen_at": candidate.last_seen_at,
                "source_types": ";".join(
                    sorted({item.source_type for item in candidate.provenance})
                ),
                "source_urls": ";".join(
                    sorted({item.source_url for item in candidate.provenance if item.source_url})
                ),
                "provenance_json": json.dumps(
                    [item.model_dump(mode="json") for item in candidate.provenance],
                    separators=(",", ":"),
                ),
            }
            for dimension in RUBRIC_DIMENSIONS:
                value = getattr(candidate.rubric, dimension)
                row[f"{dimension}_score"] = value.score
                row[f"{dimension}_evidence"] = value.evidence
            writer.writerow(row)


def _write_watchlist_csv(path: Path, entries: Iterable[CompanyWatchlistEntry]) -> None:
    fieldnames = [
        "candidate_id",
        "company_name",
        "website",
        "linkedin_company_url",
        "description",
        "rubric_total",
        "review_state",
        "reviewer",
        "reviewed_at",
        "reviewer_notes",
        "promoted_at",
        "source_types",
        "source_urls",
        "rubric_json",
        "provenance_json",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "candidate_id": entry.candidate_id,
                    "company_name": entry.company_name,
                    "website": entry.website,
                    "linkedin_company_url": entry.linkedin_company_url,
                    "description": entry.description,
                    "rubric_total": entry.rubric_total,
                    "review_state": entry.review_state.value,
                    "reviewer": entry.reviewer,
                    "reviewed_at": entry.reviewed_at,
                    "reviewer_notes": entry.reviewer_notes,
                    "promoted_at": entry.promoted_at,
                    "source_types": ";".join(
                        sorted({item.source_type for item in entry.provenance})
                    ),
                    "source_urls": ";".join(
                        sorted({item.source_url for item in entry.provenance if item.source_url})
                    ),
                    "rubric_json": json.dumps(
                        entry.rubric.model_dump(mode="json"), separators=(",", ":")
                    ),
                    "provenance_json": json.dumps(
                        [item.model_dump(mode="json") for item in entry.provenance],
                        separators=(",", ":"),
                    ),
                }
            )


def _normalize_company_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _website_domain(value: str) -> str:
    value = _clean(value)
    if not value:
        return ""
    candidate = value if "://" in value else f"https://{value}"
    try:
        host = (urlsplit(candidate).hostname or "").casefold()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _normalize_url(value: str) -> str:
    value = _clean(value)
    if not value:
        return ""
    candidate = value if "://" in value else f"https://{value}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return value.casefold().rstrip("/")
    host = (parts.hostname or "").casefold().removeprefix("www.")
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    return urlunsplit(((parts.scheme or "https").casefold(), host, path, "", ""))


def _merge_text(values: Iterable[str]) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = _clean(value)
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            output.append(clean)
    return " | ".join(output)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()

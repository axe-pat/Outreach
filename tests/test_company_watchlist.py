from __future__ import annotations

import csv
import json
from pathlib import Path

from outreach.company_watchlist import (
    CandidateCompanySignal,
    CandidateProvenance,
    CompanyFitRubric,
    CompanyReviewDecision,
    PromotionRecommendation,
    ReviewState,
    RubricDimension,
    build_candidate_review_queue,
    build_company_watchlist,
    company_discovery_summary,
    concise_company_discovery_summary,
    load_company_review_decisions,
    write_company_discovery_artifacts,
)


def _rubric(
    domain: int = 2,
    story: int = 2,
    geography: int = 2,
    quality: int = 2,
    roles: int = 2,
    *,
    prefix: str = "Evidence",
) -> CompanyFitRubric:
    return CompanyFitRubric(
        domain_fit=RubricDimension(score=domain, evidence=f"{prefix}: domain"),
        technical_mba_story=RubricDimension(score=story, evidence=f"{prefix}: story"),
        geography_remote=RubricDimension(score=geography, evidence=f"{prefix}: geography"),
        growth_quality=RubricDimension(score=quality, evidence=f"{prefix}: quality"),
        role_surface=RubricDimension(score=roles, evidence=f"{prefix}: roles"),
    )


def _provenance(
    source_type: str,
    url: str,
    *,
    run_id: str = "run-2026-07-10",
) -> CandidateProvenance:
    return CandidateProvenance(
        source_name=source_type.replace("_", " ").title(),
        source_type=source_type,
        source_run_id=run_id,
        source_url=url,
        observed_at="2026-07-10T08:00:00+00:00",
        signal_type="interesting_company",
        context=f"Found via {source_type}",
    )


def test_candidate_discovery_dedupes_across_sources_and_merges_evidence() -> None:
    signals = [
        CandidateCompanySignal(
            company_name="Orbit AI",
            website="https://www.orbit.example/",
            description="AI workflow company",
            rubric=_rubric(domain=3, story=2, geography=2, quality=2, roles=2, prefix="Feed"),
            provenance=[
                _provenance("linkedin_home_feed", "https://linkedin.com/posts/orbit-launch")
            ],
        ),
        CandidateCompanySignal(
            company_name="Orbit",
            website="orbit.example/company",
            linkedin_company_url="https://www.linkedin.com/company/orbit-ai/",
            description="Fast-growing workflow startup",
            rubric=_rubric(domain=2, story=3, geography=2, quality=3, roles=2, prefix="YC"),
            provenance=[
                _provenance("startup_directory", "https://directory.example/orbit")
            ],
        ),
    ]

    candidates = build_candidate_review_queue(signals)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.rubric_total == 13
    assert candidate.recommendation == PromotionRecommendation.PROMOTE
    assert candidate.review_state == ReviewState.PENDING
    assert not candidate.watchlist_eligible
    assert len(candidate.provenance) == 2
    assert {item.source_type for item in candidate.provenance} == {
        "linkedin_home_feed",
        "startup_directory",
    }
    assert "Feed: domain" in candidate.rubric.domain_fit.evidence
    assert "YC: domain" in candidate.rubric.domain_fit.evidence


def test_watchlist_requires_both_rubric_fit_and_human_approval() -> None:
    high_fit = CandidateCompanySignal(
        company_name="High Fit Labs",
        website="https://highfit.example",
        rubric=_rubric(),
        provenance=[_provenance("funding_news", "https://news.example/highfit")],
    )
    missing_geography = CandidateCompanySignal(
        company_name="Unknown Geography",
        website="https://unknown.example",
        rubric=_rubric(domain=3, story=3, geography=0, quality=3, roles=3),
        provenance=[_provenance("company_news", "https://news.example/unknown")],
    )
    initial = build_candidate_review_queue([high_fit, missing_geography])
    decisions = [
        CompanyReviewDecision(
            candidate_id=item.candidate_id,
            company_name=item.company_name,
            website=item.website,
            review_state=ReviewState.APPROVED,
            reviewer="Akshat",
            reviewed_at="2026-07-10T09:00:00+00:00",
        )
        for item in initial
    ]

    reviewed = build_candidate_review_queue(
        [high_fit, missing_geography],
        review_decisions=decisions,
    )
    watchlist = build_company_watchlist(reviewed, promoted_at="2026-07-10T10:00:00+00:00")

    assert [entry.company_name for entry in watchlist] == ["High Fit Labs"]
    held = next(item for item in reviewed if item.company_name == "Unknown Geography")
    assert held.review_state == ReviewState.APPROVED
    assert held.recommendation == PromotionRecommendation.RESEARCH
    assert not held.watchlist_eligible
    summary = company_discovery_summary([high_fit, missing_geography], reviewed, watchlist)
    assert summary["approved_but_below_rubric"] == 1
    assert summary["promoted_to_watchlist"] == 1


def test_company_discovery_artifacts_are_reusable_and_do_not_touch_account_tracker(
    tmp_path: Path,
) -> None:
    signal = CandidateCompanySignal(
        company_name="Signal Labs",
        website="https://signal.example",
        linkedin_company_url="https://www.linkedin.com/company/signal-labs",
        rubric=_rubric(domain=3, quality=3),
        provenance=[
            _provenance("linkedin_home_feed", "https://linkedin.com/posts/signal-hiring")
        ],
    )
    candidate = build_candidate_review_queue([signal])[0]
    decision = CompanyReviewDecision(
        candidate_id=candidate.candidate_id,
        company_name=candidate.company_name,
        website=candidate.website,
        review_state=ReviewState.APPROVED,
        reviewer="Akshat",
        reviewed_at="2026-07-10T09:30:00+00:00",
        reviewer_notes="Strong story and role surface.",
    )

    artifacts = write_company_discovery_artifacts(
        tmp_path / "artifacts",
        run_id="run-2026-07-10",
        signals=[signal],
        review_decisions=[decision],
        generated_at="2026-07-10T10:00:00+00:00",
    )

    payload = json.loads(artifacts.payload_json.read_text(encoding="utf-8"))
    watchlist = json.loads(artifacts.watchlist_json.read_text(encoding="utf-8"))
    with artifacts.review_queue_csv.open(newline="", encoding="utf-8") as handle:
        review_rows = list(csv.DictReader(handle))
    with artifacts.watchlist_csv.open(newline="", encoding="utf-8") as handle:
        watchlist_rows = list(csv.DictReader(handle))

    assert payload["run_id"] == "run-2026-07-10"
    assert payload["rubric_guidance"]["dimensions"]["role_surface"]
    assert payload["summary"]["source_signal_counts"] == {"linkedin_home_feed": 1}
    assert payload["candidates"][0]["provenance"][0]["source_run_id"] == "run-2026-07-10"
    assert len(watchlist["entries"]) == 1
    assert review_rows[0]["review_state"] == "approved"
    assert review_rows[0]["provenance_json"]
    assert watchlist_rows[0]["company_name"] == "Signal Labs"
    assert load_company_review_decisions(artifacts.review_queue_csv)[0].reviewer == "Akshat"
    assert not (tmp_path / "organizations.csv").exists()
    assert "1 approved and promoted" in concise_company_discovery_summary(payload["summary"])


def test_approved_watchlist_survives_idempotent_rebuild_after_company_is_known(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "company_discovery"
    signal = CandidateCompanySignal(
        company_name="Durable AI",
        website="https://durable.example",
        rubric=_rubric(domain=3, quality=3),
        provenance=[_provenance("company_news", "https://news.example/durable")],
    )
    candidate = build_candidate_review_queue([signal])[0]
    approval = CompanyReviewDecision(
        candidate_id=candidate.candidate_id,
        company_name=candidate.company_name,
        website=candidate.website,
        review_state=ReviewState.APPROVED,
        reviewer="Akshat",
        reviewed_at="2026-07-10T09:30:00+00:00",
    )
    first = write_company_discovery_artifacts(
        output_dir,
        run_id="first",
        signals=[signal],
        review_decisions=[approval],
        generated_at="2026-07-10T10:00:00+00:00",
    )
    original_entry = json.loads(first.watchlist_json.read_text(encoding="utf-8"))["entries"][0]

    second = write_company_discovery_artifacts(
        output_dir,
        run_id="second",
        signals=[],
        review_decisions=[],
        generated_at="2026-07-11T10:00:00+00:00",
    )

    watchlist = json.loads(second.watchlist_json.read_text(encoding="utf-8"))
    summary = json.loads(second.summary_json.read_text(encoding="utf-8"))
    with second.watchlist_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert watchlist["run_id"] == "second"
    assert watchlist["entries"] == [original_entry]
    assert [row["company_name"] for row in rows] == ["Durable AI"]
    assert summary["promoted_to_watchlist"] == 1


def test_explicit_rejection_removes_prior_cumulative_watchlist_entry(tmp_path: Path) -> None:
    output_dir = tmp_path / "company_discovery"
    signal = CandidateCompanySignal(
        company_name="Reconsidered AI",
        website="https://reconsidered.example",
        rubric=_rubric(domain=3, quality=3),
        provenance=[_provenance("company_news", "https://news.example/reconsidered")],
    )
    candidate = build_candidate_review_queue([signal])[0]
    approval = CompanyReviewDecision(
        candidate_id=candidate.candidate_id,
        company_name=candidate.company_name,
        website=candidate.website,
        review_state=ReviewState.APPROVED,
        reviewer="Akshat",
        reviewed_at="2026-07-10T09:30:00+00:00",
    )
    write_company_discovery_artifacts(
        output_dir,
        run_id="first",
        signals=[signal],
        review_decisions=[approval],
    )
    rejection = approval.model_copy(update={"review_state": ReviewState.REJECTED})

    second = write_company_discovery_artifacts(
        output_dir,
        run_id="second",
        signals=[],
        review_decisions=[rejection],
    )

    assert json.loads(second.watchlist_json.read_text(encoding="utf-8"))["entries"] == []

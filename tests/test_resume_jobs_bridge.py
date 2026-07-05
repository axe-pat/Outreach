from __future__ import annotations

from datetime import date

from openpyxl import Workbook

from outreach.resume_jobs_bridge import (
    FALL_PATH_BUCKET,
    FULL_TIME_PATH_BUCKET,
    GENERIC_INTERNSHIP_BUCKET,
    SUMMER_BUCKET,
    ResumeJob,
    build_resume_outreach_queue,
    classify_resume_role_season,
    dedupe_key_for_job,
    load_resume_jobs,
    map_resume_source_kind,
    opportunity_status_from_resume_status,
    select_resume_jobs,
)
from outreach.tracking import SourceKind


def test_select_resume_jobs_includes_fresh_high_score_applied_rows_for_catchup(tmp_path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Jobs"
    sheet.append(
        [
            "id",
            "date_found",
            "company",
            "role_title",
            "location",
            "url",
            "source",
            "fit_score",
            "fit_rationale",
            "status",
            "folder_path",
            "jd_text",
            "notes",
        ]
    )
    sheet.append(
        ["1", "2026-04-10", "OpenAI", "Product Manager Intern", "San Francisco", "https://example.com/1", "linkedin", "8.4", "strong fit", "queued", "", "jd text", ""]
    )
    sheet.append(
        ["2", "2026-04-01", "Stripe", "Product Manager Intern", "Remote", "https://example.com/2", "linkedin", "8.1", "strong fit", "queued", "", "jd text", ""]
    )
    sheet.append(
        ["3", "2026-04-12", "Datadog", "Product Manager Intern", "New York", "https://example.com/3", "indeed", "6.1", "weak fit", "queued", "", "jd text", ""]
    )
    sheet.append(
        ["4", "2026-04-12", "Figma", "Associate Product Manager", "Remote", "https://example.com/4", "linkedin", "8.0", "good fit", "applied", "", "jd text", ""]
    )
    path = tmp_path / "jobs.xlsx"
    workbook.save(path)

    jobs = load_resume_jobs(path)
    selection = select_resume_jobs(
        jobs,
        min_score=7.0,
        max_age_days=10,
        today=date(2026, 4, 15),
    )

    assert [job.company for job in selection.jobs] == ["Figma", "OpenAI"]
    assert selection.skipped_age == 1
    assert selection.skipped_score == 1
    assert selection.skipped_status == 0
    assert selection.duplicates_removed == 0


def test_resume_job_source_and_status_mapping() -> None:
    assert map_resume_source_kind("linkedin_live_jobs_v1") == SourceKind.LINKEDIN_JOB
    assert map_resume_source_kind("indeed") == SourceKind.JOB_BOARD
    assert opportunity_status_from_resume_status("generated") == "assets_ready"
    assert opportunity_status_from_resume_status("applied") == "applied_catchup"


def test_select_resume_jobs_prefers_fresher_duplicate_by_url_hash(tmp_path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Jobs"
    sheet.append(
        [
            "id",
            "date_found",
            "company",
            "role_title",
            "location",
            "url",
            "url_hash",
            "source",
            "fit_score",
            "fit_rationale",
            "status",
            "folder_path",
            "jd_text",
            "notes",
        ]
    )
    sheet.append(
        ["1", "2026-04-08", "OpenAI", "Product Manager Intern", "San Francisco", "https://example.com/job", "samehash", "linkedin", "8.6", "older", "queued", "", "jd text", ""]
    )
    sheet.append(
        ["2", "2026-04-12", "OpenAI", "Product Manager Intern", "San Francisco", "https://example.com/job", "samehash", "linkedin_live_jobs_v1", "8.2", "newer", "queued", "", "jd text", ""]
    )
    path = tmp_path / "jobs.xlsx"
    workbook.save(path)

    jobs = load_resume_jobs(path)
    selection = select_resume_jobs(
        jobs,
        min_score=7.0,
        max_age_days=10,
        today=date(2026, 4, 15),
    )

    assert len(selection.jobs) == 1
    assert selection.jobs[0].row_id == "2"
    assert selection.duplicates_removed == 1
    assert dedupe_key_for_job(selection.jobs[0]) == "url_hash:samehash"


def _resume_job(
    row_id: str,
    role_title: str,
    *,
    company: str = "ExampleCo",
    location: str = "Remote",
    fit_score: float = 8.0,
    status: str = "queued",
    date_found: date = date(2026, 7, 2),
) -> ResumeJob:
    return ResumeJob(
        row_id=row_id,
        company=company,
        role_title=role_title,
        location=location,
        url=f"https://example.com/{row_id}",
        url_hash=f"hash-{row_id}",
        source="linkedin_live_jobs_v1",
        status=status,
        normalized_status=status,
        fit_score=fit_score,
        fit_rationale="",
        date_found=date_found,
        date_posted_raw="",
        folder_path="",
        jd_text="",
        notes="",
    )


def test_fall_ft_transition_filters_out_summer_and_generic_internships() -> None:
    jobs = [
        _resume_job("1", "Associate Product Manager, New Grad", company="Figma"),
        _resume_job("2", "Product Management Intern (Fall 2026)", company="Gemini"),
        _resume_job("3", "Summer Product Management Internship", company="OfferUp"),
        _resume_job("4", "Product Manager Intern", company="Typeface"),
    ]

    selection = select_resume_jobs(
        jobs,
        min_score=7.0,
        max_age_days=10,
        season_focus="fall_ft_transition",
        today=date(2026, 7, 5),
    )

    assert {job.company for job in selection.jobs} == {"Figma", "Gemini"}
    assert selection.skipped_season_focus == 2
    assert selection.season_counts_selected == {
        FULL_TIME_PATH_BUCKET: 1,
        FALL_PATH_BUCKET: 1,
    }
    assert selection.season_counts_scanned[SUMMER_BUCKET] == 1
    assert selection.season_counts_scanned[GENERIC_INTERNSHIP_BUCKET] == 1


def test_resume_queue_exposes_season_bucket_and_scores_transition_roles() -> None:
    jobs = [
        _resume_job("1", "Product Manager Intern (Fall 2026)", company="Gemini", fit_score=8.0),
        _resume_job("2", "Summer Product Management Internship", company="OfferUp", fit_score=8.1),
    ]

    queue = build_resume_outreach_queue(jobs)

    assert queue[0].company == "Gemini"
    assert queue[0].season_bucket == FALL_PATH_BUCKET
    assert "season=fall/co-op" in queue[0].priority_reasons
    assert queue[-1].season_bucket == SUMMER_BUCKET
    assert "season=summer" in queue[-1].priority_reasons


def test_classify_resume_role_season_uses_new_grad_and_fall_language() -> None:
    assert (
        classify_resume_role_season(_resume_job("1", "Associate Product Manager, New Grad"))
        == FULL_TIME_PATH_BUCKET
    )
    assert (
        classify_resume_role_season(_resume_job("2", "Product Manager Intern - Fall 2026"))
        == FALL_PATH_BUCKET
    )


def test_classify_resume_role_season_does_not_promote_generic_internship_noise() -> None:
    job = _resume_job(
        "1",
        "Product Manager Intern, AI Services & Agentic Search",
    )
    noisy_job = ResumeJob(
        **{
            **job.__dict__,
            "fit_rationale": "Could be an APM-like path, but the posting is still an internship.",
            "notes": "search=apm_intern insight=Full-time Apply",
        }
    )

    assert classify_resume_role_season(noisy_job) == GENERIC_INTERNSHIP_BUCKET
    assert classify_resume_role_season(_resume_job("2", "MBA Intern")) == GENERIC_INTERNSHIP_BUCKET

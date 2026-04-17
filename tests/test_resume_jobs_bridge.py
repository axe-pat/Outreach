from __future__ import annotations

from datetime import date

from openpyxl import Workbook

from outreach.resume_jobs_bridge import (
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

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from outreach.shared_discovery import (
    READY,
    REVIEW_REQUIRED,
    build_shared_daily_queue,
    resolve_run_scoped_action_queue,
    write_shared_daily_queue,
)
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachWorkbook,
    SourceKind,
)


def _action_queue(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "generated_at": "2026-07-11T01:31:45",
        "inputs": {
            "startup_source_report": "/runs/run-1-startup-source-report.json",
            "current_apply_queue": "/runs/run-1-priority-order.json",
        },
        "application_plus_outreach": [],
        "application_only": [],
        "scored_application_selected": [],
        "unscored_coverage_candidates": [],
        "outreach_only_today": [],
        "relationship_buffer": [],
        "follow_up": [],
    }
    payload.update(overrides)
    return payload


def _workspace(tmp_path: Path) -> tuple[Path, OutreachWorkbook]:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    return workspace, workbook


def test_shared_queue_merges_resume_startup_and_warm_evidence_by_company(
    tmp_path: Path,
) -> None:
    payload = _action_queue(
        application_plus_outreach=[
            {
                "company": "Orbit Labs, Inc.",
                "role_title": "Product Operations Manager",
                "url": "https://jobs.example/orbit-pm",
                "source": "current_apply_queue",
                "fit_score": "8.8",
                "status": "generated",
                "reasons": ["active_application_needs_outreach"],
            }
        ],
        outreach_only_today=[
            {
                "company": "Orbit Labs",
                "company_url": "https://www.ycombinator.com/companies/orbit-labs",
                "source": "startup_org:yc_sf_bay_hiring",
                "relationship_score": 9.0,
                "recommended_action": "run_linkedin_company_pipeline",
                "reasons": ["active hiring/jobs signal"],
            }
        ],
    )
    workspace, workbook = _workspace(tmp_path)
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-orbit",
            name="Orbit Labs",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup;hiring",
            website="https://orbit.example",
            source_kind=SourceKind.YC_DIRECTORY,
            source_url="https://www.ycombinator.com/companies/orbit-labs",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-orbit-ava",
            organization_id="org-orbit",
            full_name="Ava Founder",
            title="Founder",
            status="Replied",
            linkedin_url="https://linkedin.com/in/ava",
        )
    )

    queue = build_shared_daily_queue(
        action_queue_payload=payload,
        run_id="run-1",
        action_queue_path=tmp_path / "run-1-daily-action-queue.json",
        workspace=workspace,
        limit=None,
        generated_at="2026-07-11T02:00:00+00:00",
    )

    assert len(queue.items) == 1
    item = queue.items[0]
    assert item.company == "Orbit Labs"
    assert item.organization_id == "org-orbit"
    assert item.primary_action == "application_plus_outreach"
    assert item.gate == READY
    assert set(item.recommended_actions) == {
        "application_plus_outreach",
        "company_outreach",
        "follow_up_warm_contact",
    }
    assert [role.title for role in item.roles] == ["Product Operations Manager"]
    assert [contact.full_name for contact in item.warm_contacts] == ["Ava Founder"]
    assert set(item.source_types) == {
        "outreach_warm_network",
        "resume_generator_role",
        "startup_company_source",
    }
    assert queue.summary["duplicates_merged"] == 2
    assert queue.source_coverage["yc_builtin_company_sources"]["observations"] == 1
    assert queue.scope == (
        "run_scoped_resume_generator_queue + outreach_workspace_snapshot"
    )


def test_company_only_candidate_stays_review_gated_until_approved_watchlist(
    tmp_path: Path,
) -> None:
    payload = _action_queue(
        outreach_only_today=[
            {
                "company": "Alpha Robotics",
                "company_url": "https://www.ycombinator.com/companies/alpha-robotics",
                "source": "startup_org:yc_los_angeles",
                "relationship_score": 8.5,
            }
        ]
    )
    queue_without_review = build_shared_daily_queue(
        action_queue_payload=payload,
        run_id="run-2",
        action_queue_path=tmp_path / "queue.json",
        limit=None,
    )
    assert queue_without_review.items[0].primary_action == "company_outreach"
    assert queue_without_review.items[0].gate == REVIEW_REQUIRED

    watchlist = tmp_path / "company_watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "run_id": "watchlist-run",
                "generated_at": "2026-07-11T01:45:00+00:00",
                "entries": [
                    {
                        "company_name": "Alpha Robotics, Inc.",
                        "website": "https://alpha.example",
                        "rubric_total": 12,
                        "review_state": "approved",
                        "provenance": [
                            {
                                "source_name": "LinkedIn home feed",
                                "source_type": "linkedin_home_feed",
                                "source_run_id": "feed-run",
                                "source_url": "https://linkedin.com/posts/alpha",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reviewed_queue = build_shared_daily_queue(
        action_queue_payload=payload,
        run_id="run-2",
        action_queue_path=tmp_path / "queue.json",
        watchlist_path=watchlist,
        limit=None,
    )

    assert len(reviewed_queue.items) == 1
    reviewed = reviewed_queue.items[0]
    assert reviewed.review_state == "approved"
    assert reviewed.gate == READY
    assert "approved_company_watchlist" in reviewed.source_types
    assert "linkedin_home_feed" in reviewed.source_types
    assert "human_approved_watchlist" in reviewed.priority_reasons


def test_warm_startup_without_live_role_is_added_but_nonstartup_is_not(
    tmp_path: Path,
) -> None:
    workspace, workbook = _workspace(tmp_path)
    for organization in (
        OrganizationRecord(
            organization_id="org-warm-startup",
            name="Warm Startup",
            organization_type=OrganizationType.STARTUP,
            target_lists="startup;yc",
        ),
        OrganizationRecord(
            organization_id="org-warm-enterprise",
            name="Warm Enterprise",
            organization_type=OrganizationType.COMPANY,
            target_lists="large-company",
        ),
    ):
        workbook.upsert_organization(organization)
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-startup",
            organization_id="org-warm-startup",
            full_name="Startup Alum",
            status="Connected",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-enterprise",
            organization_id="org-warm-enterprise",
            full_name="Enterprise Alum",
            status="Replied",
        )
    )

    queue = build_shared_daily_queue(
        action_queue_payload=_action_queue(),
        run_id="run-3",
        action_queue_path=tmp_path / "queue.json",
        workspace=workspace,
        warm_startups_only=True,
        limit=None,
    )

    assert [item.company for item in queue.items] == ["Warm Startup"]
    assert queue.items[0].primary_action == "warm_company_outreach"
    assert queue.items[0].gate == READY
    assert queue.items[0].roles == []
    assert queue.source_coverage["yc_builtin_company_sources"]["status"] == "zero"
    assert queue.source_coverage["approved_company_watchlist"]["status"] == "skipped"


def test_existing_outreach_follow_up_is_not_mislabeled_as_a_warm_contact(
    tmp_path: Path,
) -> None:
    queue = build_shared_daily_queue(
        action_queue_payload=_action_queue(
            follow_up=[
                {
                    "company": "Touched Startup",
                    "source": "startup_org:yc_sf_bay_hiring",
                    "relationship_score": 7.5,
                    "recommended_action": "follow_up_or_skip_recent",
                    "existing_touchpoints": 1,
                }
            ]
        ),
        run_id="run-follow-up",
        action_queue_path=tmp_path / "queue.json",
        limit=None,
    )

    assert queue.items[0].primary_action == "follow_up_existing_outreach"
    assert queue.items[0].gate == REVIEW_REQUIRED
    assert queue.items[0].warm_contacts == []


def test_nightly_summary_rejects_mismatched_or_unscoped_action_queue(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected-daily-action-queue.json"
    other = tmp_path / "other-daily-action-queue.json"
    expected.write_text(json.dumps(_action_queue()), encoding="utf-8")
    other.write_text(json.dumps(_action_queue()), encoding="utf-8")
    summary = tmp_path / "run-nightly-pipeline-summary.json"
    summary.write_text(
        json.dumps(
            {
                "action_queue": str(expected),
                "action_queue_status": "current_run",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match the nightly summary"):
        resolve_run_scoped_action_queue(
            nightly_summary_path=summary,
            action_queue_path=other,
        )

    summary.write_text(
        json.dumps(
            {
                "action_queue": str(expected),
                "action_queue_status": "workspace_snapshot",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="current_run"):
        resolve_run_scoped_action_queue(nightly_summary_path=summary)


def test_queue_writer_emits_run_scoped_and_current_json_csv(tmp_path: Path) -> None:
    queue = build_shared_daily_queue(
        action_queue_payload=_action_queue(
            application_only=[
                {
                    "company": "Ready Co",
                    "role_title": "Associate Product Manager",
                    "url": "https://jobs.example/ready",
                    "source": "current_apply_queue",
                    "fit_score": 9.1,
                }
            ]
        ),
        run_id="20260711-013145",
        action_queue_path=tmp_path / "queue.json",
        limit=None,
        generated_at="2026-07-11T02:00:00+00:00",
    )

    artifacts = write_shared_daily_queue(tmp_path / "out", queue)

    assert artifacts.run_json.name == "20260711-013145-shared-daily-queue.json"
    assert artifacts.run_json.exists()
    assert artifacts.current_json.exists()
    assert json.loads(artifacts.current_json.read_text())["run_id"] == "20260711-013145"
    with artifacts.current_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["run_id"] == "20260711-013145"
    assert rows[0]["generated_at"] == "2026-07-11T02:00:00+00:00"
    assert rows[0]["company"] == "Ready Co"
    assert rows[0]["primary_action"] == "application_only"
    assert rows[0]["role_count"] == "1"
    assert list((tmp_path / "out").glob(".*.tmp")) == []

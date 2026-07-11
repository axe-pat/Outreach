from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from outreach.shared_discovery import (
    BUFFERED,
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
        "scored_application_not_selected": [],
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


def test_strategic_company_without_current_role_remains_on_company_role_watch(
    tmp_path: Path,
) -> None:
    workspace, workbook = _workspace(tmp_path)
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-strategic",
            name="Strategic Platform",
            organization_type=OrganizationType.COMPANY,
            target_lists="strategic;wishlist;track-2",
            status="Strategic target",
            website="https://strategic.example",
            source_kind=SourceKind.MANUAL,
            source_url="https://strategic.example/about",
        )
    )
    organizations_before = (workspace / "organizations.csv").read_text(encoding="utf-8")

    queue = build_shared_daily_queue(
        action_queue_payload=_action_queue(),
        run_id="role-watch-empty",
        action_queue_path=tmp_path / "role-watch-empty.json",
        workspace=workspace,
        limit=None,
        generated_at="2026-07-11T09:00:00+00:00",
    )

    assert (workspace / "organizations.csv").read_text(encoding="utf-8") == organizations_before
    assert len(queue.items) == 1
    item = queue.items[0]
    assert item.company == "Strategic Platform"
    assert item.primary_action == "role_watch"
    assert item.recommended_actions == ["role_watch"]
    assert item.gate == BUFFERED
    assert item.role_watch_state == "watching"
    assert item.roles == []
    assert "outreach_strategic_account" in item.source_types
    coverage = queue.source_coverage["strategic_account_role_watch"]
    assert coverage["accounts_watched"] == 1
    assert coverage["triggered_accounts"] == 0


def test_strategic_role_watch_recovers_all_strong_adjacent_families_with_provenance(
    tmp_path: Path,
) -> None:
    workspace, workbook = _workspace(tmp_path)
    roles = {
        "Product Strategy Co": ("Product Strategy Lead", "product_strategy"),
        "BizOps Co": ("Business Operations Manager", "bizops_strategy"),
        "Program Co": ("Program Manager", "program_operations"),
        "Growth Co": ("Growth Strategy Manager", "growth_adjacent"),
    }
    for company in roles:
        workbook.upsert_organization(
            OrganizationRecord(
                organization_id=workbook.make_organization_id(company),
                name=company,
                organization_type=OrganizationType.COMPANY,
                target_lists="strategic;wishlist;track-2",
                status="Strategic target",
                source_kind=SourceKind.MANUAL,
            )
        )
    omitted_rows = []
    for index, (company, (title, _family)) in enumerate(roles.items(), start=1):
        omitted_rows.append(
            {
                "company": company,
                "role_title": title,
                "url": f"https://jobs.example/strategic-{index}",
                "source": "linkedin_live_jobs_v1",
                "fit_score": "8.4",
                "decision": "Proceed",
                "write_gate": "accepted",
                "post_score_status": "queued",
            }
        )

    queue = build_shared_daily_queue(
        action_queue_payload=_action_queue(
            scored_application_not_selected=omitted_rows,
        ),
        run_id="role-watch-triggered",
        action_queue_path=tmp_path / "role-watch-triggered.json",
        workspace=workspace,
        limit=None,
        generated_at="2026-07-11T09:05:00+00:00",
    )

    assert len(queue.items) == 4
    by_company = {item.company: item for item in queue.items}
    for index, (company, (title, family)) in enumerate(roles.items(), start=1):
        item = by_company[company]
        assert item.primary_action == "application_research"
        assert item.gate == REVIEW_REQUIRED
        assert item.role_watch_state == "triggered"
        assert set(item.recommended_actions) == {"application_research", "role_watch"}
        assert len(item.roles) == 1
        role = item.roles[0]
        assert role.title == title
        assert role.role_family == family
        assert role.strong_adjacent_role is True
        assert role.source == "linkedin_live_jobs_v1"
        assert role.source_url == f"https://jobs.example/strategic-{index}"
        assert role.queue_bucket == "scored_application_not_selected"
        matching_provenance = [
            value for value in item.provenance if value.source_url == role.source_url
        ]
        assert len(matching_provenance) == 1
        assert matching_provenance[0].source_run_id == "role-watch-triggered"
        assert f"role={title}" in matching_provenance[0].context
    coverage = queue.source_coverage["strategic_account_role_watch"]
    assert coverage["candidate_rows_scanned"] == 4
    assert coverage["candidate_rows_added"] == 4
    assert coverage["triggered_accounts"] == 4


def test_selected_strategic_adjacent_role_triggers_without_recovery_bucket(
    tmp_path: Path,
) -> None:
    workspace, workbook = _workspace(tmp_path)
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-selected-strategic",
            name="Selected Strategic",
            organization_type=OrganizationType.COMPANY,
            target_lists="strategic;wishlist",
            status="Strategic target",
        )
    )
    queue = build_shared_daily_queue(
        action_queue_payload=_action_queue(
            scored_application_selected=[
                {
                    "company": "Selected Strategic",
                    "role_title": "Strategic Operations Manager",
                    "url": "https://jobs.example/selected-strategic-ops",
                    "source": "linkedin_live_jobs_v1",
                    "fit_score": "8.0",
                    "decision": "Proceed",
                    "write_gate": "accepted",
                }
            ]
        ),
        run_id="role-watch-selected",
        action_queue_path=tmp_path / "role-watch-selected.json",
        workspace=workspace,
        limit=None,
    )

    item = queue.items[0]
    assert item.primary_action == "application_research"
    assert item.role_watch_state == "triggered"
    assert item.roles[0].role_family == "bizops_strategy"
    assert item.roles[0].queue_bucket == "scored_application_selected"
    coverage = queue.source_coverage["strategic_account_role_watch"]
    assert coverage["candidate_rows_scanned"] == 0
    assert coverage["candidate_rows_added"] == 0
    assert coverage["triggered_accounts"] == 1


def test_strategic_role_watch_rejects_generic_or_upstream_rejected_noise(
    tmp_path: Path,
) -> None:
    workspace, workbook = _workspace(tmp_path)
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-noise",
            name="Noise Co",
            organization_type=OrganizationType.COMPANY,
            target_lists="strategic;wishlist",
            status="Strategic target",
        )
    )
    queue = build_shared_daily_queue(
        action_queue_payload=_action_queue(
            scored_application_not_selected=[
                {
                    "company": "Noise Co",
                    "role_title": "Growth Marketing Manager",
                    "url": "https://jobs.example/growth-marketing",
                    "source": "jobspy_filtered_v1",
                    "fit_score": "9.0",
                    "decision": "Proceed",
                    "write_gate": "accepted",
                },
                {
                    "company": "Noise Co",
                    "role_title": "Program Manager",
                    "url": "https://jobs.example/rejected-program",
                    "source": "linkedin_live_jobs_v1",
                    "fit_score": "9.0",
                    "decision": "Deprioritize",
                    "write_gate": "dropped",
                },
            ]
        ),
        run_id="role-watch-noise",
        action_queue_path=tmp_path / "role-watch-noise.json",
        workspace=workspace,
        limit=None,
    )

    item = queue.items[0]
    assert item.primary_action == "role_watch"
    assert item.role_watch_state == "watching"
    assert item.roles == []
    coverage = queue.source_coverage["strategic_account_role_watch"]
    assert coverage["candidate_rows_scanned"] == 2
    assert coverage["candidate_rows_added"] == 0


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

from datetime import UTC, date, datetime
import csv
import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from outreach.cli import (
    apply_email_finder_results,
    app,
    attach_search_urls_to_candidates,
    apply_linkedin_reconcile_results,
    apply_raw_candidate,
    build_linkedin_company_queue_items,
    build_linkedin_contact_info_email_queue,
    build_linkedin_followup_drafts,
    build_daily_execution_manifest,
    build_external_email_research_queue,
    build_track_2_email_drafts,
    build_linkedin_reconcile_queue_items,
    build_linkedin_message_reconcile_results,
    build_communication_review_csv_rows,
    build_organization_intel_items,
    build_relationship_loop_items,
    build_target_action_queue_items,
    candidate_mentions_company,
    classify_opportunity_action,
    company_search_aliases,
    contact_status_from_invite_result,
    daily_plan_items_by_phase,
    detect_shared_history_signals,
    draft_track_2_email,
    execute_invite_batch,
    extract_linkedin_conversation_action_items,
    extract_team_size_from_notes,
    extract_tags_from_notes,
    extract_description_from_notes,
    filter_discovered_items,
    fit_band_from_score,
    format_team_size_signal,
    infer_role_bucket,
    infer_fit_reasons,
    import_communication_feedback_rows,
    item_matches_remote,
    item_matches_tags,
    normalize_tag,
    parse_notes_metadata,
    parse_batch_year,
    parse_team_size_headcount,
    pass_relevance,
    persist_linkedin_followup_send_result,
    recommend_auto_send_limit,
    resolve_pass_definitions,
    run_supervised_e2e_pipeline,
    score_opportunity_relevance,
    select_invite_candidates,
    summarize_linkedin_followup_actions,
    startup_pool_metadata,
    startup_pool_mode,
    startup_pool_send_min_score,
    effective_send_min_score,
    text_contains_signal,
    touchpoint_status_from_invite_result,
    _source_breakdown,
    _write_comms_learning_artifact,
    write_artifact_daily_report,
    write_communication_review_csv,
)
from outreach.config import OutreachSettings
from outreach.services.email_finder import EmailFinderResult
from outreach.resume_jobs_bridge import CompanyOverride, ResumeJob, build_resume_outreach_queue
from outreach.services.linkedin import LinkedInFollowupSendResult
from outreach.tracking import ContactRecord, OpportunityRecord, OrganizationRecord, OrganizationType, OutreachWorkbook, SourceKind, TouchpointRecord
from outreach.style_profile import CommunicationStyleProfile


def test_source_breakdown_marks_missing_sources_skipped_and_uses_run_metrics(tmp_path: Path) -> None:
    metrics_path = tmp_path / "source-run-metrics.json"
    metrics_path.write_text(json.dumps({
        "sources": {
            "linkedin": {"status": "ran", "raw_count": 12, "accepted_for_write": 3},
            "handshake": {"status": "skipped", "raw_count": None, "accepted_for_write": None},
        },
        "action_queue": {"counts": {"application_plus_outreach": 2}},
    }), encoding="utf-8")

    rows = _source_breakdown({"source_metrics": str(metrics_path), "generation_selected_count": 4})
    by_source = {row["source"]: row for row in rows}

    assert by_source["LinkedIn"]["status"] == "ran"
    assert by_source["LinkedIn"]["raw"] == 12
    assert by_source["Handshake"]["status"] == "skipped"
    assert by_source["Handshake"]["raw"] == 0
    assert by_source["Handshake"]["kept"] == 0
    assert by_source["JobSpy"]["kept"] == 0
    assert by_source["ResumeGenerator / app queue"]["status"] == "ran"
    assert by_source["Track 2 imports / maintenance"]["status"] == "not_run"
    startup_adapters = by_source["Startup sources"]["details"]["adapters"]
    assert len(startup_adapters) == 9
    assert {row["status"] for row in startup_adapters} == {"skipped"}


def test_source_breakdown_keeps_startup_lane_failures_explicit(tmp_path: Path) -> None:
    metrics_path = tmp_path / "source-run-metrics.json"
    metrics_path.write_text(
        json.dumps(
            {
                "sources": {
                    "startup_apply": {"status": "ran"},
                    "startup_relationship": {"status": "timed_out"},
                },
                "startup_source_report": {
                    "startup_apply_discovered": {"builtin": 2},
                    "startup_apply_new": {"builtin": 1},
                    "relationship_targets": 99,
                },
            }
        ),
        encoding="utf-8",
    )

    rows = _source_breakdown({"source_metrics": str(metrics_path)})
    startup = next(row for row in rows if row["source"] == "Startup sources")

    assert startup["status"] == "partial_failed"
    assert startup["raw"] == 2
    assert startup["kept"] == 1
    assert startup["details"]["lane_statuses"] == {
        "startup_apply": "ran",
        "startup_relationship": "timed_out",
    }


def test_source_breakdown_uses_referenced_linkedin_run_and_shows_zero_and_skipped(
    tmp_path: Path,
) -> None:
    old_artifact = tmp_path / "20260709-linkedin-intelligence-capture.json"
    old_artifact.write_text(
        json.dumps(
            {
                "feed": {"status": "completed", "captured": 99, "added": 20},
                "profile_viewers": {"status": "completed", "captured": 8, "added": 5},
            }
        ),
        encoding="utf-8",
    )
    current_artifact = tmp_path / "20260710-linkedin-intelligence-capture.json"
    current_artifact.write_text(
        json.dumps(
            {
                "observed_at": "2026-07-10T01:05:00-07:00",
                "feed": {"status": "completed", "captured": 0, "added": 0},
                "profile_viewers": {
                    "status": "skipped",
                    "reason": "not_scheduled_for_this_run",
                    "captured": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    rows = _source_breakdown(
        {
            "outreach_maintenance": {
                "ran": True,
                "linkedin_intelligence_returncode": 0,
                "linkedin_intelligence_artifact": str(current_artifact),
            }
        }
    )
    by_source = {row["source"]: row for row in rows}

    assert by_source["LinkedIn home feed"]["status"] == "completed"
    assert by_source["LinkedIn home feed"]["raw"] == 0
    assert by_source["LinkedIn home feed"]["kept"] == 0
    assert by_source["LinkedIn profile viewers"]["status"] == "skipped"
    assert by_source["LinkedIn profile viewers"]["raw"] == 0
    assert by_source["LinkedIn profile viewers"]["details"]["passive_context_only"] is True
    assert all(row["raw"] != 99 for row in rows)


def test_source_breakdown_marks_missing_failed_linkedin_capture_as_failed() -> None:
    rows = _source_breakdown(
        {
            "outreach_maintenance": {
                "ran": True,
                "linkedin_intelligence_returncode": 2,
                "linkedin_intelligence_artifact": "",
            }
        }
    )
    by_source = {row["source"]: row for row in rows}

    assert by_source["LinkedIn home feed"]["status"] == "failed"
    assert by_source["LinkedIn home feed"]["raw"] == 0
    assert by_source["LinkedIn home feed"]["details"]["reason"] == "capture_command_failed"
    assert by_source["LinkedIn profile viewers"]["status"] == "skipped"
    assert by_source["LinkedIn profile viewers"]["details"]["reason"] == "linkedin_capture_unavailable"


def test_daily_report_renders_run_scoped_linkedin_feed_and_viewer_rows(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    current_intelligence = artifacts / "current-linkedin-intelligence-capture.json"
    current_intelligence.write_text(
        json.dumps(
            {
                "observed_at": "2026-07-10T01:05:00-07:00",
                "feed": {"status": "completed", "captured": 0, "added": 0},
                "profile_viewers": {
                    "status": "skipped",
                    "reason": "not_scheduled_for_this_run",
                    "captured": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    # A larger stale capture in the same artifacts directory must not leak into this run.
    (artifacts / "old-linkedin-intelligence-capture.json").write_text(
        json.dumps(
            {
                "feed": {"status": "completed", "captured": 77, "added": 33},
                "profile_viewers": {"status": "completed", "captured": 9, "added": 4},
            }
        ),
        encoding="utf-8",
    )
    nightly_summary = tmp_path / "nightly-summary.json"
    nightly_summary.write_text(
        json.dumps(
            {
                "created_at": "2026-07-10T01:00:00-07:00",
                "outreach_maintenance": {
                    "ran": True,
                    "linkedin_intelligence_returncode": 0,
                    "linkedin_intelligence_artifact": str(current_intelligence),
                },
            }
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        artifacts_dir=artifacts,
        resolved_tracking_workspace_dir=workspace,
    )

    summary_path, report_path, html_artifact, _latest_html = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        nightly_summary_path=nightly_summary,
    )

    report_text = report_path.read_text(encoding="utf-8")
    html_text = html_artifact.read_text(encoding="utf-8")
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    by_source = {row["source"]: row for row in payload["source_breakdown"]}
    assert "LinkedIn home feed: `completed` · kept `0` / raw `0`" in report_text
    assert "LinkedIn profile viewers: `skipped` · kept `0` / raw `0`" in report_text
    assert "LinkedIn home feed" in html_text
    assert "LinkedIn profile viewers" in html_text
    assert by_source["LinkedIn home feed"]["raw"] == 0
    assert by_source["LinkedIn profile viewers"]["status"] == "skipped"
    # The stale artifact is not consulted; the temporary pytest path itself may
    # happen to contain the digits "77", so assert against its filename instead.
    assert "old-linkedin-intelligence-capture.json" not in str(by_source)


def test_daily_report_workspace_snapshot_never_claims_current_run_sources(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    settings = SimpleNamespace(
        artifacts_dir=artifacts,
        resolved_tracking_workspace_dir=workspace,
    )

    summary_path, report_path, html_path, _ = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=None,
        nightly_summary_path=None,
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["report_mode"] == "workspace_snapshot"
    assert {row["status"] for row in payload["source_breakdown"]} == {"not_scoped"}
    assert "Workspace Snapshot" in report_path.read_text(encoding="utf-8")
    assert "not scoped" in html_path.read_text(encoding="utf-8")


def test_daily_report_surfaces_open_inbound_resume_request(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    workbook = OutreachWorkbook(workspace)
    workbook.upsert_organization(
        OrganizationRecord(organization_id="org-lemon", name="LemonLime")
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-jordan",
            organization_id="org-lemon",
            full_name="Jordan Zietz",
            status="Replied",
            linkedin_url="https://linkedin.com/in/jordan-zietz/",
        )
    )
    (workspace / "linkedin_message_state.json").write_text(
        json.dumps(
            {
                "thread_states": {
                    "synthetic:jordan": {
                        "name": "Jordan Zietz",
                        "last_sender": "Jordan",
                        "last_seen_at": "2026-07-08T02:08:00+00:00",
                        "latest_message": "Feel free to send over your resume + any info to careers@lemonlime.ai!",
                        "signature": "jordan|resume-request",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    nightly_summary = tmp_path / "nightly-summary.json"
    nightly_summary.write_text(
        json.dumps({"created_at": "2026-07-10T01:00:00-07:00", "outreach_maintenance": {"ran": True}}),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        artifacts_dir=artifacts,
        resolved_tracking_workspace_dir=workspace,
    )

    summary_path, report_path, html_path, _ = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        nightly_summary_path=nightly_summary,
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["open_inbox_actions"][0]["company"] == "LemonLime"
    assert payload["open_inbox_actions"][0]["action_type"] == "email_resume_requested"
    assert payload["open_inbox_actions"][0]["email"] == "careers@lemonlime.ai"
    assert payload["what_needs_you"][0]["action_type"] == "email_resume_requested"
    assert payload["messages_to_review"] == []
    assert payload["auto_handled"] == []
    assert "Email your resume" in report_path.read_text(encoding="utf-8")
    assert "Jordan Zietz" in html_path.read_text(encoding="utf-8")
    assert (workspace / "linkedin_inbox_actions.csv").exists()


def test_daily_report_cli_rejects_half_scoped_mode(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["write-daily-run-report", "--nightly-summary", str(tmp_path / "summary.json")],
    )

    assert result.exit_code != 0
    assert "Pass both --since and --nightly-summary" in result.output


def test_daily_report_run_scope_ignores_unreferenced_concurrent_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    exact_invites = artifacts / "exact-invite-send-batch.json"
    exact_invites.write_text(
        json.dumps(
            {
                "company": "ExactCo",
                "results": [{"name": "A", "status": "sent"}, {"name": "B", "status": "sent"}],
            }
        ),
        encoding="utf-8",
    )
    # This looks like a valid same-window production artifact but is not owned
    # by the selected run manifest. It must be invisible to all report totals.
    contaminant = artifacts / "pytest-concurrent-invite-send-batch.json"
    contaminant.write_text(
        json.dumps(
            {
                "company": "PollutionCo",
                "results": [{"status": "sent"} for _ in range(99)],
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "daily-engine-manifest.json"
    manifest.write_text(
        json.dumps({"artifacts": {"invite_send_batches": [str(exact_invites)]}}),
        encoding="utf-8",
    )
    nightly_summary = tmp_path / "nightly-summary.json"
    nightly_summary.write_text(
        json.dumps(
            {
                "created_at": "2026-07-11T01:00:00-07:00",
                "daily_engine_manifest": str(manifest),
                "outreach_maintenance": {"ran": True},
            }
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(artifacts_dir=artifacts, resolved_tracking_workspace_dir=workspace)

    summary_path, report_path, _html, _latest = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        nightly_summary_path=nightly_summary,
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["invite_totals"] == {"sent": 2}
    assert payload["run_outcome"]["total_outbound_sends"] == 2
    assert [row["company"] for row in payload["company_execution"]] == ["ExactCo"]
    assert payload["run_integrity"]["artifact_selection"] == "explicit_pointers_only"
    assert "PollutionCo" not in report_path.read_text(encoding="utf-8")
    assert str(contaminant) not in json.dumps(payload)


def test_daily_report_missing_manifest_fails_closed_without_mtime_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "looks-current-invite-send-batch.json").write_text(
        json.dumps({"company": "MustNotLeak", "results": [{"status": "sent"}]}),
        encoding="utf-8",
    )
    nightly_summary = tmp_path / "summary.json"
    nightly_summary.write_text(
        json.dumps(
            {
                "outreach_maintenance": {
                    "ran": True,
                    "track_2_daily_run_returncode": 0,
                    "track_2_daily_run_artifact": "",
                }
            }
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(artifacts_dir=artifacts, resolved_tracking_workspace_dir=workspace)

    summary_path, report_path, _html, _latest = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        nightly_summary_path=nightly_summary,
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["invite_totals"] == {}
    assert payload["run_status"] == "failed_or_incomplete"
    assert payload["run_integrity"]["daily_engine_manifest_status"] == "not_recorded"
    assert payload["track_2_execution"]["status"] == "failed_missing_artifact"
    assert "MustNotLeak" not in report_path.read_text(encoding="utf-8")


def test_daily_report_separates_human_review_auto_handled_and_system_holds(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    workbook = OutreachWorkbook(workspace)
    workbook.upsert_organization(OrganizationRecord(organization_id="org-auto", name="AutoCo"))
    workbook.upsert_organization(OrganizationRecord(organization_id="org-review", name="ReviewCo"))
    workbook.upsert_contact(ContactRecord(contact_id="ct-auto", organization_id="org-auto", full_name="Auto Person", status="Replied"))
    workbook.upsert_contact(ContactRecord(contact_id="ct-review", organization_id="org-review", full_name="Review Person", status="Replied"))
    (workspace / "linkedin_message_state.json").write_text(
        json.dumps(
            {
                "thread_states": {
                    "auto": {"name": "Auto Person", "last_sender": "Auto Person", "latest_message": "Sounds good", "signature": "auto-1"},
                    "review": {"name": "Review Person", "last_sender": "Review Person", "latest_message": "Can you clarify?", "signature": "review-1"},
                }
            }
        ),
        encoding="utf-8",
    )
    drafts = artifacts / "exact-linkedin-followup-drafts.json"
    drafts.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "contact_id": "ct-auto", "company": "AutoCo", "name": "Auto Person",
                        "draft_kind": "conversation_reply", "send_recommendation": "safe_to_review",
                        "latest_message": "Sounds good", "draft_message": "Thanks, I will follow up.",
                    },
                    {
                        "contact_id": "ct-review", "company": "ReviewCo", "name": "Review Person",
                        "draft_kind": "conversation_reply", "send_recommendation": "review",
                        "latest_message": "Can you clarify?", "draft_message": "Here is what I meant.",
                    },
                    {
                        "contact_id": "ct-hold", "company": "HoldCo", "name": "Hold Person",
                        "draft_kind": "accepted_follow_up", "send_recommendation": "cadence_hold",
                        "latest_message": "", "draft_message": "Wait until due.",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    sends = artifacts / "exact-linkedin-followup-send-results.json"
    sends.write_text(
        json.dumps(
            {
                "count": 1,
                "status_counts": {"sent": 1},
                "results": [
                    {
                        "contact_id": "ct-auto", "company": "AutoCo", "name": "Auto Person",
                        "draft_kind": "conversation_reply", "send_recommendation": "safe_to_review",
                        "draft_message": "Thanks, I will follow up.", "status": "sent",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reconcile = artifacts / "exact-linkedin-message-reconcile.json"
    reconcile.write_text(
        json.dumps(
            {
                "thread_count": 2,
                "new_result_count": 2,
                "filtered_result_count": 2,
                "results": [
                    {"contact_id": "ct-auto", "name": "Auto Person", "status": "replied", "last_sender": "Auto Person", "latest_message": "Sounds good"},
                    {"contact_id": "ct-review", "name": "Review Person", "status": "replied", "last_sender": "Review Person", "latest_message": "Can you clarify?"},
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
                {
                    "linkedin_followup_draft_artifacts": [str(drafts)],
                    "linkedin_followup_send_artifacts": [str(sends)],
                    "linkedin_reconcile_artifacts": [str(reconcile)],
                }
        ),
        encoding="utf-8",
    )
    nightly_summary = tmp_path / "summary.json"
    nightly_summary.write_text(
        json.dumps({"daily_engine_manifest": str(manifest), "outreach_maintenance": {"ran": True}}),
        encoding="utf-8",
    )
    settings = SimpleNamespace(artifacts_dir=artifacts, resolved_tracking_workspace_dir=workspace)

    summary_path, report_path, html_path, _ = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        nightly_summary_path=nightly_summary,
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert [row["person"] for row in payload["auto_handled"]] == ["Auto Person"]
    assert [row["name"] for row in payload["messages_to_review"]] == ["Review Person"]
    assert [row["name"] for row in payload["system_held_messages"]] == ["Hold Person"]
    assert [row["action_type"] for row in payload["what_needs_you"]] == ["message_review"]
    assert payload["track_2_execution"]["status"] == "not_run"
    inbox_refresh = next(
        row for row in payload["linkedin_actions"]
        if row["action"] == "linkedin_inbox_refresh"
    )
    assert inbox_refresh["count"] == 2
    with (workspace / "linkedin_inbox_actions.csv").open(encoding="utf-8", newline="") as handle:
        inbox_rows = {row["person"]: row for row in csv.DictReader(handle)}
    assert inbox_rows["Auto Person"]["status"] == "auto_handled"
    assert inbox_rows["Review Person"]["status"] == "open"
    assert "Track 2 execution: `not_run`" in report_path.read_text(encoding="utf-8")
    html_text = html_path.read_text(encoding="utf-8")
    assert "Messages to review" in html_text
    assert "Auto-handled messages (this run)" in html_text


def test_daily_report_surfaces_email_draft_review_and_smtp_blocker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    drafts = artifacts / "exact-track-2-email-drafts.json"
    drafts.write_text(
        json.dumps(
            {
                "count": 1,
                "results": [
                    {
                        "organization_id": "org-email",
                        "contact_id": "ct-email",
                        "company": "EmailCo",
                        "name": "Email Person",
                        "email": "person@emailco.example",
                        "subject": "Specific EmailCo role fit",
                        "body": "Concise reviewed body that has not been approved or sent.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "track_2_email_draft_artifacts": [str(drafts)],
                "track_2_email_send_artifacts": [],
                "email_channel": {
                    "status": "skipped_missing_credentials",
                    "smtp_configured": False,
                    "blockers": ["Configure SMTP_HOST and SMTP_FROM_EMAIL before reviewed delivery."],
                    "approval_required": True,
                    "nightly_delivery_enabled": False,
                },
            }
        ),
        encoding="utf-8",
    )
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps({"daily_engine_manifest": str(manifest), "outreach_maintenance": {"ran": True}}),
        encoding="utf-8",
    )
    settings = SimpleNamespace(artifacts_dir=artifacts, resolved_tracking_workspace_dir=workspace)

    summary_path, report_path, html_path, _latest = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        nightly_summary_path=summary,
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["run_outcome"]["emails_sent"] == 0
    assert payload["messages_to_review"][0]["channel"] == "email"
    assert payload["messages_to_review"][0]["subject"] == "Specific EmailCo role fit"
    action_types = {row["action_type"] for row in payload["what_needs_you"]}
    assert {"message_review", "email_channel_blocker"} <= action_types
    email_source = next(row for row in payload["source_breakdown"] if row["source"] == "Cold email channel")
    assert email_source["status"] == "skipped_missing_credentials"
    assert email_source["raw"] == 1
    assert email_source["kept"] == 0
    assert "Configure SMTP_HOST" in report_path.read_text(encoding="utf-8")
    assert "Cold email actions (this run)" in html_path.read_text(encoding="utf-8")


def test_daily_report_counts_only_actual_sent_email_and_clears_matching_draft(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    draft_row = {
        "organization_id": "org-email",
        "contact_id": "ct-email",
        "company": "EmailCo",
        "name": "Email Person",
        "email": "person@emailco.example",
        "subject": "Specific EmailCo role fit",
        "body": "Approved body.",
    }
    drafts = artifacts / "exact-track-2-email-drafts.json"
    drafts.write_text(json.dumps({"results": [draft_row]}), encoding="utf-8")
    sends = artifacts / "exact-track-2-email-send-results.json"
    sends.write_text(
        json.dumps({"results": [{**draft_row, "delivery_status": "sent"}]}),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "track_2_email_draft_artifacts": [str(drafts)],
                "track_2_email_send_artifacts": [str(sends)],
                "email_channel": {"status": "sent", "smtp_configured": True, "blockers": [], "sent_count": 1},
            }
        ),
        encoding="utf-8",
    )
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps({"daily_engine_manifest": str(manifest), "outreach_maintenance": {"ran": True}}),
        encoding="utf-8",
    )
    settings = SimpleNamespace(artifacts_dir=artifacts, resolved_tracking_workspace_dir=workspace)

    summary_path, _report, _html, _latest = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        nightly_summary_path=summary,
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["run_outcome"]["emails_sent"] == 1
    assert payload["run_outcome"]["total_outbound_sends"] == 1
    assert payload["messages_to_review"] == []
    assert payload["email_actions"][0]["status"] == "sent"
    assert payload["company_execution"] == [
        {"company": "EmailCo", "counts": {"emails_sent": 1}, "summary": "emails sent 1"}
    ]


def test_daily_report_track_2_company_counts_are_actual_not_planned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    mapping = artifacts / "exact-mapping.json"
    mapping.write_text(json.dumps({"company": "Airbyte", "count": 3, "results": [{}, {}, {}]}), encoding="utf-8")
    invites = artifacts / "exact-invite-send-batch.json"
    invites.write_text(
        json.dumps({"company": "Airbyte", "results": [{"status": "sent"}, {"status": "sent"}]}),
        encoding="utf-8",
    )
    track = artifacts / "exact-track-2-daily-run.json"
    track.write_text(
        json.dumps(
            {
                "execute": True,
                "used": {"linkedin_invites": 999, "company_mapping": 999},
                "phase_results": [
                    {"phase": "4_contact_mapping", "status": "ran", "runs": [{"company": "Airbyte", "artifact": str(mapping)}]},
                    {"phase": "5_send_linkedin_invites", "status": "sent", "runs": [{"company": "Airbyte", "send_artifact": str(invites), "status_counts": {"sent": 2}}]},
                ],
            }
        ),
        encoding="utf-8",
    )
    source_metrics = tmp_path / "source-metrics.json"
    source_metrics.write_text(
        json.dumps({"sources": {}, "stage_metrics": {}, "action_queue": {"counts": {}}}),
        encoding="utf-8",
    )
    action_queue = tmp_path / "action-queue.json"
    action_queue.write_text(json.dumps({"counts": {"outreach_only_today": 0}}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "manifest_schema": "resume_generator.daily_engine_run_manifest",
                "manifest_version": 1,
                "source_metrics": str(source_metrics),
                "action_queue": str(action_queue),
                "artifacts": {},
            }
        ),
        encoding="utf-8",
    )
    nightly_summary = tmp_path / "summary.json"
    nightly_summary.write_text(
        json.dumps(
            {
                "daily_engine_returncode": 0,
                "daily_engine_manifest": str(manifest),
                "source_metrics": str(source_metrics),
                "action_queue": str(action_queue),
                "outreach_maintenance": {
                    "ran": True,
                    "track_2_daily_run_returncode": 0,
                    "track_2_daily_run_artifact": str(track),
                },
            }
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(artifacts_dir=artifacts, resolved_tracking_workspace_dir=workspace)

    summary_path, _report, _html, _latest = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        nightly_summary_path=nightly_summary,
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    airbyte = payload["company_execution"][0]
    assert airbyte["company"] == "Airbyte"
    assert airbyte["counts"]["linkedin_invites_sent"] == 2
    assert airbyte["counts"]["linkedin_profiles_mapped"] == 3
    assert 999 not in airbyte["counts"].values()
    assert payload["track_2_execution"]["status"] == "completed"
    assert payload["run_status"] == "completed"


def test_source_breakdown_exposes_each_startup_adapter_stage(tmp_path: Path) -> None:
    relationship_artifact = tmp_path / "yc-sf.json"
    relationship_artifact.write_text(json.dumps({"raw_count": 50, "count": 25}), encoding="utf-8")
    startup_report = tmp_path / "startup-report.json"
    startup_report.write_text(
        json.dumps(
            {
                "relationship_lane": {
                    "artifacts": {"yc_sf_bay_hiring": {"artifact": str(relationship_artifact), "count": 25, "status": "loaded"}},
                    "source_counts": {"yc_sf_bay_hiring": 15},
                }
            }
        ),
        encoding="utf-8",
    )
    metrics = tmp_path / "metrics.json"
    metrics.write_text(
        json.dumps(
            {
                "sources": {"startup_apply": {"status": "ran"}, "startup_relationship": {"status": "ran"}},
                "startup_source_report": {
                    "artifact": str(startup_report),
                    "startup_apply_discovered": {"a16z_job_board": 5},
                    "startup_apply_new": {"a16z_job_board": 1},
                    "relationship_source_counts": {"yc_sf_bay_hiring": 15},
                    "relationship_targets": 15,
                },
            }
        ),
        encoding="utf-8",
    )

    startup = next(
        row for row in _source_breakdown({"source_metrics": str(metrics)})
        if row["source"] == "Startup sources"
    )
    adapters = {
        (row["source"], row["lane"]): row
        for row in startup["details"]["adapters"]
    }
    assert adapters[("yc_sf_bay_hiring", "company_relationship_discovery")] == {
        "source": "yc_sf_bay_hiring",
        "lane": "company_relationship_discovery",
        "status": "loaded",
        "fetched": 50,
        "discovered": 25,
        "selected": 15,
        "artifact": str(relationship_artifact),
    }
    assert adapters[("a16z_job_board", "startup_job_discovery")]["selected"] == 1
    assert adapters[("builtin_sf_job_lists", "startup_job_discovery")]["status"] == "ran"
    assert adapters[("builtin_sf_job_lists", "startup_job_discovery")]["discovered"] == 0


def test_comms_learning_writes_gold_negative_and_silver_examples(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    artifact, summary = _write_comms_learning_artifact(
        workspace=tmp_path,
        reports_dir=reports,
        report_stem="run",
        manually_cleared_items=[{
            "company": "Example", "name": "A", "manual_latest_message": "Manual note", "draft_message": "Generated note",
        }],
        followup_payloads=[{"cleared_drafts": [{"company": "Example", "name": "B", "draft_message": "Sent draft"}]}],
        run_summary=None,
    )

    assert summary == {"gold": 1, "negative": 1, "silver": 1}
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert {item["label"] for item in payload["examples"]} == {"gold", "negative", "silver"}
    assert (tmp_path / "comms_learning" / "linkedin_examples.jsonl").exists()


def test_tpm_titles_bucket_as_product() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Principal TPM | Enterprise & Product Security",
        "Principal TPM | Enterprise & Product Security",
        settings,
    )

    assert bucket == "Product"


def test_university_recruiter_gets_separate_bucket() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Campus Recruiter",
        "Campus Recruiter USC Marshall School of Business Career Center",
        settings,
    )

    assert bucket == "University Recruiting"


def test_solution_engineer_buckets_as_adjacent() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Senior Solution Engineer at Snowflake",
        "Senior Solution Engineer at Snowflake",
        settings,
    )

    assert bucket == "Adjacent"


def test_founding_mechatronics_engineer_buckets_as_engineering() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Founding Mechatronics Engineer @ Eden",
        "Founding Mechatronics Engineer @ Eden",
        settings,
    )

    assert bucket == "Engineering"


def test_company_search_aliases_strip_common_startup_suffixes() -> None:
    assert company_search_aliases("Splash Inc.")[:2] == ["Splash Inc.", "Splash"]
    assert "Surtr" in company_search_aliases("Surtr Defense Systems")


def test_build_linkedin_contact_info_email_queue_uses_daily_email_research_accounts(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-a",
            name="Story Fit Co",
            organization_type=OrganizationType.COMPANY,
        )
    )
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-b",
            name="Other Co",
            organization_type=OrganizationType.COMPANY,
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-needs-email",
            organization_id="org-a",
            full_name="Needs Email",
            title="Product Lead",
            linkedin_url="https://www.linkedin.com/in/needs-email/",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-has-email",
            organization_id="org-a",
            full_name="Has Email",
            title="Founder",
            linkedin_url="https://www.linkedin.com/in/has-email/",
            email="has@example.com",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-other-org",
            organization_id="org-b",
            full_name="Other Org",
            title="PM",
            linkedin_url="https://www.linkedin.com/in/other-org/",
        )
    )

    queue = build_linkedin_contact_info_email_queue(
        workspace=tmp_path,
        daily_plan={
            "selected": [
                {"organization_id": "org-a", "expected_email_research": 1},
                {"organization_id": "org-b", "expected_email_research": 0},
            ]
        },
        limit=10,
    )

    assert queue == [
        {
            "contact_id": "ct-needs-email",
            "organization_id": "org-a",
            "company": "Story Fit Co",
            "name": "Needs Email",
            "title": "Product Lead",
            "linkedin_url": "https://www.linkedin.com/in/needs-email/",
            "company_website": "",
            "company_linkedin_url": "",
        }
    ]


def test_build_external_email_research_queue_allows_domain_without_linkedin(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-a",
            name="Story Fit Co",
            organization_type=OrganizationType.COMPANY,
            website="https://storyfit.example",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-domain-only",
            organization_id="org-a",
            full_name="Domain Only",
            title="Product Lead",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-excluded",
            organization_id="org-a",
            full_name="Excluded Person",
            title="Founder",
        )
    )

    queue = build_external_email_research_queue(
        workspace=tmp_path,
        daily_plan={"selected": [{"organization_id": "org-a", "expected_email_research": 2}]},
        limit=10,
        exclude_contact_ids={"ct-excluded"},
    )

    assert queue == [
        {
            "contact_id": "ct-domain-only",
            "organization_id": "org-a",
            "company": "Story Fit Co",
            "name": "Domain Only",
            "title": "Product Lead",
            "linkedin_url": "",
            "company_website": "https://storyfit.example",
            "company_linkedin_url": "",
        }
    ]


def test_apply_email_finder_results_updates_email_with_provenance(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-a",
            name="Story Fit Co",
            organization_type=OrganizationType.COMPANY,
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-needs-email",
            organization_id="org-a",
            full_name="Needs Email",
            title="Product Lead",
            linkedin_url="https://www.linkedin.com/in/needs-email/",
        )
    )

    updated = apply_email_finder_results(
        workbook=workbook,
        min_confidence=80,
        results=[
            EmailFinderResult(
                contact_id="ct-needs-email",
                organization_id="org-a",
                name="Needs Email",
                company="Story Fit Co",
                provider="hunter",
                status="found",
                detail="ok",
                email="needs.email@example.com",
                confidence=92,
                verification_status="valid",
            )
        ],
    )

    contact = OutreachWorkbook(tmp_path).list_contacts()[0]
    assert updated == 1
    assert contact.email == "needs.email@example.com"
    assert "external_email_found=" in contact.notes
    assert "provider=hunter" in contact.notes


def test_daily_execution_manifest_orders_phases_and_counts_actions() -> None:
    daily_plan = {
        "selected": [
            {
                "company": "Invite Co",
                "phase": "5_send_linkedin_invites",
                "phase_order": 50,
                "can_parallelize": False,
                "daily_action_priority": 70,
                "campaign_action": "send_initial_invites",
            },
            {
                "company": "Reply Co",
                "phase": "1_continue_live_conversations",
                "phase_order": 10,
                "can_parallelize": False,
                "daily_action_priority": 95,
                "campaign_action": "continue_conversation",
            },
            {
                "company": "Email Co",
                "phase": "3_contact_and_email_research",
                "phase_order": 30,
                "can_parallelize": True,
                "daily_action_priority": 80,
                "campaign_action": "find_email_path",
            },
        ]
    }

    grouped = daily_plan_items_by_phase(daily_plan)
    manifest = build_daily_execution_manifest(daily_plan)

    assert list(grouped) == [
        "1_continue_live_conversations",
        "3_contact_and_email_research",
        "5_send_linkedin_invites",
    ]
    assert manifest[0]["phase"] == "1_continue_live_conversations"
    assert manifest[1]["parallelizable"] is True
    assert manifest[2]["actions"] == {"send_initial_invites": 1}


def test_build_track_2_email_drafts_uses_email_contacts_and_style_review(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-email",
            name="Deepgram",
            organization_type=OrganizationType.COMPANY,
            target_lists="story-fit",
            notes="tags=voice ai,developer-tools | description=Voice AI platform for developers.",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-product",
            organization_id="org-email",
            full_name="Natalie Product",
            title="AI Product Leader",
            contact_type="Product",
            email="natalie@example.com",
        )
    )

    drafts = build_track_2_email_drafts(
        workspace=tmp_path,
        daily_plan={
            "selected": [
                {
                    "organization_id": "org-email",
                    "company": "Deepgram",
                    "campaign_action": "send_cold_email_followup",
                    "expected_email_drafts": 1,
                }
            ]
        },
        limit=3,
    )

    assert len(drafts) == 1
    assert drafts[0]["email"] == "natalie@example.com"
    assert drafts[0]["recipient_type"] == "senior_product"
    assert "Deepgram" in str(drafts[0]["subject"])
    assert "would love" not in str(drafts[0]["body"]).lower()
    assert drafts[0]["style_review"]["verdict"] == "style_ok"
    assert drafts[0]["craft_review"]["verdict"] in {"strong_send_candidate", "review"}
    assert drafts[0]["craft_review"]["score"] >= 76


def test_track_2_email_followup_copy_uses_cadence_variant() -> None:
    draft = draft_track_2_email(
        organization=OrganizationRecord(
            organization_id="org-a",
            name="Signal Co",
            organization_type=OrganizationType.COMPANY,
            notes="tags=data,workflow",
        ),
        contact=ContactRecord(
            contact_id="ct-a",
            organization_id="org-a",
            full_name="Alex Person",
            email="alex@example.com",
        ),
        campaign_action="send_cold_email_followup",
        cadence_action="email_followup_1",
        style_profile=CommunicationStyleProfile(),
    )

    assert draft["cadence_action"] == "email_followup_1"
    assert str(draft["subject"]).startswith("Re:")
    assert "keep nudging" in str(draft["body"])


def test_run_track_2_daily_plan_send_linkedin_requires_execute() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["run-track-2-daily-plan", "--send-linkedin"])

    assert result.exit_code == 1
    assert "--send-linkedin requires --execute." in result.output


def test_run_track_2_daily_plan_execute_is_cron_safe_without_live_linkedin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    OutreachWorkbook(workspace).initialize()

    fake_plan = {
        "selected_count": 2,
        "budget": {},
        "used": {"company_mapping": 1, "linkedin_invites": 1},
        "summary": {"map_more_contacts": 1, "send_initial_invites": 1},
        "phase_summary": {
            "4_contact_mapping": 1,
            "5_send_linkedin_invites": 1,
        },
        "selected": [
            {
                "organization_id": "org-map",
                "company": "Mapping Co",
                "phase": "4_contact_mapping",
                "phase_order": 40,
                "can_parallelize": True,
                "campaign_action": "map_more_contacts",
                "daily_action_priority": 70,
            },
            {
                "organization_id": "org-invite",
                "company": "Invite Co",
                "phase": "5_send_linkedin_invites",
                "phase_order": 50,
                "can_parallelize": False,
                "campaign_action": "send_initial_invites",
                "expected_linkedin_invites": 1,
                "daily_action_priority": 65,
            },
        ],
    }
    monkeypatch.setattr("outreach.cli._build_daily_plan_for_workspace", lambda **_kwargs: fake_plan)

    def _fail_live_linkedin(**_kwargs):
        raise AssertionError("live LinkedIn should not run without --live-linkedin")

    monkeypatch.setattr("outreach.cli.execute_linkedin_company_run", _fail_live_linkedin)

    result = CliRunner().invoke(
        app,
        [
            "run-track-2-daily-plan",
            "--workspace",
            str(workspace),
            "--execute",
        ],
    )

    assert result.exit_code == 0
    assert "4_contact_mapping | status=queued" in result.output
    assert "5_send_linkedin_invites | status=queued" in result.output


def test_run_supervised_e2e_send_linkedin_requires_execute(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "run-supervised-e2e",
            "--workspace",
            str(tmp_path / "workspace"),
            "--send-linkedin",
        ],
    )

    assert result.exit_code == 1
    assert "--send-linkedin requires --execute." in result.output


def test_run_supervised_e2e_dry_pipeline_writes_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"

    artifact, payload = run_supervised_e2e_pipeline(
        workspace=workspace,
        account_tracker_output=workspace / "account_tracker.xlsx",
        jobs_xlsx=tmp_path / "missing_jobs.xlsx",
        resume_blocklist=None,
        resume_jobs=False,
        resume_outreach_queue=False,
        strategic_accounts=False,
        story_fit_targets=False,
        relationship_leads=False,
    )

    assert artifact.exists()
    assert (workspace / "account_tracker.xlsx").exists()
    assert payload["execute"] is False
    assert Path(str(payload["latest_daily_report"])).exists()
    assert "Outreach Daily Run Report" in Path(str(payload["latest_daily_report"])).read_text()
    stage_statuses = {stage["name"]: stage["status"] for stage in payload["stages"]}
    assert stage_statuses["account_tracker"] == "built"
    assert stage_statuses["track_2_daily_run"] == "planned"
    track_2_stage = next(stage for stage in payload["stages"] if stage["name"] == "track_2_daily_run")
    assert any("track-2-daily-run" in path for path in track_2_stage["artifacts"])


def test_candidate_mentions_company_requires_structured_single_word_match() -> None:
    assert candidate_mentions_company(
        SimpleNamespace(title="Founder of bloom | wearebloom.io", snippet="", raw_text=""),
        ["Bloom"],
    )
    assert not candidate_mentions_company(
        SimpleNamespace(title="Engineering Team Lead at Bloomberg", snippet="", raw_text=""),
        ["Bloom"],
    )
    assert not candidate_mentions_company(
        SimpleNamespace(title="Project Manager", snippet="Current: Project Manager at Bloom Energy", raw_text=""),
        ["Bloom"],
    )
    assert not candidate_mentions_company(
        SimpleNamespace(title="Founding Partner at HedgeLegal", snippet="", raw_text=""),
        ["Hedge"],
    )


def test_product_pass_rejects_non_product_noise() -> None:
    assert not pass_relevance(
        "product_usc_marshall",
        "Other",
        "Technology & Strategy leader",
        "Technology & Strategy leader",
    )


def test_engineering_pass_rejects_solution_engineer_noise() -> None:
    assert not pass_relevance(
        "engineering_usc_marshall",
        "Adjacent",
        "Senior Solution Engineer at Snowflake",
        "Senior Solution Engineer at Snowflake",
    )


def test_marshall_passes_disabled_by_default() -> None:
    settings = OutreachSettings()

    assert settings.search.pass_definitions["product_usc_marshall"]["enabled"] is False
    assert settings.search.pass_definitions["engineering_usc_marshall"]["enabled"] is False


def test_broad_fallback_is_small_and_conditional() -> None:
    settings = OutreachSettings()
    broad = settings.search.pass_definitions["broad_fallback"]

    assert broad["limit"] == 6
    assert broad["run_if_below_pool_size"] == 18


def test_enable_marshall_turns_marshall_passes_on() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(settings, enable_marshall=True)

    assert passes["product_usc_marshall"]["enabled"] is True
    assert passes["engineering_usc_marshall"]["enabled"] is True


def test_include_pass_only_runs_selected_passes() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(
        settings,
        include_passes=("existing_connections", "product_network"),
    )

    assert passes["existing_connections"]["enabled"] is True
    assert passes["product_network"]["enabled"] is True
    assert passes["product_usc"]["enabled"] is False


def test_force_broad_fallback_removes_pool_gate() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(settings, force_broad_fallback=True)

    assert passes["broad_fallback"]["enabled"] is True
    assert "run_if_below_pool_size" not in passes["broad_fallback"]


def test_startup_mode_uses_startup_pass_stack() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(settings, company_mode="startup")

    assert list(passes) == ["existing_connections"]


def test_startup_founder_titles_are_kept_for_startup_mode() -> None:
    assert pass_relevance(
        "startup_founders",
        "Founder",
        "Founder & CEO at Icarus",
        "Founder & CEO at Icarus",
        company_mode="startup",
    )


def test_startup_operator_titles_are_kept_for_startup_mode() -> None:
    assert pass_relevance(
        "startup_operators",
        "Adjacent",
        "Chief of Staff",
        "Chief of Staff",
        company_mode="startup",
    )


def test_startup_broad_fallback_rejects_investors() -> None:
    assert not pass_relevance(
        "startup_company_coverage",
        "Other",
        "Software & AI Investments / Nexus Venture Partners",
        "Software & AI Investments / Nexus Venture Partners",
        company_mode="startup",
    )


def test_startup_company_coverage_keeps_generic_employee_titles() -> None:
    assert pass_relevance(
        "startup_preflight",
        "Other",
        "Member of Technical Staff",
        "Member of Technical Staff",
        company_mode="startup",
    )


def test_startup_preflight_boosts_exact_company_founder_candidates() -> None:
    settings = OutreachSettings()
    deduped: dict[str, dict] = {}

    kept = apply_raw_candidate(
        deduped=deduped,
        raw=SimpleNamespace(
            name="Charles Yong",
            title="Founder @ Vassar Robotics (YC X25)",
            raw_text="Founder @ Vassar Robotics (YC X25)",
            connection_degree="3rd",
            snippet="",
            linkedin_url="https://www.linkedin.com/in/charles/",
            location="",
            subtitle="",
        ),
        company="Vassar Robotics",
        pass_name="startup_preflight",
        pass_config={},
        settings=settings,
        company_mode="startup",
    )

    candidate = deduped["https://www.linkedin.com/in/charles/"]
    assert kept is True
    assert candidate["score"] >= 35
    assert "Startup founder" in candidate["triggers"]


def test_apply_raw_candidate_preserves_shared_history_signals() -> None:
    settings = OutreachSettings()
    deduped: dict[str, dict] = {}

    kept = apply_raw_candidate(
        deduped=deduped,
        raw=SimpleNamespace(
            name="Suman Sundaresh",
            title="Lead Product Manager @ Pebl | Ex-Intuit, Rappi, PayPal",
            raw_text="Lead Product Manager @ Pebl | Ex-Intuit, Rappi, PayPal",
            connection_degree="2nd",
            snippet="Mutual connection",
            linkedin_url="https://www.linkedin.com/in/suman/",
            location="",
            subtitle="",
        ),
        company="Pebl",
        pass_name="startup_preflight",
        pass_config={},
        settings=settings,
        company_mode="startup",
    )

    candidate = deduped["https://www.linkedin.com/in/suman/"]
    assert kept is True
    assert detect_shared_history_signals(candidate["title"], settings) == ["Intuit"]
    assert candidate["shared_history"] is True
    assert candidate["shared_history_signals"] == ["Intuit"]


def test_recommend_auto_send_limit_scales_with_pool_size() -> None:
    assert recommend_auto_send_limit(3) == 0
    assert recommend_auto_send_limit(5) == 5
    assert recommend_auto_send_limit(12) == 10
    assert recommend_auto_send_limit(20) == 12


def test_startup_pool_mode_drives_adaptive_threshold_and_send_cap() -> None:
    assert startup_pool_mode(1) == "micro"
    assert startup_pool_send_min_score("micro") == -5
    assert recommend_auto_send_limit(3, "micro") == 3
    assert startup_pool_mode(8) == "small"
    assert startup_pool_send_min_score("small") == 10
    assert recommend_auto_send_limit(8, "small") == 6


def test_effective_send_min_score_uses_startup_pool_metadata() -> None:
    payload = {
        "company_mode": "startup",
        "startup_pool": {
            "raw_count": 1,
            "kept_count": 1,
            "pool_mode": "micro",
            "adaptive_send_min_score": -5,
        },
        "pass_summaries": [],
    }

    assert effective_send_min_score(payload, requested_min_score=35, adaptive=True) == -5
    assert effective_send_min_score(payload, requested_min_score=35, adaptive=False) == 35


def test_startup_pool_metadata_can_be_recovered_from_older_artifacts() -> None:
    payload = {
        "company_mode": "startup",
        "pass_summaries": [
            {
                "pass_name": "startup_preflight",
                "raw_count": 2,
                "kept_count": 2,
                "coverage_only": True,
            }
        ],
    }

    metadata = startup_pool_metadata(payload)

    assert metadata["pool_mode"] == "micro"
    assert metadata["adaptive_send_min_score"] == -5


def test_sent_without_note_maps_to_sent_tracking_statuses() -> None:
    assert contact_status_from_invite_result("sent_without_note") == "Invited"
    assert touchpoint_status_from_invite_result("sent_without_note") == "Sent"


def test_select_invite_candidates_filters_existing_connections_and_blocked_notes() -> None:
    candidates = [
        {
            "name": "A",
            "linkedin_url": "https://www.linkedin.com/in/a/",
            "existing_connection": False,
            "score": 50,
            "note_qc": {"verdict": "send"},
        },
        {
            "name": "B",
            "linkedin_url": "https://www.linkedin.com/in/b/",
            "existing_connection": True,
            "score": 80,
            "note_qc": {"verdict": "send"},
        },
        {
            "name": "C",
            "linkedin_url": "https://www.linkedin.com/in/c/",
            "existing_connection": False,
            "score": 80,
            "note_qc": {"verdict": "blocked"},
        },
        {
            "name": "D",
            "linkedin_url": "https://www.linkedin.com/in/d/",
            "existing_connection": False,
            "score": 12,
            "note_qc": {"verdict": "send"},
        },
    ]

    selected = select_invite_candidates(candidates, limit=5)

    assert [item["name"] for item in selected] == ["A"]


def test_relative_linkedin_profile_is_treated_as_fallback() -> None:
    settings = OutreachSettings(linkedin_chrome_user_data_dir=Path("playwright/chrome-data"))

    assert settings.using_fallback_linkedin_profile() is True


def test_absolute_linkedin_profile_is_explicit_even_if_it_points_to_outreach_profile() -> None:
    settings = OutreachSettings(linkedin_chrome_user_data_dir=Path.cwd() / "playwright" / "chrome-data")

    assert settings.using_fallback_linkedin_profile() is False
    settings.validate_explicit_linkedin_profile()


def test_execute_invite_batch_persists_progress_per_result(monkeypatch, tmp_path: Path) -> None:
    settings = OutreachSettings(tracking_workspace_dir=tmp_path / "workspace")
    artifact_dir = tmp_path / "artifacts"
    source_artifact = tmp_path / "notes-batch.json"
    source_artifact.write_text("{}")
    persist_calls = []

    monkeypatch.setattr(OutreachSettings, "artifacts_dir", property(lambda self: artifact_dir))

    class _FakeWorkbook:
        def __init__(self, _path: Path) -> None:
            self.path = _path

    monkeypatch.setattr("outreach.cli.OutreachWorkbook", _FakeWorkbook)

    def _fake_persist(**kwargs):
        persist_calls.append(kwargs["processed_candidates"][0]["name"])
        return (1, 1)

    monkeypatch.setattr("outreach.cli.persist_invite_send_results", _fake_persist)

    final_artifact = artifact_dir / "final.json"

    def _fake_write_artifact(_artifacts_dir, _label, payload):
        final_artifact.parent.mkdir(parents=True, exist_ok=True)
        final_artifact.write_text(json.dumps(payload, indent=2))
        return final_artifact

    monkeypatch.setattr("outreach.cli.write_artifact", _fake_write_artifact)

    def _fake_send(self, batch, execute=False, on_result=None):
        results = []
        for candidate in batch:
            result = SimpleNamespace(
                name=candidate["name"],
                linkedin_url=candidate["linkedin_url"],
                status="sent",
                detail="ok",
                note=candidate.get("note", ""),
                screenshot_path="",
            )
            results.append(result)
            if on_result is not None:
                on_result(candidate, result, list(results))
        return results

    monkeypatch.setattr("outreach.cli.LinkedInScraper.send_connection_requests", _fake_send)

    batch = [
        {"name": "Alice", "linkedin_url": "https://www.linkedin.com/in/alice/", "note": "hi"},
        {"name": "Bob", "linkedin_url": "https://www.linkedin.com/in/bob/", "note": "hello"},
    ]

    artifact, progress_artifact, status_counts, contacts_added, touchpoints_added = execute_invite_batch(
        settings=settings,
        company="Scale AI",
        source_artifact_path=source_artifact,
        batch=batch,
        execute=True,
        limit=2,
        start_at=0,
        verdict="send",
        min_score=35,
    )

    assert artifact == final_artifact
    assert progress_artifact.exists()
    progress_payload = json.loads(progress_artifact.read_text())
    assert progress_payload["count"] == 2
    assert [item["name"] for item in progress_payload["results"]] == ["Alice", "Bob"]
    assert persist_calls == ["Alice", "Bob"]
    assert status_counts == {"sent": 2}
    assert contacts_added == 2
    assert touchpoints_added == 2


def test_execute_invite_batch_dry_run_does_not_persist(monkeypatch, tmp_path: Path) -> None:
    settings = OutreachSettings(tracking_workspace_dir=tmp_path / "workspace")
    artifact_dir = tmp_path / "artifacts"
    source_artifact = tmp_path / "notes-batch.json"
    source_artifact.write_text("{}")
    persist_calls = []

    monkeypatch.setattr(OutreachSettings, "artifacts_dir", property(lambda self: artifact_dir))
    monkeypatch.setattr("outreach.cli.OutreachWorkbook", lambda _path: object())

    def _fake_persist(**kwargs):
        persist_calls.append(kwargs)
        return (1, 1)

    monkeypatch.setattr("outreach.cli.persist_invite_send_results", _fake_persist)

    final_artifact = artifact_dir / "final.json"

    def _fake_write_artifact(_artifacts_dir, _label, payload):
        final_artifact.parent.mkdir(parents=True, exist_ok=True)
        final_artifact.write_text(json.dumps(payload, indent=2))
        return final_artifact

    monkeypatch.setattr("outreach.cli.write_artifact", _fake_write_artifact)

    def _fake_send(self, batch, execute=False, on_result=None):
        results = []
        for candidate in batch:
            result = SimpleNamespace(
                name=candidate["name"],
                linkedin_url=candidate["linkedin_url"],
                status="dry_run_ready",
                detail="ok",
                note=candidate.get("note", ""),
                screenshot_path="",
            )
            results.append(result)
            if on_result is not None:
                on_result(candidate, result, list(results))
        return results

    monkeypatch.setattr("outreach.cli.LinkedInScraper.send_connection_requests", _fake_send)

    _, progress_artifact, status_counts, contacts_added, touchpoints_added = execute_invite_batch(
        settings=settings,
        company="Tasklet",
        source_artifact_path=source_artifact,
        batch=[{"name": "Alice", "linkedin_url": "https://www.linkedin.com/in/alice/", "note": "hi"}],
        execute=False,
        limit=1,
        start_at=0,
        verdict="send",
        min_score=35,
    )

    assert progress_artifact.exists()
    assert persist_calls == []
    assert status_counts == {"dry_run_ready": 1}
    assert contacts_added == 0
    assert touchpoints_added == 0


def test_build_linkedin_reconcile_queue_selects_stale_invites() -> None:
    organization = OrganizationRecord(
        organization_id="org-snyk",
        name="Snyk",
        organization_type=OrganizationType.COMPANY,
    )
    stale_contact = ContactRecord(
        contact_id="ct-stale",
        organization_id="org-snyk",
        full_name="Mehak Singh",
        status="Invited",
        linkedin_url="https://www.linkedin.com/in/mehak/",
        last_contacted_at="2026-06-24T08:00:00+00:00",
    )
    fresh_contact = ContactRecord(
        contact_id="ct-fresh",
        organization_id="org-snyk",
        full_name="Fresh Invite",
        status="Invited",
        linkedin_url="https://www.linkedin.com/in/fresh/",
        last_contacted_at="2026-06-25T10:30:00+00:00",
    )
    connected_contact = ContactRecord(
        contact_id="ct-connected",
        organization_id="org-snyk",
        full_name="Already Connected",
        status="Connected",
        linkedin_url="https://www.linkedin.com/in/connected/",
        last_contacted_at="2026-06-24T08:00:00+00:00",
    )
    invite_touchpoint = TouchpointRecord(
        touchpoint_id="tp-stale",
        organization_id="org-snyk",
        contact_id="ct-stale",
        status="Sent",
        message_kind="linkedin_invite",
        message_text="Hi Mehak, would value a referral pointer.",
        sent_at="2026-06-24T08:00:00+00:00",
    )

    items = build_linkedin_reconcile_queue_items(
        organizations=[organization],
        contacts=[stale_contact, fresh_contact, connected_contact],
        touchpoints=[invite_touchpoint],
        min_age_hours=12,
        max_age_days=14,
        now=datetime(2026, 6, 25, 12, 0, tzinfo=UTC),
    )

    assert [item["contact_id"] for item in items] == ["ct-stale"]
    assert items[0]["company"] == "Snyk"
    assert items[0]["original_invite_note"] == "Hi Mehak, would value a referral pointer."


def test_apply_linkedin_reconcile_results_updates_contacts_and_touchpoints(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path / "workspace")
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-workwhile",
            name="WorkWhile",
            organization_type=OrganizationType.STARTUP,
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-roshni",
            organization_id="org-workwhile",
            full_name="Roshni Ramakrishnan",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/roshni/",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-owen",
            organization_id="org-workwhile",
            full_name="Owen Crook",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/owen/",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-deepanshu",
            organization_id="org-workwhile",
            full_name="Deepanshu Johar",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/deepanshu/",
        )
    )

    result = apply_linkedin_reconcile_results(
        workbook=workbook,
        results=[
            {
                "contact_id": "ct-roshni",
                "name": "Roshni Ramakrishnan",
                "linkedin_url": "https://www.linkedin.com/in/roshni/",
                "status": "connected",
                "detail": "Profile shows Message.",
            },
            {
                "contact_id": "ct-owen",
                "name": "Owen Crook",
                "linkedin_url": "https://www.linkedin.com/in/owen/",
                "status": "replied",
                "message_text": "Happy to help. What role are you looking at?",
            },
            {
                "contact_id": "ct-deepanshu",
                "name": "Deepanshu Johar",
                "linkedin_url": "https://www.linkedin.com/in/deepanshu/",
                "status": "connected",
                "latest_message": "You sent an attachment",
                "last_sender": "",
            },
        ],
        source_artifact="artifacts/reconcile.json",
        apply_changes=True,
    )

    assert result["summary"]["connected"] == 2
    assert result["summary"]["replied"] == 1
    assert result["summary"]["updated_contacts"] == 3
    assert result["summary"]["touchpoints_added"] == 3
    contacts = {item.contact_id: item for item in workbook.list_contacts()}
    assert contacts["ct-roshni"].status == "Connected"
    assert contacts["ct-owen"].status == "Replied"
    assert contacts["ct-deepanshu"].status == "Connected"
    deepanshu_result = next(item for item in result["results"] if item["contact_id"] == "ct-deepanshu")
    assert deepanshu_result["needs_follow_up"] is False
    touchpoints = workbook.list_touchpoints()
    assert {item.status for item in touchpoints} == {"Accepted", "Replied"}
    assert any(item.message_kind == "linkedin_reply" for item in touchpoints)


def test_build_linkedin_message_reconcile_results_uses_thread_offset() -> None:
    contacts = [
        ContactRecord(
            contact_id="ct-roshni",
            organization_id="org-workwhile",
            full_name="Roshni Ramakrishnan",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/roshni/",
        ),
        ContactRecord(
            contact_id="ct-owen",
            organization_id="org-workwhile",
            full_name="Owen Crook",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/owen/",
        ),
        ContactRecord(
            contact_id="ct-old",
            organization_id="org-workwhile",
            full_name="Old Thread",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/old/",
        ),
        ContactRecord(
            contact_id="ct-shubhankit",
            organization_id="org-d-matrix",
            full_name="Shubhankit R.",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/shubhankitr/",
        ),
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id="tp-roshni",
            organization_id="org-workwhile",
            contact_id="ct-roshni",
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Hi Roshni, I'd value a pointer on how technical PM candidates can stand out.",
        ),
        TouchpointRecord(
            touchpoint_id="tp-owen",
            organization_id="org-workwhile",
            contact_id="ct-owen",
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Hi Owen, would love a quick pointer on how builders work with product there.",
        ),
        TouchpointRecord(
            touchpoint_id="tp-shubhankit",
            organization_id="org-d-matrix",
            contact_id="ct-shubhankit",
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Hi Shubhankit, I'm exploring PM roles at d-Matrix.",
        ),
    ]
    threads = [
        {
            "thread_id": "thread-roshni",
            "name": "Roshni Ramakrishnan",
            "thread_url": "https://www.linkedin.com/messaging/thread/thread-roshni/",
            "latest_message": "You are now connected",
            "last_sender": "",
        },
        {
            "thread_id": "thread-owen",
            "name": "Owen Crook",
            "thread_url": "https://www.linkedin.com/messaging/thread/thread-owen/",
            "latest_message": "Happy to help. What role are you looking at?",
            "last_sender": "Owen Crook",
            "unread": True,
        },
        {
            "thread_id": "thread-old",
            "name": "Old Thread",
            "thread_url": "https://www.linkedin.com/messaging/thread/thread-old/",
            "latest_message": "Already seen",
            "last_sender": "Old Thread",
        },
        {
            "thread_id": "thread-shubhankit",
            "name": "Shubhankit Rathore",
            "thread_url": "",
            "latest_message": "Hi Shubhankit, I'm exploring PM roles at d-Matrix.",
            "last_sender": "You",
        },
    ]

    results, next_state = build_linkedin_message_reconcile_results(
        threads=threads,
        contacts=contacts,
        touchpoints=touchpoints,
        state={"seen_thread_ids": ["thread-old"]},
    )

    assert [item["contact_id"] for item in results] == ["ct-roshni", "ct-owen", "ct-shubhankit"]
    assert [item["status"] for item in results] == ["connected", "replied", "connected"]
    assert set(next_state["seen_thread_ids"]) == {"thread-roshni", "thread-owen", "thread-old", "thread-shubhankit"}
    assert set(next_state["thread_states"]) == {"thread-roshni", "thread-owen", "thread-old", "thread-shubhankit"}
    assert next_state["thread_states"]["thread-old"]["signature"] == "old thread|already seen"
    owen = next(item for item in results if item["contact_id"] == "ct-owen")
    assert [item["sender"] for item in owen["message_window"]] == ["You", "Owen Crook"]
    assert next_state["thread_states"]["thread-owen"]["message_window"][-1]["message"] == "Happy to help. What role are you looking at?"


def test_build_linkedin_message_reconcile_results_detects_changed_seen_thread() -> None:
    contacts = [
        ContactRecord(
            contact_id="ct-owen",
            organization_id="org-workwhile",
            full_name="Owen Crook",
            status="Connected",
            linkedin_url="https://www.linkedin.com/in/owen/",
        ),
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id="tp-owen",
            organization_id="org-workwhile",
            contact_id="ct-owen",
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Hi Owen, would love a quick pointer on technical PM paths.",
        ),
    ]

    results, next_state = build_linkedin_message_reconcile_results(
        threads=[
            {
                "thread_id": "thread-owen",
                "name": "Owen Crook",
                "thread_url": "https://www.linkedin.com/messaging/thread/thread-owen/",
                "latest_message": "Happy to help. What role are you looking at?",
                "last_sender": "Owen Crook",
            },
        ],
        contacts=contacts,
        touchpoints=touchpoints,
        state={
            "seen_thread_ids": ["thread-owen"],
            "thread_states": {
                "thread-owen": {
                    "signature": "you|hi owen would love a quick pointer on technical pm paths",
                },
            },
        },
    )

    assert len(results) == 1
    assert results[0]["contact_id"] == "ct-owen"
    assert results[0]["status"] == "replied"
    assert results[0]["thread_changed"] is True
    assert results[0]["state_reason"] == "changed_latest"
    assert next_state["thread_states"]["thread-owen"]["signature"] == "owen crook|happy to help. what role are you looking at?"


def test_build_linkedin_message_reconcile_results_baselines_legacy_seen_thread() -> None:
    contacts = [
        ContactRecord(
            contact_id="ct-old",
            organization_id="org-workwhile",
            full_name="Old Thread",
            status="Connected",
            linkedin_url="https://www.linkedin.com/in/old/",
        ),
    ]

    results, next_state = build_linkedin_message_reconcile_results(
        threads=[
            {
                "thread_id": "thread-old",
                "name": "Old Thread",
                "thread_url": "https://www.linkedin.com/messaging/thread/thread-old/",
                "latest_message": "Already seen",
                "last_sender": "Old Thread",
            },
        ],
        contacts=contacts,
        touchpoints=[],
        state={"seen_thread_ids": ["thread-old"]},
    )

    assert results == []
    assert next_state["thread_states"]["thread-old"]["signature"] == "old thread|already seen"


def test_build_linkedin_followup_drafts_handles_accepts_and_replies() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-snyk",
            name="Snyk",
            organization_type=OrganizationType.COMPANY,
        ),
        OrganizationRecord(
            organization_id="org-sortly",
            name="Sortly",
            organization_type=OrganizationType.COMPANY,
        ),
        OrganizationRecord(
            organization_id="org-voker",
            name="Voker",
            organization_type=OrganizationType.STARTUP,
            notes="description=Voker is the Agent Analytics Platform for monitoring and improving your AI agents.",
        ),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-mehak",
            organization_id="org-snyk",
            full_name="Mehak Singh",
            title="Associate Software Engineer at Snyk",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-deepanshu",
            organization_id="org-sortly",
            full_name="Deepanshu Johar",
            title="SWE 2 @ Sortly",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-mehak-reply",
            organization_id="org-snyk",
            full_name="Mehak Singh",
            title="Associate Software Engineer at Snyk",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-hamid",
            organization_id="org-snyk",
            full_name="Hamid Example",
            title="Software Engineer",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-david",
            organization_id="org-snyk",
            full_name="David Alessi",
            title="Product @ Snyk | AI, Data Infrastructure, ex-Microsoft",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-tyler",
            organization_id="org-voker",
            full_name="Tyler Postle",
            title="CEO @ Voker (YC S24): AI Founder & Operator",
            contact_type="Founder",
        ),
        ContactRecord(
            contact_id="ct-shaun",
            organization_id="org-voker",
            full_name="Shaun Weiss",
            title="Co-Founder",
            contact_type="Founder",
        ),
    ]

    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-mehak",
                "organization_id": "org-snyk",
                "name": "Mehak Singh",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Would really value a referral or pointer on how to stand out to the hiring team.",
            },
            {
                "contact_id": "ct-deepanshu",
                "organization_id": "org-sortly",
                "name": "Deepanshu Johar",
                "normalized_status": "replied",
                "latest_message": "I can share your profile to the HR in case that helps ?",
            },
            {
                "contact_id": "ct-mehak-reply",
                "organization_id": "org-snyk",
                "name": "Mehak Singh",
                "normalized_status": "replied",
                "latest_message": "I am sorry to say but I won't be able to help you.",
            },
            {
                "contact_id": "ct-hamid",
                "organization_id": "org-snyk",
                "name": "Hamid Example",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "I'm exploring PM roles and would love to learn from your experience.",
            },
            {
                "contact_id": "ct-david",
                "organization_id": "org-snyk",
                "name": "David Alessi",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Would love to connect and learn how engineering-heavy product work gets shaped.",
            },
            {
                "contact_id": "ct-tyler",
                "organization_id": "org-voker",
                "name": "Tyler Postle",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Would love to connect and learn from your experience.",
            },
            {
                "contact_id": "ct-shaun",
                "organization_id": "org-voker",
                "name": "Shaun Weiss",
                "normalized_status": "replied",
                "last_sender": "Shaun",
                "latest_message": "Hi Akshat - thanks for reaching out. If you could please send your resume to Alessandra@beyondmedplans.com, that would be helpful. Ale runs our product team. Thanks!",
            },
        ],
        organizations=organizations,
        contacts=contacts,
    )

    assert [item["draft_kind"] for item in drafts] == [
        "accepted_follow_up",
        "referral_offer_reply",
        "polite_close_reply",
        "accepted_follow_up",
        "accepted_follow_up",
        "accepted_follow_up",
        "conversation_reply",
    ]
    assert "referral" in str(drafts[0]["draft_message"]).lower()
    assert "short context" in str(drafts[1]["draft_message"]).lower()
    assert drafts[2]["send_recommendation"] == "optional"
    assert "thanks for letting me know" in str(drafts[2]["draft_message"]).lower()
    assert drafts[0]["company"] == "Snyk"
    assert drafts[0]["original_invite_note"].startswith("Would really value")
    assert drafts[0]["communication_review"]["channel"] == "linkedin_followup"
    assert "communication_recommendation" in drafts[0]
    assert drafts[1]["latest_message"].startswith("I can share your profile")
    assert "hiring contact" in str(drafts[3]["draft_message"])
    assert drafts[4]["followup_audience"] == "product"
    assert "does that background seem relevant to product work there" in str(drafts[4]["draft_message"]).lower()
    assert "could translate to the product work there" not in str(drafts[4]["draft_message"])
    assert "product or recruiting person" not in str(drafts[4]["draft_message"]).lower()
    assert drafts[5]["followup_audience"] == "founder"
    assert "AI agent analytics work" in str(drafts[5]["draft_message"])
    assert "fit anything useful at Voker" in str(drafts[5]["draft_message"])
    assert "any recs on who i should talk to" in str(drafts[5]["draft_message"]).lower()
    assert "happy to share more context if useful" not in str(drafts[5]["draft_message"])
    assert "Alessandra@beyondmedplans.com" in str(drafts[6]["draft_message"])
    assert drafts[6]["action_items"][0]["action_type"] == "email_resume"
    assert drafts[6]["action_items"][0]["email"] == "Alessandra@beyondmedplans.com"


def test_followup_draft_uses_context_window_to_hold_repeat_ack() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-ottimate",
            name="Ottimate",
            organization_type=OrganizationType.COMPANY,
        )
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-midun",
            organization_id="org-ottimate",
            full_name="Midun Raju C",
            title="Senior Software Engineer | Backend & ML",
            contact_type="Engineering",
        )
    ]

    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-midun",
                "organization_id": "org-ottimate",
                "name": "Midun Raju C",
                "normalized_status": "replied",
                "last_sender": "Midun Raju",
                "latest_message": "Absolutely",
                "message_window": [
                    {
                        "sender": "You",
                        "message": (
                            "Thanks for connecting, Midun. I'm exploring PM/product roles at Ottimate where my "
                            "backend/data engineering background could be useful. Would you be open to pointing me "
                            "toward the best referral path or hiring contact?"
                        ),
                    },
                    {"sender": "Midun Raju", "message": "Absolutely"},
                ],
            }
        ],
        organizations=organizations,
        contacts=contacts,
    )

    assert drafts[0]["draft_kind"] == "already_asked_wait"
    assert drafts[0]["send_recommendation"] == "hold"
    assert drafts[0]["communication_recommendation"] == "hold"
    assert drafts[0]["reply_intent"] == "already_asked_wait"


def test_followup_draft_does_not_invent_positive_callback_when_contact_does_not_know() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-tractian",
            name="TRACTIAN",
            organization_type=OrganizationType.COMPANY,
        )
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-bratee",
            organization_id="org-tractian",
            full_name="Bratee Podder",
            title="SWE @ Tractian",
            contact_type="Engineering",
        )
    ]
    latest = (
        "Honestly, I have no idea. I work as a developer for the marketing team, so mostly analysis work. "
        "I think a lot of engineers work differently to solve customer problems and that there's no unified method."
    )

    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-bratee",
                "organization_id": "org-tractian",
                "name": "Bratee Podder",
                "normalized_status": "replied",
                "last_sender": "Bratee",
                "latest_message": latest,
                "message_window": [{"sender": "Bratee", "message": latest}],
            }
        ],
        organizations=organizations,
        contacts=contacts,
    )

    assert drafts[0]["reply_intent"] == "does_not_know"
    assert "small-team" not in str(drafts[0]["draft_message"]).lower()
    assert "customer-feedback" not in str(drafts[0]["draft_message"]).lower()
    assert "Sure, thanks Bratee" in str(drafts[0]["draft_message"])


def test_accepted_followup_to_principal_engineer_uses_senior_ask() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-snyk",
            name="Snyk",
            organization_type=OrganizationType.COMPANY,
        )
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-emiliano",
            organization_id="org-snyk",
            full_name="Emiliano Castro",
            title="Principal Software Engineer at Snyk",
            contact_type="Engineering",
        )
    ]

    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-emiliano",
                "organization_id": "org-snyk",
                "name": "Emiliano Castro",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Hi Emiliano, I'm a Marshall MBA + former engineer exploring Snyk.",
            }
        ],
        organizations=organizations,
        contacts=contacts,
    )

    message = str(drafts[0]["draft_message"])
    assert "Does that background fit product work there" in message
    assert "Any recs on who I should talk to" in message
    assert "does that angle make sense" not in message
    assert "route I should understand" not in message
    assert "tight resume + 3-line blurb" not in message


def test_story_fit_metadata_flows_into_senior_followup() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-anam",
            name="Anam AI",
            organization_type=OrganizationType.COMPANY,
            notes=(
                "Story-fit target | story_fit_reason=FlairX gives a direct recruiting workflow pitch around "
                "AI interviews and candidate experience. | profile_evidence=FlairX AI PM internship."
            ),
        )
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-senior",
            organization_id="org-anam",
            full_name="Avery Senior",
            title="Principal Engineer",
            contact_type="Engineering",
        )
    ]

    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-senior",
                "organization_id": "org-anam",
                "name": "Avery Senior",
                "normalized_status": "connected",
                "needs_follow_up": True,
            }
        ],
        organizations=organizations,
        contacts=contacts,
    )

    assert "FlairX gives a direct recruiting workflow pitch" in str(drafts[0]["draft_message"])
    assert "Does that background fit product work there" in str(drafts[0]["draft_message"])


def test_track_2_email_uses_story_fit_reason_before_generic_fit_line() -> None:
    organization = OrganizationRecord(
        organization_id="org-anam",
        name="Anam AI",
        organization_type=OrganizationType.COMPANY,
        notes=(
            "Story-fit target | story_fit_reason=FlairX gives a direct recruiting workflow pitch around AI interviews. | "
            "profile_evidence=FlairX AI PM internship and recruiting engine work."
        ),
    )
    contact = ContactRecord(
        contact_id="ct-founder",
        organization_id="org-anam",
        full_name="Maya Founder",
        title="Founder",
        contact_type="Founder",
        email="maya@example.com",
    )

    draft = draft_track_2_email(
        organization=organization,
        contact=contact,
        campaign_action="send_initial_multichannel_outreach",
        style_profile=CommunicationStyleProfile(),
    )

    assert "The story-fit is concrete" in str(draft["body"])
    assert "FlairX gives a direct recruiting workflow pitch" in str(draft["body"])
    assert "The company looks close" not in str(draft["body"])


def test_communication_review_csv_and_feedback_import_round_trip(tmp_path: Path) -> None:
    review_artifact = tmp_path / "linkedin-review.json"
    payload = {
        "source_artifact": "artifacts/source.json",
        "results": [
            {
                "company": "Tessera Labs",
                "name": "Anirudh",
                "title": "Founder",
                "contact_id": "ct-anirudh",
                "organization_id": "org-tessera",
                "draft_kind": "accepted_follow_up",
                "draft_message": "Generic draft",
                "communication_recommendation": "rewrite_before_send",
                "communication_review": {
                    "channel": "linkedin_followup",
                    "score": 72,
                    "verdict": "needs_rewrite",
                    "recommended_action": "rewrite_before_send",
                    "flags": ["Generic company insight"],
                    "strengths": ["Concrete low-friction ask"],
                },
            }
        ],
    }
    review_artifact.write_text(json.dumps(payload), encoding="utf-8")

    rows = build_communication_review_csv_rows(payload=payload, review_artifact=review_artifact)
    assert len(rows) == 1
    assert rows[0]["row_id"]
    assert rows[0]["user_decision"] == ""
    assert "Generic company insight" in rows[0]["flags"]
    assert "generic_insight" in rows[0]["quality_labels"]
    assert "specific product" in rows[0]["rewrite_guidance"]
    assert "engineering + MBA background" in rows[0]["suggested_message"]
    assert "product/operator" not in rows[0]["suggested_message"]

    csv_path = write_communication_review_csv(payload=payload, review_artifact=review_artifact)
    with csv_path.open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    csv_rows[0]["user_decision"] = "reject"
    csv_rows[0]["user_reason"] = "generic_insight"
    csv_rows[0]["user_edit"] = "Thanks Anirudh. Is there a product path where my recruiting workflow work is relevant?"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_rows[0].keys())
        writer.writeheader()
        writer.writerows(csv_rows)

    summary = import_communication_feedback_rows(
        workspace=tmp_path / "workspace",
        feedback_path=csv_path,
        execute=True,
    )
    second_summary = import_communication_feedback_rows(
        workspace=tmp_path / "workspace",
        feedback_path=csv_path,
        execute=True,
    )

    assert summary["new_rows"] == 1
    assert summary["summary"]["decision_counts"] == {"reject": 1}
    assert second_summary["new_rows"] == 0
    assert second_summary["skipped_duplicates"] == 1


def test_communication_review_csv_suggests_simple_senior_product_question(tmp_path: Path) -> None:
    review_artifact = tmp_path / "linkedin-review.json"
    payload = {
        "source_artifact": "artifacts/source.json",
        "results": [
            {
                "company": "Snyk",
                "name": "Emiliano Castro",
                "title": "Principal Software Engineer at Snyk",
                "contact_id": "ct-emiliano",
                "organization_id": "org-snyk",
                "draft_kind": "accepted_follow_up",
                "draft_message": (
                    "Thanks for connecting, Emiliano. I'm trying to get on the radar at Snyk for PM/product roles. "
                    "If I send a tight resume + 3-line blurb, would you be open to pointing me to the right referral path?"
                ),
                "communication_review": {
                    "channel": "linkedin_followup",
                    "score": 82,
                    "verdict": "review",
                    "recommended_action": "human_review",
                    "flags": ["Seniority mismatch: tactical referral ask to senior/principal contact"],
                    "strengths": ["Concrete low-friction ask"],
                },
            }
        ],
    }

    rows = build_communication_review_csv_rows(payload=payload, review_artifact=review_artifact)
    suggestion = rows[0]["suggested_message"]

    assert "Does that background fit product work there" in suggestion
    assert "Any recs on who I should talk to" in suggestion
    assert "does that angle make sense" not in suggestion
    assert "route I should understand" not in suggestion
    assert "tight resume" not in suggestion


def test_summarize_linkedin_followup_actions_counts_daily_work() -> None:
    summary = summarize_linkedin_followup_actions(
        [
            {
                "company": "Snyk",
                "draft_kind": "accepted_follow_up",
                "send_recommendation": "safe_to_review",
            },
            {
                "company": "Snyk",
                "draft_kind": "conversation_reply",
                "send_recommendation": "review",
            },
            {
                "company": "Sortly",
                "draft_kind": "polite_close_reply",
                "send_recommendation": "optional",
            },
            {
                "company": "Beyond Med",
                "draft_kind": "conversation_reply",
                "send_recommendation": "review",
                "action_items": [
                    {
                        "priority": "high",
                        "description": "Email resume and a short role-fit note to Alessandra@beyondmedplans.com for Beyond Med.",
                    }
                ],
            },
        ],
        [
            {"action": "missing_contact"},
            {"action": "mark_connected"},
        ],
    )

    assert summary["follow_up_candidates"] == 1
    assert summary["reply_candidates"] == 3
    assert summary["optional_closes"] == 1
    assert summary["missing_contacts"] == 1
    assert summary["external_action_items"] == 1
    assert summary["action_items"][0]["priority"] == "high"
    assert summary["by_company"] == {"Snyk": 2, "Beyond Med": 1, "Sortly": 1}


def test_extract_linkedin_conversation_action_items_detects_resume_email() -> None:
    actions = extract_linkedin_conversation_action_items(
        {
            "name": "Shaun Weiss",
            "company": "Beyond Med",
            "last_sender": "Shaun",
            "latest_message": (
                "Hi Akshat - thanks for reaching out. If you could please send your resume "
                "to Alessandra@beyondmedplans.com, that would be helpful. Ale runs our product team."
            ),
        }
    )

    assert actions == [
        {
            "action_type": "email_resume",
            "priority": "high",
            "contact_name": "Shaun Weiss",
            "company": "Beyond Med",
            "email": "Alessandra@beyondmedplans.com",
            "description": "Email resume and a short role-fit note to Alessandra@beyondmedplans.com for Beyond Med.",
            "source_message": (
                "Hi Akshat - thanks for reaching out. If you could please send your resume "
                "to Alessandra@beyondmedplans.com, that would be helpful. Ale runs our product team."
            ),
        }
    ]


def test_summarize_linkedin_followup_actions_surfaces_inbound_opportunities() -> None:
    summary = summarize_linkedin_followup_actions(
        [],
        [
            {
                "action": "missing_contact",
                "name": "Alex M.",
                "latest_message": (
                    "Sponsored Remote AI projects for software engineers up to $100/hr. "
                    "I think your professional experience would make you a great candidate "
                    "for our Software Engineer position in our fellowship program. You can apply here."
                ),
            },
            {
                "action": "missing_contact",
                "name": "Random Sales",
                "latest_message": "Sponsored webinar for revenue leaders.",
            },
        ],
    )

    assert summary["missing_contacts"] == 2
    assert summary["external_action_items"] == 1
    assert summary["action_items"][0]["action_type"] == "review_inbound_opportunity"
    assert summary["action_items"][0]["contact_name"] == "Alex M."
    assert summary["action_items"][0]["priority"] == "medium"


def test_persist_linkedin_followup_send_result_records_touchpoint(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-mehak",
            organization_id="org-snyk",
            full_name="Mehak Singh",
            status="Connected",
        )
    )
    result = LinkedInFollowupSendResult(
        contact_id="ct-mehak",
        organization_id="org-snyk",
        name="Mehak Singh",
        company="Snyk",
        draft_kind="accepted_follow_up",
        send_recommendation="safe_to_review",
        draft_message="Thanks for connecting, Mehak.",
        status="sent",
        detail="Follow-up sent.",
    )

    created = persist_linkedin_followup_send_result(
        workbook=workbook,
        result=result,
        source_artifact=tmp_path / "drafts.json",
        send_artifact=tmp_path / "send.json",
    )

    assert created is True
    touchpoints = workbook.list_touchpoints()
    assert len(touchpoints) == 1
    assert touchpoints[0].message_kind == "linkedin_followup"
    contacts = {item.contact_id: item for item in workbook.list_contacts()}
    assert contacts["ct-mehak"].status == "Followed up"
    assert contacts["ct-mehak"].last_contacted_at


def test_attach_search_urls_to_candidates_uses_first_matching_pass() -> None:
    payload = {
        "pass_summaries": [
            {"pass_name": "product_network", "final_url": "https://www.linkedin.com/search/results/people/?foo=1"},
            {"pass_name": "engineering_usc", "final_url": "https://www.linkedin.com/search/results/people/?bar=2"},
        ]
    }
    candidates = [
        {"name": "Alice", "passes": ["product_network", "engineering_usc"]},
        {"name": "Bob", "passes": ["missing_pass"]},
    ]

    enriched = attach_search_urls_to_candidates(payload, candidates)

    assert enriched[0]["_search_url"] == "https://www.linkedin.com/search/results/people/?foo=1"
    assert "_search_url" not in enriched[1]


def test_parse_team_size_headcount_handles_commas() -> None:
    assert parse_team_size_headcount("1,600 employees") == 1600


def test_parse_batch_year_extracts_year() -> None:
    assert parse_batch_year("S2024") == 2024
    assert parse_batch_year("Spring 2026") == 2026


def test_normalize_tag_handles_dash_and_spacing() -> None:
    assert normalize_tag("Generative-AI") == "generative ai"


def test_item_matches_remote_uses_company_and_opportunity_location() -> None:
    assert item_matches_remote({"location": "Fully Remote", "opportunities": []}) is True
    assert item_matches_remote({"location": "Los Angeles", "opportunities": [{"location": "Remote"}]}) is True
    assert item_matches_remote({"location": "Los Angeles", "opportunities": []}) is False


def test_item_matches_tags_supports_partial_match() -> None:
    item = {"tags": ["artificial intelligence", "robotics"]}

    assert item_matches_tags(item, ("ai",)) is False
    assert item_matches_tags(item, ("robot",)) is True
    assert item_matches_tags(item, ("artificial-intelligence",)) is True


def test_extract_team_size_from_notes_reads_discovery_note() -> None:
    assert extract_team_size_from_notes("batch=Spring 2026 | team_size=12 employees | tags=ai") == 12


def test_parse_notes_metadata_extracts_structured_fields() -> None:
    notes = "batch=Spring 2026 | founded_year=2024 | tags=ai,robotics | description=Builds AI systems"

    metadata = parse_notes_metadata(notes)

    assert metadata["batch"] == "Spring 2026"
    assert metadata["founded_year"] == "2024"
    assert extract_tags_from_notes(notes) == ["ai", "robotics"]
    assert extract_description_from_notes(notes) == "Builds AI systems"


def test_text_contains_signal_avoids_short_keyword_false_positive() -> None:
    assert text_contains_signal("autonomous aircraft platform", "ai") is False
    assert text_contains_signal("artificial intelligence platform", "ai") is False
    assert text_contains_signal("ai platform", "ai") is True


def test_format_team_size_signal_adds_employee_suffix_for_bare_numbers() -> None:
    assert format_team_size_signal("16") == "16 employees"
    assert format_team_size_signal("150 Employees") == "150 Employees"


def test_filter_discovered_items_applies_jobs_size_and_batch_filters() -> None:
    items = [
        {"organization_name": "OlderCo", "jobs_url": "", "team_size": "50 employees", "batch": "W2020"},
        {"organization_name": "HiringCo", "jobs_url": "https://example.com/jobs", "team_size": "12 employees", "batch": "S2025"},
        {"organization_name": "BigCo", "jobs_url": "https://example.com/jobs", "team_size": "500 employees", "batch": "S2025"},
    ]

    filtered = filter_discovered_items(
        items,
        require_jobs_url=True,
        max_team_size=100,
        min_batch_year=2024,
    )

    assert [item["organization_name"] for item in filtered] == ["HiringCo"]


def test_filter_discovered_items_applies_remote_and_tag_filters() -> None:
    items = [
        {
            "organization_name": "RemoteAI",
            "jobs_url": "https://example.com/jobs",
            "team_size": "20 employees",
            "batch": "S2025",
            "location": "Fully Remote",
            "tags": ["artificial intelligence"],
            "opportunities": [],
        },
        {
            "organization_name": "OfficeRobotics",
            "jobs_url": "https://example.com/jobs",
            "team_size": "20 employees",
            "batch": "S2025",
            "location": "Los Angeles",
            "tags": ["robotics"],
            "opportunities": [],
        },
    ]

    filtered = filter_discovered_items(
        items,
        require_jobs_url=True,
        max_team_size=100,
        min_batch_year=2024,
        remote_only=True,
        include_tags=("artificial intelligence",),
    )

    assert [item["organization_name"] for item in filtered] == ["RemoteAI"]


def test_build_linkedin_company_queue_prioritizes_unworked_hiring_startups() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-mount",
            name="Mount",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup;sf;hiring",
            status="Researching",
            source_kind=SourceKind.YC_DIRECTORY,
            notes="batch=Spring 2026 | team_size=2 employees | tags=insurance,ai",
        ),
        OrganizationRecord(
            organization_id="org-doodle",
            name="Doodle Labs",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in;la;companies",
            status="Researching",
            source_kind=SourceKind.STARTUP_DIRECTORY,
            notes="team_size=50 Employees | tags=robotics",
        ),
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-mount",
            organization_id="org-mount",
            title="Founding AI Engineer",
        ),
        OpportunityRecord(
            opportunity_id="opp-doodle",
            organization_id="org-doodle",
            title="Marketing Designer",
        ),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-doodle",
            organization_id="org-doodle",
            full_name="Existing Contact",
        )
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id="tp-doodle",
            organization_id="org-doodle",
            message_text="hello",
        )
    ]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=touchpoints,
        require_no_contacts=False,
        require_hiring_signal=True,
    )

    assert queue[0].company == "Mount"
    assert queue[0].company_mode == "startup"
    assert "No LinkedIn-sourced contacts yet" in queue[0].triggers


def test_build_linkedin_company_queue_filters_target_lists_and_contacts() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="One",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            source_kind=SourceKind.YC_DIRECTORY,
        ),
        OrganizationRecord(
            organization_id="org-2",
            name="Two",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in;la",
            source_kind=SourceKind.STARTUP_DIRECTORY,
        ),
    ]
    opportunities = [
        OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A"),
        OpportunityRecord(opportunity_id="opp-2", organization_id="org-2", title="Role B"),
    ]
    contacts = [ContactRecord(contact_id="ct-2", organization_id="org-2", full_name="Person")]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        include_target_lists=("yc",),
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert [item.company for item in queue] == ["One"]


def test_build_linkedin_company_queue_keeps_non_linkedin_contacts_eligible() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="Mount",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            source_kind=SourceKind.YC_DIRECTORY,
        )
    ]
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A")]
    contacts = [
        ContactRecord(
            contact_id="ct-1",
            organization_id="org-1",
            full_name="Founder",
            source_kind=SourceKind.YC_DIRECTORY,
        )
    ]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert [item.company for item in queue] == ["Mount"]
    assert queue[0].linkedin_contact_count == 0


def test_build_linkedin_company_queue_mode_infers_big_company() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="BigCo",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in",
            source_kind=SourceKind.STARTUP_DIRECTORY,
            notes="team_size=5000 Employees",
        )
    ]
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A")]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=[],
        touchpoints=[],
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert queue[0].company_mode == "big_company"


def test_infer_fit_reasons_scores_ai_startup_hiring_signals() -> None:
    organization = OrganizationRecord(
        organization_id="org-mount",
        name="Mount",
        organization_type=OrganizationType.STARTUP,
        city="San Francisco",
        notes="team_size=12 employees | location=San Francisco, CA | tags=ai,insurance | description=AI risk platform",
    )
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-mount", title="Product Strategy Intern")]

    score, reasons = infer_fit_reasons(
        organization=organization,
        tags=["ai", "insurance"],
        description="AI risk platform for autonomous agents",
        opportunities=opportunities,
    )

    assert score >= 60
    assert "AI/ML angle" in reasons
    assert fit_band_from_score(score) == "strong"


def test_build_organization_intel_items_shapes_reviewable_output() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="Alpha",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            city="San Francisco",
            source_kind=SourceKind.YC_DIRECTORY,
            discovered_at="2026-04-09T10:00:00+00:00",
            website="https://alpha.example.com",
            source_url="https://www.ycombinator.com/companies/alpha",
            notes=(
                "batch=Spring 2026 | founded_year=2024 | team_size=20 employees | "
                "location=San Francisco, CA | jobs_count=2 | tags=ai,data | "
                "description=Builds AI data workflow software"
            ),
        )
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-1",
            organization_id="org-1",
            title="Product Operations Intern",
        )
    ]
    contacts = [ContactRecord(contact_id="ct-1", organization_id="org-1", full_name="Founder", contact_type="founder")]
    touchpoints: list[TouchpointRecord] = []

    items = build_organization_intel_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=touchpoints,
        require_hiring_signal=True,
    )

    assert len(items) == 1
    assert items[0]["company"] == "Alpha"
    assert items[0]["public_revenue_signal"] == "Not surfaced in the source pages yet."
    assert items[0]["fit_band"] == "strong"
    assert "AI/ML angle" in items[0]["fit_reasons"]


def test_score_opportunity_relevance_prefers_pm_intern_over_engineering() -> None:
    organization = OrganizationRecord(
        organization_id="org-1",
        name="Alpha",
        organization_type=OrganizationType.STARTUP,
    )

    product_score, product_reasons = score_opportunity_relevance("MBA Product Manager Intern", organization)
    engineer_score, engineer_reasons = score_opportunity_relevance("Senior Software Engineer", organization)

    assert product_score >= 80
    assert "Product role" in product_reasons
    assert engineer_score == 0
    assert engineer_reasons == ["Role looks functionally off-target"]
    assert classify_opportunity_action(product_score) == "apply_now"


def test_build_target_action_queue_distinguishes_apply_vs_outreach() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-apply",
            name="ApplyCo",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            notes="team_size=20 employees | location=San Francisco | tags=ai | description=AI workflow platform",
        ),
        OrganizationRecord(
            organization_id="org-outreach",
            name="OutreachCo",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            notes="team_size=8 employees | location=Los Angeles | tags=robotics | description=Robotics platform for logistics",
        ),
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-1",
            organization_id="org-apply",
            title="Product Operations Intern",
        ),
        OpportunityRecord(
            opportunity_id="opp-2",
            organization_id="org-outreach",
            title="Senior Mechanical Engineer",
        ),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-1",
            organization_id="org-outreach",
            full_name="Founder One",
            contact_type="founder",
        )
    ]

    items = build_target_action_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        include_target_lists=("yc",),
    )

    assert items[0]["company"] == "ApplyCo"
    assert items[0]["action"] == "apply_now"
    outreach_item = next(item for item in items if item["company"] == "OutreachCo")
    assert outreach_item["action"] == "outreach_now"
    assert outreach_item["relevant_role_count"] == 0


def test_relationship_loop_follow_up_connected_contact() -> None:
    organization = OrganizationRecord(
        organization_id="org-synphony",
        name="Synphony",
        organization_type=OrganizationType.STARTUP,
        target_lists="yc;startup;hiring",
        notes="team_size=12 | tags=robotics,ai,data | description=Robotics automation platform with a data pipeline for physical AI.",
    )
    contact = ContactRecord(
        contact_id="ct-sean",
        organization_id="org-synphony",
        full_name="Sean Wu",
        title="CEO",
        contact_type="Founder",
        status="Connected",
        last_contacted_at="2026-06-24T10:00:00+00:00",
    )
    touchpoint = TouchpointRecord(
        touchpoint_id="tp-1",
        organization_id="org-synphony",
        contact_id="ct-sean",
        status="Sent",
        message_kind="linkedin_invite",
        message_text="Hi Sean, I'm a Marshall MBA exploring product/operator paths at Synphony.",
        sent_at="2026-06-24T10:00:00+00:00",
    )

    items = build_relationship_loop_items(
        organizations=[organization],
        opportunities=[],
        contacts=[contact],
        touchpoints=[touchpoint],
        now=datetime(2026, 6, 25, tzinfo=UTC),
    )

    assert items[0]["company"] == "Synphony"
    assert items[0]["relationship_stage"] == "connected_no_conversation"
    assert items[0]["next_action"] == "follow_up_connected_contact"
    assert items[0]["suggested_contact_name"] == "Sean Wu"
    assert "thanks for connecting" in str(items[0]["suggested_message"])
    assert "Synphony" in str(items[0]["suggested_message"])


def test_relationship_loop_runs_people_search_when_core_company_has_no_contacts() -> None:
    organization = OrganizationRecord(
        organization_id="org-mount",
        name="Mount",
        organization_type=OrganizationType.STARTUP,
        target_lists="yc;startup;hiring",
        notes="team_size=2 | tags=insurance,ai,security | description=Insurance and risk evaluation for AI agents.",
    )
    opportunity = OpportunityRecord(
        opportunity_id="opp-1",
        organization_id="org-mount",
        title="Product Management Intern",
    )

    items = build_relationship_loop_items(
        organizations=[organization],
        opportunities=[opportunity],
        contacts=[],
        touchpoints=[],
        now=datetime(2026, 6, 25, tzinfo=UTC),
    )

    assert items[0]["relationship_stage"] == "unstarted"
    assert items[0]["next_action"] == "run_linkedin_people_search"
    assert items[0]["relationship_goal"] == "summer_fall_internship"
    assert items[0]["relationship_gap"] == 3


def test_relationship_loop_adds_channel_after_stale_linkedin_wave() -> None:
    organization = OrganizationRecord(
        organization_id="org-workwhile",
        name="WorkWhile",
        organization_type=OrganizationType.STARTUP,
        target_lists="startup;hiring;priority",
        notes="team_size=90 | tags=marketplace,platform | description=Labor marketplace platform for hourly work.",
    )
    contacts = [
        ContactRecord(
            contact_id=f"ct-{index}",
            organization_id="org-workwhile",
            full_name=f"Engineer {index}",
            contact_type="Engineering",
            status="Invited",
            last_contacted_at="2026-06-18T10:00:00+00:00",
        )
        for index in range(10)
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id=f"tp-{index}",
            organization_id="org-workwhile",
            contact_id=f"ct-{index}",
            status="Sent",
            message_kind="linkedin_invite",
            message_text=f"Invite {index}",
            sent_at="2026-06-18T10:00:00+00:00",
        )
        for index in range(10)
    ]

    items = build_relationship_loop_items(
        organizations=[organization],
        opportunities=[],
        contacts=contacts,
        touchpoints=touchpoints,
        outreach_wave_size=10,
        now=datetime(2026, 6, 25, tzinfo=UTC),
    )

    assert items[0]["relationship_stage"] == "outreach_sent"
    assert items[0]["next_action"] == "research_email_path"
    assert items[0]["sent_invite_count"] == 10


def test_relationship_loop_does_not_treat_generic_startup_tag_as_core() -> None:
    organization = OrganizationRecord(
        organization_id="org-low-fit",
        name="LowFit Startup",
        organization_type=OrganizationType.STARTUP,
        target_lists="startup;hiring",
        notes="team_size=30 | tags=consumer,social | description=Consumer social events app.",
    )

    items = build_relationship_loop_items(
        organizations=[organization],
        opportunities=[],
        contacts=[],
        touchpoints=[],
        min_fit_score=70,
        now=datetime(2026, 6, 25, tzinfo=UTC),
    )

    assert items[0]["next_action"] == "watch"
    assert "not strong enough" in str(items[0]["action_reason"])


def test_build_resume_outreach_queue_applies_override_bias_and_company_cap() -> None:
    jobs = [
        ResumeJob(
            row_id="1",
            company="Typeface",
            role_title="Product Manager Intern",
            location="San Francisco",
            url="https://example.com/typeface-1",
            url_hash="hash-1",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.4,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="2",
            company="Typeface",
            role_title="PM Intern 2",
            location="San Francisco",
            url="https://example.com/typeface-2",
            url_hash="hash-2",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.2,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="3",
            company="Typeface",
            role_title="PM Intern 3",
            location="San Francisco",
            url="https://example.com/typeface-3",
            url_hash="hash-3",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.1,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="4",
            company="TikTok",
            role_title="Product Manager Intern",
            location="San Jose",
            url="https://example.com/tiktok",
            url_hash="hash-4",
            source="linkedin_live_jobs_v1",
            status="generated",
            normalized_status="generated",
            fit_score=8.6,
            fit_rationale="excellent fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
    ]
    overrides = {
        "typeface": CompanyOverride(
            company="Typeface",
            normalized_company="typeface",
            company_type_override="startup",
            startup_bias="high",
            notes="High outreach value",
        ),
        "tiktok": CompanyOverride(
            company="TikTok",
            normalized_company="tiktok",
            company_type_override="big_company",
            startup_bias="deprioritize",
            notes="Prefer startups first",
        ),
    }

    queue = build_resume_outreach_queue(jobs, company_overrides=overrides, max_per_company=2)

    assert len(queue) == 3
    assert queue[0].company == "Typeface"
    assert queue[0].company_type == "startup"
    assert queue[0].startup_bias == "high"
    assert queue[-1].company == "TikTok"
    assert queue[-1].startup_bias == "deprioritize"

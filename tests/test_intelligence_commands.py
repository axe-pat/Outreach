import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from outreach.cli import _apply_linkedin_cadence_guards, app
from outreach.company_news import company_news_capture_snapshots, company_news_signal_id
from outreach.company_watchlist import (
    CandidateCompanySignal,
    CompanyFitRubric,
    RubricDimension,
)
from outreach.intelligence_commands import (
    _apply_email_approval,
    _capture_due,
    _email_is_approved,
    _email_is_verified,
    _promote_approved_watchlist,
)
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachChannel,
    OutreachWorkbook,
    SourceKind,
    TouchpointRecord,
)
from outreach.email_delivery import EmailDeliveryResult


runner = CliRunner()


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    OutreachWorkbook(workspace).initialize()
    return workspace


def test_company_discovery_command_builds_review_queue(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    feed = workspace / "linkedin_feed_signals.csv"
    fields = ["company", "signal_kinds", "review_disposition", "post_text", "post_url", "author_name", "last_seen_at", "company_url", "context", "relevance_reason"]
    with feed.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"company": "Signal Co", "signal_kinds": "company_discovery;startup_discovery;hiring;funding", "review_disposition": "pending", "post_text": "AI data workflow startup hiring product strategy", "post_url": "https://linkedin.test/1"})

    result = runner.invoke(app, ["build-company-discovery-review", "--workspace", str(workspace), "--run-id", "run-1"])

    assert result.exit_code == 0, result.output
    assert (workspace / "company_discovery" / "company_discovery_review.csv").exists()
    assert "Pending review in workspace: 1" in result.output


def test_promoted_watchlist_approval_rehydrates_after_empty_artifact_rebuild(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace = _workspace(tmp_path)
    workbook = OutreachWorkbook(workspace)
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-durable-ai",
            name="DurableAI",
            organization_type=OrganizationType.COMPANY,
            target_lists="company-watchlist;track-2;relationship",
            status="Reviewed watchlist",
            website="https://durable.example",
            notes=(
                "Human-approved company discovery watchlist"
                " | watchlist_review_state=approved"
                " | watchlist_reviewer=reviewer-1"
                " | watchlist_reviewed_at=2026-07-11T08:00:00+00:00"
                " | watchlist_reviewer_notes=Strong reviewed fit"
            ),
        )
    )
    rubric = CompanyFitRubric(
        domain_fit=RubricDimension(score=2, evidence="AI platform"),
        technical_mba_story=RubricDimension(score=2, evidence="technical story"),
        geography_remote=RubricDimension(score=2, evidence="US remote"),
        growth_quality=RubricDimension(score=2, evidence="funding"),
        role_surface=RubricDimension(score=2, evidence="product roles"),
    )
    signal = CandidateCompanySignal(
        company_name="Durable AI",
        website="https://durable.example",
        rubric=rubric,
        provenance=[
            {
                "source_name": "Fixture News",
                "source_type": "company_news",
                "source_run_id": "run-1",
                "source_url": "https://news.example/durable",
            }
        ],
    )
    signal_id = company_news_signal_id(signal)
    (workspace / "company_news_signals.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "signals": [
                    {
                        "signal_id": signal_id,
                        "first_seen_at": "2026-07-11T08:00:00+00:00",
                        "last_seen_at": "2026-07-11T08:00:00+00:00",
                        "seen_run_ids": ["run-1"],
                        "signal": signal.model_dump(mode="json"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = workspace / "company_discovery"
    output_dir.mkdir(parents=True)
    (output_dir / "company_watchlist.json").write_text(
        json.dumps({"schema_version": "1.0", "entries": []}),
        encoding="utf-8",
    )

    first = runner.invoke(
        app,
        ["build-company-discovery-review", "--workspace", str(workspace), "--run-id", "run-1"],
    )
    second = runner.invoke(
        app,
        ["build-company-discovery-review", "--workspace", str(workspace), "--run-id", "run-2"],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    watchlist = json.loads((output_dir / "company_watchlist.json").read_text(encoding="utf-8"))
    assert [entry["company_name"] for entry in watchlist["entries"]] == ["Durable AI"]
    assert watchlist["entries"][0]["review_state"] == "approved"


def test_failed_linkedin_capture_returns_nonzero_after_writing_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "outreach.intelligence_commands.capture_linkedin_signals_live",
        lambda *args, **kwargs: {
            "status": "failed",
            "feed": {"status": "failed", "captured": 0},
            "profile_viewers": {"status": "skipped", "captured": 0},
        },
    )

    result = runner.invoke(
        app,
        ["capture-linkedin-intelligence", "--workspace", str(_workspace(tmp_path))],
    )

    assert result.exit_code == 1
    assert "LinkedIn feed: failed" in result.output
    assert "Artifact:" in result.output


def test_profile_viewer_due_date_uses_observation_not_file_mtime(tmp_path: Path) -> None:
    path = tmp_path / "viewers.csv"
    last_seen = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    path.write_text(f"viewer_id,last_seen_at\nviewer-1,{last_seen}\n", encoding="utf-8")

    assert _capture_due(path, 7) is True


def test_company_discovery_report_is_capture_scoped_but_review_queue_is_cumulative(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    feed = workspace / "linkedin_feed_signals.csv"
    fields = [
        "signal_id",
        "company",
        "signal_kinds",
        "review_disposition",
        "post_text",
        "post_url",
        "author_name",
        "last_seen_at",
        "company_url",
        "context",
        "relevance_reason",
    ]
    with feed.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for signal_id, company in (("new", "New Co"), ("old", "Old Co")):
            writer.writerow(
                {
                    "signal_id": signal_id,
                    "company": company,
                    "signal_kinds": "company_discovery;hiring",
                    "review_disposition": "pending",
                    "post_text": "AI workflow startup hiring product strategy",
                    "post_url": f"https://linkedin.test/{signal_id}",
                }
            )
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "observed_at": "2026-07-10T08:00:00+00:00",
                "feed": {
                    "status": "completed",
                    "captured_signal_ids": ["new"],
                },
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "build-company-discovery-review",
            "--workspace",
            str(workspace),
            "--run-id",
            "run-1",
            "--capture-artifact",
            str(capture),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Company signals this run: 1" in result.output
    assert "Pending review in workspace: 2" in result.output


def test_company_news_capture_does_not_claim_old_linkedin_ledger_as_same_run(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    feed = workspace / "linkedin_feed_signals.csv"
    with feed.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "signal_id",
                "company",
                "signal_kinds",
                "review_disposition",
                "post_text",
                "post_url",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "signal_id": "old-linkedin",
                "company": "Old Feed Co",
                "signal_kinds": "company_discovery;hiring",
                "review_disposition": "pending",
                "post_text": "Old AI startup hiring",
                "post_url": "https://linkedin.test/old",
            }
        )
    signals = []
    for signal_id, company in (("news-new", "New News Co"), ("news-old", "Old News Co")):
        signals.append(
            {
                "signal_id": signal_id,
                "first_seen_at": "2026-07-10T08:00:00+00:00",
                "last_seen_at": "2026-07-10T08:00:00+00:00",
                "seen_run_ids": ["run-1"],
                "signal": {
                    "company_name": company,
                    "provenance": [
                        {
                            "source_name": "Fixture News",
                            "source_type": "company_news",
                            "source_run_id": "run-1",
                            "source_url": f"https://news.test/{signal_id}",
                        }
                    ],
                },
            }
        )
    (workspace / "company_news_signals.json").write_text(
        json.dumps({"schema_version": "1.0", "signals": signals}),
        encoding="utf-8",
    )
    capture = tmp_path / "news-capture.json"
    captured_signal = CandidateCompanySignal.model_validate(signals[0]["signal"])
    snapshots, snapshots_sha256 = company_news_capture_snapshots([captured_signal])
    capture.write_text(
        json.dumps(
            {
                "status": "completed",
                "captured_signal_ids": [company_news_signal_id(captured_signal)],
                "captured_signal_snapshots": snapshots,
                "captured_signal_snapshots_sha256": snapshots_sha256,
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "build-company-discovery-review",
            "--workspace",
            str(workspace),
            "--run-id",
            "run-1",
            "--news-capture-artifact",
            str(capture),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Company signals this run: 1" in result.output
    assert "Pending review in workspace: 3" in result.output


def test_watchlist_promotion_preserves_non_linkedin_source_provenance(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    watchlist = tmp_path / "company_watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "company_name": "News Discovered Co",
                        "rubric_total": 13,
                        "reviewer_notes": "Approved after product-surface review",
                        "provenance": [
                            {
                                "source_type": "funding_news",
                                "source_run_id": "news-run-1",
                                "source_url": "https://news.example/company",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert _promote_approved_watchlist(workspace, watchlist) == 1
    organization = OutreachWorkbook(workspace).list_organizations()[0]
    assert organization.source_kind == SourceKind.OTHER
    assert organization.source_url == "https://news.example/company"
    assert "source_types=funding_news" in organization.notes
    assert "source_run_ids=news-run-1" in organization.notes


def test_watchlist_promotion_merges_existing_organization_idempotently(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    workbook = OutreachWorkbook(workspace)
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-news-discovered-co",
            name="News Discovered Co",
            organization_type=OrganizationType.COMPANY,
            target_lists="existing-list",
            status="Researching",
            source_kind=SourceKind.MANUAL,
            notes="Existing account context | owner=akshat",
        )
    )
    watchlist = tmp_path / "company_watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "company_name": "News Discovered Co",
                        "rubric_total": 13,
                        "reviewer": "reviewer-1",
                        "reviewed_at": "2026-07-11T08:00:00+00:00",
                        "reviewer_notes": "Approved after product-surface review",
                        "provenance": [
                            {
                                "source_type": "funding_news",
                                "source_run_id": "news-run-1",
                                "source_url": "https://news.example/company",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert _promote_approved_watchlist(workspace, watchlist) == 0
    organization = workbook.list_organizations()[0]
    assert organization.status == "Reviewed watchlist"
    assert organization.target_lists == (
        "existing-list;company-watchlist;track-2;relationship"
    )
    assert organization.source_kind == SourceKind.OTHER
    assert organization.source_url == "https://news.example/company"
    assert "Existing account context" in organization.notes
    assert "owner=akshat" in organization.notes
    assert "watchlist_review_state=approved" in organization.notes
    assert "watchlist_source_types=funding_news" in organization.notes
    first_content = (workspace / "organizations.csv").read_text(encoding="utf-8")

    assert _promote_approved_watchlist(workspace, watchlist) == 0
    assert (workspace / "organizations.csv").read_text(encoding="utf-8") == first_content


def test_role_cadence_and_learning_commands_emit_artifacts(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"company": "Ops Co", "title": "Business Operations Intern"}]}), encoding="utf-8")
    metrics = tmp_path / "source-run-metrics.json"
    metrics.write_text(json.dumps({"sources": {"jobspy": {"status": "ran", "details": {"raw_artifact": str(jobs)}}}}), encoding="utf-8")

    role = runner.invoke(app, ["build-role-surface-report", "--source-metrics", str(metrics), "--workspace", str(workspace), "--run-id", "run-1"])
    cadence = runner.invoke(app, ["build-outreach-cadence-report", "--workspace", str(workspace)])
    learning = runner.invoke(app, ["build-outcome-learning-report", "--workspace", str(workspace)])

    assert role.exit_code == 0, role.output
    assert cadence.exit_code == 0, cadence.output
    assert learning.exit_code == 0, learning.output
    assert (workspace / "role_surface" / "role_surface_report.json").exists()
    assert (workspace / "outreach_cadence_plan.json").exists()
    assert (workspace / "comms_learning" / "outcome_learning.json").exists()


def test_email_command_holds_unreviewed_drafts(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    draft = tmp_path / "drafts.json"
    draft.write_text(json.dumps({"results": [{"organization_id": "org-a", "contact_id": "ct-a", "email": "a@example.com", "subject": "Hello", "body": "Specific body"}]}), encoding="utf-8")

    result = runner.invoke(app, ["send-track-2-emails", "--draft-artifact", str(draft), "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "held: 1" in result.output


def test_generated_communication_recommendation_is_not_human_email_approval() -> None:
    assert not _email_is_approved({"communication_recommendation": "send"})
    assert not _email_is_approved({"user_decision": "approved"})
    assert not _email_is_approved(
        {
            "user_decision": "approved",
            "approval_binding_valid": True,
            "approval_email_matches": False,
        }
    )
    assert _email_is_approved(
        {
            "user_decision": "approved",
            "approval_binding_valid": True,
            "approval_email_matches": True,
        }
    )


def test_email_approval_does_not_follow_tracker_and_draft_recipient_mutation() -> None:
    approvals = {
        (
            "org-a",
            "ct-a",
            "old@example.com",
            "Specific subject",
            "Reviewed body",
        ): {
            "organization_id": "org-a",
            "contact_id": "ct-a",
            "email": "old@example.com",
            "subject": "Specific subject",
            "message": "Reviewed body",
            "user_decision": "approve",
            "review_artifact": "review.json",
        }
    }
    mutated_draft = {
        "organization_id": "org-a",
        "contact_id": "ct-a",
        "email": "new@example.com",
        "subject": "Specific subject",
        "body": "Reviewed body",
    }
    mutated_contact = ContactRecord(
        contact_id="ct-a",
        organization_id="org-a",
        full_name="A",
        email="new@example.com",
        notes="email_verified=true",
    )

    bound = _apply_email_approval(mutated_draft, approvals)

    assert _email_is_verified(mutated_contact, bound)
    assert not _email_is_approved(bound)
    assert bound.get("approval_binding_valid") is not True


def test_verified_marker_cannot_authorize_a_different_artifact_recipient() -> None:
    contact = ContactRecord(
        contact_id="ct-a",
        organization_id="org-a",
        full_name="A",
        email="verified@example.com",
        notes="linkedin_contact_info_email_found=2026-07-10",
    )

    assert not _email_is_verified(contact, {"email": "other@example.com"})
    assert _email_is_verified(contact, {"email": "verified@example.com"})


def test_email_command_consumes_human_approval_csv_for_preview(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workbook = OutreachWorkbook(workspace)
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-approved",
            organization_id="org-approved",
            full_name="Approved Person",
            email="approved@example.com",
        )
    )
    draft = tmp_path / "drafts.json"
    draft.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "organization_id": "org-approved",
                        "contact_id": "ct-approved",
                        "email": "approved@example.com",
                        "subject": "Specific subject",
                        "body": "Original specific body",
                        "cadence_action": "email_initial",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    approval_csv = tmp_path / "review.csv"
    approval_csv.write_text(
        "organization_id,contact_id,email,subject,message,user_decision,user_reason,user_edit,review_artifact\n"
        f"org-approved,ct-approved,approved@example.com,Specific subject,Original specific body,approve,specific fit,Edited human-approved body,{draft}\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "send-track-2-emails",
            "--draft-artifact",
            str(draft),
            "--approval-csv",
            str(approval_csv),
            "--workspace",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Eligible: 1; held: 0; sent: 0" in result.output

    approval_csv.write_text(
        "organization_id,contact_id,email,subject,message,user_decision,user_reason,user_edit,review_artifact\n"
        f"org-approved,ct-approved,approved@example.com,Specific subject,Different reviewed body,approve,specific fit,,{draft}\n",
        encoding="utf-8",
    )
    stale = runner.invoke(
        app,
        [
            "send-track-2-emails",
            "--draft-artifact",
            str(draft),
            "--approval-csv",
            str(approval_csv),
            "--workspace",
            str(workspace),
        ],
    )
    assert stale.exit_code == 0, stale.output
    assert "Eligible: 0; held: 1; sent: 0" in stale.output


def test_email_execute_records_each_send_before_a_retry_can_advance(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _workspace(tmp_path)
    workbook = OutreachWorkbook(workspace)
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-send",
            organization_id="org-send",
            full_name="Send Person",
            email="send@example.com",
        )
    )
    draft = tmp_path / "review.json"
    draft.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "organization_id": "org-send",
                        "contact_id": "ct-send",
                        "email": "send@example.com",
                        "subject": "Bound subject",
                        "body": "Bound reviewed body",
                        "cadence_action": "email_initial",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    approvals = tmp_path / "review.csv"
    approvals.write_text(
        "organization_id,contact_id,email,subject,message,user_decision,user_reason,user_edit,review_artifact\n"
        f"org-send,ct-send,send@example.com,Bound subject,Bound reviewed body,approve,specific,,{draft}\n",
        encoding="utf-8",
    )
    sent: list[str] = []

    class FakeSmtpSender:
        def __init__(self, config):
            pass

        def send(self, *, recipient: str, subject: str, body: str):
            sent.append(recipient)
            return EmailDeliveryResult(recipient, subject, "sent")

    monkeypatch.setattr(
        "outreach.intelligence_commands.SmtpEmailSender",
        FakeSmtpSender,
    )
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "sender@test")

    args = [
        "send-track-2-emails",
        "--draft-artifact",
        str(draft),
        "--approval-csv",
        str(approvals),
        "--workspace",
        str(workspace),
        "--execute",
    ]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert "sent: 1" in first.output
    assert second.exit_code == 0, second.output
    assert "sent: 0" in second.output
    assert sent == ["send@example.com"]
    recorded = OutreachWorkbook(workspace).list_touchpoints()
    assert len(recorded) == 1
    assert recorded[0].status == "Sent"


def test_email_replay_after_ninety_days_appends_a_fresh_touchpoint(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _workspace(tmp_path)
    workbook = OutreachWorkbook(workspace)
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-restart",
            organization_id="org-restart",
            full_name="Restart Person",
            email="restart@example.com",
        )
    )
    old_sent_at = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id="tp-old-email",
            organization_id="org-restart",
            contact_id="ct-restart",
            channel=OutreachChannel.EMAIL,
            status="Sent",
            message_kind="email_initial",
            message_text="Old body",
            recorded_at=old_sent_at,
            sent_at=old_sent_at,
        )
    )
    draft = tmp_path / "restart-review.json"
    draft.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "organization_id": "org-restart",
                        "contact_id": "ct-restart",
                        "email": "restart@example.com",
                        "subject": "Fresh sequence",
                        "body": "Fresh reviewed body",
                        "cadence_action": "email_initial",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    approvals = tmp_path / "restart-review.csv"
    approvals.write_text(
        "organization_id,contact_id,email,subject,message,user_decision,user_reason,user_edit,review_artifact\n"
        f"org-restart,ct-restart,restart@example.com,Fresh sequence,Fresh reviewed body,approve,specific,,{draft}\n",
        encoding="utf-8",
    )

    class FakeSmtpSender:
        def __init__(self, config):
            pass

        def send(self, *, recipient: str, subject: str, body: str):
            return EmailDeliveryResult(recipient, subject, "sent")

    monkeypatch.setattr(
        "outreach.intelligence_commands.SmtpEmailSender",
        FakeSmtpSender,
    )
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "sender@test")

    result = runner.invoke(
        app,
        [
            "send-track-2-emails",
            "--draft-artifact",
            str(draft),
            "--approval-csv",
            str(approvals),
            "--workspace",
            str(workspace),
            "--execute",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "sent: 1" in result.output
    recorded = OutreachWorkbook(workspace).list_touchpoints()
    assert len(recorded) == 2
    assert {item.touchpoint_id for item in recorded} > {"tp-old-email"}
    old = next(item for item in recorded if item.touchpoint_id == "tp-old-email")
    assert old.sent_at == old_sent_at


def test_linkedin_cadence_guard_allows_day_four_and_holds_pending(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workbook = OutreachWorkbook(workspace)
    now = datetime.now(UTC)
    for contact_id in ("ct-due", "ct-pending"):
        workbook.upsert_contact(ContactRecord(contact_id=contact_id, organization_id="org-a", full_name=contact_id))
        workbook.append_touchpoint(TouchpointRecord(
            touchpoint_id=f"tp-invite-{contact_id}", organization_id="org-a", contact_id=contact_id,
            channel=OutreachChannel.LINKEDIN, status="Sent", message_kind="linkedin_invite",
            message_text="Specific invite", sent_at=(now - timedelta(days=8)).isoformat(),
        ))
    workbook.append_touchpoint(TouchpointRecord(
        touchpoint_id="tp-accept", organization_id="org-a", contact_id="ct-due",
        channel=OutreachChannel.LINKEDIN, status="Connected", message_kind="connection_accepted",
        message_text="LinkedIn invite accepted.", recorded_at=(now - timedelta(days=5)).isoformat(),
    ))

    allowed, held = _apply_linkedin_cadence_guards(
        workbook=workbook,
        drafts=[
            {"organization_id": "org-a", "contact_id": "ct-due", "draft_message": "Your launch was interesting—curious how the team is approaching adoption.", "send_recommendation": "safe_to_review"},
            {"organization_id": "org-a", "contact_id": "ct-pending", "draft_message": "Just following up", "send_recommendation": "safe_to_review"},
        ],
    )

    assert [item["contact_id"] for item in allowed] == ["ct-due"]
    assert held[0]["contact_id"] == "ct-pending"
    assert held[0]["send_recommendation"] == "cadence_hold"


def test_linkedin_cadence_guard_holds_a_learned_negative_pattern(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workbook = OutreachWorkbook(workspace)
    now = datetime.now(UTC)
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-negative",
            organization_id="org-a",
            full_name="Negative Pattern",
        )
    )
    workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id="tp-invite-negative",
            organization_id="org-a",
            contact_id="ct-negative",
            channel=OutreachChannel.LINKEDIN,
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Invite",
            sent_at=(now - timedelta(days=8)).isoformat(),
        )
    )
    workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id="tp-accept-negative",
            organization_id="org-a",
            contact_id="ct-negative",
            channel=OutreachChannel.LINKEDIN,
            status="Connected",
            message_kind="connection_accepted",
            message_text="Accepted",
            recorded_at=(now - timedelta(days=4)).isoformat(),
        )
    )

    allowed, held = _apply_linkedin_cadence_guards(
        workbook=workbook,
        drafts=[
            {
                "organization_id": "org-a",
                "contact_id": "ct-negative",
                "draft_message": "A sufficiently specific message",
                "send_recommendation": "safe_to_review",
                "communication_review": {
                    "flags": ["Repeats learned negative message pattern: learned_negative"]
                },
            }
        ],
    )

    assert allowed == []
    assert held[0]["send_recommendation"] == "cadence_hold"
    assert "learned negative" in held[0]["cadence_reasons"][0]


def test_linkedin_send_guard_rechecks_new_negative_against_stale_draft(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workbook = OutreachWorkbook(workspace)
    now = datetime.now(UTC)
    message = "This stale generated message repeats the newly learned weak wording."
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-stale-negative",
            organization_id="org-a",
            full_name="Stale Negative",
        )
    )
    workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id="tp-invite-stale-negative",
            organization_id="org-a",
            contact_id="ct-stale-negative",
            channel=OutreachChannel.LINKEDIN,
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Invite",
            sent_at=(now - timedelta(days=8)).isoformat(),
        )
    )
    workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id="tp-accept-stale-negative",
            organization_id="org-a",
            contact_id="ct-stale-negative",
            channel=OutreachChannel.LINKEDIN,
            status="Connected",
            message_kind="connection_accepted",
            message_text="Accepted",
            recorded_at=(now - timedelta(days=4)).isoformat(),
        )
    )
    (workspace / "communication_style_profile.yml").write_text(
        "weak_messages:\n"
        "  - label: learned_negative_stale\n"
        "    recipient_type: general\n"
        f"    message: '{message}'\n"
        "    source: comms_learning/linkedin_examples.jsonl\n",
        encoding="utf-8",
    )

    allowed, held = _apply_linkedin_cadence_guards(
        workbook=workbook,
        drafts=[
            {
                "organization_id": "org-a",
                "contact_id": "ct-stale-negative",
                "draft_message": message,
                "send_recommendation": "safe_to_review",
                "communication_review": {"flags": []},
            }
        ],
    )

    assert allowed == []
    assert held[0]["send_recommendation"] == "cadence_hold"
    assert "learned_negative_stale" in held[0]["cadence_reasons"][0]

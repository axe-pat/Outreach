import json
from datetime import UTC, datetime, timedelta

from outreach.outcome_learning import (
    build_outcome_learning,
    concise_learning_summary,
    load_labeled_examples,
    write_outcome_learning_artifact,
)
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OutreachChannel,
    TouchpointRecord,
)


START = datetime(2026, 7, 1, 12, tzinfo=UTC)


def touchpoint(
    suffix: str,
    *,
    organization_id: str,
    contact_id: str,
    at: datetime,
    status: str,
    kind: str,
    message: str,
    channel: OutreachChannel = OutreachChannel.LINKEDIN,
    notes: str = "",
) -> TouchpointRecord:
    return TouchpointRecord(
        touchpoint_id=f"tp-{suffix}",
        organization_id=organization_id,
        contact_id=contact_id,
        channel=channel,
        status=status,
        message_kind=kind,
        message_text=message,
        recorded_at=at.isoformat(),
        sent_at=at.isoformat() if status == "Sent" else "",
        notes=notes,
    )


def fixture_data():
    organizations = [
        OrganizationRecord(organization_id="org-acme", name="Acme"),
        OrganizationRecord(organization_id="org-beta", name="Beta"),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-product",
            organization_id="org-acme",
            full_name="Priya Product",
            title="Senior Product Manager",
        ),
        ContactRecord(
            contact_id="ct-recruiter",
            organization_id="org-beta",
            full_name="Riley Recruiter",
            title="Technical Recruiter",
            email="riley@example.com",
        ),
        ContactRecord(
            contact_id="ct-founder",
            organization_id="org-acme",
            full_name="Fran Founder",
            title="Founder and CEO",
        ),
    ]
    followup_message = "The Acme launch maps to my data-platform work. Is PM hiring likely this fall?"
    manual_message = "Saw the new workflow launch. The operator angle is genuinely interesting."
    touchpoints = [
        touchpoint(
            "invite",
            organization_id="org-acme",
            contact_id="ct-product",
            at=START,
            status="Sent",
            kind="linkedin_invite",
            message="Specific invite",
        ),
        touchpoint(
            "accept",
            organization_id="org-acme",
            contact_id="ct-product",
            at=START + timedelta(days=1),
            status="Accepted",
            kind="linkedin_reconcile",
            message="LinkedIn invite accepted.",
        ),
        touchpoint(
            "followup",
            organization_id="org-acme",
            contact_id="ct-product",
            at=START + timedelta(days=5),
            status="Sent",
            kind="linkedin_followup",
            message=followup_message,
        ),
        touchpoint(
            "reply",
            organization_id="org-acme",
            contact_id="ct-product",
            at=START + timedelta(days=6),
            status="Replied",
            kind="linkedin_reply",
            message="LinkedIn reply detected.",
        ),
        touchpoint(
            "email",
            organization_id="org-beta",
            contact_id="ct-recruiter",
            at=START,
            status="Sent",
            kind="email_initial",
            message="Specific cold email",
            channel=OutreachChannel.EMAIL,
        ),
        touchpoint(
            "email-rejection",
            organization_id="org-beta",
            contact_id="ct-recruiter",
            at=START + timedelta(days=2),
            status="Replied",
            kind="email_reply",
            message="Thanks, but we are not interested right now.",
            channel=OutreachChannel.EMAIL,
        ),
        touchpoint(
            "manual",
            organization_id="org-acme",
            contact_id="ct-founder",
            at=START,
            status="Sent",
            kind="linkedin_manual_message",
            message=manual_message,
            notes="manual_outbound_detected=true",
        ),
    ]
    labeled_examples = [
        {
            "label": "gold",
            "company": "Acme",
            "name": "Fran Founder",
            "channel": "linkedin",
            "message": manual_message,
        },
        {
            "label": "silver",
            "company": "Acme",
            "name": "Priya Product",
            "channel": "linkedin",
            "message": followup_message,
        },
        {
            "label": "negative",
            "company": "Acme",
            "name": "Priya Product",
            "channel": "linkedin",
            "message": "I was impressed by your innovative company and would love to connect.",
        },
    ]
    return touchpoints, contacts, organizations, labeled_examples


def test_outcome_learning_attributes_outcomes_and_labels_across_dimensions() -> None:
    touchpoints, contacts, organizations, labeled_examples = fixture_data()

    report = build_outcome_learning(
        touchpoints,
        contacts=contacts,
        organizations=organizations,
        labeled_examples=labeled_examples,
        generated_at=START + timedelta(days=10),
        recommendation_min_sends=1,
    )

    assert report.totals.sends == 4
    assert report.totals.accepts == 1
    assert report.totals.replies == 2
    assert report.totals.rejections == 1
    assert report.totals.gold == 1
    assert report.totals.silver == 3
    assert report.totals.negative == 1
    assert report.unattributed_outcomes == {"accepts": 0, "replies": 0, "rejections": 0}

    assert report.by_message["linkedin_invite"].accepts == 1
    assert report.by_message["linkedin_followup"].replies == 1
    assert report.by_message["email_initial"].rejections == 1
    assert report.by_message["linkedin_manual_message"].gold == 1
    assert report.by_audience["product"].sends == 2
    assert report.by_audience["recruiting"].rejections == 1
    assert report.by_audience["founder_executive"].gold == 1
    assert report.by_account["Acme"].sends == 3
    assert report.by_account["Beta"].rejections == 1
    assert any(item.action == "review_targeting_and_copy" for item in report.recommendations)


def test_learning_artifact_is_advisory_and_report_summary_is_concise(tmp_path) -> None:
    touchpoints, contacts, organizations, labeled_examples = fixture_data()
    report = build_outcome_learning(
        touchpoints,
        contacts=contacts,
        organizations=organizations,
        labeled_examples=labeled_examples,
        generated_at=START + timedelta(days=10),
        recommendation_min_sends=1,
    )

    path = write_outcome_learning_artifact(tmp_path / "learning" / "outcomes.json", report)
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = concise_learning_summary(report, limit=1)

    assert payload["application_contract"] == {
        "recommendations_are_advisory": True,
        "production_prompts_mutated": False,
        "human_review_required_before_prompt_changes": True,
    }
    assert set(payload) >= {"totals", "by_message", "by_audience", "by_account"}
    assert len(summary["recommendations"]) == 1
    assert summary["advisory_only"] is True


def test_load_labeled_examples_supports_jsonl_and_run_artifact(tmp_path) -> None:
    jsonl = tmp_path / "examples.jsonl"
    jsonl.write_text(
        json.dumps({"label": "gold", "message": "Manual"})
        + "\n"
        + json.dumps({"label": "negative", "message": "Replaced"})
        + "\n",
        encoding="utf-8",
    )
    artifact = tmp_path / "run.json"
    artifact.write_text(
        json.dumps({"examples": [{"label": "silver", "message": "Approved"}]}),
        encoding="utf-8",
    )

    assert [item["label"] for item in load_labeled_examples(jsonl)] == ["gold", "negative"]
    assert [item["label"] for item in load_labeled_examples(artifact)] == ["silver"]

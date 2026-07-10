from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from outreach.account_tracker import AccountRow, DailyPlanBudget, build_track_2_daily_plan
from outreach.communication_lab import build_rewrite_guidance
from outreach.cli import (
    build_communication_review_csv_rows,
    build_company_note_context,
    build_linkedin_followup_drafts,
    build_linkedin_message_reconcile_results,
    build_linkedin_reconcile_queue_items,
    build_track_2_email_drafts,
    draft_track_2_email,
    persist_invite_send_results,
)
from outreach.messaging_roles import TargetRoleFamily, infer_target_role_context
from outreach.services.notes import NoteGenerator
from outreach.style_profile import CommunicationStyleProfile
from outreach.tracking import (
    ContactRecord,
    OpportunityRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachWorkbook,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Product Manager", TargetRoleFamily.PRODUCT_PM),
        ("Product Strategy Manager", TargetRoleFamily.PRODUCT_STRATEGY),
        ("Business Operations Lead", TargetRoleFamily.BIZOPS_STRATEGY),
        ("Technical Program Manager", TargetRoleFamily.PROGRAM_OPERATIONS),
        ("GTM Strategy & Operations", TargetRoleFamily.GROWTH_GTM),
        ("Strategic Partnerships Manager", TargetRoleFamily.GENERAL_BUSINESS),
    ],
)
def test_infers_first_class_target_role_families(title: str, expected: TargetRoleFamily) -> None:
    target = infer_target_role_context(opportunity_titles=[title])

    assert target.family == expected
    assert target.is_concrete is True
    assert target.matched_text == title


def test_product_opportunity_remains_primary_in_mixed_company() -> None:
    target = infer_target_role_context(
        opportunity_titles=["Product Manager Intern", "Business Operations Associate"]
    )

    assert target.family == TargetRoleFamily.PRODUCT_PM
    assert target.matched_text == "Product Manager Intern"


def test_product_opportunity_remains_primary_when_mixed_titles_are_reversed() -> None:
    target = infer_target_role_context(
        opportunity_titles=["Business Operations Associate", "Product Manager Intern"]
    )

    assert target.family == TargetRoleFamily.PRODUCT_PM
    assert target.matched_text == "Product Manager Intern"


def test_explicit_product_target_still_wins_over_mixed_company_opportunities() -> None:
    target = infer_target_role_context(
        explicit_title="Product Manager Intern",
        opportunity_titles=["Business Operations Associate"],
    )

    assert target.family == TargetRoleFamily.PRODUCT_PM
    assert target.source == "explicit_title"


def test_concrete_adjacent_opportunity_outranks_broad_product_target_notes() -> None:
    target = infer_target_role_context(
        opportunity_titles=["Business Operations Associate"],
        organization_notes="target_roles=Product, strategy",
    )

    assert target.family == TargetRoleFamily.BIZOPS_STRATEGY
    assert target.source == "opportunity_title"


@pytest.mark.parametrize(
    "signal",
    [
        {"existing_connection": True},
        {"usc": True},
        {"usc_marshall": True},
        {"shared_history": True, "shared_history_signals": ["Intuit"]},
    ],
)
def test_invite_special_paths_remove_pm_pivot_for_concrete_bizops_target(signal: dict) -> None:
    candidate = {
        "name": "Maya Person",
        "title": "Strategy Leader, ex-Intuit",
        "role_bucket": "Adjacent",
        "existing_connection": False,
        "usc": False,
        "usc_marshall": False,
        "shared_history": False,
        **signal,
    }

    note = NoteGenerator().generate(
        candidate,
        company="Acme",
        note_context={"opportunity_titles": ["Business Operations Associate"]},
    )

    assert note.target_role_family == "bizops_strategy"
    assert "business operations" in note.text.lower() or "bizops" in note.text.lower()
    assert not re.search(r"\bpm\b|pm/product|product roles|product work", note.text, flags=re.I)


def test_invite_preserves_fallback_and_opportunity_provenance() -> None:
    generator = NoteGenerator()
    candidate = {"name": "Maya Person", "role_bucket": "Product"}

    fallback = generator.generate(dict(candidate), "Acme")
    opportunity = generator.generate(
        dict(candidate),
        "Acme",
        note_context={"opportunity_titles": ["Business Operations Associate"]},
    )
    precomputed = generator.generate(
        dict(candidate),
        "Acme",
        note_context={
            "target_role_family": "bizops_strategy",
            "target_role_source": "opportunity_title",
            "target_role_matched_text": "Business Operations Associate",
            "target_role_matched_rule": "business operations",
            "target_role_is_concrete": True,
        },
    )

    assert fallback.target_role_source == "product_primary_default"
    assert fallback.target_role_is_concrete is False
    assert opportunity.target_role_source == "note_context.opportunity_title"
    assert opportunity.target_role_is_concrete is True
    assert precomputed.target_role_source == "opportunity_title"
    assert precomputed.target_role_matched_text == "Business Operations Associate"


def test_followup_artifact_and_review_csv_keep_role_context_end_to_end(tmp_path) -> None:
    organization = OrganizationRecord(
        organization_id="org-acme",
        name="Acme",
        notes="target_roles=Business Operations Associate",
    )
    contact = ContactRecord(
        contact_id="ct-maya",
        organization_id="org-acme",
        full_name="Maya Person",
        title="Staff Engineer",
        contact_type="Engineering",
        linkedin_url="https://linkedin.com/in/maya",
    )
    opportunity = OpportunityRecord(
        opportunity_id="opp-bizops",
        organization_id="org-acme",
        title="Business Operations Associate",
    )
    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-maya",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Hi Maya, open to connecting?",
            }
        ],
        organizations=[organization],
        contacts=[contact],
        opportunities=[opportunity],
        style_profile=CommunicationStyleProfile(),
    )

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["target_role_family"] == "bizops_strategy"
    assert draft["target_role_matched_text"] == "Business Operations Associate"
    assert "BizOps/Strategy" in str(draft["draft_message"])
    assert not re.search(r"\bpm\b|pm/product|product roles|product work", str(draft["draft_message"]), re.I)

    draft["communication_review"]["flags"] = ["Generic company insight"]
    rows = build_communication_review_csv_rows(
        payload={"results": drafts},
        review_artifact=tmp_path / "followups.json",
    )
    assert rows[0]["target_role_family"] == "bizops_strategy"
    assert "BizOps/Strategy" in rows[0]["suggested_message"]
    assert not re.search(r"\bpm\b|pm/product|product roles|product work", rows[0]["suggested_message"], re.I)


@pytest.mark.parametrize(
    ("title", "contact_type", "invite_note"),
    [
        ("Founder", "Founder", "Founder note"),
        ("Head of Product", "Product", "Product note"),
        ("University Recruiter", "Recruiting", "Recruiter note"),
        ("Staff Engineer", "Engineering", "Referral or pointer"),
        ("Staff Engineer", "Engineering", "Neutral invite"),
        ("Software Engineer", "Engineering", "Neutral invite"),
        ("Finance Partner", "Other", "General note"),
    ],
)
def test_accepted_followup_audience_branches_are_bizops_aware(
    title: str,
    contact_type: str,
    invite_note: str,
) -> None:
    organization = OrganizationRecord(organization_id="org-acme", name="Acme")
    contact = ContactRecord(
        contact_id="ct-maya",
        organization_id="org-acme",
        full_name="Maya Person",
        title=title,
        contact_type=contact_type,
    )
    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-maya",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "target_role_family": "bizops_strategy",
                "original_invite_note": invite_note,
            }
        ],
        organizations=[organization],
        contacts=[contact],
        style_profile=CommunicationStyleProfile(),
    )

    message = str(drafts[0]["draft_message"])
    assert not re.search(r"\bpm\b|pm/product|product roles|product work", message, re.I)
    assert "for a candidates" not in message.lower()
    assert (
        "BizOps/Strategy" in message
        or "BizOps / Strategy" in message
        or "business operations and strategy" in message
    )


def test_non_product_target_does_not_rewrite_recipient_product_fact() -> None:
    organization = OrganizationRecord(organization_id="org-acme", name="Acme")
    contact = ContactRecord(
        contact_id="ct-product",
        organization_id="org-acme",
        full_name="Maya Person",
        title="Director of Product, Developer Platform",
        contact_type="Product",
    )
    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-product",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "target_role_family": "bizops_strategy",
                "original_invite_note": "Product note",
            }
        ],
        organizations=[organization],
        contacts=[contact],
        style_profile=CommunicationStyleProfile(),
    )

    message = str(drafts[0]["draft_message"])
    assert "Your developer-facing product work" in message
    assert "relevant to business operations and strategy work" in message


def test_review_csv_keeps_seniority_guidance_role_neutral(tmp_path) -> None:
    flags = ["Seniority mismatch: tactical referral ask to senior/principal contact"]
    guidance = build_rewrite_guidance(
        flags=flags,
        channel="linkedin_followup",
        recipient_type="senior_product",
        recipient_title="Director of Product",
    )
    rows = build_communication_review_csv_rows(
        payload={
            "results": [
                {
                    "company": "Acme",
                    "name": "Maya Person",
                    "title": "Director of Product",
                    "target_role_family": "bizops_strategy",
                    "target_role_label": "BizOps / Strategy",
                    "draft_kind": "accepted_follow_up",
                    "draft_message": "BizOps draft",
                    "communication_review": {
                        "channel": "linkedin_followup",
                        "flags": flags,
                        "rewrite_guidance": guidance,
                    },
                }
            ]
        },
        review_artifact=tmp_path / "review.json",
    )

    assert "role-fit" in rows[0]["rewrite_guidance"]
    assert "product-fit" not in rows[0]["rewrite_guidance"]


@pytest.mark.parametrize(
    ("target_family", "draft_kind", "reply_intent", "title", "flags", "needle"),
    [
        (
            "bizops_strategy",
            "already_asked_wait",
            "already_asked_wait",
            "Engineer",
            [],
            "BizOps / Strategy fit",
        ),
        (
            "program_operations",
            "conversation_reply",
            "does_not_know",
            "Recruiter",
            [],
            "Program / Operations internship path",
        ),
        (
            "growth_gtm",
            "accepted_follow_up",
            "",
            "Principal Engineer",
            ["Seniority mismatch: tactical referral ask to senior/principal contact"],
            "Growth/GTM strategy",
        ),
        (
            "general_business",
            "accepted_follow_up",
            "",
            "Engineer",
            ["Generic fit framing"],
            "business-side role",
        ),
    ],
)
def test_review_suggestion_matrix_preserves_non_product_target(
    tmp_path,
    target_family: str,
    draft_kind: str,
    reply_intent: str,
    title: str,
    flags: list[str],
    needle: str,
) -> None:
    rows = build_communication_review_csv_rows(
        payload={
            "results": [
                {
                    "company": "Acme",
                    "name": "Maya Person",
                    "title": title,
                    "target_role_family": target_family,
                    "target_role_label": target_family,
                    "target_role_source": "opportunity_title",
                    "target_role_matched_text": "Concrete adjacent role",
                    "target_role_matched_rule": "test rule",
                    "target_role_is_concrete": True,
                    "draft_kind": draft_kind,
                    "reply_intent": reply_intent,
                    "draft_message": "Original draft",
                    "communication_review": {
                        "channel": "linkedin_followup",
                        "flags": flags,
                    },
                }
            ]
        },
        review_artifact=tmp_path / f"{target_family}.json",
    )

    row = rows[0]
    suggestion = row["suggested_message"]
    assert row["target_role_family"] == target_family
    assert row["target_role_source"] == "opportunity_title"
    assert row["target_role_is_concrete"] == "True"
    assert needle in suggestion
    assert not re.search(r"\bpm\b|pm/product|product roles|product work", suggestion, re.I)


def test_reply_draft_uses_role_context_from_invite_round_trip() -> None:
    organization = OrganizationRecord(organization_id="org-acme", name="Acme")
    contact = ContactRecord(
        contact_id="ct-maya",
        organization_id="org-acme",
        full_name="Maya Person",
        title="Recruiter",
        contact_type="Recruiting",
    )
    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-maya",
                "normalized_status": "replied",
                "target_role_family": "program_operations",
                "latest_message": "I do not know who owns that area.",
                "message_window": [],
            }
        ],
        organizations=[organization],
        contacts=[contact],
        style_profile=CommunicationStyleProfile(),
    )

    assert drafts[0]["target_role_family"] == "program_operations"
    assert "Program / Operations" in str(drafts[0]["draft_message"])
    assert "PM/product" not in str(drafts[0]["draft_message"])
    assert "a Program / Operations internship path" in str(drafts[0]["draft_message"])
    assert "internship paths" not in str(drafts[0]["draft_message"])


def test_role_aware_reply_does_not_repeat_an_already_acknowledged_ask() -> None:
    organization = OrganizationRecord(organization_id="org-acme", name="Acme")
    contact = ContactRecord(
        contact_id="ct-maya",
        organization_id="org-acme",
        full_name="Maya Person",
    )
    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-maya",
                "normalized_status": "replied",
                "target_role_family": "bizops_strategy",
                "latest_message": "Sure",
                "message_window": [
                    {
                        "sender": "You",
                        "message": "Would you point me to the right person for a BizOps/Strategy role?",
                    },
                    {"sender": "Maya Person", "message": "Sure"},
                ],
            }
        ],
        organizations=[organization],
        contacts=[contact],
        style_profile=CommunicationStyleProfile(),
    )

    assert drafts[0]["draft_kind"] == "already_asked_wait"
    assert drafts[0]["send_recommendation"] == "hold"


def test_role_aware_let_me_know_reply_does_not_add_another_routing_ask() -> None:
    organization = OrganizationRecord(organization_id="org-acme", name="Acme")
    contact = ContactRecord(
        contact_id="ct-maya",
        organization_id="org-acme",
        full_name="Maya Person",
    )
    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-maya",
                "normalized_status": "replied",
                "target_role_family": "bizops_strategy",
                "latest_message": "Let me know if a BizOps role opens.",
                "message_window": [],
            }
        ],
        organizations=[organization],
        contacts=[contact],
        style_profile=CommunicationStyleProfile(),
    )

    message = str(drafts[0]["draft_message"])
    assert "kept in mind" in message
    assert "Happy to send a short fit summary" in message
    assert "who I should talk to" not in message
    assert "someone in" not in message


@pytest.mark.parametrize(
    ("target_family", "latest_message", "expected_kind", "needle"),
    [
        (
            "product_strategy",
            "Please email your resume to maya@example.com.",
            "conversation_reply",
            "product strategy work",
        ),
        (
            "growth_gtm",
            "Please share your resume with HR.",
            "referral_offer_reply",
            "Growth/GTM strategy roles",
        ),
        (
            "program_operations",
            "We are a small team with high ownership and a tight feedback loop.",
            "conversation_reply",
            "Program / Operations internship path",
        ),
        (
            "general_business",
            "Thanks for the context.",
            "conversation_reply",
            "business-side roles",
        ),
        (
            "bizops_strategy",
            "I can't help with this, sorry.",
            "polite_close_reply",
            "Appreciate it",
        ),
    ],
)
def test_reply_template_matrix_keeps_target_role_and_avoids_pm_pivot(
    target_family: str,
    latest_message: str,
    expected_kind: str,
    needle: str,
) -> None:
    organization = OrganizationRecord(organization_id="org-acme", name="Acme")
    contact = ContactRecord(
        contact_id="ct-maya",
        organization_id="org-acme",
        full_name="Maya Person",
    )
    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-maya",
                "normalized_status": "replied",
                "target_role_family": target_family,
                "latest_message": latest_message,
                "message_window": [],
            }
        ],
        organizations=[organization],
        contacts=[contact],
        style_profile=CommunicationStyleProfile(),
    )

    draft = drafts[0]
    message = str(draft["draft_message"])
    assert draft["target_role_family"] == target_family
    assert draft["draft_kind"] == expected_kind
    assert needle in message
    assert not re.search(r"\bpm\b|pm/product|product roles|product work", message, re.I)


def test_track_2_daily_plan_to_email_preserves_bizops_target(tmp_path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-acme",
            name="Acme",
            organization_type=OrganizationType.COMPANY,
            notes="target_roles=Business Operations Associate | tags=data,marketplace",
        )
    )
    workbook.upsert_opportunity(
        OpportunityRecord(
            opportunity_id="opp-bizops",
            organization_id="org-acme",
            title="Business Operations Associate",
        )
    )
    workbook.upsert_opportunity(
        OpportunityRecord(
            opportunity_id="opp-product",
            organization_id="org-acme",
            title="Product Manager Intern",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-founder",
            organization_id="org-acme",
            full_name="Maya Founder",
            title="Founder",
            contact_type="Founder",
            email="maya@example.com",
        )
    )
    account = AccountRow(
        organization_id="org-acme",
        company="Acme",
        org_type="company",
        city="",
        website="",
        target_role="Business Operations Associate",
        campaign_action="send_initial_multichannel_outreach",
        campaign_channel="linkedin+email",
        daily_action_priority=90,
        account_score=80,
        fit_score=80,
        tier="A",
    )
    daily_plan = build_track_2_daily_plan(
        [account],
        budget=DailyPlanBudget(max_email_drafts=1),
    )

    assert daily_plan["selected"][0]["target_role"] == "Business Operations Associate"
    invite_context = build_company_note_context(
        workbook,
        "Acme",
        target_role_title=str(daily_plan["selected"][0]["target_role"]),
    )
    assert invite_context["target_role_family"] == "bizops_strategy"
    invite_note = NoteGenerator().generate(
        {
            "name": "Maya Person",
            "role_bucket": "Product",
            "usc": True,
        },
        "Acme",
        note_context=invite_context,
    )
    assert invite_note.target_role_family == "bizops_strategy"
    assert "BizOps" in invite_note.text or "business operations" in invite_note.text
    drafts = build_track_2_email_drafts(
        workspace=tmp_path,
        daily_plan=daily_plan,
        limit=1,
        style_profile=CommunicationStyleProfile(),
    )

    assert drafts[0]["target_role_family"] == "bizops_strategy"
    assert "BizOps / Strategy" in str(drafts[0]["subject"])
    assert "BizOps/Strategy" in str(drafts[0]["body"])
    assert not re.search(r"\bpm\b|pm/product|product roles|product work", str(drafts[0]["body"]), re.I)


@pytest.mark.parametrize(
    ("title", "contact_type", "cadence_action"),
    [
        ("Founder", "Founder", "email_initial"),
        ("Head of Product", "Product", "email_initial"),
        ("University Recruiter", "Recruiting", "email_initial"),
        ("Staff Engineer", "Engineering", "email_initial"),
        ("Finance Partner", "Other", "email_initial"),
        ("Founder", "Founder", "email_followup_1"),
        ("Founder", "Founder", "email_final_optional"),
    ],
)
def test_track_2_email_recipient_and_cadence_branches_are_bizops_aware(
    title: str,
    contact_type: str,
    cadence_action: str,
) -> None:
    organization = OrganizationRecord(organization_id="org-acme", name="Acme")
    contact = ContactRecord(
        contact_id="ct-maya",
        organization_id="org-acme",
        full_name="Maya Person",
        title=title,
        contact_type=contact_type,
        email="maya@example.com",
    )
    target = infer_target_role_context(opportunity_titles=["Business Operations Associate"])

    draft = draft_track_2_email(
        organization=organization,
        contact=contact,
        campaign_action="send_initial_multichannel_outreach",
        cadence_action=cadence_action,
        style_profile=CommunicationStyleProfile(),
        target_role=target,
    )

    combined = f"{draft['subject']}\n{draft['body']}"
    assert draft["target_role_family"] == "bizops_strategy"
    assert not re.search(r"\bpm\b|pm/product|product roles|product work", combined, re.I)
    assert "a a BizOps" not in combined
    assert "BizOps" in combined or "business operations and strategy" in combined


def test_track_2_program_ops_final_email_uses_natural_overlap_language() -> None:
    target = infer_target_role_context(opportunity_titles=["Technical Program Manager"])
    draft = draft_track_2_email(
        organization=OrganizationRecord(organization_id="org-acme", name="Acme"),
        contact=ContactRecord(
            contact_id="ct-maya",
            organization_id="org-acme",
            full_name="Maya Person",
            title="Founder",
            contact_type="Founder",
            email="maya@example.com",
        ),
        campaign_action="send_cold_email_followup",
        cadence_action="email_final_optional",
        style_profile=CommunicationStyleProfile(),
        target_role=target,
    )

    assert "technical background + Program / Operations fit" in str(draft["body"])
    assert "technical/Program" not in str(draft["body"])


def test_invite_target_family_round_trips_through_touchpoint_and_message_reconcile(tmp_path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    persist_invite_send_results(
        workbook=workbook,
        company="Acme",
        source_artifact_path=tmp_path / "source.json",
        processed_candidates=[
            {
                "name": "Maya Person",
                "title": "Staff Engineer",
                "role_bucket": "Engineering",
                "linkedin_url": "https://linkedin.com/in/maya",
                "note": "Hi Maya, I'm exploring BizOps/Strategy roles at Acme. Open to connecting?",
                "target_role_family": "bizops_strategy",
                "target_role_label": "BizOps / Strategy",
                "target_role_source": "opportunity_title",
                "target_role_matched_text": "Business Operations Associate",
                "target_role_matched_rule": "business operations",
                "target_role_is_concrete": True,
            }
        ],
        send_results=[
            SimpleNamespace(
                name="Maya Person",
                linkedin_url="https://linkedin.com/in/maya",
                status="sent",
                note="Hi Maya, I'm exploring BizOps/Strategy roles at Acme. Open to connecting?",
                detail="sent",
            )
        ],
        send_artifact_path=tmp_path / "sent.json",
    )

    contacts = workbook.list_contacts()
    touchpoints = workbook.list_touchpoints()
    queue = build_linkedin_reconcile_queue_items(
        organizations=workbook.list_organizations(),
        contacts=contacts,
        touchpoints=touchpoints,
        min_age_hours=0,
    )
    assert queue[0]["target_role_family"] == "bizops_strategy"
    assert queue[0]["target_role_source"] == "opportunity_title"
    assert queue[0]["target_role_matched_text"] == "Business Operations Associate"
    assert queue[0]["target_role_is_concrete"] == "True"

    results, _ = build_linkedin_message_reconcile_results(
        threads=[
            {
                "name": "Maya Person",
                "thread_url": "https://linkedin.com/messaging/thread/1",
                "latest_message": "Thanks for reaching out.",
                "last_sender": "Maya Person",
            }
        ],
        contacts=contacts,
        touchpoints=touchpoints,
        state={},
    )
    assert results[0]["target_role_family"] == "bizops_strategy"
    assert results[0]["target_role_source"] == "opportunity_title"
    drafts = build_linkedin_followup_drafts(
        reconcile_results=results,
        organizations=workbook.list_organizations(),
        contacts=contacts,
        style_profile=CommunicationStyleProfile(),
    )
    assert drafts[0]["target_role_source"] == "opportunity_title"
    assert drafts[0]["target_role_matched_text"] == "Business Operations Associate"
    assert drafts[0]["target_role_matched_rule"] == "business operations"
    assert drafts[0]["target_role_is_concrete"] is True

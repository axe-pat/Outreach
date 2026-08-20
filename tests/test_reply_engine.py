"""Regression tests built from the real 2026-08-07 backlog failures.

Each test names the contact whose draft went wrong, so a future change that
reintroduces the failure says which one it broke.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from outreach.reply_engine.extract import _coerce, read_thread, validate_ai_read
from outreach.reply_engine import (
    Message,
    Action,
    APPLY_NOW,
    Ask,
    Capability,
    CompanyFacts,
    CREATE_WEDGE,
    Decision,
    NamedPerson,
    PIPELINE_SIGNAL,
    ThreadInput,
    ThreadRead,
    ThreadState,
    decide,
    deterministic_read,
    evaluate_reopen_conditions,
    is_telemetry,
    load_proof_beats,
    order_messages,
    outbound_followup_touch_counts,
    inbound_probably_missing,
    persist_reopen_conditions,
    requisition_state,
    requisition_actionability,
    resolve_capability,
    resolve_state,
    review,
    run,
    select_ask,
    select_proof_beats,
)
from outreach.tracking import (
    ContactRecord,
    OpportunityRecord,
    OrganizationRecord,
    OutreachWorkbook,
    TouchpointRecord,
)
from outreach.cli import compact_message_window
from outreach.reply_engine.org_identity import (
    CONFIRMED_HERE,
    EXPECTED_REFERRAL_AFFILIATION,
    FLAG_CONFIRMED_NAME_COLLISION,
    FLAG_ORG_MEMBERSHIP_CONFLICT,
    NON_PERSON_NAME,
    UNKNOWN_MEMBERSHIP,
    WORKS_ELSEWHERE,
    audit_org_identities,
    classify_contact_membership,
    membership_conflict_disposition,
    needs_membership_verification,
    parse_current_experience_lines,
    render_identity_report,
    resolve_membership_from_profile,
    title_entity_conflict,
)
from outreach.reply_engine.unmatched import (
    NEEDS_CONTACT_ROW,
    NOISE,
    classify_unmatched_thread,
    render_unmatched_report,
)
from outreach.reply_engine.compose import build_prompt
from outreach.reply_engine.critic import company_ask_key
from scripts.run_reply_engine_all_lanes import (
    _has_prior_outbound_linkedin as followup_has_prior_outbound,
    _locked_names,
)
from scripts.recritic_reply_review import (
    SavedDraft,
    parse_review,
    render_reissued_review,
)
from scripts.reissue_followup_pack_20260819 import VERBATIM_DRAFTS

NOW = datetime(2026, 8, 7, tzinfo=UTC)


def contact(title: str = "", name: str = "Test Person", **kw) -> ContactRecord:
    return ContactRecord(
        contact_id="ct-1", organization_id="org-1", full_name=name, title=title, **kw
    )


def org(notes: str = "", name: str = "Acme", organization_type: str = "company") -> OrganizationRecord:
    return OrganizationRecord(
        organization_id="org-1", name=name, notes=notes, organization_type=organization_type
    )


def test_dry_run_preserves_decision_without_empty_message_hold():
    drafts = run(
        [
            ThreadInput(
                contact=contact("Founder"),
                organization=org(notes="team_size=8", organization_type="startup"),
                raw_window=[],
                opportunities=[],
            )
        ],
        client=None,
    )

    assert drafts[0].decision.action is Action.ASK
    assert drafts[0].message == ""
    assert drafts[0].critic_flags == []


def test_pulkit_greeting_is_not_a_referral_to_himself():
    """The AI read returned Pulkit as a referral off a bare "Hey, Akshat",
    firing rule 1 on 23 of 29 live threads and turning every one into a
    thank-you note for a referral that never happened."""

    read = _coerce(
        {"named_people": [{"name": "Pulkit", "why": "greeted me"}]},
        recipient="Pulkit Kumar",
    )
    assert read.named_people == []


def test_named_third_party_still_survives_the_self_filter():
    """Kunal really did route us to Jean Georges Perres - rule 1 must still fire."""

    read = _coerce(
        {"named_people": [{"name": "Jean Georges Perres", "role_hint": "product"}]},
        recipient="Kunal Keshav Singh Sahni",
    )
    assert [p.name for p in read.named_people] == ["Jean Georges Perres"]


def test_the_job_seeker_is_never_a_named_person():
    read = _coerce(
        {"named_people": [{"name": "Akshat Pathak"}]}, recipient="Someone Else"
    )
    assert read.named_people == []


def test_pranav_recruiting_lead_is_not_a_contact_row():
    """"happy to shoot your linkedin over to the recruiting lead" created a
    contact literally named "recruiting lead"."""

    read = _coerce(
        {"named_people": [{"name": "recruiting lead"}, {"name": "Manu Monga"}]},
        recipient="Pranav Shikarpur",
    )
    assert [p.name for p in read.named_people] == ["Manu Monga"]


def test_pulkit_bare_greeting_is_not_a_refusal():
    """"Hey, Akshat" came back as cannot_help, so rule 6 parked a live thread
    at an AI recruiting company with a goodbye."""

    read = validate_ai_read(
        ThreadRead(capability=Capability.CANNOT_HELP, source="ai"),
        [Message(sender="Pulkit", text="Hey, Akshat")],
    )
    assert read.capability is Capability.CAN_OPINE


def test_dhruvi_small_talk_is_not_a_refusal():
    read = validate_ai_read(
        ThreadRead(capability=Capability.CANNOT_HELP, source="ai"),
        [Message(sender="Dhruvi", text="btw my husband got his MBA at USC Marshall too")],
    )
    assert read.capability is Capability.CAN_OPINE


def test_colin_real_refusal_is_still_honoured():
    """Colin's referral policy is a floor, while his other help remains usable."""

    read = validate_ai_read(
        ThreadRead(capability=Capability.CANNOT_HELP, source="ai"),
        [
            Message(
                sender="Colin",
                text="I'm not able to submit referrals for people I haven't worked with directly.",
            )
        ],
    )
    assert read.capability is Capability.DECLINED_REFERRAL


def test_referral_refusal_phrasing_is_detected():
    messages = [
        Message(
            sender="Colin",
            text=(
                "I only refer people I've worked with directly, but I can share "
                "what the case study and SQL test are like."
            ),
        )
    ]
    assert deterministic_read(messages).capability is Capability.DECLINED_REFERRAL


def test_deterministic_refusal_overrides_optimistic_ai_read():
    read = validate_ai_read(
        ThreadRead(capability=Capability.CAN_REFER, source="ai"),
        [
            Message(
                sender="Colin",
                text="I'm not able to refer people I haven't worked with directly.",
            )
        ],
    )
    assert read.capability is Capability.DECLINED_REFERRAL


def test_declined_referral_gets_name_ask_not_refer():
    assert select_ask(
        Capability.DECLINED_REFERRAL,
        has_citable_req=True,
        req_actionability=APPLY_NOW,
    ) is Ask.NAME


def test_declined_referral_is_not_flagged_for_asking_a_name():
    result = review(
        message="Who should I talk to about fall product internships at Abridge?",
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(capability=Capability.DECLINED_REFERRAL),
        capability=Capability.DECLINED_REFERRAL,
    )
    assert result.passed


def test_name_ask_targets_the_goal_not_an_org_position():
    """Kirk Hanson should route against Akshat's goal, not map a guessed
    SentinelOne product slot."""

    bad = review(
        message="Who owns product for data protection or cloud security?",
        decision=Decision(action=Action.ASK, ask=Ask.NAME, word_budget=50),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
    )
    assert "name_ask_targets_org_chart_slot" in bad.flags

    _, prompt = build_prompt(
        messages=[],
        decision=Decision(action=Action.ASK, ask=Ask.NAME, word_budget=50),
        read=ThreadRead(),
        name="Kirk Hanson",
        title="Sr. Director",
        company="SentinelOne",
        facts=CompanyFacts(name="SentinelOne"),
        banned=[],
    )
    assert "fall product internship or co-op" in prompt
    assert "Let the insider map the org" in prompt


def test_name_ask_does_not_invent_a_focus_area_from_the_company_description():
    description = "Builds a Security Data Lake for cloud data protection."
    _, prompt = build_prompt(
        messages=[],
        decision=Decision(action=Action.ASK, ask=Ask.NAME, word_budget=50),
        read=ThreadRead(),
        name="Kirk Hanson",
        title="Sr. Director",
        company="SentinelOne",
        facts=CompanyFacts(name="SentinelOne", description=description),
        banned=[],
    )
    assert description not in prompt
    assert "withheld for NAME routing" in prompt


def test_accept_offer_cannot_bolt_on_a_second_ask():
    result = review(
        message=(
            "That would be great, thank you. Who owns product for the interview "
            "experience side?"
        ),
        decision=_decision(action=Action.ACCEPT_OFFER, ask=Ask.NONE),
        read=ThreadRead(
            offer_made="route_to_recruiter",
            offer_target="recruiting lead",
        ),
        capability=Capability.CAN_REFER,
    )
    assert not result.passed
    assert "asks_beyond_the_offer" in result.flags


def test_accept_offer_accepting_only_passes():
    result = review(
        message=(
            "That would be great, thank you. I'm looking specifically for a fall "
            "product internship or co-op while I'm mid-MBA, not a full-time role. "
            "Please feel free to send my profile to the recruiting lead."
        ),
        decision=_decision(action=Action.ACCEPT_OFFER, ask=Ask.NONE),
        read=ThreadRead(
            offer_made="route_to_recruiter",
            offer_target="recruiting lead",
        ),
        capability=Capability.CAN_REFER,
    )
    assert result.passed


def test_answer_to_interest_question_states_timing_not_biography():
    read = ThreadRead(
        question_asked_of_me="Are you still looking at Workday roles?",
        question_kind="interest_availability_intent",
    )
    good = review(
        message=(
            "Yes, I'm still looking at Workday roles, specifically a fall product "
            "internship or co-op. Do you know whether that cycle is still open?"
        ),
        decision=_decision(action=Action.ANSWER, ask=Ask.NONE),
        read=read,
        capability=Capability.CAN_REFER,
    )
    assert good.passed

    biography_dump = review(
        message=(
            "Yes. I'm a Marshall MBA with five years in backend and data platform "
            "engineering at Gojek and Hevo."
        ),
        decision=_decision(action=Action.ANSWER, ask=Ask.NONE),
        read=read,
        capability=Capability.CAN_REFER,
    )
    assert "biography_dump_in_interest_answer" in biography_dump.flags


def test_answer_to_background_question_still_hands_judgement_back():
    result = review(
        message=(
            "No direct defense-sector experience. The closest work is production "
            "data and platform systems. Would that background translate on your side, "
            "or is defense experience the hard gate?"
        ),
        decision=_decision(action=Action.ANSWER, ask=Ask.NONE),
        read=ThreadRead(
            question_asked_of_me="Do you have defense-sector experience?",
            question_kind="background_fit",
        ),
        capability=Capability.CAN_REFER,
    )
    assert result.passed


def test_hemang_own_offer_is_not_their_request():
    """We wrote "if I send a tight resume"; he said "Sure, let me know." The read
    called that a resume request, firing rule 3 and breaking our own promise."""

    read = validate_ai_read(
        ThreadRead(explicit_request="resume", source="ai"),
        [
            Message(sender="You", text="If I send a tight resume + 3-line blurb, would you point me to the right path?"),
            Message(sender="Hemang", text="Sure, let me know."),
        ],
    )
    assert read.explicit_request == "none"


def test_thirunaavukkarasu_real_resume_request_survives():
    read = validate_ai_read(
        ThreadRead(explicit_request="resume", source="ai"),
        [Message(sender="Thiru", text="Hi Akshat Pathak Can you please share me your resume")],
    )
    assert read.explicit_request == "resume"


def test_nagendra_real_call_request_survives():
    read = validate_ai_read(
        ThreadRead(explicit_request="call", source="ai"),
        [Message(sender="Nagendra", text="I would like to chat with you over phone call")],
    )
    assert read.explicit_request == "call"


# ---------------------------------------------------------------- Layer 5 guards


def test_austin_pitch_is_caught_even_when_not_transact():
    """A bad capability read routed the blast to RECIPROCATE, so the existing
    TRANSACT-gated check never ran and the pitch went out."""

    result = review(
        message=(
            "Congrats on the AI-v2 launch. Built data platforms for years. "
            "If you're hiring interns for fall, I'd love to explore it."
        ),
        decision=Decision(action=Action.RECIPROCATE, word_budget=70),
        read=ThreadRead(is_mass_blast=True),
        capability=Capability.CAN_CREATE,
    )
    assert not result.passed
    assert any("pitched_into_mass_blast" in flag for flag in result.flags)


def test_hemang_attachment_claim_violates_commitment():
    """The draft said "Attached - Marshall MBA", never the word resume, so the
    commitment check missed it."""

    result = review(
        message="Attached - Marshall MBA (STEM) plus five years building data systems.",
        decision=Decision(action=Action.SEND_ATTACHMENT, word_budget=45),
        read=ThreadRead(commitments_i_made=["only send you a fit if there's a real match"]),
        capability=Capability.CAN_REFER,
        has_attachment_task=True,
    )
    assert not result.passed
    assert any("violates_prior_commitment" in flag for flag in result.flags)


def test_souhail_two_separate_asks_is_caught():
    result = review(
        message=(
            "Can you point me to the careers page or the right person on the "
            "recruiting team? Also, who runs product for the vetting side?"
        ),
        decision=Decision(action=Action.ACCEPT_OFFER, ask=Ask.NAME, word_budget=70),
        read=ThreadRead(),
        capability=Capability.CAN_REFER,
    )
    assert not result.passed
    assert any("multiple_asks" in flag for flag in result.flags)


def test_colin_exponent_course_is_not_a_referral():
    """He recommended an interview-prep course, which came back as a named
    person and would have created a contact row for a product."""

    read = validate_ai_read(
        ThreadRead(named_people=[NamedPerson(name="Exponent")], source="ai"),
        [
            Message(
                sender="Colin",
                text=(
                    "I would also highly recommend Exponent! I took their course "
                    "on interviewing for PM roles, and it really helped."
                ),
            )
        ],
    )
    assert read.named_people == []


def test_kunal_referral_survives_the_routing_evidence_check():
    read = validate_ai_read(
        ThreadRead(named_people=[NamedPerson(name="Jean Georges Perres")], source="ai"),
        [Message(sender="Kunal", text="Please find Jean Georges Perres he handles product")],
    )
    assert [p.name for p in read.named_people] == ["Jean Georges Perres"]


def test_deepak_at_mention_referral_survives():
    read = validate_ai_read(
        ThreadRead(
            named_people=[
                NamedPerson(name="Shashank Masurkar"),
                NamedPerson(name="Manu Monga"),
            ],
            source="ai",
        ),
        [Message(sender="Deepak", text="Pleasr reach out to @Shashank Masurkar or @Manu Monga")],
    )
    assert len(read.named_people) == 2


def test_pranav_unfilled_placeholder_is_blocked():
    """The draft shipped "Here's my LinkedIn: [your LinkedIn URL]"."""

    result = review(
        message="That's helpful, thanks. Here's my LinkedIn: [your LinkedIn URL].",
        decision=Decision(action=Action.ACCEPT_OFFER, word_budget=55),
        read=ThreadRead(),
        capability=Capability.CAN_REFER,
    )
    assert not result.passed
    assert any("unfilled_placeholder" in flag for flag in result.flags)


def test_pranav_invented_linkedin_url_is_blocked():
    """Asked for a profile link the composer produced a handle it was never
    given, which would have sent a stranger a wrong URL."""

    result = review(
        message="That's really helpful, thanks. Here's my LinkedIn: linkedin.com/in/akshatpathak",
        decision=Decision(action=Action.ACCEPT_OFFER, word_budget=55),
        read=ThreadRead(),
        capability=Capability.CAN_REFER,
    )
    assert not result.passed
    assert any("unverified_url" in flag for flag in result.flags)


def test_citable_requisition_url_is_still_allowed():
    result = review(
        message=(
            "Saw the fall product internship req and would love a referral to it. "
            "I'm mid-MBA, so I'm looking for an internship or co-op rather than "
            "a full-time role."
        ),
        decision=Decision(
            action=Action.ASK,
            ask=Ask.REFER,
            word_budget=65,
            citable_req="Fall Product Intern",
            citable_req_url="https://example.com/jobs/fall-product-intern",
        ),
        read=ThreadRead(),
        capability=Capability.CAN_REFER,
    )
    assert result.passed


def test_referral_message_must_state_availability():
    """Dhruvi offered to refer against any fit; without the constraint she
    would spend effort searching full-time roles Akshat cannot take."""

    read = ThreadRead()
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=read,
        contact=contact("Product Director"),
        facts=CompanyFacts(name="Acme", team_size=500),
        opportunities=[
            OpportunityRecord(
                opportunity_id="opp-fall",
                organization_id="org-1",
                title="Fall Product Intern",
                source_url="https://example.com/jobs/fall-product-intern",
                status="open",
                discovered_at="2026-08-10T00:00:00+00:00",
            )
        ],
        pursuit_season="fall",
        now=NOW,
    )
    assert decision.ask is Ask.REFER
    assert "not a full-time role" in decision.availability_qualifier

    _, prompt = build_prompt(
        messages=[],
        decision=decision,
        read=read,
        name="Dhruvi Sonani",
        title="Product Director",
        company="Acme",
        facts=CompanyFacts(name="Acme"),
        banned=[],
    )
    assert "AVAILABILITY CONSTRAINT:" in prompt
    assert decision.availability_qualifier in prompt

    result = review(
        message="Happy to send context. A referral to that role would be wonderful.",
        decision=decision,
        read=read,
        capability=Capability.CAN_REFER,
    )
    assert "missing_availability_qualifier" in result.flags


def test_park_message_does_not_require_availability():
    result = review(
        message="No worries at all, and thanks for letting me know.",
        decision=Decision(action=Action.PARK, word_budget=25),
        read=ThreadRead(),
        capability=Capability.CANNOT_HELP,
    )
    assert result.passed


def test_chirag_promised_send_without_attach_task_is_blocked():
    result = review(
        message="Sending over that context now - resume + blurb. Happy to flag anything relevant.",
        decision=Decision(action=Action.RECIPROCATE, word_budget=45),
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
    )
    assert not result.passed
    assert any("claims_attachment_without_task" in flag for flag in result.flags)


def test_vincent_declined_without_asking_anything():
    """He wrote "not sure how I can help" - no question - but the read invented
    one, and rule 5 outranks rule 6, so he was sent an ask anyway."""

    read = validate_ai_read(
        ThreadRead(
            question_asked_of_me="does your background apply to Revolut?",
            capability=Capability.CANNOT_HELP,
            source="ai",
        ),
        [Message(sender="Vincent", text="Hey man not sure how I can help. Wishing you the best of luck")],
    )
    assert read.question_asked_of_me is None
    assert read.capability is Capability.CANNOT_HELP


def test_raymond_real_question_survives():
    read = validate_ai_read(
        ThreadRead(question_asked_of_me="Do you have experience in the defense sector?", source="ai"),
        [Message(sender="Raymond", text="Do you have experience in the defense sector?")],
    )
    assert read.question_asked_of_me is not None


def test_perkin_yang_second_exact_proof_sentence_is_allowed():
    """A second exact use is not enough evidence to hold a whole batch."""

    result = review(
        message="Thanks for the heads up. Who owns product for the platform side?",
        decision=Decision(action=Action.ASK, ask=Ask.NAME, word_budget=70),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        batch_sentence_counts=Counter({"thanks for the heads up.": 1}),
    )
    assert not any("repeated_in_batch" in flag for flag in result.flags)


def test_sean_wu_fourth_exact_proof_sentence_is_held():
    shared = "I caught a billing failure at Intuit affecting 1,500 businesses."
    result = review(
        message=f"{shared} Open to working through a problem together?",
        decision=Decision(action=Action.ASK, ask=Ask.CREATE, word_budget=70),
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        batch_sentence_counts=Counter({shared.casefold(): 3}),
    )
    assert any("repeated_in_batch" in flag for flag in result.flags)


def test_kirk_hanson_prescribed_name_ask_is_exempt_from_batch_repetition():
    ask = "Who should I talk to about fall product internships at SentinelOne?"
    result = review(
        message=ask,
        decision=Decision(action=Action.ASK, ask=Ask.NAME, word_budget=45),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        batch_sentence_counts=Counter({ask.casefold(): 59}),
    )
    assert not any("repeated_in_batch" in flag for flag in result.flags)


def test_cooper_shropshire_prescribed_intel_ask_is_exempt_from_batch_repetition():
    ask = "Have you seen product interns come through Micro1?"
    result = review(
        message=ask,
        decision=Decision(action=Action.ASK, ask=Ask.INTEL, word_budget=45),
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
        batch_sentence_counts=Counter({ask.casefold(): 52}),
    )
    assert not any("repeated_in_batch" in flag for flag in result.flags)


def test_johnson_su_and_julien_colombain_ramp_identical_ask_is_capped():
    ask = (
        "Have you seen new grads come through Ramp, or know when 2027 recruiting opens?"
    )
    decision = Decision(
        action=Action.ASK,
        ask=Ask.INTEL,
        word_budget=45,
        campaign_track="large_company",
    )
    counts = Counter({company_ask_key("Ramp", ask): 1})

    colleague = review(
        message=f"Hi Julien, reaching out about Ramp. {ask}",
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
        batch_company_ask_counts=counts,
        recipient_name="Julien Colombain",
        company="Ramp",
    )
    unrelated_company = review(
        message=f"Hi Sandeep, reaching out about Glean. {ask}",
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
        batch_company_ask_counts=counts,
        recipient_name="Sandeep",
        company="Glean",
    )

    assert any(flag.startswith("repeated_company_ask:") for flag in colleague.flags)
    assert not any(
        flag.startswith("repeated_company_ask:")
        for flag in unrelated_company.flags
    )


def test_intel_is_limited_to_one_question():
    """Harsha Singla got two questions under one question mark; the second
    clause still made the message expensive to answer."""

    result = review(
        message=(
            "Does HireVue run a product internship or co-op track, and do you "
            "know who'd own that?"
        ),
        decision=Decision(action=Action.ASK, ask=Ask.INTEL, word_budget=45),
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
        recipient_title="Senior SDET",
    )
    assert "multiple_asks:2" in result.flags


def test_harsha_singla_ic_gets_one_product_hiring_routing_question():
    """An IC can usually name the person responsible for product hiring."""

    result = review(
        message="Do you know who owns product internship recruiting at HireVue?",
        decision=Decision(action=Action.ASK, ask=Ask.INTEL, word_budget=45),
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
        recipient_title="Senior SDET",
    )
    assert "intel_asks_ic_about_org_structure" not in result.flags
    assert not any(flag.startswith("intel_focus_mismatch") for flag in result.flags)

    timing = review(
        message="Hi Harsha, when did product recruiting start at HireVue?",
        decision=Decision(action=Action.ASK, ask=Ask.INTEL, word_budget=45),
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
        recipient_title="Senior SDET",
        recipient_name="Harsha Singla",
        company="HireVue",
    )
    assert "intel_focus_mismatch:routing_to_timing" in timing.flags

    decision = Decision(action=Action.ASK, ask=Ask.INTEL, word_budget=45)
    _, prompt = build_prompt(
        messages=[],
        decision=decision,
        read=ThreadRead(),
        name="Harsha Singla",
        title="Senior SDET",
        company="HireVue",
        facts=CompanyFacts(name="HireVue"),
        banned=[],
    )
    assert "ask exactly ONE casual routing question" in prompt
    assert "who owns product hiring" in prompt
    assert "timing question" in prompt


# ---------------------------------------------------------------- Layer 0


def test_partial_capture_holds_instead_of_drafting():
    """A preview-only inbound line cannot stand in for a conversation."""

    drafts = run(
        [
            ThreadInput(
                contact=contact("Product Manager", name="Dhruvi Sonani"),
                organization=org(name="Invisible Technologies"),
                raw_window=[
                    {
                        "sender": "Dhruvi Sonani",
                        "message": "btw my husband got his MBA at USC Marshall too",
                        "source": "linkedin_preview",
                    }
                ],
                opportunities=[],
                capture_confidence="partial",
                captured_message_count=1,
            )
        ],
        client=None,
    )

    assert drafts[0].decision.action is Action.HOLD
    assert "capture partial" in drafts[0].decision.reason


def test_full_thread_capture_records_all_inbound_messages():
    """The reconcile window consumes every message opened from the thread."""

    captured = compact_message_window(
        thread={
            "message_window": [
                {
                    "sender": "Dhruvi Sonani",
                    "message": "Please feel free to pass along any positions that might be a good fit for you!",
                    "source": "linkedin_thread",
                },
                {
                    "sender": "Dhruvi Sonani",
                    "message": "would be happy to refer you",
                    "source": "linkedin_thread",
                },
                {
                    "sender": "Dhruvi Sonani",
                    "message": "btw my husband got his MBA at USC Marshall too",
                    "source": "linkedin_thread",
                },
            ]
        }
    )

    assert [item["message"] for item in captured] == [
        "Please feel free to pass along any positions that might be a good fit for you!",
        "would be happy to refer you",
        "btw my husband got his MBA at USC Marshall too",
    ]


def test_contact_titles_naming_a_different_entity_are_flagged():
    findings = audit_org_identities(
        [org(name="Ventura", notes="team_size=2 | location=San Francisco")],
        [
            contact(
                "Flutter Developer @ Ventura Securities Ltd",
                name="Harman Singh Jaggi",
            )
        ],
    )

    assert any(finding.kind == "title_names_different_entity" for finding in findings)


def test_contact_geography_conflicting_with_org_location_is_flagged():
    findings = audit_org_identities(
        [org(name="Ventura", notes="team_size=2 | location=San Francisco")],
        [
            contact("Backend Developer", name="Deepak Singh", notes="Mumbai, India"),
            contact("Flutter Developer", name="SHOBHIT", notes="India-based"),
        ],
    )

    assert any(
        finding.kind == "contact_geography_conflicts_with_org"
        for finding in findings
    )


def test_ex_employer_in_title_is_not_a_collision():
    assert (
        title_entity_conflict(
            "Clara",
            "Ex Data Scientist at Secure AI Labs | Product leader",
        )
        == ""
    )


def test_generic_token_without_at_marker_is_not_a_collision():
    assert title_entity_conflict("Mount", "Scale-focused automation") == ""
    assert title_entity_conflict("Mount", "Automation at Scale") == ""
    assert title_entity_conflict("Mount", "AI Product Manager @ Iron Mountain")


def test_report_ranks_by_findings_per_org():
    findings = audit_org_identities(
        [
            org(name="Mount"),
            OrganizationRecord(organization_id="org-clara", name="Clara"),
        ],
        [
            contact("AI Product Manager @ Iron Mountain", name="Sidharth"),
            ContactRecord(
                contact_id="ct-clara-1",
                organization_id="org-clara",
                full_name="Piyush",
                title="Senior Software Engineer @Intuit",
            ),
            ContactRecord(
                contact_id="ct-clara-2",
                organization_id="org-clara",
                full_name="Animesh",
                title="SDE IV at Hevo Data",
            ),
        ],
    )

    report = render_identity_report(findings)
    assert report.index("org-clara") < report.index("org-1")
    assert "`likely_collision`" in report
    assert "`low_signal`" in report


def test_sidharth_confirmed_name_collision_stays_flagged_for_reassignment():
    """Sidharth Menon is filed under Mount, but his title explicitly says
    Iron Mountain; this is contact-level evidence, not an org judgement."""

    membership = classify_contact_membership(
        contact(
            "AI Product Manager @ Iron Mountain | AI, Tech and design",
            name="Sidharth Menon",
        ),
        org(name="Mount"),
    )
    assert membership.classification == WORKS_ELSEWHERE
    assert membership.named_employer == "Iron Mountain"
    assert membership.proposed_action == "evaluate_outreach_relationship_context"

    action, reason = membership_conflict_disposition(
        membership,
        contact("AI Product Manager @ Iron Mountain", name="Sidharth Menon"),
        [],
        confirmed_name_collision=True,
    )
    assert action == FLAG_CONFIRMED_NAME_COLLISION
    assert "review reassignment" in reason


def test_ex_employer_mention_does_not_reassign():
    """Saurabh Dubey's Secure AI Labs mention is explicitly historical."""

    membership = classify_contact_membership(
        contact(
            "Senior ML Engineer || Ex Data Scientist at Secure AI Labs || Product leader",
            name="Saurabh Dubey",
        ),
        org(name="Invisible Technologies"),
    )
    assert membership.classification == UNKNOWN_MEMBERSHIP
    assert membership.named_employer == ""


def test_education_mention_is_not_an_employer():
    """Gaurav Khatwani's MSCS @ USC is education, not current employment."""

    membership = classify_contact_membership(
        contact("MSCS @ USC | Ex-Microsoft, Skyworks", name="Gaurav Khatwani"),
        org(name="Clara"),
    )
    assert membership.classification == UNKNOWN_MEMBERSHIP
    assert membership.named_employer == ""


def test_non_person_name_is_flagged():
    membership = classify_contact_membership(
        contact("Founder", name="Building Clara."),
        org(name="Clara"),
    )
    assert membership.classification == NON_PERSON_NAME
    assert "verb_initial_phrase" in membership.garbage_reasons
    assert membership.proposed_action == "review_delete_garbage_row"


def test_title_naming_this_employer_is_confirmed_without_profile_pull():
    membership = classify_contact_membership(
        contact("Software Engineer at Anthropic", name="Otilia Mutricy"),
        org(name="Anthropic"),
    )
    assert membership.classification == CONFIRMED_HERE


def test_salman_explicit_landmark_employer_beats_bare_micro1_token():
    membership = classify_contact_membership(
        contact(
            "SDE 2 @Landmark Group | Micro1 | Scale.ai | ex - Paytm",
            name="Salman Khalid",
        ),
        org(name="Micro1"),
    )
    assert membership.classification == WORKS_ELSEWHERE
    assert membership.named_employer == "Landmark Group"
    assert membership.proposed_action == "evaluate_outreach_relationship_context"


def test_allison_conflicting_affiliation_never_becomes_a_reassignment():
    membership = classify_contact_membership(
        contact(
            "Incoming Product Intern at American Express | USC Marshall",
            name="Allison Romero",
        ),
        org(name="A different workbook company"),
    )
    assert membership.classification == WORKS_ELSEWHERE
    assert membership.named_employer == "American Express"
    assert "reassign" not in membership.proposed_action
    assert membership.proposed_action == "evaluate_outreach_relationship_context"


def test_allison_romero_creator_program_is_not_an_insider_binding():
    membership = classify_contact_membership(
        contact(
            "CS @ FIU | 3x SWE Intern @ Amex | Autodesk Student Ambassador",
            name="Allison Romero",
            notes="bound_affiliation_type=creator_program",
        ),
        org(name="Jobright.ai"),
    )

    assert membership.classification == WORKS_ELSEWHERE
    assert membership.routing_employers == ("Amex",)
    assert membership.named_employer == "Amex"
    assert membership.bound_affiliation_type == "creator_program"

    draft = run(
        [
            ThreadInput(
                contact=contact(
                    "CS @ FIU | 3x SWE Intern @ Amex",
                    name="Allison Romero",
                    notes="bound_affiliation_type=creator_program",
                ),
                organization=org(name="Jobright.ai"),
                raw_window=[
                    {
                        "sender": "You",
                        "message": "I'm exploring roles at Jobright.ai.",
                        "source": "original_invite",
                    }
                ],
                opportunities=[],
                relationship_context="accepted_silent",
            )
        ],
        client=None,
    )[0]
    assert draft.decision.action is Action.HOLD
    assert "creator_program affiliation without routing value" in draft.decision.reason


def test_rishi_goomar_advisory_role_is_not_employee_routing():
    membership = classify_contact_membership(
        contact(
            "Senior Software Engineer at Mercury | Advisor at Qpoint",
            name="Rishi Goomar",
        ),
        org(name="Zapier"),
    )

    assert membership.classification == WORKS_ELSEWHERE
    assert membership.routing_employers == ("Mercury",)
    assert membership.non_routing_affiliations == ("Qpoint (advisory)",)


def test_savannah_yang_bare_keck_affiliation_has_no_routing_value():
    membership = classify_contact_membership(
        contact(
            "AI PM @TikTok | Keck Medicine of USC | Tech Consulting | Prev @Deloitte",
            name="Savannah Yang",
        ),
        org(name="Keck Medicine of USC"),
    )

    assert membership.classification == WORKS_ELSEWHERE
    assert membership.routing_employers == ("TikTok",)
    assert membership.bound_affiliation_type == "untyped_non_employment_affiliation"


def test_rajashekar_school_affiliation_is_not_a_reassignment_destination():
    membership = classify_contact_membership(
        contact(
            "AI Engineer at Fetch.AI | DS & AI @ UC Berkeley",
            name="Rajashekar V",
        ),
        org(name="Clara"),
    )
    assert membership.classification == WORKS_ELSEWHERE
    assert membership.named_employer == "Fetch.AI"
    assert membership.proposed_action == "evaluate_outreach_relationship_context"


def test_tim_drahn_other_employer_is_expected_for_a_referral_path():
    tim = contact(
        "Senior Principal Software Engineer at Optum",
        name="Tim Drahn",
        target_lists="referrals;linkedin;track-2",
    )
    membership = classify_contact_membership(tim, org(name="Clara"))
    action, reason = membership_conflict_disposition(membership, tim, [])
    assert action == EXPECTED_REFERRAL_AFFILIATION
    assert "referral path" in reason


def test_allison_invited_affiliation_conflict_is_flagged_not_hidden():
    allison = contact(
        "Incoming Product Intern at American Express",
        name="Allison Romero",
    )
    membership = classify_contact_membership(
        allison,
        org(name="A different workbook company"),
    )
    invite = TouchpointRecord(
        touchpoint_id="tp-allison-invite",
        organization_id="org-1",
        contact_id="ct-1",
        status="Sent",
        message_kind="linkedin_invite",
        message_text="Invite note",
        sent_at="2026-08-01T00:00:00+00:00",
    )
    action, reason = membership_conflict_disposition(
        membership,
        allison,
        [invite],
    )
    assert action == FLAG_ORG_MEMBERSHIP_CONFLICT
    assert "sent LinkedIn invite" in reason


def test_cuauhtli_profile_read_can_override_an_unknown_founder_title():
    """Cuauhtli Padilla's bare Founder title cannot reveal that he left Clara;
    a current-experience read can resolve that fact without guessing."""

    workbook_only = classify_contact_membership(
        contact("Founder", name="Cuauhtli Padilla"),
        org(name="Clara"),
    )
    resolved = resolve_membership_from_profile(
        workbook_only,
        current_employer="A different current company",
        current_title="Founder",
    )
    assert workbook_only.classification == UNKNOWN_MEMBERSHIP
    assert resolved.classification == WORKS_ELSEWHERE
    assert resolved.source == "linkedin_profile"


def test_cuauhtli_profile_read_is_deferred_while_suppressed():
    """Cuauhtli is unknown, but a suppressed contact does not need a read."""

    membership = classify_contact_membership(
        contact(
            "Founder",
            name="Cuauhtli Padilla",
            linkedin_url="https://www.linkedin.com/in/cuauhtli-padilla/",
        ),
        org(name="Clara"),
    )
    assert membership.proposed_action == "defer_profile_read_until_live"
    assert not needs_membership_verification(membership, is_live=False)
    assert needs_membership_verification(
        membership,
        is_live=False,
        is_diagnostic_sample=True,
    )


def test_wenjing_employer_is_verified_when_her_contact_becomes_live():
    membership = classify_contact_membership(
        contact(
            "Product Data Scientist | AI & LLM Evaluation | Product Analytics",
            name="Wenjing Huang",
            linkedin_url="https://www.linkedin.com/in/jocelynnnwj/",
        ),
        org(name="Mercor"),
    )
    assert needs_membership_verification(membership, is_live=True)


def test_dzmitry_experience_page_extracts_current_employer():
    employer, title, evidence = parse_current_experience_lines(
        [
            "Experience",
            "Member of Technical Staff",
            "Stealth Startup · Full-time",
            "Feb 2025 - Present · 1 yr 7 mos",
            "Greater Seattle Area",
            "Staff Software Engineer",
            "Retool · Full-time",
            "Oct 2022 - Nov 2024 · 2 yrs 2 mos",
        ]
    )
    assert employer == "Stealth Startup"
    assert title == "Member of Technical Staff"
    assert evidence[-1].startswith("Feb 2025 - Present")


def test_ryan_grouped_experience_uses_company_anchor_not_role_date():
    from scripts.resolve_org_membership import parse_current_experience

    employer, title, _ = parse_current_experience(
        {
            "items": [
                {
                    "company": "",
                    "lines": [
                        "Founding Robotics Engineer",
                        "Nov 2025 - Present · 10 mos",
                    ],
                },
                {
                    "company": "yondu",
                    "lines": [
                        "yondu",
                        "Full-time · 1 yr 2 mos",
                        "Founding Robotics Engineer",
                        "Nov 2025 - Present · 10 mos",
                    ],
                },
            ]
        }
    )
    assert employer == "yondu"
    assert title == "Founding Robotics Engineer"


def test_unmatched_threads_are_reported_not_dropped():
    suresh = classify_unmatched_thread(
        {
            "thread_id": "thread-suresh",
            "name": "Suresh Mergu",
            "latest_message": (
                "I know there is an internship program that Optum hires for every year."
            ),
            "last_sender": "Suresh Mergu",
        }
    )

    report = render_unmatched_report([suresh])
    assert suresh.classification == NEEDS_CONTACT_ROW
    assert suresh.company == "Optum"
    assert "Suresh Mergu" in report
    assert "internship program" in report


@pytest.mark.parametrize(
    "thread",
    [
        {
            "name": "Johns Hopkins University in collaboration with Great Learning",
            "latest_message": "Sponsored: Earn a certificate in Agentic AI",
        },
        {
            "name": "Unknown Recruiter",
            "latest_message": "Sponsored InMail: exclusive role for you",
        },
    ],
)
def test_sponsored_and_inmail_threads_classify_as_noise(thread):
    assert classify_unmatched_thread(thread).classification == NOISE


def test_telemetry_is_not_a_message():
    """71 of 185 drafts treated invite worker output as the recipient's reply."""

    assert is_telemetry(
        "invite_result=send_unknown_reserved | detail=Invite worker returned "
        "ambiguous status 'preflight_failed'"
    )
    assert not is_telemetry("Hi Akshat, great to connect with you")


def test_telemetry_stripped_from_window():
    messages, _ = order_messages(
        [
            {"sender": "You", "message": "invite_result=sent | detail=Invitation sent successfully."},
            {"sender": "Kiran", "message": "Happy to connect!", "timestamp_text": "Jul 30"},
        ]
    )
    assert [m.text for m in messages] == ["Happy to connect!"]


def test_austin_buhl_window_is_reordered():
    """Austin's window ran 11:52 AM -> [undated invite] -> Jul 12 -> Jul 23."""

    messages, confident = order_messages(
        [
            {"sender": "Austin", "message": "nice to meet you", "timestamp_text": "Jul 5"},
            {"sender": "You", "message": "Hi Austin", "source": "original_invite"},
            {"sender": "Austin", "message": "We just launched Orbit", "timestamp_text": "Jul 12"},
            {"sender": "Austin", "message": "Salestrics AI-v2 is live", "timestamp_text": "Jul 23"},
        ],
        invite_sent_at=datetime(2026, 7, 8),
    )
    assert confident
    assert [m.text for m in messages] == [
        "nice to meet you",
        "Hi Austin",
        "We just launched Orbit",
        "Salestrics AI-v2 is live",
    ]


def test_sandeep_undated_invite_reports_low_confidence():
    """Sandeep's thread produced a hallucinated referral because the engine
    guessed at an order it could not know."""

    _, confident = order_messages(
        [
            {"sender": "Sandeep", "message": "https://linkedin.com/in/ajit-bhave", "timestamp_text": "Jul 9"},
            {"sender": "You", "message": "Hi Sandeep", "source": "original_invite"},
        ]
    )
    assert confident is False


def test_unreliable_order_is_held_not_drafted():
    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(),
        contact=contact("Healthcare Product Leader"),
        facts=CompanyFacts(name="ServiceNow"),
        order_confident=False,
    )
    assert decision.action is Action.HOLD
    assert not decision.emits_message


def test_chirag_ambiguous_identity_holds_before_the_ai_read():
    class ModelMustNotRun:
        class Messages:
            def create(self, **kwargs):  # pragma: no cover - failure path only
                raise AssertionError("identity holds must not call the model")

        messages = Messages()

    drafts = run(
        [
            ThreadInput(
                contact=contact("Software Engineer", name="Chirag Jain"),
                organization=org(name="Unknown until profile URL is captured"),
                raw_window=[
                    {
                        "sender": "Chirag",
                        "message": "Happy to help",
                        "timestamp_text": "2026-08-15T12:00:00+00:00",
                    }
                ],
                opportunities=[],
                hold_reason="ambiguous_contact_match",
            )
        ],
        client=ModelMustNotRun(),
    )

    assert drafts[0].decision.action is Action.HOLD
    assert drafts[0].decision.reason == "ambiguous_contact_match"


# ---------------------------------------------------------------- Layer 1


def test_no_context_state_when_only_our_invite_exists():
    messages, _ = order_messages([{"sender": "You", "message": "Hi there", "timestamp_text": "Jul 1"}])
    assert resolve_state(messages) is ThreadState.NO_CONTEXT


def test_offchannel_conversations_are_not_redrafted():
    """Nagendra moved to a phone call; the engine must not draft again."""

    messages, _ = order_messages(
        [{"sender": "Nagendra", "message": "Perfect", "timestamp_text": "Jul 2"}]
    )
    state = resolve_state(messages, contact_notes="spoke by phone, moved to email")
    assert state is ThreadState.CLOSED_OFFCHANNEL
    decision = decide(
        state=state, read=ThreadRead(), contact=contact(), facts=CompanyFacts(name="Actian")
    )
    assert decision.action is Action.SUPPRESS


# ---------------------------------------------------------------- Layer 2


def test_kunal_referral_is_extracted():
    messages, _ = order_messages(
        [
            {"sender": "You", "message": "Hi Kunal", "timestamp_text": "Aug 1"},
            {
                "sender": "Kunal",
                "message": "Please find Jean Georges Perres he handles product",
                "timestamp_text": "Aug 3",
            },
        ]
    )
    read = deterministic_read(messages)
    assert any("Jean Georges" in p.name for p in read.named_people)


def test_vincent_cannot_help_is_detected():
    messages, _ = order_messages(
        [
            {
                "sender": "Vincent",
                "message": "Hey man not sure how I can help. Wishing you the best of luck",
                "timestamp_text": "Jul 4",
            }
        ]
    )
    assert deterministic_read(messages).capability is Capability.CANNOT_HELP


def test_shobhit_left_company_is_detected():
    messages, _ = order_messages(
        [
            {
                "sender": "SHOBHIT",
                "message": "Hi Akshat, I have left Ventura. You can connect with Jay Parab.",
                "timestamp_text": "Jul 4",
            }
        ]
    )
    read = deterministic_read(messages)
    assert read.capability is Capability.NO_LONGER_THERE


def test_how_are_you_is_not_a_question_needing_an_answer():
    messages, _ = order_messages(
        [{"sender": "Amritansh", "message": "Hey I'm good! How are you?", "timestamp_text": "Jul 4"}]
    )
    assert deterministic_read(messages).question_asked_of_me is None


# -------------------------------------------------------- authority + ladder


@pytest.mark.parametrize(
    "title,notes,expected",
    [
        ("CEO, Co-Founder at Acme (W26)", "team_size=2", Capability.CAN_CREATE),
        ("Founder", "team_size=16", Capability.CAN_CREATE),
        ("Co-founder at Acme", "team_size=221", Capability.CAN_REFER),
        ("Sr. Director, Strategic Solutions Engineering", "", Capability.CAN_REFER),
        ("Software Engineer", "", Capability.CAN_NAME),
        ("SWE Intern @ HeyGen", "", Capability.CAN_OPINE),
        ("GDD @ RIT | Game Developer", "", Capability.CAN_OPINE),
        ("CS @ FIU | 3x SWE Intern @ Amex", "", Capability.CAN_OPINE),
    ],
)
def test_authority_resolution(title, notes, expected):
    from outreach.reply_engine import company_facts

    assert resolve_capability(contact(title), company_facts(org(notes))) is expected


def test_naomi_carrigan_unrelated_founder_identity_cannot_create_at_deepgram():
    facts = CompanyFacts(name="Deepgram", team_size=150)
    naomi = contact(
        "cultivating online communities for Deepgram and freeCodeCamp - founder "
        "of NHCarrigan - senior software engineer, community manager, developer educator"
    )
    assert resolve_capability(naomi, facts, state=ThreadState.NO_CONTEXT) is Capability.CAN_REFER


def test_ryan_liu_founding_engineer_can_refer_but_cannot_create():
    ryan = contact("Founding Engineer at Jobright", name="Ryan Liu")
    facts = CompanyFacts(name="Jobright", team_size=100)
    assert resolve_capability(
        ryan,
        facts,
        state=ThreadState.NO_CONTEXT,
    ) is Capability.CAN_REFER
    role = OpportunityRecord(
        opportunity_id="op-jobright-fall-pm",
        organization_id="org-1",
        title="Fall 2026 Product Manager Intern",
        opportunity_type="internship",
        discovered_at="2026-08-04T00:00:00+00:00",
    )
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=ryan,
        facts=facts,
        opportunities=[role],
        now=NOW,
    )
    assert decision.ask is Ask.REFER


def test_kelly_mcdonald_target_company_lead_uses_current_headline_authority():
    kelly = contact(
        "Staff Product Lead @Abridge | Former Product Lead @Rippling | "
        "Ex-CPO @Cipio.ai, CEO/Co-Founder @Kyndoo",
        name="Kelly McDonald",
        contact_type="Founder",
    )
    assert resolve_capability(
        kelly,
        CompanyFacts(name="Abridge", team_size=200),
        state=ThreadState.NO_CONTEXT,
    ) is Capability.CAN_CREATE


@pytest.mark.parametrize(
    "name,title,company,team_size,expected",
    [
        ("Akash Mahtani", "Founding Electronics Engineer at Anoria", "ANORIA", 5, Capability.CAN_NAME),
        ("JJ Zhao", "Founding Engineer, Idler | CS, Math @ UPenn", "Idler", 6, Capability.CAN_OPINE),
        ("Max Zou", "Founding Engineer @ LemonLime | Statistics&ML @ CMU", "LemonLime", 5, Capability.CAN_OPINE),
        ("Perkin Yang", "Founder of Still Human Podcast | Interns at Synphony", "Synphony", 10, Capability.CAN_OPINE),
        ("Zachary Ta", "Founding Engineer @ Voker.ai", "Voker", 6, Capability.CAN_NAME),
    ],
    ids=lambda value: str(value).replace(" ", "-").casefold(),
)
def test_create_block_false_authority_is_not_promoted(
    name, title, company, team_size, expected
):
    assert resolve_capability(
        contact(title, name=name),
        CompanyFacts(name=company, team_size=team_size),
        state=ThreadState.NO_CONTEXT,
    ) is expected


def test_junior_contacts_get_intel_not_name():
    """Makaela and Shashwat were asked deep product questions that went
    nowhere. People without authority get hiring intel asks instead."""

    assert select_ask(Capability.CAN_OPINE, has_citable_req=False) is Ask.INTEL


def test_founder_at_small_company_gets_create_ask():
    assert select_ask(Capability.CAN_CREATE, has_citable_req=False) is Ask.CREATE


def test_name_is_the_fallback_not_the_default():
    """The previous engine used NAME 103 times out of 185."""

    assert select_ask(Capability.CAN_REFER, has_citable_req=True) is Ask.REFER
    assert select_ask(Capability.CAN_REFER, has_citable_req=False) is Ask.NAME


# ------------------------------------------------------------- freshness


def test_summer_requisition_is_stale_in_fall():
    """The Raymond Chan draft linked '2026 Summer Intern - Strategy &
    Operations (R5065)' in August."""

    opportunity = OpportunityRecord(
        opportunity_id="op-1",
        organization_id="org-1",
        title="2026 Summer Intern - Strategy & Operations (R5065)",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    assert requisition_state(opportunity, pursuit_season="fall", now=NOW) == "stale"


def test_fall_requisition_is_citable():
    opportunity = OpportunityRecord(
        opportunity_id="op-2",
        organization_id="org-1",
        title="Network Strategy Intern (Fall 2026)",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    assert requisition_state(opportunity, pursuit_season="fall", now=NOW) == "citable"


def test_unmarked_requisition_needs_verification():
    opportunity = OpportunityRecord(
        opportunity_id="op-3",
        organization_id="org-1",
        title="Product Management Intern",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    assert requisition_state(opportunity, pursuit_season="fall", now=NOW) == "needs_verification"


def test_fulltime_req_at_small_company_is_create_wedge():
    opportunity = OpportunityRecord(
        opportunity_id="op-small-pm",
        organization_id="org-1",
        title="Product Manager, Data Platform",
        opportunity_type="full_time",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    assert requisition_actionability(
        opportunity,
        CompanyFacts(name="SmallCo", team_size=18),
        now=NOW,
    ) == CREATE_WEDGE


def test_fulltime_req_at_large_company_is_pipeline_signal():
    opportunity = OpportunityRecord(
        opportunity_id="op-large-pm",
        organization_id="org-1",
        title="Product Manager, Data Platform",
        opportunity_type="full_time",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    assert requisition_actionability(
        opportunity,
        CompanyFacts(name="LargeCo", team_size=10_000),
        now=NOW,
    ) == PIPELINE_SIGNAL


def test_fall_internship_req_is_apply_now():
    opportunity = OpportunityRecord(
        opportunity_id="op-fall-pm",
        organization_id="org-1",
        title="Fall 2026 Product Management Co-op",
        opportunity_type="internship",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    assert requisition_actionability(
        opportunity,
        CompanyFacts(name="Acme", team_size=2_000),
        pursuit_season="fall",
        now=NOW,
    ) == APPLY_NOW


def test_intern_economics_never_appears_at_large_company():
    _, prompt = build_prompt(
        messages=[],
        decision=Decision(
            action=Action.ASK,
            ask=Ask.INTEL,
            citable_req="Product Manager, Data Platform",
            req_actionability=PIPELINE_SIGNAL,
        ),
        read=ThreadRead(),
        name="Large Company Contact",
        title="Director of Product",
        company="LargeCo",
        facts=CompanyFacts(name="LargeCo", team_size=10_000),
        banned=[],
    )
    forbidden = ("cheaper", "lower-cost", "fewer-hours", "convert later")
    assert not any(phrase in prompt.casefold() for phrase in forbidden)
    assert "next graduate cycle" in prompt.casefold()

    blocked = review(
        message="A lower-cost internship could use fewer hours and convert later.",
        decision=Decision(
            action=Action.ASK,
            ask=Ask.INTEL,
            req_actionability=PIPELINE_SIGNAL,
        ),
        read=ThreadRead(intern_economics_objection=True),
        capability=Capability.CAN_REFER,
    )
    assert "intern_economics_without_small_company_objection" in blocked.flags


# ------------------------------------------------------- company facts


def test_story_fit_text_is_rejected_as_a_company_fact():
    """Entrust's description= field is analysis about Akshat, not a fact
    about Entrust. Feeding it to the writer produces nonsense."""

    from outreach.reply_engine import company_facts

    facts = company_facts(
        org(
            "description=Clear APM role with product ownership where Akshat's "
            "backend and systems architecture experience applies"
        )
    )
    assert facts.has_usable_description is False


def test_real_description_is_kept():
    from outreach.reply_engine import company_facts

    facts = company_facts(
        org("team_size=2 | description=Alt-X is building the Cursor for financial modeling in Excel.")
    )
    assert "Cursor for financial modeling" in facts.description
    assert facts.is_small


# ---------------------------------------------------------------- Layer 3


def test_rule_1_named_people_creates_contacts_and_beats_everything():
    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(named_people=[NamedPerson(name="Jean Georges Perres")]),
        contact=contact("SWE Intern @Actian"),
        facts=CompanyFacts(name="Actian"),
    )
    assert decision.action is Action.CREATE_CONTACTS
    assert decision.contacts_to_create[0].name == "Jean Georges Perres"


def test_rule_5_question_beats_asking():
    """Raymond Chan asked about defense experience and was never answered."""

    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(question_asked_of_me="Do you have experience in the defense sector?"),
        contact=contact("Staff Technical Product Manager @ Shield AI"),
        facts=CompanyFacts(name="Shield AI"),
    )
    assert decision.action is Action.ANSWER
    assert decision.ask is Ask.NONE


def test_rule_6_cannot_help_parks_with_reopen_condition():
    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(capability=Capability.CANNOT_HELP),
        contact=contact("iOS Software Engineer at Revolut"),
        facts=CompanyFacts(name="Revolut"),
    )
    assert decision.action is Action.PARK
    assert decision.reopen_condition


def test_manogna_cannot_help_plus_own_need_reciprocates():
    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(
            capability=Capability.CANNOT_HELP, their_need="open to work"
        ),
        contact=contact("Software Engineer #OpenToWork"),
        facts=CompanyFacts(name="Snorkel AI"),
    )
    assert decision.action is Action.RECIPROCATE


def test_rule_7_mass_blast_from_founder_transacts():
    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(is_mass_blast=True, capability=Capability.CAN_CREATE),
        contact=contact("Founder & CEO @ Salestrics"),
        facts=CompanyFacts(name="Salestrics", team_size=8),
    )
    assert decision.action is Action.TRANSACT
    assert decision.human_tasks


def test_cooper_silent_junior_gets_intel():
    """Cooper Shropshire, a game-dev undergraduate, was asked 'who owns
    product there'."""

    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("GDD @ RIT | Game Developer"),
        facts=CompanyFacts(name="Micro1"),
    )
    assert decision.action is Action.ASK
    assert decision.ask is Ask.INTEL


def test_cooper_accepted_invite_does_not_call_layer_2_without_an_inbound():
    class ExplodingMessages:
        def create(self, **_kwargs):
            raise AssertionError("Layer 2 must not read an outbound-only thread")

    class ExplodingClient:
        messages = ExplodingMessages()

    read = read_thread(
        [Message(sender="You", text="Would love to connect about Micro1.")],
        client=ExplodingClient(),
        contact_title="GDD @ RIT | Game Developer",
        company="Micro1",
        contact_name="Cooper Shropshire",
    )

    assert read.source == "deterministic"
    assert read.capability is Capability.CAN_OPINE


def test_silent_ic_with_no_authority_gets_intel_not_name():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Software Engineer", name="Jake Fleischer"),
        facts=CompanyFacts(name="Mercor", team_size=400),
    )

    assert decision.action is Action.ASK
    assert decision.ask is Ask.INTEL


def test_silent_senior_contact_still_gets_name():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Director of Engineering", name="Kirk Hanson"),
        facts=CompanyFacts(name="SentinelOne", team_size=2500),
    )

    assert decision.action is Action.ASK
    assert decision.ask is Ask.NAME


def test_replied_ic_can_still_get_name():
    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(),
        contact=contact("Software Engineer", name="Chirag Jain"),
        facts=CompanyFacts(name="d-Matrix", team_size=300),
    )

    assert decision.action is Action.ASK
    assert decision.ask is Ask.NAME


def test_silent_ic_at_tiny_company_can_still_get_name():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Software Engineer", name="Achim Munene"),
        facts=CompanyFacts(name="Primer", team_size=2, is_startup=True),
    )

    assert decision.action is Action.ASK
    assert decision.ask is Ask.NAME


def test_vitid_nakareseisoon_large_company_ic_stays_intel():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Software Engineer", name="Vitid Nakareseisoon"),
        facts=CompanyFacts(name="Adobe", team_size=1000),
        band="parked_large",
    )
    assert decision.action is Action.ASK
    assert decision.ask is Ask.INTEL


def test_wissem_gamra_pipeline_signal_does_not_promote_ic_to_name():
    pipeline_signal = OpportunityRecord(
        opportunity_id="op-airtable-pm",
        organization_id="org-1",
        title="Product Manager, Platform",
        opportunity_type="full_time",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Software Engineer", name="Wissem Gamra"),
        facts=CompanyFacts(name="Airtable", team_size=1000),
        opportunities=[pipeline_signal],
        invite_text="Exploring product roles at Airtable.",
        now=NOW,
    )
    assert decision.req_actionability == PIPELINE_SIGNAL
    assert decision.ask is Ask.INTEL
    _, prompt = build_prompt(
        messages=[],
        decision=decision,
        read=ThreadRead(),
        name="Wissem Gamra",
        title="Software Engineer",
        company="Airtable",
        facts=CompanyFacts(name="Airtable", team_size=1000),
        banned=[],
    )
    assert "who owns product hiring" in prompt
    assert "Request a name, not an introduction" in prompt


def test_tim_drahn_warm_referral_prompt_does_not_invent_an_accepted_invite():
    """Tim works at Optum but is mapped to Clara as a warm referral path."""

    _, prompt = build_prompt(
        messages=[],
        decision=_decision(ask=Ask.NAME, budget=50),
        read=ThreadRead(),
        name="Tim Drahn",
        title="Senior Principal Software Engineer at Optum",
        company="Clara",
        facts=CompanyFacts(name="Clara"),
        banned=[],
        relationship_context="warm_uninvited_referral",
    )

    assert "(no prior LinkedIn message)" in prompt
    assert "TARGET COMPANY FOR REFERRAL PATH: Clara" in prompt
    assert "invite was accepted" not in prompt
    assert "Tim Drahn - Senior Principal Software Engineer at Optum at Clara" not in prompt


def test_tim_drahn_warm_referral_false_acceptance_premise_fails_critic():
    result = review(
        message="Thanks for connecting. Who would be the right person to ask at Clara?",
        decision=_decision(ask=Ask.NAME, budget=50),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        relationship_context="warm_uninvited_referral",
    )

    assert not result.passed
    assert "warm_contact_false_acceptance_premise" in result.flags


def test_harsha_named_opening_without_citable_req_converts_to_name_ask():
    """The opening she meant was a full-time posting both sides can see."""

    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(named_opening="we currently have an opening for Product role"),
        contact=contact("Senior SDET"),
        facts=CompanyFacts(name="HireVue"),
        opportunities=[],
    )
    assert decision.action is Action.RESOLVE_REQ
    assert decision.ask is Ask.NAME


# ---------------------------------------------------------------- Layer 5


def _decision(action=Action.ASK, ask=Ask.NAME, budget=60, **kw) -> Decision:
    return Decision(action=action, ask=ask, word_budget=budget, **kw)


def test_critic_flags_the_103_use_sentence():
    result = review(
        message=(
            "Thanks for connecting. Who owns product there? I'd rather get in "
            "front of them with something concrete than wait for a posting."
        ),
        decision=_decision(),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
    )
    assert not result.passed
    assert any("legacy_template_phrase" in f for f in result.flags)


def test_critic_flags_unanswered_question():
    result = review(
        message="Doing well, thanks for asking. I went through Shield AI openings.",
        decision=_decision(action=Action.ANSWER, ask=Ask.NONE),
        read=ThreadRead(question_asked_of_me="Do you have experience in the defense sector?"),
        capability=Capability.CAN_REFER,
    )
    assert not result.passed
    assert "did_not_answer_their_question" in result.flags


def test_critic_accepts_a_real_answer():
    result = review(
        message=(
            "Straight answer: no defense sector experience. Closest is five years "
            "on data and platform systems. Does that background count for anything "
            "there, or is domain the hard gate?"
        ),
        decision=_decision(action=Action.ANSWER, ask=Ask.NONE),
        read=ThreadRead(question_asked_of_me="Do you have experience in the defense sector?"),
        capability=Capability.CAN_REFER,
    )
    assert result.passed, result.flags


def test_critic_flags_preachy_hedging():
    """'I'd rather not pretend that's the same thing' reads self-congratulatory."""

    result = review(
        message=(
            "No defense experience. Five years on data systems instead. I'd rather "
            "not pretend that's the same thing at all here."
        ),
        decision=_decision(action=Action.ANSWER, ask=Ask.NONE),
        read=ThreadRead(question_asked_of_me="Do you have defense sector experience?"),
        capability=Capability.CAN_REFER,
    )
    assert any("preachy" in f for f in result.flags)


def test_critic_flags_long_raw_url():
    url = "https://app.joinhandshake.com/job-search/11195929?" + "x=1&" * 40
    result = review(
        message=f"Here's the link: {url} Would you refer me?",
        decision=_decision(),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
    )
    assert any("raw_url_too_long" in f for f in result.flags)


def test_critic_flags_fake_attachment():
    result = review(
        message="Attaching my resume now. Five years on data platforms before the MBA.",
        decision=_decision(action=Action.ASK),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
    )
    assert "claims_attachment_without_task" in result.flags


def test_critic_allows_attachment_with_task():
    result = review(
        message="Sending it over now. USC Marshall MBA, five years on data platforms before that.",
        decision=_decision(action=Action.SEND_ATTACHMENT, ask=Ask.NONE, budget=45),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        has_attachment_task=True,
    )
    assert result.passed, result.flags


def test_critic_flags_asking_someone_who_cannot_help():
    """Pratik said no twice and was asked a third time."""

    result = review(
        message="Understood. Would you point me to anyone on the product side there?",
        decision=_decision(action=Action.PARK, ask=Ask.NONE, budget=25),
        read=ThreadRead(capability=Capability.CANNOT_HELP),
        capability=Capability.CANNOT_HELP,
    )
    assert "asks_help_from_cannot_help" in result.flags


def test_critic_flags_correcting_the_recipient():
    """Hiten wrote 'Akshay'. Correcting him reads petty."""

    result = review(
        message="No problem at all Hiten, appreciate you being straight with me. (Akshat, by the way.)",
        decision=_decision(action=Action.PARK, ask=Ask.NONE, budget=30),
        read=ThreadRead(factual_errors_about_me=["called me Akshay"]),
        capability=Capability.CANNOT_HELP,
    )
    assert "corrects_recipient" in result.flags


def test_critic_flags_pitching_into_a_mass_blast():
    result = review(
        message=(
            "Just upvoted Orbit. Also I'm looking for a fall internship and would "
            "love to talk about a role at Salestrics."
        ),
        decision=_decision(action=Action.TRANSACT, ask=Ask.NONE, budget=50),
        read=ThreadRead(is_mass_blast=True),
        capability=Capability.CAN_CREATE,
    )
    assert "pitched_into_mass_blast" in result.flags


def test_critic_flags_batch_repetition():
    """The previous rubric scored one draft at a time and structurally could
    not see that 103 messages shared a sentence."""

    shared = "i'm looking for a fall product internship at your company."
    counts = Counter({shared: 4})
    result = review(
        message="Thanks for connecting. I'm looking for a fall product internship at your company.",
        decision=_decision(),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        batch_sentence_counts=counts,
    )
    assert any("repeated_in_batch" in f for f in result.flags)


def test_critic_flags_over_budget():
    result = review(
        message=" ".join(["word"] * 80),
        decision=_decision(budget=40),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
    )
    assert any("over_budget" in f for f in result.flags)


def test_marginal_overage_is_trimmed_not_held():
    """Chirag's 46-word draft must not be held against a 45-word target."""

    result = review(
        message=" ".join(["word"] * 46),
        decision=_decision(budget=45),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
    )
    assert result.passed


def test_proof_beats_load_from_resume():
    beats = load_proof_beats(Path("workspace/proof_beats.yml"))
    assert any("50,000+ billing accounts" in beat.text for beat in beats)
    assert any("120K+ pipelines" in beat.text for beat in beats)

    selected = select_proof_beats(
        beats,
        recipient_context="fintech billing and payments product",
        limit=3,
    )
    assert 2 <= len(selected) <= 3
    assert all("billing_fintech" in beat.domains for beat in selected)


def test_unsourced_employer_claim_is_flagged():
    beats = load_proof_beats(Path("workspace/proof_beats.yml"))
    result = review(
        message="At Intuit, I built tax filing software for small businesses.",
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        proof_beats=beats,
    )
    assert any(flag.startswith("unsourced_self_claim") for flag in result.flags)


def test_claim_matching_a_proof_beat_passes():
    beats = load_proof_beats(Path("workspace/proof_beats.yml"))
    result = review(
        message=(
            "At Intuit, I worked on billing reconciliation that restored accurate "
            "billing for 80K+ businesses."
        ),
        decision=_decision(action=Action.ANSWER, ask=Ask.NONE),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        proof_beats=beats,
    )
    assert result.passed


def test_andrew_pekin_unrelated_optum_proof_is_held():
    beats = load_proof_beats(Path("workspace/proof_beats.yml"))
    result = review(
        message=(
            "Connecting 1,300 apps means inheriting their failure modes. At Optum, "
            "I designed an ML affordability product that moved from hackathon to clinical pilot."
        ),
        decision=_decision(action=Action.ASK, ask=Ask.CREATE),
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        proof_beats=beats,
    )
    assert "proof_domain_mismatch:optum-affordability-ml" in result.flags


def test_max_zou_third_optum_proof_in_create_block_is_held():
    beats = load_proof_beats(Path("workspace/proof_beats.yml"))
    result = review(
        message=(
            "Custom AI agents are hard to trust. At Optum, I designed an ML-based "
            "affordability product that advanced from hackathon win to clinical pilot."
        ),
        decision=_decision(action=Action.ASK, ask=Ask.CREATE),
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        proof_beats=beats,
        proof_beat_counts=Counter({(Ask.CREATE, "optum-affordability-ml"): 2}),
    )
    assert "proof_beat_reuse:optum-affordability-ml" in result.flags


def test_zachary_ta_one_intuit_claim_maps_to_one_proof_beat():
    from outreach.reply_engine.proof import used_proof_beats

    beats = load_proof_beats(Path("workspace/proof_beats.yml"))
    used = used_proof_beats(
        "At Intuit, I caught a billing failure affecting 1,500+ businesses and resolved it in hours.",
        beats,
    )
    assert [beat.beat_id for beat in used] == ["intuit-billing-incident"]


# ------------------------------------------------------- follow-up copy spec


def test_kelly_mcdonald_em_dash_clause_is_normalized_before_review():
    result = review(
        message="Hi Kelly, thanks for connecting—I'm following up about Abridge.",
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Kelly McDonald",
        company="Abridge",
    )
    assert result.normalized_message == (
        "Hi Kelly, thanks for connecting. I'm following up about Abridge."
    )
    assert not any(flag.startswith("em_dash") for flag in result.flags)


def test_chris_thomas_em_dash_before_conjunction_becomes_a_comma():
    result = review(
        message=(
            "Thanks for connecting. I'm exploring a fall product internship or co-op "
            "at Entrust and would find it helpful to know who I should talk to—and "
            "whether an intern cycle exists."
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Chris Thomas",
        company="Entrust",
    )
    assert "talk to, and whether" in result.normalized_message
    assert "—" not in result.normalized_message


def test_tommy_joyner_em_dash_sentence_capitalizes_the_following_word():
    result = review(
        message=(
            "Thanks for connecting. I'm exploring a fall BizOps internship at "
            "Amperesand—who on the team should I talk to?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Tommy Joyner",
        company="Amperesand",
    )
    assert "Amperesand. Who on the team" in result.normalized_message
    assert "—" not in result.normalized_message


def test_savar_chaturvedi_short_sentence_created_by_em_dash_is_held():
    result = review(
        message=(
            "Thanks for connecting. I'm curious—when you came through Retool's "
            "hiring process, do you remember when recruiting kicked off?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.INTEL),
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
        recipient_name="Savar Chaturvedi",
        company="Retool",
    )
    assert "I'm curious. When you came through" in result.normalized_message
    assert "em_dash_fragment:I'm curious." in result.flags


def test_angela_lee_meta_text_and_third_person_are_blocked():
    result = review(
        message=(
            "Since you're connected with Angela, here's the message:\n---\n"
            "Who should I talk to at Adobe?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Angela Lee",
        company="Adobe",
    )
    assert "meta_text" in result.flags
    assert "meta_recipient_third_person" in result.flags


def test_ryan_samadi_company_judgement_is_not_graded():
    result = review(
        message=(
            "Hi Ryan, the Excel agent angle is sharp. I'm following up about Alt-X."
        ),
        decision=_decision(action=Action.ASK, ask=Ask.CREATE),
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        recipient_name="Ryan Samadi",
        company="Alt-X",
    )
    assert "evaluative_predicate" in result.flags


def test_stephen_lin_cold_question_needs_an_opening_beat():
    result = review(
        message="Who should I talk to about product roles at Advanced Metal Research?",
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Stephen Lin",
        company="Advanced Metal Research",
    )
    assert "missing_opening_beat" in result.flags


def test_ryan_samadi_observation_led_opening_counts_as_a_beat():
    result = review(
        message=(
            "Alt-X is automating an Excel workflow analysts still handle manually. "
            "Would you consider taking on a product intern this fall?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.CREATE),
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        recipient_name="Ryan Samadi",
        company="Alt-X",
    )
    assert "missing_opening_beat" not in result.flags


def test_kelly_mcdonald_bare_here_cannot_hide_the_company():
    result = review(
        message="Hi Kelly, thanks for connecting. Who should I talk to about roles here?",
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Kelly McDonald",
        company="Abridge",
    )
    assert "company_not_named" in result.flags
    assert "bare_company_referent" in result.flags


def test_wissem_gamra_intel_cannot_carry_a_resume_pitch():
    result = review(
        message=(
            "Hi Wissem, thanks for connecting. Five years in data systems at Hevo "
            "and Intuit. Have you seen interns at Airtable?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.INTEL),
        read=ThreadRead(),
        capability=Capability.CAN_OPINE,
        recipient_name="Wissem Gamra",
        company="Airtable",
    )
    assert "proof_not_allowed_for_intel" in result.flags


def test_piyush_jhanwar_required_intuit_name_is_not_mistaken_for_proof():
    result = review(
        message=(
            "Hi Piyush, I'm looking at full-time product roles at Intuit. "
            "Do you know who I should talk to?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Piyush Jhanwar",
        company="Intuit",
    )
    assert "proof_not_allowed_for_name" not in result.flags


def test_angela_lee_spent_personal_hook_cannot_be_reused():
    invite = "Hi Angela, great seeing a fellow Trojan at Adobe. Fight On!"
    result = review(
        message=(
            "Hi Angela, thanks for connecting, fellow Trojan. I'm following up "
            "about Adobe. Fight On."
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Angela Lee",
        company="Adobe",
        invite_text=invite,
    )
    assert any(flag.startswith("invite_overlap:") for flag in result.flags)


def test_angela_lee_batch_pipeline_checks_her_invite_not_the_next_contacts():
    angela_message = (
        "Hi Angela, fellow Trojan, Fight On! I'm looking at full-time product "
        "roles at Adobe after my MBA. Have you seen new grads join product at Adobe?"
    )
    andrew_message = (
        "Hi Andrew, thanks for connecting. I'm reaching out about Bellagent. "
        "Could I send a short product teardown for a problem worth solving this fall?"
    )

    class Messages:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            # Angela is retried after invite-overlap, then Andrew is composed.
            message = angela_message if self.calls < 2 else andrew_message
            self.calls += 1
            block = type("Block", (), {"text": message})()
            return type("Response", (), {"content": [block]})()

    class Client:
        messages = Messages()

    drafts = run(
        [
            ThreadInput(
                contact=contact("Product Manager @ Adobe", name="Angela Lee"),
                organization=org(name="Adobe", notes="team_size=30000"),
                raw_window=[
                    {
                        "sender": "You",
                        "message": "Hi Angela, fellow Trojan at Adobe. Fight On!",
                        "source": "original_invite",
                    }
                ],
                opportunities=[],
            ),
            ThreadInput(
                contact=contact("Founder @ Bellagent", name="Andrew Pekin"),
                organization=org(
                    name="Bellagent",
                    notes="team_size=8 | batch=W26",
                    organization_type="startup",
                ),
                raw_window=[
                    {
                        "sender": "You",
                        "message": "Hi Andrew, interested in Bellagent.",
                        "source": "original_invite",
                    }
                ],
                opportunities=[],
            ),
        ],
        client=Client(),
    )

    assert any(
        flag.startswith("invite_overlap:") for flag in drafts[0].critic_flags
    )


def test_andrew_pekin_filler_closer_is_blocked():
    result = review(
        message=(
            "Hi Andrew, following up about Bellagent. Would love to talk through "
            "how I could help. Low lift."
        ),
        decision=_decision(action=Action.ASK, ask=Ask.CREATE),
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        recipient_name="Andrew Pekin",
        company="Bellagent",
    )
    assert "filler_closer" in result.flags


def test_jairo_camacho_name_ask_cannot_escalate_to_an_intro():
    result = review(
        message=(
            "Hi Jairo, thanks for connecting. I'm following up about 1Password. "
            "Would you be open to a quick intro to the hiring manager?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Jairo Camacho",
        company="1Password",
    )
    assert "ask_exceeds_decision:name_to_forward" in result.flags


def test_jairo_camacho_vocative_is_not_third_person_meta_text():
    result = review(
        message=(
            "Jairo—thanks for connecting. I'm looking at a role at 1Password. "
            "Do you know who handles hiring?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Jairo Camacho",
        company="1Password",
    )
    assert "meta_recipient_third_person" not in result.flags


def test_andrew_pekin_voice_allows_only_one_exclamation():
    result = review(
        message="Hi Andrew! Thanks for connecting! Following up about Bellagent.",
        decision=_decision(action=Action.ASK, ask=Ask.CREATE),
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        recipient_name="Andrew Pekin",
        company="Bellagent",
    )
    assert "too_many_exclamations" in result.flags


@pytest.mark.parametrize(
    "name,title,company",
    [
        ("Stephen Lin", "Software Engineer at NASA | Machine Learning Engineer", "Advanced Metal Research"),
        ("Samuel Pullman", "Talent @ Armadin | Executive Search", "Alchemy"),
        ("Praneel Khiantani", "CS @ Harvard | SWE @ Janet AI", "Anthropic"),
    ],
    ids=["stephen-lin", "samuel-pullman", "praneel-khiantani"],
)
def test_live_org_binding_mismatches_are_held(name, title, company):
    draft = run(
        [
            ThreadInput(
                contact=contact(title, name=name),
                organization=org(name=company),
                raw_window=[
                    {
                        "sender": "You",
                        "message": f"I'm exploring roles at {company}.",
                        "source": "original_invite",
                    }
                ],
                opportunities=[],
                relationship_context="accepted_silent",
            )
        ],
        client=None,
    )[0]
    assert draft.decision.action is Action.HOLD
    assert "org binding unverified" in draft.decision.reason


@pytest.mark.parametrize(
    "name,title,company",
    [
        (
            "Tommy Joyner",
            "Sr. VP of Engineering at Amperesand - Focused on Grid Transformation",
            "Amperesand",
        ),
        ("Jake Winterborne", "Founding team @ Anam", "Anam AI"),
        ("Paymon Mogharabi", "Senior Product Manager at Cisco Systems", "Cisco"),
        (
            "Nikhil Vijay",
            "Mechatronics Engineering @ General Autonomy, Robotics Researcher @ RAM Lab",
            "Gen Auto AI",
        ),
        ("Shirley Kabir", "Technical Staff @ Hebbia.ai", "Hebbia"),
        ("Emily Lee", "Analyst @ Keck | Business Analytics @ USC Marshall", "Keck Medicine of USC"),
        (
            "Pierre-Alexandre Kamienny",
            "Solving insurance distribution with AI @Kinro. Ex-DeepMind",
            "Kinro",
        ),
        ("Ruben Gimenez Linares", "Building Software @ Opto", "Opto Investments"),
        ("Mark Huber", "Software Engineer at Outset", "Outset AI"),
    ],
    ids=[
        "tommy-joyner",
        "jake-winterborne",
        "paymon-mogharabi",
        "nikhil-vijay",
        "shirley-kabir",
        "emily-lee",
        "pierre-alexandre-kamienny",
        "ruben-gimenez-linares",
        "mark-huber",
    ],
)
def test_live_same_company_variants_do_not_trigger_org_hold(name, title, company):
    membership = classify_contact_membership(
        contact(title, name=name),
        org(name=company),
    )
    assert membership.classification != WORKS_ELSEWHERE


def test_jacqueline_short_second_at_clause_resolves_keck_membership():
    membership = classify_contact_membership(
        contact(
            "MBA candidate at USC Marshall and People Analytics specialist at "
            "Keck Medicine of USC, using data to drive workforce strategy",
            name="Jacqueline Short",
        ),
        org(name="Keck Medicine of USC"),
    )
    assert membership.classification == CONFIRMED_HERE


def test_savannah_yang_explicit_tiktok_employer_beats_bare_keck_token():
    membership = classify_contact_membership(
        contact(
            "AI PM @TikTok | Keck Medicine of USC | Tech Consulting | Prev @Deloitte",
            name="Savannah Yang",
        ),
        org(name="Keck Medicine of USC"),
    )
    assert membership.classification == WORKS_ELSEWHERE


@pytest.mark.parametrize(
    "name,title,company",
    [
        ("Vishesh Mehta", "MSCS @ CSU, Long Beach | Software Engineer", "Centerfield"),
        ("Ankit Garg", "CS Grad @ NCSU", "NVIDIA"),
        ("Sonia Behal", "Gold Medalist @ LPU", "Scratch Financial"),
        ("Vihar Ramesh Jain", "Computer Engineering @ ASU", "Trucker Path"),
    ],
    ids=["vishesh-mehta", "ankit-garg", "sonia-behal", "vihar-ramesh-jain"],
)
def test_live_education_acronym_is_not_an_employer_mismatch(name, title, company):
    membership = classify_contact_membership(
        contact(title, name=name),
        org(name=company),
    )
    assert membership.classification != WORKS_ELSEWHERE


def test_naman_kothari_apology_thread_is_human_only():
    draft = run(
        [
            ThreadInput(
                contact=contact("Software Engineer at Amplitude", name="Naman Kothari"),
                organization=org(name="Amplitude"),
                raw_window=[
                    {
                        "sender": "You",
                        "message": "I'd love to connect about Amplitude.",
                        "source": "original_invite",
                    },
                    {
                        "sender": "You",
                        "message": "Sorry Naman, a few draft follow-ups got pasted here by mistake.",
                    },
                ],
                opportunities=[],
            )
        ],
        client=None,
    )[0]
    assert draft.decision.action is Action.HOLD
    assert "apology or correction" in draft.decision.reason


def test_vamshi_ramarapu_svp_function_before_org_gets_name_not_intel():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("SVP Engineering @ Actian", name="Vamshi Ramarapu"),
        facts=CompanyFacts(name="Actian"),
        invite_text="Exploring product roles at Actian.",
        now=NOW,
    )
    assert decision.ask is Ask.NAME


def test_anirudh_sriram_cto_authority_wins_over_academic_headline_tokens():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact(
            "CTO @ Tessera Labs | PhD in Computer Science",
            name="Anirudh Sriram",
        ),
        facts=CompanyFacts(
            name="Tessera Labs",
            team_size=20,
            is_startup=True,
        ),
        invite_text="Exploring product roles at Tessera Labs.",
        now=NOW,
    )
    assert decision.ask is Ask.CREATE


def test_chris_gomes_muffat_executive_chairman_binds_to_target_company():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact(
            "CEO @ OnePanel, Executive Chairman @ Zenyt.ai",
            name="Chris Gomes Muffat",
        ),
        facts=CompanyFacts(
            name="Zenyt.ai",
            team_size=30,
            is_startup=True,
        ),
        invite_text="Exploring product roles at Zenyt.ai.",
        now=NOW,
    )
    assert decision.ask is Ask.CREATE


def test_christopher_smith_lead_compact_at_binding_gets_name_not_intel():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Senior AI Lead @Turing", name="Christopher Smith"),
        facts=CompanyFacts(name="Turing", team_size=1000),
        invite_text="Exploring product roles at Turing.",
        now=NOW,
    )
    assert decision.ask is Ask.NAME


def test_tim_drahn_no_prior_outbound_never_enters_followup_pack():
    no_prior_message = {
        "name": "Tim Drahn",
        "segment": "warm_uninvited",
        "message_window": [],
        "original_invite_note": "",
    }
    with_prior_message = {
        **no_prior_message,
        "message_window": [
            {"sender": "You", "message": "Hi Tim, reaching out about Optum."}
        ],
    }

    assert followup_has_prior_outbound(no_prior_message) is False
    assert followup_has_prior_outbound(with_prior_message) is True


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "ryan samadi",
            "Hi Ryan, last note from me on this. More directly: would you be open to bringing on a part-time product intern at Alt-X this fall? Either way, wishing you the best with Alt-X!",
        ),
        (
            "andrew pekin",
            "Hi Andrew, last note from me on this. Simple ask rather than another pitch: would you be open to bringing on a part-time product intern at Bellagent this fall? Either way, best of luck with Bellagent!",
        ),
        (
            "daichi hiraoka",
            "Hi Daichi, last note from me on this. Would you consider taking on a fall product intern at Korso? Happy to start on whatever's most annoying right now. Either way, best of luck!",
        ),
        (
            "sean wu",
            "Hi Sean, thanks for connecting. Synphony's bed-level analytics and pipeline integration is close to what I spent five years on at Hevo, diagnosing reliability across 120K+ pipelines. Would love to talk about a fall product internship there if you're open to it!",
        ),
        (
            "ryan liu",
            "Hi Ryan, thanks for connecting. I built an AI agent that runs my whole job search: sourcing, ranking, outreach, follow-ups. Which is to say I've built a worse version of Jobright for an audience of one. I saw the Product Manager Intern role and it's exactly what I'm after this fall. Would you be open to referring me?",
        ),
        (
            "kelly mcdonald",
            "Hi Kelly, thanks for connecting. I'm exploring a fall product internship or co-op at Abridge and would find it really helpful to know who I should be talking to about that. I'll stop bugging you after this one, promise!",
        ),
    ],
    ids=[
        "ryan_samadi",
        "andrew_pekin",
        "daichi_hiraoka",
        "sean_wu",
        "ryan_liu",
        "kelly_mcdonald",
    ],
)
def test_operator_verbatim_followup_replacements(name: str, expected: str):
    assert VERBATIM_DRAFTS[name][1] == expected


def test_henry_kwan_manual_send_is_logged_and_resolves_you_replied_last():
    workbook = OutreachWorkbook(Path(__file__).resolve().parents[1] / "workspace")
    messages = [
        touchpoint
        for touchpoint in workbook.list_touchpoints()
        if touchpoint.contact_id == "ct-org-icarus-https-linkedin-com-in-kwan-henry"
        and touchpoint.message_kind == "linkedin_manual_message"
    ]
    assert len(messages) == 1
    assert messages[0].status == "Sent"
    assert messages[0].source_artifact == "artifacts/20260814-approved-sends.md"

    assert outbound_followup_touch_counts(messages) == {
        "ct-org-icarus-https-linkedin-com-in-kwan-henry": 1
    }
    assert "henry kwan" in _locked_names(
        Path(__file__).resolve().parents[1]
        / "artifacts"
        / "20260814-approved-sends.md"
    )


def test_raajan_ashish_pal_and_all_flairx_contacts_are_permanently_suppressed():
    workbook = OutreachWorkbook(Path(__file__).resolve().parents[1] / "workspace")
    contacts = workbook.list_contacts()
    flairx = [contact for contact in contacts if contact.organization_id == "org-flairx"]
    raajan = next(contact for contact in contacts if contact.full_name == "Raajan Ashish Pal")
    assert len(flairx) == 9
    assert all("suppress follow up permanently" in contact.notes.casefold() for contact in flairx)
    assert "suppress follow up permanently" in raajan.notes.casefold()


def test_elena_m_vp_product_function_before_org_has_routing_authority():
    capability = resolve_capability(
        contact(
            "VP Product Marketing at Product.ai building category narratives",
            name="Elena M.",
        ),
        CompanyFacts(name="Product.ai"),
        state=ThreadState.NO_CONTEXT,
    )
    assert capability is Capability.CAN_REFER


def test_leonardo_von_mutius_head_of_function_before_org_has_routing_authority():
    capability = resolve_capability(
        contact(
            "Head of Deployed Engineering @ Tessera",
            name="Leonardo von Mutius",
        ),
        CompanyFacts(name="Tessera"),
        state=ThreadState.NO_CONTEXT,
    )
    assert capability is Capability.CAN_REFER


def test_obayashi_ayano_recruiter_never_gets_intel():
    pipeline_signal = OpportunityRecord(
        opportunity_id="op-arches-pm",
        organization_id="org-1",
        title="Product Manager",
        opportunity_type="full_time",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("人事・採用担当者", name="大林彩乃"),
        facts=CompanyFacts(name="Arches", team_size=1000),
        opportunities=[pipeline_signal],
        invite_text="Exploring product roles at Arches.",
        now=NOW,
    )
    assert decision.ask is Ask.NAME


def test_obayashi_ayano_large_referral_uses_2027_availability_not_internship():
    new_grad_role = OpportunityRecord(
        opportunity_id="op-arches-new-grad",
        organization_id="org-1",
        title="2027 New Grad Product Manager",
        opportunity_type="full_time",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("人事・採用担当者", name="大林彩乃"),
        facts=CompanyFacts(name="Arches", team_size=1000),
        opportunities=[new_grad_role],
        invite_text="Exploring product roles at Arches.",
        now=NOW,
    )
    assert decision.ask is Ask.REFER
    assert "2027 new-grad" in decision.availability_qualifier
    assert "intern" not in decision.availability_qualifier
    result = review(
        message=(
            "Hi, thanks for connecting. I'm targeting full-time product work in "
            "Arches' 2027 new-grad cycle. Would you refer me for that role?"
        ),
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_REFER,
        company="Arches",
    )
    assert "missing_availability_qualifier" not in result.flags
    assert "large_company_uses_internship_goal" not in result.flags


def test_deepika_v_warm_large_company_defaults_to_product_goal_without_invite():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Software Engineer", name="Deepika V"),
        facts=CompanyFacts(name="Fivetran", team_size=1000),
        invite_text="",
    )
    assert decision.goal_role_family == "product"
    assert "full-time product roles" in decision.goal


def test_deepika_v_large_company_terminal_touch_is_parked_for_2027():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Software Engineer", name="Deepika V"),
        facts=CompanyFacts(name="Fivetran", team_size=1000),
        touch_count=1,
    )

    assert decision.action is Action.SUPPRESS
    assert decision.terminal_touch is False
    assert "preserve for 2027 re-entry" in decision.reason
    assert decision.reopen_condition == (
        "2027 full-time or new-grad product recruiting opens at Fivetran"
    )


def test_tommy_joyner_followup_preserves_bizops_invite_goal():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("SVP of Engineering", name="Tommy Joyner"),
        facts=CompanyFacts(name="Amperesand", team_size=300),
        invite_text="I'm looking at BizOps/Strategy roles at Amperesand.",
    )
    result = review(
        message=(
            "Hi Tommy, thanks for connecting. I'm looking for a product internship "
            "at Amperesand. Do you know who I should talk to?"
        ),
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_REFER,
        recipient_name="Tommy Joyner",
        company="Amperesand",
    )
    assert decision.goal_role_family == "bizops_strategy"
    assert "invite_goal_mismatch:bizops_strategy" in result.flags


def test_ryan_samadi_third_touch_budget_is_half_and_terminal():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("CEO, Co-Founder at Alt-X", name="Ryan Samadi"),
        facts=CompanyFacts(name="Alt-X", team_size=2),
        touch_count=2,
        reopen_condition_fired=True,
        invite_text="Looking at product roles at Alt-X.",
        has_prior_outbound=True,
    )
    assert decision.ask is Ask.CREATE
    assert decision.word_budget == 35
    assert decision.terminal_touch is True


def test_ryan_samadi_missing_terminal_close_is_added_warmly():
    decision = _decision(action=Action.ASK, ask=Ask.NAME)
    decision.terminal_touch = True
    result = review(
        message="Hi Ryan, following up about Alt-X. Who should I talk to?",
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Ryan Samadi",
        company="Alt-X",
    )
    assert "terminal_touch_not_named" not in result.flags
    assert "I'll stop bugging you after this one, promise!" in result.normalized_message


def test_daichi_hiraoka_prescribed_terminal_sentence_is_not_batch_repetition():
    decision = _decision(action=Action.ASK, ask=Ask.CREATE)
    decision.terminal_touch = True
    terminal = "I'll stop bugging you after this one, promise!"
    result = review(
        message=(
            "Korso is tackling the manual work inside interview intelligence. "
            "Would you consider taking on a fall product intern? "
            f"{terminal}"
        ),
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        batch_sentence_counts=Counter({terminal.casefold(): 36}),
    )
    assert not any(flag.startswith("repeated_in_batch") for flag in result.flags)


def test_kelly_mcdonald_cold_terminal_countdown_is_replaced_by_rule():
    decision = _decision(action=Action.ASK, ask=Ask.NAME)
    decision.terminal_touch = True
    result = review(
        message=(
            "Hi Kelly, I'm exploring product roles at Abridge. "
            "Who should I talk to? This is my last note on it."
        ),
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Kelly McDonald",
        company="Abridge",
    )
    assert "This is my last note on it" not in result.normalized_message
    assert "I'll stop bugging you after this one, promise!" in result.normalized_message


def test_andrew_pekin_second_touch_asks_directly_instead_of_reoffering_work():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Founder", name="Andrew Pekin"),
        facts=CompanyFacts(name="Bellagent", team_size=20),
        invite_text="Exploring product roles at Bellagent.",
        has_prior_outbound=True,
    )
    bad = review(
        message=(
            "Hi Andrew, following up about Bellagent. Send me a product problem and "
            "I'll come back with a written take."
        ),
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        recipient_name="Andrew Pekin",
        company="Bellagent",
    )
    good = review(
        message=(
            "Hi Andrew, following up about Bellagent. Would Bellagent take on a "
            "product intern this fall?"
        ),
        decision=decision,
        read=ThreadRead(),
        capability=Capability.CAN_CREATE,
        recipient_name="Andrew Pekin",
        company="Bellagent",
    )
    assert decision.create_direct_ask is True
    assert "create_repeats_work_offer" in bad.flags
    assert "create_missing_direct_intern_proposal" not in good.flags
    assert "create_queries_program_existence" not in good.flags


def test_andrew_pekin_small_company_terminal_touch_still_proposes_intern_role():
    decision = decide(
        state=ThreadState.NO_CONTEXT,
        read=ThreadRead(),
        contact=contact("Founder", name="Andrew Pekin"),
        facts=CompanyFacts(name="Bellagent", team_size=20, is_startup=True),
        touch_count=1,
        has_prior_outbound=True,
    )

    assert decision.action is Action.ASK
    assert decision.ask is Ask.CREATE
    assert decision.terminal_touch is True
    assert decision.create_direct_ask is True


def test_suresh_mergu_attachment_reply_engages_sponsorship_point():
    inbound = (
        "There is an internship program every year, but Product is mostly FTEs and "
        "there is no sponsorship. Send me your resume."
    )
    old = review(
        message=(
            "Thanks Suresh, resume attached. Five years in data systems including "
            "Optum. Appreciate you looking into options."
        ),
        decision=_decision(action=Action.SEND_ATTACHMENT, ask=Ask.NONE),
        read=ThreadRead(),
        capability=Capability.CAN_REFER,
        has_attachment_task=True,
        recipient_name="Suresh Mergu",
        company="Optum",
        last_inbound_message=inbound,
    )
    revised = review(
        message=(
            "Thanks Suresh, that's genuinely useful on the sponsorship side. Resume "
            "attached in case something opens at Optum. I appreciate you taking a look."
        ),
        decision=_decision(action=Action.SEND_ATTACHMENT, ask=Ask.NONE),
        read=ThreadRead(),
        capability=Capability.CAN_REFER,
        has_attachment_task=True,
        recipient_name="Suresh Mergu",
        company="Optum",
        last_inbound_message=inbound,
    )
    assert "does_not_engage_material_reply" in old.flags
    assert "does_not_engage_material_reply" not in revised.flags


def test_suresh_mergu_direct_reply_is_not_called_a_terminal_followup():
    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=ThreadRead(explicit_request="resume"),
        contact=contact(name="Suresh Mergu"),
        facts=CompanyFacts(name="Optum", team_size=1000),
        touch_count=1,
    )
    assert decision.action is Action.SEND_ATTACHMENT
    assert not decision.terminal_touch


def test_suresh_mergu_manual_ledger_hold_blocks_model_and_copy():
    class ModelMustNotRun:
        class Messages:
            @staticmethod
            def create(**_kwargs):
                raise AssertionError("manual hold must block every model call")

        messages = Messages()

    draft = run(
        [
            ThreadInput(
                contact=contact(
                    name="Suresh Mergu",
                    notes=(
                        "manual_followup_hold=Akshat will reply later | "
                        "set=2026-08-19"
                    ),
                ),
                organization=org(name="Optum", notes="team_size=1000"),
                raw_window=[
                    {
                        "sender": "Suresh Mergu",
                        "message": "Send me your resume and I'll see if I can help.",
                    }
                ],
                opportunities=[],
            )
        ],
        client=ModelMustNotRun(),
    )[0]

    assert draft.decision.action is Action.HOLD
    assert draft.decision.reason == (
        "manual follow-up hold in contact ledger; Akshat will reply later"
    )
    assert draft.message == ""


def test_jj_zhao_review_heading_makes_all_intel_holds_explicit(tmp_path):
    source = tmp_path / "source-review.md"
    source.write_text(
        "# Review\n\n"
        "## HELD — NO DRAFT\n\n"
        "## Suppressed for 2027 re-entry\n\n"
        "## Contact rows to create\n",
        encoding="utf-8",
    )
    decision = _decision(action=Action.ASK, ask=Ask.INTEL)
    rendered = render_reissued_review(
        rows=[
            {
                "draft": SavedDraft(
                    name="JJ Zhao",
                    company="Idler",
                    title="Founding Engineer",
                    ask=Ask.INTEL,
                    message="Hi JJ, do you know who owns product hiring at Idler?",
                    old_flags=[],
                    last_thing="You: Thanks for connecting.",
                ),
                "flags": ["composer_unavailable:APIConnectionError"],
                "status": "regenerate",
                "decision": decision,
            }
        ],
        original_review=source,
        meta={},
    )

    assert "## INTEL (1 — ALL HELD)" in rendered
    assert "none are currently sendable" in rendered
    rendered_path = tmp_path / "rendered-review.md"
    rendered_path.write_text(rendered, encoding="utf-8")
    assert [draft.ask for draft in parse_review(rendered_path)] == [Ask.INTEL]


def test_tim_drahn_warm_opener_has_no_false_connection_premise():
    result = review(
        message=(
            "Hi Tim, reaching out about the 2027 product cycle at Clara. Do you know "
            "who I should talk to?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Tim Drahn",
        company="Clara",
        relationship_context="warm_uninvited_referral",
    )
    assert "missing_opening_beat" not in result.flags
    assert "warm_contact_false_acceptance_premise" not in result.flags


def test_piyush_jhanwar_warm_lane_still_requires_a_truthful_greeting():
    result = review(
        message=(
            "I'm reaching out because Clara is one of the teams I'm looking at. "
            "Do you know who I should talk to?"
        ),
        decision=_decision(action=Action.ASK, ask=Ask.NAME),
        read=ThreadRead(),
        capability=Capability.CAN_NAME,
        recipient_name="Piyush Jhanwar",
        company="Clara",
        relationship_context="warm_uninvited_referral",
    )
    assert "missing_opening_beat" in result.flags


def test_tim_drahn_warm_referral_employer_mismatch_is_not_held():
    draft = run(
        [
            ThreadInput(
                contact=contact(
                    "Senior Principal Software Engineer at Optum",
                    name="Tim Drahn",
                ),
                organization=org(name="Clara"),
                raw_window=[],
                opportunities=[],
                relationship_context="warm_uninvited_referral",
            )
        ],
        client=None,
    )[0]
    assert draft.decision.action is not Action.HOLD


def test_parked_contact_reopens_when_matching_req_appears():
    assessments = evaluate_reopen_conditions(
        contacts=[
            contact(
                name="Harsh Ranjan",
                reopen_condition="Will keep an eye on Acme roles",
            )
        ],
        organizations=[org("team_size=18", name="Acme", organization_type="startup")],
        opportunities=[
            OpportunityRecord(
                opportunity_id="op-acme-fall",
                organization_id="org-1",
                title="Fall 2026 Product Manager Intern",
                opportunity_type="internship",
                discovered_at="2026-08-01T00:00:00+00:00",
            )
        ],
        now=NOW,
    )
    assert assessments[0].status == "reopen_candidate"
    assert assessments[0].req_actionability == APPLY_NOW


def test_parked_contact_stays_parked_without_a_trigger():
    assessments = evaluate_reopen_conditions(
        contacts=[
            contact(
                name="Harsh Ranjan",
                reopen_condition="Will keep an eye on Acme roles",
            )
        ],
        organizations=[org("team_size=18", name="Acme", organization_type="startup")],
        opportunities=[
            OpportunityRecord(
                opportunity_id="op-acme-summer",
                organization_id="org-1",
                title="Summer 2026 Product Manager Intern",
                opportunity_type="internship",
                discovered_at="2026-08-01T00:00:00+00:00",
            )
        ],
        now=NOW,
    )
    assert assessments[0].status == "still_parked"


def test_reopen_condition_persists_on_the_contact_record(tmp_path: Path):
    workbook = OutreachWorkbook(tmp_path / "workspace")
    workbook.upsert_organization(org(name="Sortly"))
    workbook.upsert_contact(contact(name="Harsh Ranjan"))

    updated = persist_reopen_conditions(
        workbook,
        [
            {
                "contact_id": "ct-1",
                "reopen_condition": "Will keep an eye on Sortly roles",
            }
        ],
    )
    persisted = workbook.list_contacts()[0]
    assert updated == 1
    assert persisted.reopen_condition == "Will keep an eye on Sortly roles"
    draft = run(
        [
            ThreadInput(
                contact=persisted,
                organization=workbook.list_organizations()[0],
                raw_window=[{"sender": "Harsh", "message": "Sounds good."}],
                opportunities=[],
            )
        ]
    )[0]
    assert draft.thread_state is ThreadState.PARKED
    assert draft.decision.action is Action.SUPPRESS


def test_open_ended_offer_is_read_as_an_offer():
    messages = [Message(sender="Kirk", text="Let me know how I can help.")]
    assert deterministic_read(messages).offer_made == "advice"

    validated = validate_ai_read(
        ThreadRead(offer_made="none", source="ai"),
        messages,
    )
    assert validated.offer_made == "advice"


def _converzai_live_collision_drafts():
    organization = OrganizationRecord(
        organization_id="org-converzai",
        name="ConverzAI",
    )
    return run(
        [
            ThreadInput(
                contact=ContactRecord(
                    contact_id="ct-pulkit",
                    organization_id="org-converzai",
                    full_name="Pulkit Kumar",
                    title="Software Developer @ ConverzAI",
                    status="Replied",
                ),
                organization=organization,
                raw_window=[
                    {"sender": "You", "message": "Would love to connect."},
                    {"sender": "Pulkit", "message": "Hey, Akshat"},
                ],
                opportunities=[],
            ),
            ThreadInput(
                contact=ContactRecord(
                    contact_id="ct-ramashish",
                    organization_id="org-converzai",
                    full_name="Ramashish Pandey",
                    title="SWE @ ConverzAI",
                    status="Replied",
                ),
                organization=organization,
                raw_window=[
                    {"sender": "You", "message": "Would love to connect."},
                    {
                        "sender": "Ramashish",
                        "message": "Happy to help. I would get in touch if any PM role comes up.",
                    },
                ],
                opportunities=[],
            ),
        ]
    )


def test_each_converzai_live_thread_keeps_its_substantive_ask():
    drafts = _converzai_live_collision_drafts()
    assert all(draft.decision.action is not Action.SUPPRESS for draft in drafts)
    substantive = [
        draft
        for draft in drafts
        if draft.decision.ask in {Ask.NAME, Ask.FORWARD, Ask.REFER, Ask.CREATE}
    ]
    assert {draft.name for draft in substantive} == {
        "Pulkit Kumar",
        "Ramashish Pandey",
    }


def test_pulkit_is_not_downgraded_because_ramashish_also_replied():
    drafts = _converzai_live_collision_drafts()
    pulkit = next(draft for draft in drafts if draft.name == "Pulkit Kumar")
    assert pulkit.decision.action is Action.ASK
    assert pulkit.decision.ask is Ask.NAME
    assert "collision" not in pulkit.decision.reason


def test_contact_at_touch_cap_is_suppressed():
    draft = run(
        [
            ThreadInput(
                contact=contact("Founder", name="Harsha Singla"),
                organization=org(notes="team_size=8", organization_type="startup"),
                raw_window=[],
                opportunities=[],
                touch_count=2,
            )
        ],
        client=None,
    )[0]

    assert draft.decision.action is Action.SUPPRESS
    assert draft.touch_count == 2
    assert draft.touch_cap_reached is True
    assert "touch cap reached (2/2)" in draft.decision.reason


def test_third_touch_requires_a_fired_reopen_condition():
    parked = contact(
        "Founder",
        name="Harsha Singla",
        reopen_condition="a concrete HireVue fall product role appears",
    )
    base = dict(
        contact=parked,
        organization=org(notes="team_size=8", organization_type="startup"),
        raw_window=[],
        opportunities=[],
        touch_count=2,
    )

    not_fired = run([ThreadInput(**base)], client=None)[0]
    fired = run(
        [ThreadInput(**base, reopen_condition_fired=True)], client=None
    )[0]
    spent_trigger = run(
        [
            ThreadInput(
                **{
                    **base,
                    "touch_count": 3,
                    "reopen_condition_fired": True,
                }
            )
        ],
        client=None,
    )[0]

    assert not_fired.decision.action is Action.SUPPRESS
    assert not_fired.thread_state is ThreadState.PARKED
    assert fired.decision.action is Action.ASK
    assert fired.touch_cap_reached is False
    assert spent_trigger.decision.action is Action.SUPPRESS
    assert spent_trigger.touch_cap_reached is True


def test_first_touch_on_a_silent_accept_is_allowed():
    draft = run(
        [
            ThreadInput(
                contact=contact("Founder", name="Harsha Singla"),
                organization=org(notes="team_size=8", organization_type="startup"),
                raw_window=[],
                opportunities=[],
                touch_count=0,
            )
        ],
        client=None,
    )[0]

    assert draft.thread_state is ThreadState.NO_CONTEXT
    assert draft.decision.action is Action.ASK
    assert draft.touch_cap_reached is False


def test_harsha_singla_invite_is_not_a_followup_touch():
    rows = [
        TouchpointRecord(
            touchpoint_id="tp-invite",
            organization_id="org-1",
            contact_id="ct-1",
            channel="linkedin",
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Would love to connect.",
        ),
        TouchpointRecord(
            touchpoint_id="tp-followup",
            organization_id="org-1",
            contact_id="ct-1",
            channel="linkedin",
            status="Sent",
            message_kind="linkedin_followup",
            message_text="Thanks for connecting.",
        ),
        TouchpointRecord(
            touchpoint_id="tp-draft",
            organization_id="org-1",
            contact_id="ct-1",
            channel="linkedin",
            status="Draft",
            message_kind="linkedin_followup",
            message_text="This was never sent.",
        ),
    ]

    assert outbound_followup_touch_counts(rows) == {"ct-1": 1}


def test_thanks_anyway_without_inbound_flags_missing_inbound():
    evidence = inbound_probably_missing(
        [
            TouchpointRecord(
                touchpoint_id="tp-invite",
                organization_id="org-cosm",
                contact_id="ct-bhavin",
                channel="linkedin",
                status="Sent",
                message_kind="linkedin_invite",
                message_text="Hi Bhavin, I'd love to connect.",
            ),
            TouchpointRecord(
                touchpoint_id="tp-responsive",
                organization_id="org-cosm",
                contact_id="ct-bhavin",
                channel="linkedin",
                status="Sent",
                message_kind="linkedin_manual_message",
                message_text="Thanks anyways!",
            ),
        ]
    )

    assert evidence is not None
    assert evidence.touchpoint_id == "tp-responsive"


def test_cold_opener_with_courtesy_words_is_not_flagged():
    for message in (
        "Hi Bhavin! How are you doing?",
        "Hi Bhavin, I'm a 1Y MBA at USC and would love to connect.",
        "Thanks for connecting, Bhavin. I'm exploring product roles at Cosm.",
    ):
        evidence = inbound_probably_missing(
            [
                TouchpointRecord(
                    touchpoint_id="tp-cold",
                    organization_id="org-cosm",
                    contact_id="ct-bhavin",
                    channel="linkedin",
                    status="Sent",
                    message_kind="linkedin_manual_message",
                    message_text=message,
                )
            ]
        )
        assert evidence is None


def test_bratee_placeholder_reply_still_needs_targeted_repull():
    evidence = inbound_probably_missing(
        [
            TouchpointRecord(
                touchpoint_id="tp-placeholder",
                organization_id="org-tractian",
                contact_id="ct-bratee",
                channel="linkedin",
                status="Replied",
                message_kind="linkedin_reply",
                message_text="LinkedIn reply detected.",
                recorded_at="2026-07-01T19:51:02+00:00",
            ),
            TouchpointRecord(
                touchpoint_id="tp-responsive",
                organization_id="org-tractian",
                contact_id="ct-bratee",
                channel="linkedin",
                status="Sent",
                message_kind="linkedin_manual_message",
                message_text=(
                    "Thanks so much Bratee for making the effort to share all this!"
                ),
                recorded_at="2026-07-09T07:19:25+00:00",
            ),
        ]
    )

    assert evidence is not None
    assert evidence.touchpoint_id == "tp-responsive"


def test_critic_enforces_prior_commitment():
    """Akshat promised Hemang he would only send a real match."""

    result = review(
        message="Attaching my resume. Would you be open to a referral at Snyk?",
        decision=_decision(action=Action.ASK),
        read=ThreadRead(commitments_i_made=["only send you a fit if there's a real match"]),
        capability=Capability.CAN_NAME,
    )
    assert "violates_prior_commitment" in result.flags


# ------------------------------------------- extractor false positives


def test_connect_with_you_is_not_a_referral():
    """Shashwat's 'great to connect with you' produced a contact called
    'Hey Akshat'."""

    messages, _ = order_messages(
        [{"sender": "Shashwat", "message": "Hey Akshat, great to connect with you", "timestamp_text": "Aug 6"}]
    )
    assert deterministic_read(messages).named_people == []


def test_negated_hiring_is_not_an_opening():
    """Pranav said 'I don't believe we're hiring any product / PM roles'."""

    messages, _ = order_messages(
        [
            {
                "sender": "Pranav",
                "message": "I don't believe we're hiring any product / PM roles, but happy to shoot your linkedin over.",
                "timestamp_text": "Aug 4",
            }
        ]
    )
    assert deterministic_read(messages).named_opening is None


def test_real_opening_is_still_detected():
    messages, _ = order_messages(
        [{"sender": "Harsha", "message": "Hi Akshat, I think we currently have an opening for Product role.", "timestamp_text": "Aug 4"}]
    )
    assert deterministic_read(messages).named_opening


@pytest.mark.parametrize("reply", ["Absolutely", "Yes that will be great", "Sure, let me know."])
def test_acknowledged_standing_ask_sends_nothing(reply):
    """Midun, Harsh and Hemang each agreed to an ask already made. The engine
    previously drafted a fresh pitch at all three."""

    messages, _ = order_messages(
        [
            {"sender": "You", "message": "Would you point me to the right hiring contact?", "timestamp_text": "Jul 1"},
            {"sender": "Them", "message": reply, "timestamp_text": "Jul 2"},
        ]
    )
    read = deterministic_read(messages)
    assert read.acknowledged_standing_ask
    decision = decide(
        state=ThreadState.THEY_REPLIED_UNANSWERED,
        read=read,
        contact=contact("Senior Software Engineer"),
        facts=CompanyFacts(name="Ottimate"),
    )
    assert decision.action is Action.SUPPRESS
    assert decision.reopen_condition


def test_invite_placed_after_messages_that_predate_it():
    """Sandeep's profile link predates the invite by three weeks, so it was
    never a reply to us."""

    messages, confident = order_messages(
        [
            {"sender": "contact", "message": "Sandeep sent a post", "timestamp_text": "Jul 6"},
            {"sender": "Sandeep", "message": "https://linkedin.com/in/ajit-bhave", "timestamp_text": "Jul 9"},
            {"sender": "You", "message": "Hi Sandeep", "source": "original_invite"},
        ],
        invite_sent_at=datetime(2026, 7, 29),
    )
    assert confident
    assert messages[-1].text == "Hi Sandeep"
    assert resolve_state(messages) is ThreadState.YOU_REPLIED_LAST


def test_same_day_reply_sorts_after_the_invite():
    """Pranav replied the same day; a date-only stamp must not sort before a
    timestamped invite."""

    messages, confident = order_messages(
        [
            {"sender": "You", "message": "Hi Pranav", "source": "original_invite"},
            {"sender": "Pranav", "message": "Hey Akshat, nice to meet!", "timestamp_text": "Aug 4"},
        ],
        invite_sent_at=datetime(2026, 8, 4, 5, 32),
    )
    assert confident
    assert messages[0].text == "Hi Pranav"
    assert resolve_state(messages) is ThreadState.THEY_REPLIED_UNANSWERED

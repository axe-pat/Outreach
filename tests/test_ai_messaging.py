from __future__ import annotations

import json
from dataclasses import dataclass

from outreach.ai_messaging import AIMessagingService, AIMessageRequest
from outreach.cli import build_linkedin_followup_drafts, draft_track_2_email
from outreach.services.notes import NoteGenerator
from outreach.style_profile import CommunicationStyleProfile
from outreach.tracking import ContactRecord, OrganizationRecord


@dataclass
class _TextBlock:
    text: str


@dataclass
class _Response:
    content: list[_TextBlock]


class _Messages:
    def __init__(self, outputs: list[dict[str, object] | Exception]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> _Response:
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return _Response([_TextBlock(json.dumps(output))])


class _Client:
    def __init__(self, outputs: list[dict[str, object] | Exception]) -> None:
        self.messages = _Messages(outputs)


def _output(
    message: str,
    *,
    subject: str = "",
    story_source: str = "",
    story_evidence: str = "",
    critique: list[str] | None = None,
) -> dict[str, object]:
    return {
        "scenario_summary": "Akshat has a grounded reason to contact this person.",
        "selected_story_source": story_source,
        "selected_story_evidence": story_evidence,
        "first_draft": message,
        "critique": critique or ["Make the note less formal and keep the ask concrete."],
        "subject": subject,
        "final_message": message,
    }


def test_ai_composer_rewrites_generic_first_attempt_and_preserves_story_evidence() -> None:
    evidence = "Hevo experience with connectors, ETL, and data movement."
    client = _Client(
        [
            _output(
                "Hi Maya, your work stood out and I would love to connect about Airbyte.",
                story_source="story_fit_reason",
                story_evidence=evidence,
            ),
            _output(
                (
                    "Hi Maya, Airbyte's connector work is close to the data movement systems I "
                    "built at Hevo. I've been deep in product for a while now. Would be great "
                    "to connect."
                ),
                story_source="story_fit_reason",
                story_evidence=evidence,
                critique=["The first pass sounded like a networking template."],
            ),
        ]
    )
    service = AIMessagingService(
        client=client,
        model="test-model",
        style_profile=CommunicationStyleProfile(
            banned_phrases=["would love to connect", "your work stood out"]
        ),
    )

    result = service.compose(
        AIMessageRequest(
            channel="linkedin_invite",
            base_message="Hi Maya, I'm looking at product roles at Airbyte. Open to connecting?",
            company="Airbyte",
            recipient_name="Maya Singh",
            recipient_type="senior_product",
            story_evidence={"story_fit_reason": evidence},
            style_guidance=(
                "Strong examples to emulate: [manual_gold] Curious if that background fits?"
            ),
            max_chars=300,
        )
    )

    assert result.used_ai is True
    assert result.attempts == 2
    assert result.selected_story_source == "story_fit_reason"
    assert result.selected_story_evidence == evidence
    assert "Hevo" in result.message
    assert "would love" not in result.message.lower()
    assert len(client.messages.calls) == 2
    first_prompt = str(client.messages.calls[0]["messages"])
    assert "manual_gold" in first_prompt
    assert "Weak examples to avoid" not in first_prompt
    assert client.messages.calls[0]["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "scenario_summary": {"type": "string"},
                    "selected_story_source": {"type": "string"},
                    "selected_story_evidence": {"type": "string"},
                    "first_draft": {"type": "string"},
                    "critique": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "final_message": {"type": "string"},
                },
                "required": [
                    "scenario_summary",
                    "selected_story_source",
                    "selected_story_evidence",
                    "first_draft",
                    "critique",
                    "subject",
                    "final_message",
                ],
                "additionalProperties": False,
            },
        }
    }


def test_ai_composer_makes_dual_institution_overlap_personal() -> None:
    client = _Client(
        [
            _output(
                (
                    "Hi Rhea, kind of wild that we both did Thapar and USC! I'm looking at "
                    "product work at Stripe and had to say hi. Open to connecting? Fight On!"
                )
            )
        ]
    )
    service = AIMessagingService(client=client, model="test-model")

    result = service.compose(
        AIMessageRequest(
            channel="linkedin_invite",
            base_message="Hi Rhea, fellow Trojan here. I'm exploring product work at Stripe.",
            company="Stripe",
            recipient_name="Rhea Kapoor",
            institution_signals=("thapar", "usc"),
            max_chars=300,
        )
    )

    assert result.used_ai is True
    assert "kind of wild" in result.message
    assert "Thapar" in result.message
    assert "USC" in result.message
    assert "!" in result.message


def test_ai_composer_falls_back_without_key_or_after_model_error() -> None:
    request = AIMessageRequest(
        channel="linkedin_followup",
        base_message="Thanks Priya. Any recs on who owns this at Acme?",
        company="Acme",
    )

    disabled = AIMessagingService.from_api_key(None).compose(request)
    failing = AIMessagingService(
        client=_Client([RuntimeError("temporary failure")]),
        model="test-model",
    ).compose(request)

    assert disabled.used_ai is False
    assert disabled.message == request.base_message
    assert disabled.fallback_reason == "missing_anthropic_api_key"
    assert failing.used_ai is False
    assert failing.message == request.base_message
    assert failing.fallback_reason == "model_error:RuntimeError"
    assert failing.deterministic_decisions_preserved is True


def test_ai_composer_repairs_target_role_drift_and_unsupported_person_claims() -> None:
    client = _Client(
        [
            _output(
                (
                    "Hi Maya, your robotics background is fascinating, and wild that we both "
                    "worked at Google! I'm looking for an engineering role at Airbyte. "
                    "Open to connecting?"
                )
            ),
            _output(
                "Hi Maya, I'm exploring Product Strategy roles at Airbyte. Open to connecting?"
            ),
        ]
    )
    service = AIMessagingService(client=client, model="test-model")

    result = service.compose(
        AIMessageRequest(
            channel="linkedin_invite",
            base_message=(
                "Hi Maya, my robotics work led me to explore Product Strategy roles at Airbyte. "
                "Open to connecting?"
            ),
            company="Airbyte",
            recipient_name="Maya Singh",
            recipient_title="Senior Product Manager",
            person_evidence=("Senior Product Manager at Airbyte",),
            target_role_family="product_strategy",
            target_role_label="Product Strategy",
            max_chars=300,
        )
    )

    assert result.used_ai is True
    assert result.attempts == 2
    assert "Product Strategy" in result.message
    assert "Google" not in result.message
    assert "engineering role" not in result.message
    repair_prompt = str(client.messages.calls[1]["messages"])
    assert "unsupported shared-employment claim" in repair_prompt
    assert "unsupported recipient fact" in repair_prompt
    assert "target role drift" in repair_prompt


def test_ai_composer_allows_grounded_shared_employment_claim() -> None:
    evidence = "I worked at Hevo on data systems."
    message = (
        "Hi Maya, your connector background makes the overlap kind of wild, we both worked "
        "at Hevo! I'm exploring product roles there. Open to connecting?"
    )
    client = _Client(
        [
            _output(
                message,
                story_source="private_outreach_context",
                story_evidence=evidence,
            )
        ]
    )

    result = AIMessagingService(client=client, model="test-model").compose(
        AIMessageRequest(
            channel="linkedin_invite",
            base_message="Hi Maya, I'm exploring product roles at Hevo. Open to connecting?",
            company="Hevo",
            recipient_name="Maya Singh",
            recipient_title="Product Manager at Hevo",
            person_evidence=("Product Manager at Hevo; built connector systems",),
            story_evidence={"private_outreach_context": evidence},
            target_role_family="product_pm",
            target_role_label="Product / PM",
            max_chars=300,
        )
    )

    assert result.used_ai is True
    assert result.attempts == 1
    assert result.message == message
    assert "\u2014" not in result.message


def test_invite_batch_falls_back_before_ungrounded_ai_copy_can_be_sendable() -> None:
    ungrounded = (
        "Hi Maya, your robotics background is fascinating, and wild that we both worked "
        "at Google! I'm looking for an engineering role at Airbyte. Open to connecting?"
    )
    client = _Client([_output(ungrounded), _output(ungrounded)])
    generator = NoteGenerator(
        ai_messaging=AIMessagingService(client=client, model="test-model")
    )

    row = generator.generate_batch(
        [
            {
                "name": "Maya Singh",
                "title": "Product Manager",
                "role_bucket": "Product",
                "existing_connection": False,
            }
        ],
        company="Airbyte",
    )[0]

    assert row["ai_messaging"]["used_ai"] is False
    assert row["ai_messaging"]["status"] == "fallback"
    assert row["ai_messaging"]["fallback_reason"] == "validation_failed"
    assert "Google" not in row["note"]
    assert "engineering role" not in row["note"]
    assert row["note"] == row["ai_messaging"]["applied_message"]


def test_invite_batch_uses_ai_copy_and_keeps_existing_qc_gate() -> None:
    client = _Client(
        [
            _output(
                (
                    "Hi Rhea, kind of wild that we both did Thapar and USC! I'm exploring PM "
                    "work at Stripe after building data platforms. Open to connecting? Fight On!"
                )
            )
        ]
    )
    generator = NoteGenerator(
        ai_messaging=AIMessagingService(client=client, model="test-model")
    )

    row = generator.generate_batch(
        [
            {
                "name": "Rhea Kapoor",
                "title": "Product Manager",
                "role_bucket": "Product",
                "usc": True,
                "usc_marshall": False,
                "existing_connection": False,
                "shared_history": True,
                "shared_history_signals": ["Thapar"],
            }
        ],
        company="Stripe",
    )[0]

    assert row["ai_messaging"]["status"] == "composed"
    assert "kind of wild" in row["note"]
    assert row["note_qc"]["verdict"] == "send"
    assert row["note_within_limit"] is True


def test_followup_ai_changes_only_copy_not_reply_or_send_classification() -> None:
    evidence = "Hevo connector and ETL systems are directly relevant."
    client = _Client(
        [
            _output(
                (
                    "Thanks Priya! Airbyte's connector problem is unusually close to what I built "
                    "at Hevo. Does that background fit product work there?"
                ),
                story_source="story_fit_reason",
                story_evidence=evidence,
            )
        ]
    )
    organization = OrganizationRecord(
        organization_id="org-airbyte",
        name="Airbyte",
        notes=f"story_fit_reason={evidence} | profile_evidence=Hevo engineering",
    )
    contact = ContactRecord(
        contact_id="ct-priya",
        organization_id="org-airbyte",
        full_name="Priya Shah",
        title="Senior Product Manager",
        notes="triggers=USC,Thapar",
    )

    draft = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-priya",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Hi Priya, fellow Trojan here. Fight On!",
            }
        ],
        organizations=[organization],
        contacts=[contact],
        ai_messaging=AIMessagingService(client=client, model="test-model"),
    )[0]

    assert draft["draft_kind"] == "accepted_follow_up"
    assert draft["send_recommendation"] == "review"
    assert draft["ai_messaging"]["deterministic_decisions_preserved"] is True
    assert "Hevo" in draft["draft_message"]


def test_track_2_email_uses_ai_story_copy_but_stays_review_only() -> None:
    evidence = "FlairX recruiting workflow and AI interview product work."
    body = (
        "Hi Nina,\n\nFlairX gave me direct scar tissue with AI interview and recruiting "
        "workflows, which is why Acme is a specific target for me. I spent the last several "
        "months testing where candidate context gets lost and where automation actually helps. "
        "Your product role looks close to that problem, but I do not want to force the fit.\n\n"
        "Does that background fit anything the product team is working on? If yes, any recs on "
        "who I should talk to; if not, a quick no is useful too.\n\nBest,\nAkshat"
    )
    client = _Client(
        [
            _output(
                body,
                subject="A specific product overlap at Acme",
                story_source="story_fit_reason",
                story_evidence=evidence,
            )
        ]
    )
    profile = CommunicationStyleProfile()
    organization = OrganizationRecord(
        organization_id="org-acme",
        name="Acme",
        notes=f"story_fit_reason={evidence} | profile_evidence=FlairX AI PM internship",
    )
    contact = ContactRecord(
        contact_id="ct-nina",
        organization_id="org-acme",
        full_name="Nina Rao",
        title="Director of Product",
        email="nina@acme.example",
    )

    draft = draft_track_2_email(
        organization=organization,
        contact=contact,
        campaign_action="send_cold_email",
        cadence_action="email_initial",
        style_profile=profile,
        ai_messaging=AIMessagingService(
            client=client,
            model="test-model",
            style_profile=profile,
        ),
    )

    assert draft["cadence_action"] == "email_initial"
    assert draft["send_recommendation"] in {"review", "needs_rewrite"}
    assert draft["subject"] == "A specific product overlap at Acme"
    assert "FlairX" in draft["body"]
    assert draft["ai_messaging"]["status"] == "composed"

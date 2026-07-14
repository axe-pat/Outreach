from __future__ import annotations

from outreach.config import OutreachSettings
from outreach.mapped_invites import (
    augment_invite_source_with_mapped_contacts,
    build_mapped_invite_candidates,
    merge_and_prioritize_invite_candidates,
)
from outreach.services.notes import NoteGenerator
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachChannel,
    TouchpointRecord,
)


def _organization() -> OrganizationRecord:
    return OrganizationRecord(
        organization_id="org-acme",
        name="Acme",
        organization_type=OrganizationType.COMPANY,
    )


def _contact(
    contact_id: str,
    name: str,
    *,
    target_lists: str = "",
    notes: str = "",
    status: str = "Discovered",
) -> ContactRecord:
    return ContactRecord(
        contact_id=contact_id,
        organization_id="org-acme",
        full_name=name,
        title="Senior Product Manager",
        contact_type="Product",
        target_lists=target_lists,
        preferred_channel=OutreachChannel.LINKEDIN,
        status=status,
        linkedin_url=f"https://www.linkedin.com/in/{contact_id}/",
        notes=notes,
    )


def test_mapped_candidates_put_shared_institutions_ahead_of_regular_contacts() -> None:
    candidates = build_mapped_invite_candidates(
        organization=_organization(),
        contacts=[
            _contact("regular", "Regular Product"),
            _contact("usc", "USC Product", target_lists="usc-network;priority-medium"),
            _contact(
                "dual",
                "Dual Product",
                target_lists="usc-network;thapar-network;priority-high",
                notes="school=USC | lead_notes=Thapar alum | connection_degree=2nd",
            ),
        ],
        settings=OutreachSettings(),
    )

    assert [item["name"] for item in candidates] == [
        "Dual Product",
        "USC Product",
        "Regular Product",
    ]
    assert candidates[0]["mapped_contact_institution_signals"] == ["thapar", "usc"]
    assert candidates[0]["passes"] == ["mapped_workbook_contact"]
    assert "Current: Senior Product Manager at Acme" in candidates[0]["raw_text"]


def test_mapped_candidates_fail_closed_for_holds_prior_invites_and_sent_statuses() -> None:
    contacts = [
        _contact("ready", "Ready"),
        _contact("held", "Held", target_lists="outreach-hold;usc-network"),
        _contact("prepared", "Prepared"),
        _contact("sent", "Sent", status="Invited"),
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id="touch-prepared",
            organization_id="org-acme",
            contact_id="prepared",
            channel=OutreachChannel.LINKEDIN,
            status="Prepared",
            message_kind="linkedin_invite",
            message_text="Prepared invite",
        )
    ]

    candidates = build_mapped_invite_candidates(
        organization=_organization(),
        contacts=contacts,
        touchpoints=touchpoints,
    )

    assert [item["name"] for item in candidates] == ["Ready"]


def test_merge_prefers_reviewed_mapped_identity_and_keeps_generated_note() -> None:
    discovered = [
        {
            "name": "USC Product",
            "company": "Acme",
            "linkedin_url": "https://www.linkedin.com/in/usc/",
            "score": 50,
            "usc": True,
            "note": "Generated USC note",
            "note_qc": {"verdict": "send"},
        },
        {
            "name": "Regular Founder",
            "company": "Acme",
            "linkedin_url": "https://www.linkedin.com/in/founder/",
            "score": 99,
        },
    ]
    mapped = [
        {
            "name": "USC Product",
            "company": "Acme",
            "linkedin_url": "https://www.linkedin.com/in/usc/",
            "score": 80,
            "usc": True,
            "mapped_contact_id": "contact-usc",
            "mapped_contact_affinity_rank": 1,
        }
    ]

    merged = merge_and_prioritize_invite_candidates(mapped, discovered)

    assert [item["name"] for item in merged] == ["USC Product", "Regular Founder"]
    assert merged[0]["mapped_contact_id"] == "contact-usc"
    assert merged[0]["note"] == "Generated USC note"
    assert merged[0]["note_qc"] == {"verdict": "send"}


def test_merge_deduplicates_linkedin_tracking_url_variants() -> None:
    discovered = [
        {
            "name": "USC Product",
            "company": "Acme",
            "linkedin_url": "https://linkedin.com/in/usc/?trk=people-search",
            "score": 50,
            "note": "Generated USC note",
        }
    ]
    mapped = [
        {
            "name": "USC Product",
            "company": "Acme",
            "linkedin_url": "https://www.linkedin.com/in/usc/",
            "score": 80,
            "mapped_contact_id": "contact-usc",
        }
    ]

    merged = merge_and_prioritize_invite_candidates(mapped, discovered)

    assert len(merged) == 1
    assert merged[0]["mapped_contact_id"] == "contact-usc"
    assert merged[0]["note"] == "Generated USC note"


def test_augment_note_generates_mapped_rows_and_recovers_from_search_filter_failure() -> None:
    payload, mapped_count = augment_invite_source_with_mapped_contacts(
        organization=_organization(),
        contacts=[
            _contact(
                "usc",
                "USC Product",
                target_lists="usc-network;priority-high",
            )
        ],
        touchpoints=[],
        settings=OutreachSettings(),
        note_generator=NoteGenerator(),
        target_role_title="Senior Product Manager",
        search_payload={
            "company": "Acme",
            "company_mode": "default",
            "company_filter_status": "failed_exact_company_suggestion",
            "company_filter_error": "Could not find an exact company suggestion for Acme",
            "pass_summaries": [{"pass_name": "product_usc", "error": "filter failed"}],
            "results": [
                {
                    "name": "Unsafe Search Result",
                    "linkedin_url": "https://www.linkedin.com/in/unsafe/",
                }
            ],
        },
        search_error="",
    )

    assert mapped_count == 1
    assert payload["company_filter_status"] == "completed_mapped_workbook_assignment"
    assert payload["company_filter_error"] == ""
    assert payload["company_search_filter_status"] == "failed_exact_company_suggestion"
    assert [item["name"] for item in payload["results"]] == ["USC Product"]
    candidate = payload["results"][0]
    assert candidate["note"]
    assert candidate["note_qc"]["verdict"] == "send"
    assert candidate["target_role_is_concrete"] is True


def test_augment_does_not_promote_current_role_review_hold() -> None:
    payload, mapped_count = augment_invite_source_with_mapped_contacts(
        organization=_organization(),
        contacts=[
            _contact(
                "held",
                "Held USC Product",
                target_lists=(
                    "usc-network;current-role-review-required;outreach-hold;priority-high"
                ),
            )
        ],
        touchpoints=[],
        settings=OutreachSettings(),
        note_generator=NoteGenerator(),
        search_payload={"company": "Acme", "results": []},
    )

    assert mapped_count == 0
    assert payload == {"company": "Acme", "results": []}


def test_augment_filters_live_search_duplicate_of_locator_review_hold() -> None:
    held = _contact(
        "held",
        "Held USC Product",
        target_lists="locator-review-hold;usc-network",
    )
    payload, mapped_count = augment_invite_source_with_mapped_contacts(
        organization=_organization(),
        contacts=[held],
        touchpoints=[],
        settings=OutreachSettings(),
        note_generator=NoteGenerator(),
        search_payload={
            "company": "Acme",
            "results": [
                {
                    "name": held.full_name,
                    "company": "Acme",
                    "linkedin_url": held.linkedin_url + "?trk=people-search",
                    "score": 90,
                }
            ],
        },
    )

    assert mapped_count == 0
    assert payload["results"] == []

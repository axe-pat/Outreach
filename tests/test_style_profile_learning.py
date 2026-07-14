import json

import yaml

from outreach.style_profile import (
    CommunicationStyleProfile,
    StyleMessageExample,
    dump_style_profile,
    load_comms_learning_examples,
    load_style_profile,
    merge_comms_learning_examples,
    sync_comms_learning_into_style_profile,
)
from outreach.tracking import ContactRecord, OrganizationRecord
from outreach.cli import build_linkedin_followup_drafts, draft_track_2_email


def curated_profile() -> CommunicationStyleProfile:
    return CommunicationStyleProfile(
        preferred_directness="curated directness",
        preferred_casualness="curated warmth",
        banned_phrases=["pick your brain"],
        self_intro_variants=["Curated intro"],
        approved_asks_by_recipient_type={"founder": ["Curated founder ask"]},
        strong_messages=[
            StyleMessageExample(
                label="curated_strong",
                recipient_type="founder",
                message="A concrete existing message.",
                notes="Keep this curated example.",
            )
        ],
        weak_messages=[
            StyleMessageExample(
                label="curated_weak",
                recipient_type="general",
                message="Generic existing message.",
            )
        ],
        notes="Curated profile notes",
    )


def test_merge_maps_labels_preserves_curated_fields_and_dedupes() -> None:
    profile = curated_profile()
    contacts = [
        ContactRecord(
            contact_id="ct-founder",
            organization_id="org-acme",
            full_name="Fran Founder",
            title="Founder and CEO",
        )
    ]
    organizations = [OrganizationRecord(organization_id="org-acme", name="Acme")]
    examples = [
        {
            "label": "gold",
            "message": "A concrete existing message!",
            "recipient_type": "founder",
        },
        {
            "label": "silver",
            "message": "The platform launch maps to my Hevo work. Is the product team hiring?",
            "recipient_type": "Product",
        },
        {
            "label": "negative",
            "message": "Your work stood out and I would love to connect.",
            "name": "Fran Founder",
            "company": "Acme",
            "reason": "manual message replaced this draft",
        },
        {"label": "mystery", "message": "Ignore this row"},
    ]

    merged, summary = merge_comms_learning_examples(
        profile,
        examples,
        contacts=contacts,
        organizations=organizations,
    )

    assert summary.as_dict() == {
        "examples_seen": 4,
        "strong_added": 1,
        "weak_added": 1,
        "conflicts_pruned": 0,
        "duplicates_skipped": 1,
        "invalid_skipped": 1,
        "profile_updated": True,
    }
    assert merged.preferred_directness == profile.preferred_directness
    assert merged.preferred_casualness == profile.preferred_casualness
    assert merged.banned_phrases == profile.banned_phrases
    assert merged.self_intro_variants == profile.self_intro_variants
    assert merged.approved_asks_by_recipient_type == profile.approved_asks_by_recipient_type
    assert merged.notes == profile.notes
    assert merged.strong_messages[-1].recipient_type == "senior_product"
    assert merged.strong_messages[-1].source == "comms_learning/linkedin_examples.jsonl"
    assert merged.weak_messages[-1].recipient_type == "founder"

    merged_again, second_summary = merge_comms_learning_examples(
        merged,
        examples,
        contacts=contacts,
        organizations=organizations,
    )

    assert second_summary.profile_updated is False
    assert second_summary.duplicates_skipped == 3
    assert len(merged_again.strong_messages) == len(merged.strong_messages)
    assert len(merged_again.weak_messages) == len(merged.weak_messages)


def test_merge_skips_ui_placeholders_and_tiny_closers_as_strong_examples() -> None:
    examples = [
        {"label": "gold", "message": "You sent an attachment"},
        {"label": "gold", "message": "Sure, thanks a lot!!😃"},
        {"label": "silver", "message": "Oh okay Thanks a lot anyways!😃"},
        {"label": "silver", "message": "Sounds good"},
        {
            "label": "gold",
            "message": "But thanks for the info, I'll try reaching out to Irina about this!",
        },
        {"label": "silver", "message": "Who owns PM hiring there?"},
        {"label": "negative", "message": "Thanks!"},
    ]

    merged, summary = merge_comms_learning_examples(
        CommunicationStyleProfile(),
        examples,
    )

    assert [item.message for item in merged.strong_messages] == [
        "But thanks for the info, I'll try reaching out to Irina about this!",
        "Who owns PM hiring there?",
    ]
    assert [item.message for item in merged.weak_messages] == ["Thanks!"]
    assert summary.as_dict() == {
        "examples_seen": 7,
        "strong_added": 2,
        "weak_added": 1,
        "conflicts_pruned": 0,
        "duplicates_skipped": 0,
        "invalid_skipped": 4,
        "profile_updated": True,
    }


def test_merge_prioritizes_manual_gold_and_semantically_dedupes_reordered_copy() -> None:
    manual = "Thanks Priya, does that background fit product work at Acme?"
    reordered = "At Acme, Priya, does that background fit product work? Thanks."
    examples = [
        {"label": "negative", "message": manual},
        {"label": "gold", "message": manual},
        {"label": "silver", "message": reordered},
    ]

    merged, summary = merge_comms_learning_examples(
        CommunicationStyleProfile(),
        examples,
    )

    assert [item.message for item in merged.strong_messages] == [manual]
    assert merged.weak_messages == []
    assert summary.strong_added == 1
    assert summary.weak_added == 0
    assert summary.duplicates_skipped == 2


def test_merge_repairs_historical_learned_negative_positive_conflict() -> None:
    manual = (
        "Thanks for connecting, Emiliano. I'm exploring technical PM/product paths at Snyk "
        "from a backend/data engineering background. Does that background fit product work "
        "there? Any recommendations on people I should talk to about that?"
    )
    generated = manual.replace(
        "recommendations on people I should talk to", "recs on who I should talk to"
    )
    profile = CommunicationStyleProfile(
        strong_messages=[
            StyleMessageExample(
                label="learned_gold_existing",
                message=manual,
                source="comms_learning/linkedin_examples.jsonl",
            )
        ],
        weak_messages=[
            StyleMessageExample(
                label="curated_weak",
                message="Thanks for connecting. I would love to pick your brain.",
            ),
            StyleMessageExample(
                label="learned_negative_stale",
                message=generated,
                source="comms_learning/linkedin_examples.jsonl",
            ),
        ],
    )

    merged, summary = merge_comms_learning_examples(profile, [])

    assert [item.label for item in merged.weak_messages] == ["curated_weak"]
    assert summary.conflicts_pruned == 1
    assert summary.profile_updated is True


def test_incoming_gold_supersedes_previously_learned_negative() -> None:
    manual = "The connector work maps directly to my Hevo background. Is the team hiring?"
    profile = CommunicationStyleProfile(
        weak_messages=[
            StyleMessageExample(
                label="learned_negative_stale",
                message=manual,
                source="comms_learning/linkedin_examples.jsonl",
            )
        ]
    )

    merged, summary = merge_comms_learning_examples(
        profile,
        [{"label": "gold", "message": manual}],
    )

    assert [item.message for item in merged.strong_messages] == [manual]
    assert merged.weak_messages == []
    assert summary.strong_added == 1
    assert summary.conflicts_pruned == 1


def test_near_identical_incoming_negative_does_not_contradict_gold() -> None:
    gold = (
        "Thanks for connecting, Emiliano. Does that background fit product work there? "
        "Any recommendations on people I should talk to about that?"
    )
    negative = gold.replace(
        "recommendations on people I should talk to", "recs on who I should talk to"
    )

    merged, summary = merge_comms_learning_examples(
        CommunicationStyleProfile(),
        [
            {"label": "negative", "message": negative},
            {"label": "gold", "message": gold},
        ],
    )

    assert [item.message for item in merged.strong_messages] == [gold]
    assert merged.weak_messages == []
    assert summary.duplicates_skipped == 1


def test_prompt_guidance_uses_only_bounded_recipient_relevant_examples() -> None:
    profile = CommunicationStyleProfile(
        strong_messages=[
            StyleMessageExample(label="founder-1", recipient_type="founder", message="Founder strong one"),
            StyleMessageExample(label="founder-2", recipient_type="founder", message="Founder strong two"),
            StyleMessageExample(label="founder-3", recipient_type="founder", message="Founder strong three"),
            StyleMessageExample(label="general", recipient_type="general", message="General strong"),
            StyleMessageExample(label="engineer", recipient_type="engineer", message="Engineer strong"),
        ],
        weak_messages=[
            StyleMessageExample(
                label="founder-weak",
                recipient_type="founder",
                message="Founder weak message that is intentionally too long for the bound",
            ),
            StyleMessageExample(label="general-weak", recipient_type="general", message="General weak"),
            StyleMessageExample(label="recruiter-weak", recipient_type="recruiter", message="Recruiter weak"),
        ],
    )

    guidance = profile.prompt_guidance(
        "founder",
        max_strong_examples=2,
        max_weak_examples=1,
        max_example_chars=32,
    )

    assert "Founder strong one" in guidance
    assert "Founder strong two" in guidance
    assert "Founder strong three" not in guidance
    assert "General strong" not in guidance
    assert "Engineer strong" not in guidance
    assert "[founder-weak] Founder weak message that is..." in guidance
    assert "General weak" not in guidance
    assert "Recruiter weak" not in guidance


def test_learned_negative_example_blocks_near_duplicate_future_copy() -> None:
    profile = CommunicationStyleProfile(
        weak_messages=[
            StyleMessageExample(
                label="learned_negative",
                recipient_type="founder",
                message="Your work stood out and I would love to connect about product opportunities.",
            )
        ]
    )

    repeated = profile.review_message(
        "Your work stood out and I would love to connect about product opportunities!",
        "founder",
    )
    unrelated = profile.review_message(
        "Your launch maps to my data-platform work. Is the team hiring a product intern?",
        "founder",
    )

    assert repeated.verdict == "needs_review"
    assert repeated.weak_example_labels == ["learned_negative"]
    assert unrelated.verdict == "style_ok"


def test_sync_jsonl_updates_profile_once_and_ignores_outcome_recommendations(tmp_path) -> None:
    profile_path = tmp_path / "communication_style_profile.yml"
    profile_path.write_text(
        yaml.safe_dump(dump_style_profile(curated_profile()), sort_keys=False),
        encoding="utf-8",
    )
    corpus_dir = tmp_path / "comms_learning"
    corpus_dir.mkdir()
    examples_path = corpus_dir / "linkedin_examples.jsonl"
    examples_path.write_text(
        json.dumps(
            {
                "label": "gold",
                "message": "Manual message with a concrete product question.",
                "recipient_type": "founder",
            }
        )
        + "\n"
        + "{malformed json}\n"
        + json.dumps(
            {
                "label": "negative",
                "message": "I was impressed by your innovative company.",
                "recipient_type": "founder",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (corpus_dir / "outcome_learning.json").write_text(
        json.dumps(
            {
                "recommendations": [
                    {"action": "replace banned phrases", "value": "mutate the profile"}
                ]
            }
        ),
        encoding="utf-8",
    )

    first = sync_comms_learning_into_style_profile(
        profile_path=profile_path,
        examples_path=examples_path,
    )
    synced = load_style_profile(profile_path)
    second = sync_comms_learning_into_style_profile(
        profile_path=profile_path,
        examples_path=examples_path,
    )

    assert first.strong_added == 1
    assert first.weak_added == 1
    assert first.invalid_skipped == 1
    assert second.profile_updated is False
    assert second.duplicates_skipped == 2
    assert synced.banned_phrases == ["pick your brain"]
    assert synced.preferred_directness == "curated directness"
    assert synced.approved_asks_by_recipient_type == {"founder": ["Curated founder ask"]}
    assert len(synced.strong_messages) == 2
    assert len(synced.weak_messages) == 2


def test_load_comms_learning_examples_skips_malformed_rows(tmp_path) -> None:
    path = tmp_path / "examples.jsonl"
    path.write_text(
        json.dumps({"label": "silver", "message": "Approved message"})
        + "\nnot-json\n"
        + json.dumps(["not", "a", "mapping"])
        + "\n",
        encoding="utf-8",
    )

    assert load_comms_learning_examples(path) == [
        {"label": "silver", "message": "Approved message"}
    ]


def test_learned_positive_guides_followup_reply_and_email_drafts() -> None:
    profile = CommunicationStyleProfile(
        strong_messages=[
            StyleMessageExample(
                label="learned_gold_direct_fit",
                recipient_type="senior_product",
                message=(
                    "I'm exploring product work from an engineering background. "
                    "Does that background fit product work there? Any recs on who I should talk to?"
                ),
                source="comms_learning/linkedin_examples.jsonl",
            )
        ]
    )
    organization = OrganizationRecord(organization_id="org-acme", name="Acme")
    contact = ContactRecord(
        contact_id="ct-product",
        organization_id="org-acme",
        full_name="Priya Product",
        title="Senior Product Manager",
        email="priya@acme.example",
    )

    accepted = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-product",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Specific invite",
            }
        ],
        organizations=[organization],
        contacts=[contact],
        style_profile=profile,
    )[0]
    reply = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-product",
                "normalized_status": "replied",
                "latest_message": "Thanks for reaching out.",
                "last_sender": "Priya Product",
            }
        ],
        organizations=[organization],
        contacts=[contact],
        style_profile=profile,
    )[0]
    email = draft_track_2_email(
        organization=organization,
        contact=contact,
        campaign_action="send_cold_email",
        style_profile=profile,
    )

    assert "Does that background fit product work there?" in accepted["draft_message"]
    assert "Does that background fit anything at Acme?" in reply["draft_message"]
    assert "I'm not trying to send" in email["body"]
    for draft in (accepted, reply, email):
        assert draft["style_example_labels"] == ["learned_gold_direct_fit"]
        assert "learned_gold_direct_fit" in draft["style_guidance"]
        assert draft["style_transformations"]

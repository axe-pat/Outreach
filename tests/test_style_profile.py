from pathlib import Path

import pytest

from outreach.style_profile import CommunicationStyleProfile, load_style_profile, normalize_recipient_type


def test_load_style_profile_reads_approved_asks_and_examples() -> None:
    profile = load_style_profile(Path("workspace/communication_style_profile.yml"))

    assert profile.preferred_directness.startswith("direct")
    assert "would love to connect" in profile.banned_phrases
    assert profile.strong_messages[0].recipient_type == "engineer_india"
    assert any("hiring-team pointer" in ask for ask in profile.approved_asks_for("engineer_india"))


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("APM", "junior_product_apm"),
        ("Product", "senior_product"),
        ("Engineering India", "engineer_india"),
        ("Talent", "recruiter"),
        ("", "general"),
    ],
)
def test_normalize_recipient_type(raw: str, normalized: str) -> None:
    assert normalize_recipient_type(raw) == normalized


def test_review_message_flags_banned_phrases() -> None:
    profile = CommunicationStyleProfile(
        banned_phrases=["would love to connect", "pick your brain"],
        approved_asks_by_recipient_type={
            "senior_product": ["Ask a specific product/team-direction question."],
            "general": ["Ask for one concrete pointer."],
        },
    )

    review = profile.review_message(
        "Hi Alex, would love to connect and pick your brain.",
        recipient_type="Product",
    )

    assert review.verdict == "needs_review"
    assert review.banned_phrases == ["would love to connect", "pick your brain"]
    assert review.approved_asks == [
        "Ask a specific product/team-direction question.",
        "Ask for one concrete pointer.",
    ]


def test_prompt_guidance_includes_local_style_controls() -> None:
    profile = CommunicationStyleProfile(
        preferred_directness="specific and reply-oriented",
        preferred_casualness="warm",
        banned_phrases=["coffee chat"],
        self_intro_variants=["I'm a Marshall MBA and former engineer."],
        approved_asks_by_recipient_type={"founder": ["Ask where this background could be useful."]},
    )

    guidance = profile.prompt_guidance("founder")

    assert "specific and reply-oriented" in guidance
    assert "I'm a Marshall MBA and former engineer." in guidance
    assert "Ask where this background could be useful." in guidance
    assert "coffee chat" in guidance

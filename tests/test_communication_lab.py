from pathlib import Path

from outreach.communication_lab import build_communication_lab, review_email_craft, review_outreach_message


def test_review_email_craft_rewards_specific_human_email() -> None:
    body = (
        "Hi Sean,\n\n"
        "I know cold emails from MBA candidates usually blur together, so I'll make the reason specific.\n\n"
        "I'm a Marshall MBA and former data/platform engineer exploring technical product paths. "
        "The data/platform side maps cleanly to my Hevo, Gojek, and Intuit engineering background.\n\n"
        "The thing I'm trying to test is whether Synphony has a product or internship path where "
        "that mix is actually useful, or whether I'm forcing the fit. If it is directionally relevant, "
        "who should I ask? If not, a blunt no is genuinely useful too.\n\n"
        "Best,\nAkshat"
    )

    review = review_email_craft("Product fit at Synphony", body, company="Synphony")

    assert review.verdict in {"strong_send_candidate", "review"}
    assert review.score >= 88
    assert review.flags == []


def test_review_email_craft_flags_ai_slop() -> None:
    body = (
        "Hi Alex,\n\n"
        "I hope this email finds you well. I was impressed by your innovative company and would love "
        "to connect to pick your brain. I believe my unique blend of skills makes me a strong fit.\n\n"
        "Best,\nAkshat"
    )

    review = review_email_craft("Coffee chat", body, company="Snowflake")

    assert review.verdict == "needs_rewrite"
    assert any("Slop phrase" in flag for flag in review.flags)
    assert "Missing earned Akshat-specific background" in review.flags


def test_review_outreach_message_flags_linkedin_followup_slop() -> None:
    review = review_outreach_message(
        body="Thanks for connecting. I would love to connect and pick your brain.",
        channel="linkedin_followup",
        company="Snyk",
        recipient_type="engineer",
    )

    assert review.verdict == "needs_rewrite"
    assert review.recommended_action == "rewrite_before_send"
    assert "would love to connect" in review.banned_phrases
    assert "pick your brain" in review.banned_phrases


def test_review_outreach_message_flags_unsupported_callback() -> None:
    review = review_outreach_message(
        body=(
            "This is super helpful, thanks Bratee. The small-team/high-ownership + "
            "customer-feedback loop at TRACTIAN is exactly what I'm looking for. "
            "Do you think there's a PM/product internship path there?"
        ),
        channel="linkedin_followup",
        company="TRACTIAN",
        recipient_type="engineer",
        grounding_context=(
            "Honestly, I have no idea. I work as a developer for the marketing team, "
            "so mostly analysis work."
        ),
    )

    assert review.verdict == "needs_rewrite"
    assert any("Unsupported callback" in flag for flag in review.flags)


def test_review_outreach_message_accepts_specific_linkedin_followup() -> None:
    review = review_outreach_message(
        body=(
            "Thanks for connecting, Emiliano. I'm trying to get on the radar at Snyk "
            "for technical PM/product roles where my data/platform background helps. "
            "If you see a relevant opening, would you be open to referring me or pointing "
            "me to the right hiring contact?"
        ),
        channel="linkedin_followup",
        company="Snyk",
        recipient_type="engineer",
    )

    assert review.verdict in {"strong_send_candidate", "review"}
    assert review.score >= 80
    assert not review.banned_phrases


def test_review_outreach_message_flags_senior_referral_mismatch() -> None:
    review = review_outreach_message(
        body=(
            "Thanks for connecting, Emiliano. I'm trying to get on the radar at Snyk for PM/product roles. "
            "If I send a tight resume + 3-line blurb, would you be open to pointing me to the right referral path "
            "or hiring contact?"
        ),
        channel="linkedin_followup",
        company="Snyk",
        recipient_type="engineer",
        recipient_title="Principal Software Engineer at Snyk",
    )

    assert review.verdict == "needs_rewrite"
    assert any("Seniority mismatch" in flag for flag in review.flags)
    assert any("one simple product-fit" in guidance for guidance in review.rewrite_guidance)


def test_review_outreach_message_flags_generic_company_insight() -> None:
    review = review_outreach_message(
        body=(
            "Thanks for connecting, Anirudh. I'm exploring product/operator paths where my engineering + "
            "Marshall background can be useful. Tessera Labs feels like an early team where product, ops, "
            "and execution sit close. Would love your perspective on whether my background could translate "
            "to what you're building."
        ),
        channel="linkedin_followup",
        company="Tessera Labs",
        recipient_type="founder",
        recipient_title="Founder",
    )

    assert review.verdict != "strong_send_candidate"
    assert "generic_insight" in review.quality_labels
    assert any("specific product" in guidance for guidance in review.rewrite_guidance)


def test_build_communication_lab_includes_coffee_dump_and_story_sources() -> None:
    lab = build_communication_lab(
        workspace=Path("workspace"),
        repo_root=Path("."),
        resume_root=Path("../ResumeGenerator v1"),
    )

    source_types = {source["source_type"] for source in lab["source_summary"]}

    assert "gold_user_written_coffee_chat" in source_types
    assert "silver_sent_outreach" in source_types
    assert "story_material" in source_types
    assert any("Specific beats polished" in principle for principle in lab["stellar_email_principles"])

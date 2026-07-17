import re

import pytest

from outreach.services.notes import NOTE_CHAR_LIMIT, NoteGenerator


BAD_CONTEXTUAL_NOTE_PATTERNS = [
    r"\byour engineering work stood out\b",
    r"\byour .* work stood out\b",
    r"\bthe company direction feels\b",
    r"\bAI product direction\b",
    r"\bproduct/company direction\b",
    r"\bPM/product roles feels\b",
    r"\btechnical startup where my builder background\b",
]


def assert_contextual_note_quality(note_text: str, company: str) -> None:
    lower = note_text.lower()
    assert len(note_text) <= NOTE_CHAR_LIMIT
    assert company.lower() in note_text[:140].lower()
    assert "deep in product for a while now" in lower
    assert not re.search(r"\b(transition|pivot|moving toward|making the shift)\b", lower)
    assert not re.search(r"\b(referral|hiring contact|right person)\b", lower)
    for pattern in BAD_CONTEXTUAL_NOTE_PATTERNS:
        assert not re.search(pattern, note_text, flags=re.I), pattern


def test_generates_usc_note_with_limit() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Stephanie Stillman Amezquita",
            "role_bucket": "Product",
            "usc": True,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        company="Snowflake",
        company_mode="big_company",
    )

    assert note.family == "usc"
    assert note.ask_style == "guidance"
    assert note.within_limit is True
    assert note.length <= NOTE_CHAR_LIMIT
    assert "Fight On!" in note.text
    assert "Snowflake" in note.text


def test_generates_existing_connection_note() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Devesh Joshi",
            "role_bucket": "Engineering",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": True,
            "shared_history": False,
        },
        company="Snowflake",
    )

    assert note.family == "existing_connection"
    assert note.ask_style == "direct_help"
    assert note.within_limit is True
    assert note.length <= NOTE_CHAR_LIMIT
    assert any(phrase in note.text.lower() for phrase in ["reconnect", "stay in touch", "keep in touch"])


def test_generates_product_note_under_limit() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Diana W.",
            "role_bucket": "Product",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        company="Snowflake",
        company_mode="startup",
    )

    assert note.family == "product"
    assert note.ask_style == "conversation"
    assert note.within_limit is True
    assert note.length <= NOTE_CHAR_LIMIT


def test_company_name_trailing_period_does_not_double_punctuate() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Diana W.",
            "role_bucket": "Engineering",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        company="Splash Inc.",
        company_mode="startup",
    )

    assert "Splash Inc.." not in note.text


def test_quality_check_marks_good_usc_note_sendable() -> None:
    generator = NoteGenerator()
    candidate = {
        "name": "Saisrinath Narra",
        "role_bucket": "Engineering",
        "usc": True,
        "usc_marshall": False,
        "existing_connection": False,
        "shared_history": False,
    }

    note = generator.generate(candidate, company="Snowflake")
    qc = generator.quality_check(candidate, note)

    assert qc.verdict == "send"
    assert qc.score >= 70


def test_big_company_mode_pushes_guidance_style() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Diana W.",
            "role_bucket": "Product",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": True,
        },
        company="Microsoft",
        company_mode="big_company",
    )

    assert note.ask_style == "guidance"


def test_shared_history_note_names_matched_company() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Suman Sundaresh",
            "role_bucket": "Product",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": True,
            "shared_history_signals": ["Intuit"],
            "title": "Lead Product Manager @ Pebl | Ex-Intuit, Rappi, PayPal",
        },
        company="Pebl",
        company_mode="startup",
    )

    assert note.family == "shared_history"
    assert "Intuit" in note.text
    assert "shared background" not in note.text
    assert note.within_limit is True


def test_thapar_shared_history_beats_trojan_template() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Bhavleen Kaur Ahuja",
            "role_bucket": "Product",
            "usc": False,
            "usc_marshall": True,
            "existing_connection": False,
            "shared_history": True,
            "shared_history_signals": ["Thapar", "Thaparian"],
            "snippet": "Deepak Garg, Sanjay Gupta Thaparian & 132 other mutual connections",
        },
        company="Cisco",
        company_mode="big_company",
    )

    assert note.family == "shared_history"
    assert "Thapar" in note.text
    # usc_marshall is also flagged, so the note should name both affinities
    # instead of burying Thapar under a generic Trojan template.
    assert "USC" in note.text or "Trojan" in note.text
    assert note.within_limit is True


def test_thapar_shared_history_beats_existing_connection_template() -> None:
    generator = NoteGenerator()
    candidate = {
        "name": "Bhavleen Kaur Ahuja",
        "role_bucket": "Product",
        "usc": True,
        "usc_marshall": True,
        "existing_connection": True,
        "shared_history": True,
        "shared_history_signals": ["Thapar"],
        "title": "AI Product Manager 2",
    }
    note = generator.generate(candidate, company="Cisco", company_mode="big_company")
    quality = generator.quality_check(candidate, note)

    assert note.family == "shared_history"
    assert "Thapar" in note.text
    assert "confirmed Thapar connection was dropped from the invite" not in quality.flags


def test_thapar_plus_usc_note_acknowledges_both_affinities() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Bhavleen Kaur Ahuja",
            "role_bucket": "Product",
            "usc": True,
            "usc_marshall": True,
            "existing_connection": False,
            "shared_history": True,
            "shared_history_signals": ["Thapar"],
            "title": "AI Product Manager 2",
        },
        company="Cisco",
        company_mode="big_company",
    )

    assert note.family == "shared_history"
    assert "Thapar" in note.text
    assert "USC" in note.text or "Trojan" in note.text
    assert note.within_limit is True


def test_quality_check_flags_thapar_drop() -> None:
    generator = NoteGenerator()
    candidate = {
        "name": "Fellow Alum",
        "shared_history": True,
        "shared_history_signals": ["Thapar"],
        "usc": True,
        "role_bucket": "Product",
        "title": "Product Manager",
    }
    note = generator.generate(candidate, company="Adobe", company_mode="big_company")
    # Force a Trojan-style note that omits Thapar.
    note.text = (
        "Hi Fellow, fellow Trojan here — Marshall MBA exploring PM roles at Adobe. "
        "Open to connecting?"
    )
    note.length = len(note.text)
    quality = generator.quality_check(candidate, note)
    assert "confirmed Thapar connection was dropped from the invite" in quality.flags


def test_senior_product_invite_is_warm_and_saves_the_ask_for_followup() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Natalie Abeysena",
            "title": "Head of Product, AI Platform",
            "role_bucket": "Product",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        company="Deepgram",
        company_mode="startup",
        note_context={
            "opportunity_titles": ["Product Management Intern, Summer 2026"],
            "tags": ["ai", "developer tools"],
            "description": "Voice AI platform for developers building speech products.",
        },
    )

    assert note.family == "senior_product_contribution"
    assert note.within_limit is True
    assert note.length <= NOTE_CHAR_LIMIT
    assert "deep in product for a while now" in note.text
    assert "Would be great to connect." in note.text
    assert "referral" not in note.text.lower()
    assert "quick read on fit" not in note.text.lower()
    assert "Deepgram" in note.text.split(".", 1)[0]


def test_india_based_engineer_invite_defers_referral_ask_to_followup() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Arjun Rao",
            "title": "Senior Software Engineer",
            "location": "Bengaluru, Karnataka, India",
            "role_bucket": "Engineering",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        company="1Password",
        company_mode="big_company",
        note_context={
            "opportunity_titles": ["Product Management Intern, Device Trust - Fall 2026"],
        },
    )

    assert note.family == "engineering_referral"
    assert note.ask_style == "referral"
    assert note.within_limit is True
    assert "referral" not in note.text.lower()
    assert "hiring team" not in note.text.lower()
    assert "deep in product for a while now" in note.text.lower()
    assert "connect" in note.text.lower()


def test_founder_note_uses_company_interest_and_product_credibility() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Michael Chen",
            "title": "Co-Founder and CEO",
            "role_bucket": "Founder",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        company="Yondu",
        company_mode="startup",
        note_context={
            "tags": ["robotics", "logistics", "ai"],
            "description": "Robotics workforce for logistics automation.",
        },
    )

    assert note.family == "founder_builder_fit"
    assert note.within_limit is True
    assert "deep in product for a while now" in note.text.lower()
    assert "connect" in note.text.lower()
    assert "referral" not in note.text.lower()
    assert "Yondu" in note.text.split(".", 1)[0]


def test_engineering_context_note_starts_with_company_and_established_product_identity() -> None:
    generator = NoteGenerator()
    note = generator.generate(
        {
            "name": "Roshni Ramakrishnan",
            "title": "Senior Software Engineer | ex-Gemini | ex-AWS",
            "role_bucket": "Engineering",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        company="WorkWhile",
        company_mode="startup",
        note_context={
            "tags": ["artificial intelligence", "hr tech", "machine learning"],
            "description": "Labor platform helping businesses fill shifts and reduce no-shows.",
        },
    )

    first_sentence = note.text.split(".", 1)[0]
    lower = note.text.lower()
    assert "WorkWhile" in first_sentence
    assert "deep in product for a while now" in lower
    assert "your engineering work stood out" not in lower
    assert "ai product direction" not in lower
    assert "company direction" not in lower
    assert "roles feels" not in lower


@pytest.mark.parametrize(
    "story_context",
    [
        {
            "story_fit_reason": (
                "Hevo experience gives a direct story around connectors, ETL, integrations, "
                "and how data teams adopt infrastructure tooling."
            )
        },
        {"profile_evidence": "Hevo Data engineering; connector and ETL product familiarity."},
        {"why_this_company": "My Hevo connector and ETL experience is directly relevant."},
        {
            "private_outreach_context": {
                "earned_anchor": "Hevo Data engineering",
                "scenario": "connectors, ETL, and data movement",
            }
        },
    ],
)
def test_airbyte_story_fit_context_uses_relevant_domain_without_unexplained_employer(
    story_context: dict,
) -> None:
    note = NoteGenerator().generate(
        {
            "name": "Francis Genet",
            "linkedin_url": "https://www.linkedin.com/in/francisgenet",
            "title": "Engineering Manager",
            "role_bucket": "Engineering",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        company="Airbyte",
        company_mode="startup",
        note_context={
            "tags": ["data-infrastructure", "data-platform", "developer-tools"],
            "description": "Open data movement and connector platform.",
            **story_context,
        },
    )

    assert note.family == "engineering_product_bridge"
    assert note.within_limit is True
    assert note.length <= 270
    assert "data/platform engineering" in note.text
    assert "deep in product for a while now" in note.text
    assert "Hevo" not in note.text
    assert "platform work connects with systems I've built before" not in note.text


@pytest.mark.parametrize(
    ("case_name", "company", "company_mode", "candidate", "note_context", "expected_family", "expected_phrases"),
    [
        (
            "founder_without_specific_company_context",
            "Zenyt.ai",
            "startup",
            {
                "name": "Raphael Rozenblum",
                "title": "Zenyt.ai co-founder and CTO | ex Amazon",
                "role_bucket": "Founder",
                "usc": False,
                "usc_marshall": False,
                "existing_connection": False,
                "shared_history": False,
            },
            {"opportunity_titles": ["Product Owner Internship"]},
            "founder_builder_fit",
            ["deep in product", "connect"],
        ),
        (
            "founder_with_specific_company_context",
            "Synphony",
            "startup",
            {
                "name": "Sean Wu",
                "title": "ex-NVIDIA AI | CEO @ Synphony",
                "role_bucket": "Founder",
                "usc": False,
                "usc_marshall": False,
                "existing_connection": False,
                "shared_history": False,
            },
            {
                "tags": ["robotics", "agriculture", "saas"],
                "description": "Strawberry picking robots, bed-level analytics, data pipeline integration, and robotics services.",
            },
            "founder_builder_fit",
            ["Synphony", "deep in product", "connect"],
        ),
        (
            "engineer_pointer",
            "WorkWhile",
            "startup",
            {
                "name": "Roshni Ramakrishnan",
                "title": "Senior Software Engineer | ex-Gemini | ex-AWS",
                "role_bucket": "Engineering",
                "usc": False,
                "usc_marshall": False,
                "existing_connection": False,
                "shared_history": False,
            },
            {
                "tags": ["artificial intelligence", "hr tech"],
                "description": "Labor platform helping businesses fill shifts and reduce no-shows.",
            },
            "engineering_product_bridge",
            ["deep in product", "connect"],
        ),
        (
            "india_engineer_referral",
            "Snyk",
            "big_company",
            {
                "name": "Mehak Singh",
                "title": "Associate Software Engineer at Snyk",
                "location": "Haryana, India",
                "role_bucket": "Engineering",
                "usc": False,
                "usc_marshall": False,
                "existing_connection": False,
                "shared_history": False,
            },
            {
                "tags": ["security", "developer tools"],
                "description": "Developer-security platform for secure software delivery.",
            },
            "engineering_referral",
            ["deep in product", "connect"],
        ),
        (
            "senior_product_contribution",
            "Deepgram",
            "startup",
            {
                "name": "Natalie Abeysena",
                "title": "AI Product Leader",
                "role_bucket": "Product",
                "usc": False,
                "usc_marshall": False,
                "existing_connection": False,
                "shared_history": False,
            },
            {
                "tags": ["voice ai", "developer tools"],
                "description": "Voice AI platform for developers building speech products.",
                "opportunity_titles": ["Product Management Intern, Summer 2026"],
            },
            "senior_product_contribution",
            ["deep in product", "Deepgram", "connect"],
        ),
        (
            "operator_contribution",
            "Zenyt.ai",
            "startup",
            {
                "name": "Tristan Turon-Barrere",
                "title": "Operations | UCSB",
                "role_bucket": "Adjacent",
                "usc": False,
                "usc_marshall": False,
                "existing_connection": False,
                "shared_history": False,
            },
            {"opportunity_titles": ["Product Owner Internship"]},
            "operator_contribution",
            ["PM/product roles", "connect"],
        ),
        (
            "cloud_agent_workspace",
            "Endstack",
            "startup",
            {
                "name": "Sam Park",
                "title": "Co-founder, CEO @ Endstack | YC F24",
                "role_bucket": "Founder",
                "usc": False,
                "usc_marshall": False,
                "existing_connection": False,
                "shared_history": False,
            },
            {
                "tags": ["generative-ai", "developer-tools", "ai-assistant"],
                "description": "Desktop OS and cloud desktop workspace for humans and agents running in the cloud.",
            },
            "founder_builder_fit",
            ["Endstack", "deep in product", "connect"],
        ),
    ],
)
def test_contextual_notes_match_real_world_quality_bar(
    case_name: str,
    company: str,
    company_mode: str,
    candidate: dict,
    note_context: dict,
    expected_family: str,
    expected_phrases: list[str],
) -> None:
    note = NoteGenerator().generate(
        candidate,
        company=company,
        company_mode=company_mode,
        note_context=note_context,
    )

    assert note.family == expected_family, case_name
    assert note.within_limit is True
    assert_contextual_note_quality(note.text, company)
    for phrase in expected_phrases:
        assert phrase.lower() in note.text.lower(), f"{case_name}: missing {phrase!r} in {note.text!r}"


def test_warm_school_note_mentions_company_early() -> None:
    note = NoteGenerator().generate(
        {
            "name": "Alden Chan",
            "title": "Founding Finance @ Synphony (YC P26) | USC Marshall",
            "role_bucket": "Other",
            "usc": True,
            "usc_marshall": True,
            "existing_connection": False,
            "shared_history": False,
        },
        company="Synphony",
        company_mode="startup",
    )

    assert note.family == "usc_marshall"
    assert note.within_limit is True
    assert_contextual_note_quality(note.text, "Synphony")
    assert "Fight On!" in note.text


def test_contextual_batch_avoids_failed_send_shapes() -> None:
    generator = NoteGenerator()
    company = "WorkWhile"
    note_context = {
        "tags": ["artificial intelligence", "hr tech", "machine learning"],
        "description": "Labor platform helping businesses fill shifts, improve fill rates, and reduce no-shows.",
    }
    candidates = [
        {
            "name": "Roshni Ramakrishnan",
            "title": "Senior Software Engineer | ex-Gemini | ex-AWS",
            "role_bucket": "Engineering",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        {
            "name": "Owen Crook",
            "title": "Software Engineer @ WorkWhile",
            "role_bucket": "Engineering",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
        {
            "name": "Nikhil Bhagwat",
            "title": "Product @ WorkWhile | McK, MIT Sloan & Harvard Kennedy alum",
            "role_bucket": "Product",
            "usc": False,
            "usc_marshall": False,
            "existing_connection": False,
            "shared_history": False,
        },
    ]

    annotated = generator.generate_batch(
        candidates,
        company=company,
        company_mode="startup",
        note_context=note_context,
    )

    assert len(annotated) == len(candidates)
    signatures = {generator._note_signature(item["note"]) for item in annotated}
    assert len(signatures) >= 2
    for item in annotated:
        assert_contextual_note_quality(item["note"], company)
        assert item["note_qc"]["verdict"] == "send"


def test_batch_generation_adds_qc_payload() -> None:
    generator = NoteGenerator()
    annotated = generator.generate_batch(
        [
            {
                "name": "Devesh Joshi",
                "role_bucket": "Engineering",
                "usc": False,
                "usc_marshall": False,
                "existing_connection": True,
                "shared_history": False,
            }
        ],
        company="Snowflake",
    )

    assert len(annotated) == 1
    assert "note" in annotated[0]
    assert "note_qc" in annotated[0]
    assert annotated[0]["note_qc"]["verdict"] in {"send", "blocked"}
    assert annotated[0]["style_review"]["verdict"] in {"style_ok", "needs_review"}


def test_quality_check_blocks_local_style_banned_phrases() -> None:
    generator = NoteGenerator()
    candidate = {
        "name": "Alex Doe",
        "role_bucket": "Product",
        "usc": False,
        "usc_marshall": False,
        "existing_connection": False,
        "shared_history": False,
    }

    qc = generator.quality_check(
        candidate,
        generated=type(
            "Generated",
            (),
            {
                "text": "Hi Alex, would love to connect and pick your brain about Product at Snowflake.",
                "length": 78,
                "family": "product",
            },
        )(),
    )

    assert qc.verdict == "blocked"
    assert any("would love to connect" in flag for flag in qc.flags)


def test_batch_qc_penalizes_repeated_wording() -> None:
    generator = NoteGenerator()
    candidate = {
        "name": "Saisrinath Narra",
        "role_bucket": "Engineering",
        "usc": True,
        "usc_marshall": False,
        "existing_connection": False,
        "shared_history": False,
    }
    note = generator.generate(candidate, company="Snowflake")
    qc = generator.quality_check(candidate, note, recent_notes=[note.text, note.text])

    assert "Repeated note wording in batch" in qc.flags
    assert qc.score < 100


def test_quality_check_blocks_note_without_clear_ask() -> None:
    generator = NoteGenerator()
    candidate = {
        "name": "Alex Doe",
        "role_bucket": "Other",
        "usc": False,
        "usc_marshall": False,
        "existing_connection": False,
        "shared_history": False,
    }

    qc = generator.quality_check(
        candidate,
        generated=type("Generated", (), {"text": "Hi Alex, liked your background at Snowflake.", "length": 48, "family": "general"})(),
    )

    assert qc.verdict == "blocked"
    assert "Ask is not clear" in qc.flags


def test_note_signature_normalizes_name_and_company() -> None:
    generator = NoteGenerator()
    a = "Hi Diana, fellow Trojan here. I'm exploring PM roles at Snowflake. Fight On!"
    b = "Hi Sonal, fellow Trojan here. I'm exploring PM roles at Snowflake. Fight On!"

    assert generator._note_signature(a) == generator._note_signature(b)


def test_normalize_outbound_punctuation_strips_dashes() -> None:
    from outreach.ai_messaging import normalize_outbound_punctuation

    assert (
        normalize_outbound_punctuation("a fellow Thaparian and Trojan — small world!")
        == "a fellow Thaparian and Trojan - small world!"
    )
    assert normalize_outbound_punctuation("teams of 100–200 people") == "teams of 100-200 people"
    assert normalize_outbound_punctuation("no dashes here") == "no dashes here"


def test_generated_notes_never_contain_em_dashes() -> None:
    generator = NoteGenerator()
    candidate = {
        "name": "Fellow Alum",
        "shared_history": True,
        "shared_history_signals": ["Thapar"],
        "usc": True,
        "role_bucket": "Product",
        "title": "Product Manager",
    }
    for _ in range(6):
        note = generator.generate(candidate, company="Adobe", company_mode="big_company")
        assert "\u2014" not in note.text
        assert "\u2013" not in note.text


def test_quality_check_flags_em_dash() -> None:
    generator = NoteGenerator()
    candidate = {"name": "Sam", "role_bucket": "Product", "title": "PM"}
    note = generator.generate(candidate, company="Adobe", company_mode="big_company")
    note.text = "Hi Sam, exploring PM roles at Adobe — open to connecting?"
    note.length = len(note.text)
    quality = generator.quality_check(candidate, note)
    assert any("Em/en dash" in flag for flag in quality.flags)


def test_quality_check_accepts_warm_close_and_blocks_pitch_asks_in_invites() -> None:
    generator = NoteGenerator()
    candidate = {
        "name": "Zack Tanner",
        "usc": True,
        "role_bucket": "Product",
        "title": "Product Manager",
    }
    note = generator.generate(candidate, company="Vercel", company_mode="default")
    warm = (
        "Hi Zack, fellow Trojan here. I've been interested in Vercel for a while. "
        "I've been deep in product for a while now. Would be great to connect. Fight On!"
    )
    note.text = warm
    note.length = len(warm)
    quality = generator.quality_check(candidate, note)
    assert quality.verdict == "send", quality.flags

    pitched = (
        "Hi Zack, fellow Trojan here. I'm looking at product roles at Vercel. "
        "Does my background fit, and who should I talk to? Fight On!"
    )
    note.text = pitched
    note.length = len(pitched)
    quality = generator.quality_check(candidate, note)
    assert quality.verdict == "blocked"
    assert "Substantive ask belongs in the follow-up" in quality.flags


def test_quality_check_blocks_transition_language_and_premature_fall_ask() -> None:
    generator = NoteGenerator()
    candidate = {"name": "Alex Doe", "role_bucket": "Product", "title": "Product Manager"}
    note = generator.generate(candidate, company="Acme")
    note.text = (
        "Hi Alex, I'm transitioning into product and looking for a fall product internship "
        "at Acme. Would be great to connect."
    )
    note.length = len(note.text)

    quality = generator.quality_check(candidate, note)

    assert quality.verdict == "blocked"
    assert "Frames Akshat as transitioning into product" in quality.flags
    assert "Fall internship ask belongs in the follow-up" in quality.flags


def test_generate_batch_keeps_template_note_when_ai_note_fails_qc() -> None:
    class _RegressingAI:
        def compose(self, request):
            from outreach.ai_messaging import AIMessageResult

            # An "ask" QC cannot recognize: no ask vocabulary at all.
            return AIMessageResult(
                message="Hi there, Vercel is doing interesting infrastructure work these days.",
                subject="",
                model="test-model",
                used_ai=True,
                attempts=1,
                status="composed",
            )

    generator = NoteGenerator(ai_messaging=_RegressingAI())
    candidates = [
        {
            "name": "Zack Tanner",
            "usc": True,
            "role_bucket": "Product",
            "title": "Product Manager",
            "linkedin_url": "https://www.linkedin.com/in/zack",
            "score": 82,
        }
    ]
    annotated = generator.generate_batch(candidates, company="Vercel", company_mode="default")

    item = annotated[0]
    assert item["note_qc"]["verdict"] == "send", item["note_qc"]["flags"]
    assert any("kept the sendable template note" in flag for flag in item["note_qc"]["flags"])
    assert "interesting infrastructure work these days" not in item["note"]

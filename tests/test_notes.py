from outreach.services.notes import NOTE_CHAR_LIMIT, NoteGenerator


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


def test_senior_product_note_uses_contribution_fit_ending() -> None:
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
    assert any(
        phrase in note.text
        for phrase in [
            "where my engineering + PM background could be useful",
            "project areas the product team is most excited about",
            "what tends to matter most to the product team",
        ]
    )
    assert "Deepgram" in note.text.split(".", 1)[0]


def test_india_based_engineer_note_asks_for_referral_or_pointer() -> None:
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
    assert "referral" in note.text.lower()
    assert any(phrase in note.text.lower() for phrase in ["pointer", "hiring-team", "hiring team"])


def test_founder_note_uses_builder_fit_instead_of_generic_connection() -> None:
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
    assert any(phrase in note.text.lower() for phrase in ["builder", "operator", "useful", "team grows"])
    assert "Yondu" in note.text.split(".", 1)[0]


def test_engineering_context_note_starts_with_identity_and_company() -> None:
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
    assert "marshall mba" in lower or "usc marshall" in lower
    assert "your engineering work stood out" not in lower
    assert "ai product direction" not in lower
    assert "company direction" not in lower
    assert "roles feels" not in lower


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

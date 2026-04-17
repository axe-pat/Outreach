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

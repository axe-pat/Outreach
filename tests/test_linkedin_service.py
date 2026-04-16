from outreach.services.linkedin import normalize_typeahead_text, score_typeahead_option


def test_normalize_typeahead_text_collapses_whitespace_and_case() -> None:
    assert normalize_typeahead_text("  Santa   Clara University ") == "santa clara university"


def test_score_typeahead_option_prefers_exact_company_match_over_university() -> None:
    requested = "Clara"

    clara_score = score_typeahead_option("Add a company", requested, "Clara")
    santa_clara_score = score_typeahead_option("Add a company", requested, "Santa Clara University")

    assert clara_score > santa_clara_score
    assert santa_clara_score < 0


def test_score_typeahead_option_prefers_school_for_school_trigger() -> None:
    scu_score = score_typeahead_option("Add a school", "Santa Clara", "Santa Clara University")
    startup_score = score_typeahead_option("Add a school", "Santa Clara", "Santa Clara Health")

    assert scu_score > startup_score

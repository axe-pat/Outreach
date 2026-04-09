from outreach.cli import infer_role_bucket, pass_relevance, resolve_pass_definitions
from outreach.config import OutreachSettings


def test_tpm_titles_bucket_as_product() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Principal TPM | Enterprise & Product Security",
        "Principal TPM | Enterprise & Product Security",
        settings,
    )

    assert bucket == "Product"


def test_university_recruiter_gets_separate_bucket() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Campus Recruiter",
        "Campus Recruiter USC Marshall School of Business Career Center",
        settings,
    )

    assert bucket == "University Recruiting"


def test_solution_engineer_buckets_as_adjacent() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Senior Solution Engineer at Snowflake",
        "Senior Solution Engineer at Snowflake",
        settings,
    )

    assert bucket == "Adjacent"


def test_product_pass_rejects_non_product_noise() -> None:
    assert not pass_relevance(
        "product_usc_marshall",
        "Other",
        "Technology & Strategy leader",
        "Technology & Strategy leader",
    )


def test_engineering_pass_rejects_solution_engineer_noise() -> None:
    assert not pass_relevance(
        "engineering_usc_marshall",
        "Adjacent",
        "Senior Solution Engineer at Snowflake",
        "Senior Solution Engineer at Snowflake",
    )


def test_marshall_passes_disabled_by_default() -> None:
    settings = OutreachSettings()

    assert settings.search.pass_definitions["product_usc_marshall"]["enabled"] is False
    assert settings.search.pass_definitions["engineering_usc_marshall"]["enabled"] is False


def test_broad_fallback_is_small_and_conditional() -> None:
    settings = OutreachSettings()
    broad = settings.search.pass_definitions["broad_fallback"]

    assert broad["limit"] == 6
    assert broad["run_if_below_pool_size"] == 18


def test_enable_marshall_turns_marshall_passes_on() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(settings, enable_marshall=True)

    assert passes["product_usc_marshall"]["enabled"] is True
    assert passes["engineering_usc_marshall"]["enabled"] is True


def test_include_pass_only_runs_selected_passes() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(
        settings,
        include_passes=("existing_connections", "product_network"),
    )

    assert passes["existing_connections"]["enabled"] is True
    assert passes["product_network"]["enabled"] is True
    assert passes["product_usc"]["enabled"] is False


def test_force_broad_fallback_removes_pool_gate() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(settings, force_broad_fallback=True)

    assert passes["broad_fallback"]["enabled"] is True
    assert "run_if_below_pool_size" not in passes["broad_fallback"]

from datetime import date
from pathlib import Path

from outreach.cli import (
    build_linkedin_company_queue_items,
    build_organization_intel_items,
    build_target_action_queue_items,
    classify_opportunity_action,
    extract_team_size_from_notes,
    extract_tags_from_notes,
    extract_description_from_notes,
    filter_discovered_items,
    fit_band_from_score,
    format_team_size_signal,
    infer_role_bucket,
    infer_fit_reasons,
    item_matches_remote,
    item_matches_tags,
    normalize_tag,
    parse_notes_metadata,
    parse_batch_year,
    parse_team_size_headcount,
    pass_relevance,
    resolve_pass_definitions,
    score_opportunity_relevance,
    text_contains_signal,
)
from outreach.config import OutreachSettings
from outreach.resume_jobs_bridge import CompanyOverride, ResumeJob, build_resume_outreach_queue
from outreach.tracking import ContactRecord, OpportunityRecord, OrganizationRecord, OrganizationType, SourceKind, TouchpointRecord


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


def test_relative_linkedin_profile_is_treated_as_fallback() -> None:
    settings = OutreachSettings(linkedin_chrome_user_data_dir=Path("playwright/chrome-data"))

    assert settings.using_fallback_linkedin_profile() is True


def test_absolute_linkedin_profile_is_explicit_even_if_it_points_to_outreach_profile() -> None:
    settings = OutreachSettings(linkedin_chrome_user_data_dir=Path.cwd() / "playwright" / "chrome-data")

    assert settings.using_fallback_linkedin_profile() is False
    settings.validate_explicit_linkedin_profile()


def test_parse_team_size_headcount_handles_commas() -> None:
    assert parse_team_size_headcount("1,600 employees") == 1600


def test_parse_batch_year_extracts_year() -> None:
    assert parse_batch_year("S2024") == 2024
    assert parse_batch_year("Spring 2026") == 2026


def test_normalize_tag_handles_dash_and_spacing() -> None:
    assert normalize_tag("Generative-AI") == "generative ai"


def test_item_matches_remote_uses_company_and_opportunity_location() -> None:
    assert item_matches_remote({"location": "Fully Remote", "opportunities": []}) is True
    assert item_matches_remote({"location": "Los Angeles", "opportunities": [{"location": "Remote"}]}) is True
    assert item_matches_remote({"location": "Los Angeles", "opportunities": []}) is False


def test_item_matches_tags_supports_partial_match() -> None:
    item = {"tags": ["artificial intelligence", "robotics"]}

    assert item_matches_tags(item, ("ai",)) is False
    assert item_matches_tags(item, ("robot",)) is True
    assert item_matches_tags(item, ("artificial-intelligence",)) is True


def test_extract_team_size_from_notes_reads_discovery_note() -> None:
    assert extract_team_size_from_notes("batch=Spring 2026 | team_size=12 employees | tags=ai") == 12


def test_parse_notes_metadata_extracts_structured_fields() -> None:
    notes = "batch=Spring 2026 | founded_year=2024 | tags=ai,robotics | description=Builds AI systems"

    metadata = parse_notes_metadata(notes)

    assert metadata["batch"] == "Spring 2026"
    assert metadata["founded_year"] == "2024"
    assert extract_tags_from_notes(notes) == ["ai", "robotics"]
    assert extract_description_from_notes(notes) == "Builds AI systems"


def test_text_contains_signal_avoids_short_keyword_false_positive() -> None:
    assert text_contains_signal("autonomous aircraft platform", "ai") is False
    assert text_contains_signal("artificial intelligence platform", "ai") is False
    assert text_contains_signal("ai platform", "ai") is True


def test_format_team_size_signal_adds_employee_suffix_for_bare_numbers() -> None:
    assert format_team_size_signal("16") == "16 employees"
    assert format_team_size_signal("150 Employees") == "150 Employees"


def test_filter_discovered_items_applies_jobs_size_and_batch_filters() -> None:
    items = [
        {"organization_name": "OlderCo", "jobs_url": "", "team_size": "50 employees", "batch": "W2020"},
        {"organization_name": "HiringCo", "jobs_url": "https://example.com/jobs", "team_size": "12 employees", "batch": "S2025"},
        {"organization_name": "BigCo", "jobs_url": "https://example.com/jobs", "team_size": "500 employees", "batch": "S2025"},
    ]

    filtered = filter_discovered_items(
        items,
        require_jobs_url=True,
        max_team_size=100,
        min_batch_year=2024,
    )

    assert [item["organization_name"] for item in filtered] == ["HiringCo"]


def test_filter_discovered_items_applies_remote_and_tag_filters() -> None:
    items = [
        {
            "organization_name": "RemoteAI",
            "jobs_url": "https://example.com/jobs",
            "team_size": "20 employees",
            "batch": "S2025",
            "location": "Fully Remote",
            "tags": ["artificial intelligence"],
            "opportunities": [],
        },
        {
            "organization_name": "OfficeRobotics",
            "jobs_url": "https://example.com/jobs",
            "team_size": "20 employees",
            "batch": "S2025",
            "location": "Los Angeles",
            "tags": ["robotics"],
            "opportunities": [],
        },
    ]

    filtered = filter_discovered_items(
        items,
        require_jobs_url=True,
        max_team_size=100,
        min_batch_year=2024,
        remote_only=True,
        include_tags=("artificial intelligence",),
    )

    assert [item["organization_name"] for item in filtered] == ["RemoteAI"]


def test_build_linkedin_company_queue_prioritizes_unworked_hiring_startups() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-mount",
            name="Mount",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup;sf;hiring",
            status="Researching",
            source_kind=SourceKind.YC_DIRECTORY,
            notes="batch=Spring 2026 | team_size=2 employees | tags=insurance,ai",
        ),
        OrganizationRecord(
            organization_id="org-doodle",
            name="Doodle Labs",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in;la;companies",
            status="Researching",
            source_kind=SourceKind.STARTUP_DIRECTORY,
            notes="team_size=50 Employees | tags=robotics",
        ),
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-mount",
            organization_id="org-mount",
            title="Founding AI Engineer",
        ),
        OpportunityRecord(
            opportunity_id="opp-doodle",
            organization_id="org-doodle",
            title="Marketing Designer",
        ),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-doodle",
            organization_id="org-doodle",
            full_name="Existing Contact",
        )
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id="tp-doodle",
            organization_id="org-doodle",
            message_text="hello",
        )
    ]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=touchpoints,
        require_no_contacts=False,
        require_hiring_signal=True,
    )

    assert queue[0].company == "Mount"
    assert queue[0].company_mode == "startup"
    assert "No LinkedIn-sourced contacts yet" in queue[0].triggers


def test_build_linkedin_company_queue_filters_target_lists_and_contacts() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="One",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            source_kind=SourceKind.YC_DIRECTORY,
        ),
        OrganizationRecord(
            organization_id="org-2",
            name="Two",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in;la",
            source_kind=SourceKind.STARTUP_DIRECTORY,
        ),
    ]
    opportunities = [
        OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A"),
        OpportunityRecord(opportunity_id="opp-2", organization_id="org-2", title="Role B"),
    ]
    contacts = [ContactRecord(contact_id="ct-2", organization_id="org-2", full_name="Person")]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        include_target_lists=("yc",),
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert [item.company for item in queue] == ["One"]


def test_build_linkedin_company_queue_keeps_non_linkedin_contacts_eligible() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="Mount",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            source_kind=SourceKind.YC_DIRECTORY,
        )
    ]
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A")]
    contacts = [
        ContactRecord(
            contact_id="ct-1",
            organization_id="org-1",
            full_name="Founder",
            source_kind=SourceKind.YC_DIRECTORY,
        )
    ]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert [item.company for item in queue] == ["Mount"]
    assert queue[0].linkedin_contact_count == 0


def test_build_linkedin_company_queue_mode_infers_big_company() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="BigCo",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in",
            source_kind=SourceKind.STARTUP_DIRECTORY,
            notes="team_size=5000 Employees",
        )
    ]
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A")]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=[],
        touchpoints=[],
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert queue[0].company_mode == "big_company"


def test_infer_fit_reasons_scores_ai_startup_hiring_signals() -> None:
    organization = OrganizationRecord(
        organization_id="org-mount",
        name="Mount",
        organization_type=OrganizationType.STARTUP,
        city="San Francisco",
        notes="team_size=12 employees | location=San Francisco, CA | tags=ai,insurance | description=AI risk platform",
    )
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-mount", title="Product Strategy Intern")]

    score, reasons = infer_fit_reasons(
        organization=organization,
        tags=["ai", "insurance"],
        description="AI risk platform for autonomous agents",
        opportunities=opportunities,
    )

    assert score >= 60
    assert "AI/ML angle" in reasons
    assert fit_band_from_score(score) == "strong"


def test_build_organization_intel_items_shapes_reviewable_output() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="Alpha",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            city="San Francisco",
            source_kind=SourceKind.YC_DIRECTORY,
            discovered_at="2026-04-09T10:00:00+00:00",
            website="https://alpha.example.com",
            source_url="https://www.ycombinator.com/companies/alpha",
            notes=(
                "batch=Spring 2026 | founded_year=2024 | team_size=20 employees | "
                "location=San Francisco, CA | jobs_count=2 | tags=ai,data | "
                "description=Builds AI data workflow software"
            ),
        )
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-1",
            organization_id="org-1",
            title="Product Operations Intern",
        )
    ]
    contacts = [ContactRecord(contact_id="ct-1", organization_id="org-1", full_name="Founder", contact_type="founder")]
    touchpoints: list[TouchpointRecord] = []

    items = build_organization_intel_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=touchpoints,
        require_hiring_signal=True,
    )

    assert len(items) == 1
    assert items[0]["company"] == "Alpha"
    assert items[0]["public_revenue_signal"] == "Not surfaced in the source pages yet."
    assert items[0]["fit_band"] == "strong"
    assert "AI/ML angle" in items[0]["fit_reasons"]


def test_score_opportunity_relevance_prefers_pm_intern_over_engineering() -> None:
    organization = OrganizationRecord(
        organization_id="org-1",
        name="Alpha",
        organization_type=OrganizationType.STARTUP,
    )

    product_score, product_reasons = score_opportunity_relevance("MBA Product Manager Intern", organization)
    engineer_score, engineer_reasons = score_opportunity_relevance("Senior Software Engineer", organization)

    assert product_score >= 80
    assert "Product role" in product_reasons
    assert engineer_score == 0
    assert engineer_reasons == ["Role looks functionally off-target"]
    assert classify_opportunity_action(product_score) == "apply_now"


def test_build_target_action_queue_distinguishes_apply_vs_outreach() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-apply",
            name="ApplyCo",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            notes="team_size=20 employees | location=San Francisco | tags=ai | description=AI workflow platform",
        ),
        OrganizationRecord(
            organization_id="org-outreach",
            name="OutreachCo",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            notes="team_size=8 employees | location=Los Angeles | tags=robotics | description=Robotics platform for logistics",
        ),
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-1",
            organization_id="org-apply",
            title="Product Operations Intern",
        ),
        OpportunityRecord(
            opportunity_id="opp-2",
            organization_id="org-outreach",
            title="Senior Mechanical Engineer",
        ),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-1",
            organization_id="org-outreach",
            full_name="Founder One",
            contact_type="founder",
        )
    ]

    items = build_target_action_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        include_target_lists=("yc",),
    )

    assert items[0]["company"] == "ApplyCo"
    assert items[0]["action"] == "apply_now"
    outreach_item = next(item for item in items if item["company"] == "OutreachCo")
    assert outreach_item["action"] == "outreach_now"
    assert outreach_item["relevant_role_count"] == 0


def test_build_resume_outreach_queue_applies_override_bias_and_company_cap() -> None:
    jobs = [
        ResumeJob(
            row_id="1",
            company="Typeface",
            role_title="Product Manager Intern",
            location="San Francisco",
            url="https://example.com/typeface-1",
            url_hash="hash-1",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.4,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="2",
            company="Typeface",
            role_title="PM Intern 2",
            location="San Francisco",
            url="https://example.com/typeface-2",
            url_hash="hash-2",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.2,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="3",
            company="Typeface",
            role_title="PM Intern 3",
            location="San Francisco",
            url="https://example.com/typeface-3",
            url_hash="hash-3",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.1,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="4",
            company="TikTok",
            role_title="Product Manager Intern",
            location="San Jose",
            url="https://example.com/tiktok",
            url_hash="hash-4",
            source="linkedin_live_jobs_v1",
            status="generated",
            normalized_status="generated",
            fit_score=8.6,
            fit_rationale="excellent fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
    ]
    overrides = {
        "typeface": CompanyOverride(
            company="Typeface",
            normalized_company="typeface",
            company_type_override="startup",
            startup_bias="high",
            notes="High outreach value",
        ),
        "tiktok": CompanyOverride(
            company="TikTok",
            normalized_company="tiktok",
            company_type_override="big_company",
            startup_bias="deprioritize",
            notes="Prefer startups first",
        ),
    }

    queue = build_resume_outreach_queue(jobs, company_overrides=overrides, max_per_company=2)

    assert len(queue) == 3
    assert queue[0].company == "Typeface"
    assert queue[0].company_type == "startup"
    assert queue[0].startup_bias == "high"
    assert queue[-1].company == "TikTok"
    assert queue[-1].startup_bias == "deprioritize"

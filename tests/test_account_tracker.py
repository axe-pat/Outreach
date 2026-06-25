from pathlib import Path

from outreach.account_tracker import build_account_rows, build_campaign_plan_rows
from outreach.tracking import ContactRecord, OpportunityRecord, OrganizationRecord, OrganizationType, OutreachWorkbook


def test_account_tracker_filters_unrelated_existing_connections(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-clara",
            name="Clara",
            organization_type=OrganizationType.STARTUP,
            notes=(
                "team_size=9 | tags=healthcare,ai | "
                "description=Clara is an AI primary care assistant."
            ),
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-founder",
            organization_id="org-clara",
            full_name="George Founder",
            title="Founder at Clara",
            status="Discovered",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-warm-unrelated",
            organization_id="org-clara",
            full_name="Warm Unrelated",
            title="Senior Principal Software Engineer at Optum",
            status="Warm",
            notes="passes=existing_connections | triggers=Existing Connection,USC Marshall",
        )
    )

    row = build_account_rows(tmp_path)[0]

    assert row.company == "Clara"
    assert row.people_mapped == 1
    assert row.accepted == 0
    assert row.replies == 0
    assert row.score_relationship == 0
    assert row.account_stage == "people_mapped"


def test_account_tracker_counts_company_relevant_warm_contact_as_accepted(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-centerfield",
            name="Centerfield",
            organization_type=OrganizationType.COMPANY,
            notes="tags=data-platform,analytics | description=Marketing technology and data platform.",
        )
    )
    workbook.upsert_opportunity(
        OpportunityRecord(
            opportunity_id="opp-centerfield",
            organization_id="org-centerfield",
            title="Product Manager Intern",
            opportunity_type="internship",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-product",
            organization_id="org-centerfield",
            full_name="Product Person",
            title="Product Manager at Centerfield",
            status="Warm",
            notes="passes=existing_connections | triggers=Existing Connection",
        )
    )

    row = build_account_rows(tmp_path)[0]

    assert row.people_mapped == 1
    assert row.accepted == 1
    assert row.replies == 0
    assert row.score_relationship == 8
    assert row.account_stage == "connected_no_conversation"


def test_account_tracker_parses_team_size_with_commas_and_words(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-deel",
            name="Deel",
            organization_type=OrganizationType.COMPANY,
            notes="team_size=5,000 employees | tags=fintech,hr-tech | description=Global payroll platform.",
        )
    )

    row = build_account_rows(tmp_path)[0]

    assert row.team_size == 5000
    assert row.score_team_gate == 0
    assert "team_size_unparsed" not in row.data_quality_flags


def test_account_tracker_profile_fit_uses_token_boundaries(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-capital-fulfillment",
            name="Capital Fulfillment",
            organization_type=OrganizationType.COMPANY,
            notes=(
                "team_size=225 Employees | tags=fintech,financial services | "
                "description=Capital Fulfillment hires talented teams for high trust member support."
            ),
        )
    )

    row = build_account_rows(tmp_path)[0]

    assert row.score_profile_fit == 4
    assert "AI/ML" not in row.why_fit
    assert "LLM/AI" not in row.why_fit
    assert "API/integration" not in row.why_fit
    assert "talent" not in row.why_fit


def test_account_tracker_manual_priority_counts_without_stacking_brand(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-dream-data",
            name="Dream Data",
            organization_type=OrganizationType.COMPANY,
            target_lists="relationship;dream",
            notes="tags=data-platform | description=Data platform for enterprise workflows.",
        )
    )

    row = build_account_rows(tmp_path)[0]

    assert row.score_brand == 12
    assert "manual priority" in row.why_fit
    assert row.account_score > row.fit_score


def test_account_tracker_prestige_signals_feed_brand_component(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-funded-ai",
            name="Funded AI",
            organization_type=OrganizationType.COMPANY,
            notes=(
                "tags=artificial-intelligence | description=AI workflow platform. | "
                "prestige_signals=sequoia-backed,series-b | context_confidence=external_verified"
            ),
        )
    )

    row = build_account_rows(tmp_path)[0]

    assert row.score_brand == 10
    assert "top investor signal" in row.why_fit


def test_account_campaign_plan_switches_channel_after_large_dead_linkedin_wave(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-centerfield",
            name="Centerfield",
            organization_type=OrganizationType.COMPANY,
            notes="tags=data-platform,analytics | description=Marketing technology and data platform.",
        )
    )
    workbook.upsert_opportunity(
        OpportunityRecord(
            opportunity_id="opp-centerfield",
            organization_id="org-centerfield",
            title="Product Manager Intern",
            opportunity_type="internship",
        )
    )
    for index in range(9):
        workbook.upsert_contact(
            ContactRecord(
                contact_id=f"ct-{index}",
                organization_id="org-centerfield",
                full_name=f"Person {index}",
                title=f"Product Manager at Centerfield {index}",
                status="Invited",
            )
        )

    row = build_account_rows(tmp_path)[0]

    assert row.account_stage == "outreach_active"
    assert row.campaign_action == "switch_to_email_or_wellfound"
    assert row.campaign_channel == "email/wellfound"
    assert row.lane_1_policy == "track_2_owns"


def test_account_campaign_plan_flags_role_without_domain_context(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-typeface",
            name="Typeface",
            organization_type=OrganizationType.COMPANY,
            notes="Imported from ResumeGenerator v1 jobs.xlsx | latest_resume_status=generated",
        )
    )
    workbook.upsert_opportunity(
        OpportunityRecord(
            opportunity_id="opp-typeface",
            organization_id="org-typeface",
            title="Product Manager Intern",
            opportunity_type="internship",
        )
    )

    row = build_account_rows(tmp_path)[0]
    plan = build_campaign_plan_rows([row])[0]

    assert "needs_domain_enrichment" in row.data_quality_flags
    assert row.account_score < row.fit_score
    assert row.campaign_action == "enrich_company_context"
    assert plan.campaign_action == "enrich_company_context"
    assert plan.account_score == row.account_score
    assert plan.lane_1_policy == "fresh_role_only"

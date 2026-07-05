from pathlib import Path

from outreach.account_tracker import build_account_rows, build_campaign_plan_rows
from outreach.story_fit_targets import import_story_fit_targets, load_story_fit_targets
from outreach.tracking import OrganizationRecord, OutreachWorkbook


def test_load_story_fit_targets_from_csv(tmp_path: Path) -> None:
    source = tmp_path / "story_fit_targets.csv"
    source.write_text(
        "\n".join(
            [
                "company,website,story_cluster,story_angle,tags,description,why_this_company,profile_evidence,target_roles,priority,organization_type,team_size,city,source_url,verification_status",
                "Anam AI,https://anam.ai,hiring_interview_ai,recruiting workflow AI,\"artificial-intelligence,hiring\",AI interview avatars,FlairX angle,FlairX,Founder,core,startup,40,Los Angeles,https://anam.ai,manual_seed",
            ]
        ),
        encoding="utf-8",
    )

    targets = load_story_fit_targets(source)

    assert len(targets) == 1
    assert targets[0].company == "Anam AI"
    assert targets[0].story_cluster == "hiring_interview_ai"
    assert targets[0].story_angle == "recruiting workflow AI"
    assert targets[0].why_this_company == "FlairX angle"
    assert targets[0].why_you_have_a_case == "FlairX angle"
    assert targets[0].priority == "core"


def test_import_story_fit_targets_adds_track_2_account(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path / "workspace")
    workbook.initialize()
    source = tmp_path / "story_fit_targets.csv"
    source.write_text(
        "\n".join(
            [
                "company,website,story_cluster,tags,description,why_you_have_a_case,profile_evidence,target_roles,priority,organization_type,team_size,city,source_url,verification_status",
                "Anam AI,https://anam.ai,hiring_interview_ai,\"artificial-intelligence,hiring,workflow-automation\",AI avatar platform for interview workflows,FlairX gives a direct recruiting workflow pitch,FlairX AI PM internship,Founder and product,core,startup,40,Los Angeles,https://anam.ai,manual_seed",
            ]
        ),
        encoding="utf-8",
    )

    summary = import_story_fit_targets(tmp_path / "workspace", source_path=source, execute=True)
    rows = build_account_rows(tmp_path / "workspace")
    plan = build_campaign_plan_rows(rows)

    assert summary["added"] == 1
    assert rows[0].company == "Anam AI"
    assert "story-fit" in rows[0].target_lists
    assert rows[0].campaign_action == "map_more_contacts"
    assert plan[0].company == "Anam AI"


def test_import_story_fit_targets_updates_existing_account_without_losing_verified_context(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path / "workspace")
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-monte-carlo",
            name="Monte Carlo",
            target_lists="built_in;sf;companies",
            website="https://www.montecarlodata.com",
            notes=(
                "Existing verified context | tags=data-platform | "
                "description=Verified data observability platform. | "
                "context_confidence=external_verified | context_source=public_web | "
                "context_evidence_url=https://www.montecarlodata.com"
            ),
        )
    )
    source = tmp_path / "story_fit_targets.csv"
    source.write_text(
        "\n".join(
            [
                "company,website,story_cluster,tags,description,why_you_have_a_case,profile_evidence,target_roles,priority,organization_type,team_size,city,source_url,verification_status",
                "Monte Carlo,https://www.montecarlodata.com,data_infra_observability,\"observability,monitoring\",Manual description,Hevo monitoring gives a direct data reliability pitch,Hevo 2.0 AI monitoring,Product,priority,company,300,,https://www.montecarlodata.com,manual_seed",
            ]
        ),
        encoding="utf-8",
    )

    summary = import_story_fit_targets(tmp_path / "workspace", source_path=source, execute=True)
    orgs = workbook.list_organizations()

    assert summary["updated"] == 1
    assert len(orgs) == 1
    assert "story-fit" in orgs[0].target_lists
    assert "data_infra_observability" in orgs[0].target_lists
    assert "story_cluster=data_infra_observability" in orgs[0].notes
    assert "source=story_fit_targets" in orgs[0].notes
    assert "why_this_company=Hevo monitoring gives a direct data reliability pitch" in orgs[0].notes
    assert "story_angle=data_infra_observability" in orgs[0].notes
    assert "priority=priority" in orgs[0].notes
    assert "context_confidence=external_verified" in orgs[0].notes
    assert "description=Verified data observability platform." in orgs[0].notes
    assert "tags=data-platform,observability,monitoring" in orgs[0].notes

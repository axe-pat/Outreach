from pathlib import Path

from outreach.relationship_leads import (
    ensure_relationship_leads_template,
    import_relationship_leads,
    load_relationship_leads,
    relationship_source_default_path,
)
from outreach.tracking import OrganizationRecord, OrganizationType, OutreachWorkbook, SourceKind


def test_ensure_relationship_leads_template_writes_header(tmp_path: Path) -> None:
    path = tmp_path / "relationship_leads.csv"

    ensure_relationship_leads_template(path)

    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("source_type,full_name,company")


def test_source_preset_template_writes_csv_and_capture_guide(tmp_path: Path) -> None:
    path = tmp_path / relationship_source_default_path("peoplegrove_usc").name

    ensure_relationship_leads_template(path, source_key="peoplegrove_usc")

    assert path.exists()
    guide = path.with_suffix(".md")
    assert guide.exists()
    guide_text = guide.read_text(encoding="utf-8")
    assert "PeopleGrove" in guide_text
    assert "source_type`: `peoplegrove`" in guide_text


def test_import_relationship_leads_applies_recent_mba_pm_preset_defaults(tmp_path: Path) -> None:
    source = tmp_path / "relationship_leads_recent_mba_pm.csv"
    source.write_text(
        "\n".join(
            [
                "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes",
                ",Riley Product,Deepgram,Product Manager,https://www.linkedin.com/in/riley,,,,,Kellogg,MBA,2024,recent MBA to PM,,medium,,,,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = import_relationship_leads(
        tmp_path / "workspace",
        source_path=source,
        source_key="recent_mba_pm",
        execute=True,
    )

    assert summary["source_key"] == "recent_mba_pm"
    workbook = OutreachWorkbook(tmp_path / "workspace")
    org = workbook.list_organizations()[0]
    contact = workbook.list_contacts()[0]
    assert "recent-mba-pm" in org.target_lists
    assert "relationship_source_type=recent_mba_pm" in org.notes
    assert "recent-mba-pm" in contact.target_lists
    assert contact.source_kind == SourceKind.LINKEDIN


def test_import_relationship_leads_adds_company_and_contact(tmp_path: Path) -> None:
    source = tmp_path / "relationship_leads.csv"
    source.write_text(
        "\n".join(
            [
                "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes",
                "peoplegrove,Avery Founder,Anam AI,Founder & CEO,https://www.linkedin.com/in/avery,avery@example.com,https://anam.ai,,Los Angeles,USC Marshall,MBA,2021,USC founder and startup operator,Founder,high,story-fit,ai;avatar,https://usc.peoplegrove.com/person/1,strong warm USC path",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = import_relationship_leads(tmp_path / "workspace", source_path=source, execute=True)

    assert summary["organizations_added"] == 1
    assert summary["contacts_added"] == 1
    workbook = OutreachWorkbook(tmp_path / "workspace")
    org = workbook.list_organizations()[0]
    contact = workbook.list_contacts()[0]
    assert org.name == "Anam AI"
    assert org.source_kind == SourceKind.UNIVERSITY_DIRECTORY
    assert "peoplegrove" in org.target_lists
    assert "relationship_signal=USC founder and startup operator" in org.notes
    assert contact.email == "avery@example.com"
    assert contact.contact_type == "Founder"
    assert contact.preferred_channel.value == "email"
    assert "school-usc-marshall" in contact.target_lists


def test_import_relationship_leads_updates_existing_without_losing_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id("Deepgram"),
            name="Deepgram",
            organization_type=OrganizationType.COMPANY,
            target_lists="story-fit",
            website="https://deepgram.com",
            notes="Existing verified context | context_confidence=external_verified | tags=voice-ai",
        )
    )
    source = tmp_path / "relationship_leads.csv"
    source.write_text(
        "\n".join(
            [
                "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes",
                "linkedin_recent_mba_pm,Natalie Product,Deepgram,AI Product Leader,https://www.linkedin.com/in/natalie,,,,,Kellogg,MBA,2024,recent MBA PM transition,Product,medium,recent-mba-pm,voice-ai,https://www.linkedin.com/in/natalie,recent MBA PM path",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = import_relationship_leads(workspace, source_path=source, execute=True)

    assert summary["organizations_added"] == 0
    assert summary["organizations_updated"] == 1
    assert summary["contacts_added"] == 1
    org = OutreachWorkbook(workspace).list_organizations()[0]
    contact = OutreachWorkbook(workspace).list_contacts()[0]
    assert org.website == "https://deepgram.com"
    assert "context_confidence=external_verified" in org.notes
    assert "recent-mba-pm" in org.target_lists
    assert contact.source_kind == SourceKind.LINKEDIN
    assert "program-mba" in contact.target_lists


def test_load_relationship_leads_skips_blank_optional_fields(tmp_path: Path) -> None:
    source = tmp_path / "relationship_leads.csv"
    source.write_text(
        "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes\n"
        "usc_founder,Sam Startup,Startup Co,CTO,,,,,,,,USC founder,,high,,,,,\n",
        encoding="utf-8",
    )

    leads = load_relationship_leads(source)

    assert len(leads) == 1
    assert leads[0].source_type == "usc_founder"
    assert leads[0].email == ""

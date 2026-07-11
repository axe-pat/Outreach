import csv
import json
from pathlib import Path

import pytest

from outreach.relationship_leads import (
    RELATIONSHIP_LEAD_FIELDS,
    RelationshipLeadConflictError,
    RelationshipLeadReviewError,
    _program_tag,
    _school_tag,
    _source_kind_for_lead,
    ensure_relationship_leads_template,
    import_relationship_leads,
    load_relationship_leads,
    review_staged_relationship_leads,
    relationship_source_default_path,
    stage_relationship_leads,
)
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachWorkbook,
    SourceKind,
)


def _stage_and_approve(source: Path, *, source_key: str = "") -> Path:
    summary = stage_relationship_leads(source, source_key=source_key)
    staged = Path(str(summary["staged_path"]))
    review_staged_relationship_leads(
        staged,
        reviewer="test-reviewer",
        approve_all_ready=True,
        reject_all_blocked=True,
    )
    return staged


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


def test_school_and_program_tags_are_safe_single_tokens() -> None:
    assert _school_tag("University of California, Berkeley, Haas School of Business") == (
        "school-university-of-california-berkeley-haas-school-of-business"
    )
    assert _program_tag("MBA / Product Strategy") == "program-mba-product-strategy"


def test_peoplegrove_public_corroboration_retains_directory_provenance() -> None:
    assert _source_kind_for_lead("peoplegrove_public_web") == (
        SourceKind.UNIVERSITY_DIRECTORY
    )


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

    staged = _stage_and_approve(source, source_key="recent_mba_pm")
    summary = import_relationship_leads(
        tmp_path / "workspace",
        source_path=staged,
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

    staged = _stage_and_approve(source)
    summary = import_relationship_leads(tmp_path / "workspace", source_path=staged, execute=True)

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

    staged = _stage_and_approve(source)
    summary = import_relationship_leads(workspace, source_path=staged, execute=True)

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


def test_stage_relationship_leads_preserves_validation_and_duplicate_findings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "relationship_leads.csv"
    source.write_text(
        "\n".join(
            [
                "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes",
                "recent_mba_pm,Riley Product,Deepgram,Product Manager,https://www.linkedin.com/in/riley?trk=test,,,,,Kellogg,MBA,2024,recent MBA PM,,medium,,,,",
                "recent_mba_pm,Riley Product,Deepgram,Product Lead,https://www.linkedin.com/in/riley-two,,,,,Kellogg,MBA,2024,recent MBA PM,,medium,,,,",
                "recent_mba_pm,Bad Row,Bad Co,,not-a-url,bad-email,,,,,MBA,2040,,,,,,,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = stage_relationship_leads(source)

    assert summary["rows_total"] == 3
    assert summary["rows_ready"] == 1
    assert summary["rows_blocked"] == 2
    assert summary["duplicate_rows"] == 1
    staged = Path(str(summary["staged_path"]))
    with staged.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/riley"
    assert rows[0]["review_status"] == "pending"
    assert rows[0]["row_fingerprint"]
    assert rows[1]["validation_status"] == "blocked"
    assert "duplicate_row" in rows[1]["validation_issues"]
    assert "invalid_email" in rows[2]["validation_issues"]
    manifest = json.loads(Path(str(summary["manifest_path"])).read_text(encoding="utf-8"))
    assert manifest["source_sha256"]
    assert manifest["batch_id"] == rows[0]["batch_id"]


def test_review_cannot_approve_validation_blocked_row(tmp_path: Path) -> None:
    source = tmp_path / "relationship_leads.csv"
    source.write_text(
        "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes\n"
        "recent_mba_pm,Bad Row,Bad Co,,not-a-url,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    summary = stage_relationship_leads(source)
    staged = Path(str(summary["staged_path"]))
    with staged.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    with pytest.raises(RelationshipLeadReviewError, match="validation-blocked"):
        review_staged_relationship_leads(
            staged,
            reviewer="reviewer",
            approve_row_ids=(row["row_id"],),
        )


def test_execute_rejects_raw_unreviewed_file_without_creating_workspace(tmp_path: Path) -> None:
    source = tmp_path / "relationship_leads.csv"
    source.write_text(
        "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes\n"
        "recent_mba_pm,Riley Product,Deepgram,PM,https://www.linkedin.com/in/riley,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"

    with pytest.raises(RelationshipLeadReviewError, match="staged and reviewed"):
        import_relationship_leads(workspace, source_path=source, execute=True)

    assert not workspace.exists()


def test_execute_rejects_staged_file_changed_after_review(tmp_path: Path) -> None:
    source = tmp_path / "relationship_leads.csv"
    source.write_text(
        "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes\n"
        "recent_mba_pm,Riley Product,Deepgram,PM,https://www.linkedin.com/in/riley,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    staged = _stage_and_approve(source)
    staged.write_text(
        staged.read_text(encoding="utf-8").replace("Deepgram", "Changed Co"),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"

    with pytest.raises(RelationshipLeadReviewError, match="changed after review"):
        import_relationship_leads(workspace, source_path=staged, execute=True)

    assert not workspace.exists()


def test_execute_blocks_person_locator_owned_by_another_organization(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    old_org_id = workbook.make_organization_id("Old Company")
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id=old_org_id,
            name="Old Company",
            organization_type=OrganizationType.COMPANY,
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id=workbook.make_contact_id(
                old_org_id,
                "Riley Product",
                linkedin_url="https://www.linkedin.com/in/riley",
            ),
            organization_id=old_org_id,
            full_name="Riley Product",
            linkedin_url="https://www.linkedin.com/in/riley",
        )
    )
    source = tmp_path / "relationship_leads.csv"
    source.write_text(
        "source_type,full_name,company,title,linkedin_url,email,company_website,company_linkedin_url,location,school,program,grad_year,relationship_signal,contact_type,priority,target_lists,tags,source_url,notes\n"
        "recent_mba_pm,Riley Product,New Company,PM,https://www.linkedin.com/in/riley,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    staged = _stage_and_approve(source)
    contacts_before = (workspace / "contacts.csv").read_text(encoding="utf-8")

    with pytest.raises(RelationshipLeadConflictError, match="already belongs"):
        import_relationship_leads(workspace, source_path=staged, execute=True)

    assert (workspace / "contacts.csv").read_text(encoding="utf-8") == contacts_before


def test_execute_blocks_stable_source_identity_at_a_different_company(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    old_org_id = workbook.make_organization_id("Old Company")
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id=old_org_id,
            name="Old Company",
            organization_type=OrganizationType.COMPANY,
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id=workbook.make_contact_id(old_org_id, "Trojan Operator"),
            organization_id=old_org_id,
            full_name="Trojan Operator",
            notes=(
                "Relationship lead contact | relationship_source_type=peoplegrove"
                " | relationship_source_record_id=stable-person-123"
                " | relationship_evidence_url=https://usc.peoplegrove.com/hub/usc/person"
                "?userProfile=stable-person-123"
            ),
        )
    )
    source = tmp_path / "relationship_leads.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RELATIONSHIP_LEAD_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "source_type": "peoplegrove_public_web",
                "full_name": "Trojan Operator",
                "company": "New Company",
                "title": "Product Manager",
                "source_url": (
                    "https://usc.peoplegrove.com/hub/usc/person"
                    "?userProfile=stable-person-123"
                ),
                "source_record_id": "stable-person-123",
            }
        )
    staged = _stage_and_approve(source, source_key="peoplegrove_usc")
    contacts_before = (workspace / "contacts.csv").read_text(encoding="utf-8")

    with pytest.raises(RelationshipLeadConflictError, match="already belongs"):
        import_relationship_leads(
            workspace,
            source_path=staged,
            source_key="peoplegrove_usc",
            execute=True,
        )

    assert (workspace / "contacts.csv").read_text(encoding="utf-8") == contacts_before


def test_peoplegrove_review_gate_handles_hundreds_scale_idempotently(
    tmp_path: Path,
) -> None:
    source = tmp_path / "relationship_leads_peoplegrove_usc.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RELATIONSHIP_LEAD_FIELDS)
        writer.writeheader()
        for index in range(150):
            writer.writerow(
                {
                    "source_type": "peoplegrove",
                    "full_name": f"Relevant Trojan {index:03d}",
                    "company": f"Relevant Company {index // 2:03d}",
                    "title": "Product Manager" if index % 2 == 0 else "Product Strategy Lead",
                    "school": "USC Marshall School of Business",
                    "relationship_signal": "USC Trojan product path",
                    "priority": "medium",
                    "source_url": f"https://usc.peoplegrove.com/hub/usc/person/{index:04d}",
                    "source_record_id": f"peoplegrove-{index:04d}",
                    "capture_batch": "peoplegrove-scale-fixture",
                    "captured_at": "2026-07-11T08:00:00+00:00",
                    "captured_by": "test-browser-capture",
                }
            )

    staged = _stage_and_approve(source, source_key="peoplegrove_usc")
    with staged.open(newline="", encoding="utf-8") as handle:
        staged_rows = list(csv.DictReader(handle))
    assert staged_rows[0]["target_lists"] == "peoplegrove;usc-network"
    assert "usc-founder" not in staged_rows[0]["target_lists"]
    assert "usc-operator" not in staged_rows[0]["target_lists"]

    first = import_relationship_leads(
        tmp_path / "workspace",
        source_path=staged,
        source_key="peoplegrove_usc",
        execute=True,
    )
    second = import_relationship_leads(
        tmp_path / "workspace",
        source_path=staged,
        source_key="peoplegrove_usc",
        execute=True,
    )

    assert first["rows_selected"] == 150
    assert first["validation_issue_count"] == 0
    assert first["organizations_added"] == 75
    assert first["contacts_added"] == 150
    assert second["organizations_added"] == 0
    assert second["organizations_updated"] == 0
    assert second["contacts_added"] == 0
    assert second["contacts_updated"] == 0
    assert second["contacts_unchanged"] == 150

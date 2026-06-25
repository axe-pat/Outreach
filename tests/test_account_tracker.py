from pathlib import Path

from outreach.account_tracker import build_account_rows
from outreach.tracking import ContactRecord, OrganizationRecord, OrganizationType, OutreachWorkbook


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
            notes="description=Marketing technology and data platform.",
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

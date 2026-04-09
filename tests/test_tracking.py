from __future__ import annotations

import json

from outreach.tracking import (
    OrganizationRecord,
    OrganizationType,
    OutreachWorkbook,
    SourceKind,
)


def test_initialize_creates_expected_csv_tables(tmp_path) -> None:
    workbook = OutreachWorkbook(tmp_path / "workspace")

    paths = workbook.initialize()

    assert set(paths) == {"organizations", "opportunities", "contacts", "touchpoints", "sources"}
    for path in paths.values():
        assert path.exists()


def test_upsert_organization_dedupes_by_name(tmp_path) -> None:
    workbook = OutreachWorkbook(tmp_path / "workspace")

    first, created_first = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id("Snowflake"),
            name="Snowflake",
            organization_type=OrganizationType.COMPANY,
            source_kind=SourceKind.MANUAL,
        )
    )
    second, created_second = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id("Snowflake"),
            name="Snowflake",
            organization_type=OrganizationType.STARTUP,
            source_kind=SourceKind.LINKEDIN,
        )
    )

    assert created_first is True
    assert created_second is False
    assert first.organization_id == second.organization_id
    assert workbook.summary_counts()["organizations"] == 1


def test_import_linkedin_artifact_creates_contacts_and_draft_touchpoints(tmp_path) -> None:
    workbook = OutreachWorkbook(tmp_path / "workspace")
    artifact_path = tmp_path / "20260408-101500-dry-run-pipeline.json"
    artifact_path.write_text(
        json.dumps(
            {
                "company": "Figma",
                "pass_summaries": [
                    {
                        "pass_name": "product_network",
                        "final_url": "https://www.linkedin.com/search/results/people/?keywords=figma",
                    }
                ],
                "results": [
                    {
                        "name": "Avery Product",
                        "title": "Product Manager",
                        "linkedin_url": "https://www.linkedin.com/in/avery-product/",
                        "role_bucket": "Product",
                        "passes": ["product_network"],
                        "tier": "High",
                        "priority_bucket": "High",
                        "triggers": ["USC alumni"],
                        "existing_connection": False,
                        "note": "Hi Avery, I'm a USC MBA exploring PM roles and would love to connect.",
                        "note_family": "usc",
                        "note_qc": {"verdict": "send", "score": 91, "flags": []},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = workbook.import_linkedin_artifact(artifact_path)
    counts = workbook.summary_counts()

    assert summary.organization_id == "org-figma"
    assert summary.contacts_added == 1
    assert summary.touchpoints_added == 1
    assert counts["organizations"] == 1
    assert counts["contacts"] == 1
    assert counts["touchpoints"] == 1

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from outreach.cli import app
from outreach.peoplegrove_curation import (
    PeopleGroveCurationError,
    curate_peoplegrove_capture,
    parse_current_title_company,
)
from outreach.relationship_leads import RELATIONSHIP_LEAD_FIELDS
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachWorkbook,
)


def _write_capture(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


def _write_enrichment(
    path: Path,
    capture: Path,
    profiles: dict[str, dict[str, object]],
    *,
    source_capture_sha256: str = "",
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_capture_sha256": source_capture_sha256
                or hashlib.sha256(capture.read_bytes()).hexdigest(),
                "captured_at": "2026-07-11T12:00:00+00:00",
                "captured_by": "test-career-journey-capture",
                "profiles": profiles,
            }
        ),
        encoding="utf-8",
    )


def _profile(
    name: str,
    headline: str,
    record_id: str,
    *,
    source_url: str | None = None,
    member_type: str = "Alumni",
    labels: list[str] | None = None,
) -> dict[str, object]:
    return {
        "full_name": name,
        "headline": headline,
        "program": "USC Marshall MBA",
        "grad_year": "2022",
        "member_type": member_type,
        "source_url": source_url or f"https://usc.peoplegrove.com/profile/{record_id}",
        "source_record_id": record_id,
        "queries": ["USC Marshall product operators"],
        "labels": labels if labels is not None else ["Trojan Network"],
    }


def test_curate_peoplegrove_capture_keeps_only_high_signal_current_roles(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    _write_capture(
        capture,
        [
            _profile("Avery Founder", "Founder & CEO at Acme AI", "pg-1"),
            _profile("Priya Product", "Senior Product Manager @ Google", "pg-2"),
            _profile("Blair BizOps", "Head of Strategy & Operations at Stripe", "pg-3"),
            _profile("Casey Program", "Program Manager at Amazon", "pg-4"),
            _profile("Riley Recruiter", "Technical Recruiter at Anthropic", "pg-5"),
            _profile("Morgan Venture", "Operating Partner at Seed Fund", "pg-6"),
            _profile("Val Strategy", "VP, Product & Strategy at LPL Financial", "pg-7"),
            _profile("Pat Product", "Product @ Palmetto | USC Marshall", "pg-8"),
            _profile("Gray Growth", "VP Product and Growth @ Supernatural", "pg-9"),
            _profile(
                "Fran Advisor",
                "Founder & Strategic Advisor, The Human Algorithm | USC",
                "pg-10",
            ),
        ],
    )

    summary = curate_peoplegrove_capture(
        capture,
        output_path=output,
        capture_batch="pg-test",
        captured_by="test-capture",
    )

    assert summary["rows_accepted"] == 10
    assert summary["rows_rejected"] == 0
    with output.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        assert reader.fieldnames == RELATIONSHIP_LEAD_FIELDS
    assert {row["company"] for row in rows} == {
        "Acme AI",
        "Google",
        "Stripe",
        "Amazon",
        "Anthropic",
        "Seed Fund",
        "LPL Financial",
        "Palmetto",
        "Supernatural",
        "The Human Algorithm",
    }
    assert {row["source_type"] for row in rows} == {"peoplegrove"}
    assert {row["capture_batch"] for row in rows} == {"pg-test"}
    assert {row["captured_by"] for row in rows} == {"test-capture"}
    assert all(row["source_record_id"] for row in rows)
    assert summary["accepted_categories"] == {
        "bizops_strategy": 1,
        "founder_c_suite": 2,
        "product_product_strategy": 4,
        "program_operations_leadership": 1,
        "recruiting_talent": 1,
        "venture_startup_operator": 1,
    }
    assert summary["manual_company_fit_review_required"] is True
    assert summary["query_coverage"]["USC Marshall product operators"] == {
        "rows_input": 10,
        "rows_accepted": 10,
        "rows_rejected": 0,
    }
    first_decision = summary["decisions"][0]
    assert first_decision["program"] == "USC Marshall MBA"
    assert first_decision["member_type"] == "Alumni"
    assert first_decision["queries"] == ["USC Marshall product operators"]
    assert first_decision["review_flags"] == [
        "manual_company_fit_review",
        "company_not_in_outreach_universe",
    ]


def test_browser_ui_labels_are_audited_but_never_promoted_to_tags(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    _write_capture(
        capture,
        [
            _profile(
                "Avery Founder",
                "Founder & CEO at Acme AI",
                "pg-ui-labels",
                labels=[
                    "Save Avery Founder's profile",
                    "Avery Founder, currently offline",
                    "Responds within a day",
                    "1 more items",
                    "Message",
                    "View Profile",
                ],
            )
        ],
    )

    summary = curate_peoplegrove_capture(capture, output_path=output)

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["tags"].split(",") == [
        "peoplegrove",
        "usc",
        "trojan-network",
        "warm-network",
        "founder_c_suite",
    ]
    assert "capture_labels=" in row["notes"]
    assert summary["decisions"][0]["labels"] == [
        "Save Avery Founder's profile",
        "Avery Founder, currently offline",
        "Responds within a day",
        "1 more items",
        "Message",
        "View Profile",
    ]


def test_curation_rejects_students_job_seekers_vague_and_irrelevant_roles(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    _write_capture(
        capture,
        [
            _profile("Student", "MBA Candidate at USC Marshall", "pg-1", member_type="Student"),
            _profile("Intern", "Product Management Intern at Google", "pg-2"),
            _profile("Seeker", "Open to Work - Product Manager at Startups", "pg-3"),
            _profile("Vague", "Experienced leader at Google", "pg-4"),
            _profile("Engineer", "Software Engineer at Meta", "pg-5"),
            _profile("No Company", "Senior Product Manager", "pg-6"),
            _profile("Stealth", "Founder at Stealth Startup", "pg-7"),
            _profile("Food", "Food Safety Quality Product Manager at General Mills", "pg-8"),
            _profile("Creative", "Founder & Brand Strategist at Brand Studio", "pg-9"),
        ],
    )

    summary = curate_peoplegrove_capture(capture, output_path=output)

    assert summary["rows_accepted"] == 0
    assert summary["rows_rejected"] == 9
    reasons = summary["rejection_reasons"]
    assert reasons["student_or_intern"] == 2
    assert reasons["job_seeker"] == 1
    assert reasons["no_high_signal_role"] == 2
    assert reasons["unparseable_current_role_company"] == 2
    assert reasons["irrelevant_role_context"] == 2
    audit = json.loads(output.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert all(not decision["accepted"] for decision in audit["decisions"])
    assert all("category" in decision and "score" in decision for decision in audit["decisions"])


def test_only_founders_get_non_at_company_separator_parsing() -> None:
    assert parse_current_title_company("Founder & CEO | Acme AI") == (
        "Founder & CEO",
        "Acme AI",
    )
    assert parse_current_title_company("Co-Founder - Useful Labs") == (
        "Co-Founder",
        "Useful Labs",
    )
    assert parse_current_title_company("Product Manager | Google") is None
    assert parse_current_title_company("Founder at Stealth Startup") is None
    assert parse_current_title_company("Founder | Investor | Advisor") is None
    assert parse_current_title_company("CEO | Founder | Life Coach") is None
    assert parse_current_title_company("Founder | Product Lead | Ex-Microsoft") is None
    assert parse_current_title_company("Ingage Co-Founder & CEO | LAFB Co-Owner") is None
    assert (
        parse_current_title_company(
            "Founder of Blur Technologies • Worked at @RadPad, @GOOD, @PWC Consulting"
        )
        is None
    )
    assert (
        parse_current_title_company(
            "Senior Product Manager at Panopto 20+ Years experience in streaming media"
        )
        is None
    )


def test_current_company_aliases_use_existing_workbook_accounts() -> None:
    assert parse_current_title_company("Product Manager at Facebook") == (
        "Product Manager",
        "Meta",
    )
    assert parse_current_title_company("Technical Recruiter at Amazon Web Services (AWS)") == (
        "Technical Recruiter",
        "Amazon",
    )
    assert parse_current_title_company("Senior Program Manager at The Boeing Company") == (
        "Senior Program Manager",
        "Boeing",
    )
    assert parse_current_title_company("Technical Program Manager at Snowflake ❄️") == (
        "Technical Program Manager",
        "Snowflake",
    )
    assert parse_current_title_company("Lead Program Manager at YouTube") == (
        "Lead Program Manager",
        "Google",
    )


def test_enrichment_selects_strongest_explicit_relevant_current_role(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    enrichment = tmp_path / "career-journey.json"
    profile = _profile(
        "Priya Product",
        "Builder across product, systems, and AI",
        "pg-enriched",
    )
    _write_capture(capture, [profile])
    source_url = str(profile["source_url"])
    _write_enrichment(
        enrichment,
        capture,
        {
            source_url: {
                "source_record_id": "pg-enriched",
                "source_url": source_url,
                "current_roles": [
                    {
                        "title": "Software Engineer",
                        "company": "Meta",
                        "date_range": "2024 - Present",
                        "location": "Menlo Park, California",
                    },
                    {
                        "title": "Senior Product Manager",
                        "company": "Amazon Web Services (AWS)",
                        "date_range": "2025 - Present",
                        "location": "Los Angeles, California",
                    },
                ],
            }
        },
    )

    summary = curate_peoplegrove_capture(
        capture,
        output_path=output,
        enrichment_path=enrichment,
    )

    assert summary["rows_accepted"] == 1
    assert summary["enrichment_records"] == 1
    assert summary["enrichment_capture_rows_matched"] == 1
    assert summary["roles_selected_from_enrichment"] == 1
    assert summary["accepted_from_enrichment"] == 1
    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["title"] == "Senior Product Manager"
    assert row["company"] == "Amazon"
    assert "captured_headline=Builder across product, systems, and AI" in row["notes"]
    assert "peoplegrove_role_source=career_journey_enrichment" in row["notes"]
    assert "enrichment_current_role_title=Senior Product Manager" in row["notes"]
    assert (
        "enrichment_current_role_company=Amazon Web Services (AWS)" in row["notes"]
    )
    assert "enrichment_current_role_date_range=2025 - Present" in row["notes"]
    assert "enrichment_current_role_location=Los Angeles, California" in row["notes"]
    decision = summary["decisions"][0]
    assert decision["headline"] == "Builder across product, systems, and AI"
    assert decision["company"] == "Amazon"
    assert decision["role_source"] == "peoplegrove_career_journey_enrichment"
    assert decision["enrichment_title"] == "Senior Product Manager"
    assert decision["enrichment_company"] == "Amazon Web Services (AWS)"
    assert decision["enrichment_mapping_key"] == source_url
    assert decision["enrichment_source_record_id"] == "pg-enriched"
    assert decision["enrichment_source_url"] == source_url
    assert decision["enrichment_captured_by"] == "test-career-journey-capture"
    assert len(decision["enrichment_artifact_sha256"]) == 64


def test_enrichment_rejects_unknown_capture_identity(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    enrichment = tmp_path / "career-journey.json"
    _write_capture(capture, [_profile("Known", "Product builder", "pg-known")])
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-unknown": {
                "source_record_id": "pg-unknown",
                "source_url": "",
                "current_roles": [
                    {
                        "title": "Product Manager",
                        "company": "Google",
                        "date_range": "2025 - Present",
                        "location": "",
                    }
                ],
            }
        },
    )

    with pytest.raises(PeopleGroveCurationError, match="unknown source_record_id"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )


def test_enrichment_rejects_cross_profile_id_url_mapping(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    enrichment = tmp_path / "career-journey.json"
    first = _profile("First", "Product builder", "pg-first")
    second = _profile("Second", "Product builder", "pg-second")
    _write_capture(capture, [first, second])
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-first": {
                "source_record_id": "pg-first",
                "source_url": str(second["source_url"]),
                "current_roles": [
                    {
                        "title": "Product Manager",
                        "company": "Google",
                        "date_range": "2025 - Present",
                        "location": "",
                    }
                ],
            }
        },
    )

    with pytest.raises(PeopleGroveCurationError, match="identity mismatch"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )


def test_enrichment_requires_exact_source_record_id(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    enrichment = tmp_path / "career-journey.json"
    _write_capture(capture, [_profile("Known", "Product builder", "pg-known")])
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg_known": {
                "source_record_id": "pg_known",
                "source_url": "",
                "current_roles": [
                    {
                        "title": "Product Manager",
                        "company": "Google",
                        "date_range": "2025 - Present",
                        "location": "",
                    }
                ],
            }
        },
    )

    with pytest.raises(PeopleGroveCurationError, match="unknown source_record_id"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )


def test_enrichment_schema_version_must_be_an_integer(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    enrichment = tmp_path / "career-journey.json"
    _write_capture(capture, [_profile("Known", "Product builder", "pg-known")])
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-known": {
                "source_record_id": "pg-known",
                "source_url": "",
                "current_roles": [
                    {
                        "title": "Product Manager",
                        "company": "Google",
                        "date_range": "2025 - Present",
                        "location": "",
                    }
                ],
            }
        },
    )
    payload = json.loads(enrichment.read_text(encoding="utf-8"))
    payload["schema_version"] = 1.0
    enrichment.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PeopleGroveCurationError, match="integer 1"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )


def test_enrichment_supports_url_only_identity(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    enrichment = tmp_path / "career-journey.json"
    source_url = "https://usc.peoplegrove.com/profile/url-only"
    _write_capture(
        capture,
        [_profile("URL Only", "Product builder", "", source_url=source_url)],
    )
    _write_enrichment(
        enrichment,
        capture,
        {
            source_url: {
                "source_record_id": "",
                "source_url": source_url,
                "current_roles": [
                    {
                        "title": "Product Manager",
                        "company": "Google",
                        "date_range": "2025 - Present",
                        "location": "",
                    }
                ],
            }
        },
    )

    summary = curate_peoplegrove_capture(
        capture,
        output_path=output,
        enrichment_path=enrichment,
    )

    assert summary["accepted_from_enrichment"] == 1
    assert summary["decisions"][0]["enrichment_mapping_key"] == source_url


def test_enrichment_rejects_unknown_or_ambiguous_url_only_identity(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    enrichment = tmp_path / "career-journey.json"
    shared_url = "https://usc.peoplegrove.com/profile/shared"
    _write_capture(
        capture,
        [
            _profile("First", "Product builder", "pg-first", source_url=shared_url),
            _profile("Second", "Product builder", "pg-second", source_url=shared_url),
        ],
    )
    role = {
        "title": "Product Manager",
        "company": "Google",
        "date_range": "2025 - Present",
        "location": "",
    }
    unknown_url = "https://usc.peoplegrove.com/profile/unknown"
    _write_enrichment(
        enrichment,
        capture,
        {
            unknown_url: {
                "source_record_id": "",
                "source_url": unknown_url,
                "current_roles": [role],
            }
        },
    )
    with pytest.raises(PeopleGroveCurationError, match="unknown source_url"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )

    _write_enrichment(
        enrichment,
        capture,
        {
            shared_url: {
                "source_record_id": "",
                "source_url": shared_url,
                "current_roles": [role],
            }
        },
    )
    with pytest.raises(PeopleGroveCurationError, match="ambiguous"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )


def test_parseable_card_headline_wins_over_conflicting_enrichment(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    enrichment = tmp_path / "career-journey.json"
    profile = _profile("Known", "Product Manager at Google", "pg-known")
    _write_capture(capture, [profile])
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-known": {
                "source_record_id": "pg-known",
                "source_url": str(profile["source_url"]),
                "current_roles": [
                    {
                        "title": "Founder & CEO",
                        "company": "Conflicting Company",
                        "date_range": "2025 - Present",
                        "location": "",
                    }
                ],
            }
        },
    )

    summary = curate_peoplegrove_capture(
        capture,
        output_path=output,
        enrichment_path=enrichment,
    )

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["title"] == "Product Manager"
    assert row["company"] == "Google"
    assert summary["accepted_from_enrichment"] == 0
    assert summary["decisions"][0]["role_source"] == "card_headline"


@pytest.mark.parametrize(
    ("title", "company"),
    [
        ("", "Google"),
        ("Product Manager", ""),
        ("Product Manager", "Stealth Startup"),
    ],
)
def test_enrichment_fails_closed_on_invalid_current_role_or_company(
    tmp_path: Path,
    title: str,
    company: str,
) -> None:
    capture = tmp_path / "capture.json"
    enrichment = tmp_path / "career-journey.json"
    output = tmp_path / "curated.csv"
    summary_path = output.with_suffix(".summary.json")
    output.write_text("existing curated output\n", encoding="utf-8")
    summary_path.write_text('{"existing": true}\n', encoding="utf-8")
    _write_capture(capture, [_profile("Known", "Product builder", "pg-known")])
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-known": {
                "source_record_id": "pg-known",
                "source_url": "https://usc.peoplegrove.com/profile/pg-known",
                "current_roles": [
                    {
                        "title": title,
                        "company": company,
                        "date_range": "2025 - Present",
                        "location": "",
                    }
                ],
            }
        },
    )

    with pytest.raises(PeopleGroveCurationError, match="title|company"):
        curate_peoplegrove_capture(
            capture,
            output_path=output,
            enrichment_path=enrichment,
        )
    assert output.read_text(encoding="utf-8") == "existing curated output\n"
    assert summary_path.read_text(encoding="utf-8") == '{"existing": true}\n'


def test_enrichment_does_not_guess_from_unclassified_current_roles(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    enrichment = tmp_path / "career-journey.json"
    _write_capture(capture, [_profile("Known", "Product builder", "pg-known")])
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-known": {
                "source_record_id": "pg-known",
                "source_url": "https://usc.peoplegrove.com/profile/pg-known",
                "current_roles": [
                    {
                        "title": "Software Engineer",
                        "company": "Google",
                        "date_range": "2025 - Present",
                        "location": "",
                    }
                ],
            }
        },
    )

    summary = curate_peoplegrove_capture(
        capture,
        output_path=output,
        enrichment_path=enrichment,
    )

    assert summary["rows_accepted"] == 0
    assert summary["rejection_reasons"] == {"no_eligible_enriched_current_role": 1}
    assert summary["roles_selected_from_enrichment"] == 0
    with output.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []


def test_enrichment_rejects_duplicate_ids_and_tampered_bindings(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    enrichment = tmp_path / "career-journey.json"
    profile = _profile("Known", "Product builder", "pg-known")
    _write_capture(capture, [profile])
    source_url = str(profile["source_url"])
    role = {
        "title": "Product Manager",
        "company": "Google",
        "date_range": "2025 - Present",
        "location": "",
    }
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-known": {
                "source_record_id": "pg-known",
                "source_url": source_url,
                "current_roles": [role],
            },
            source_url: {
                "source_record_id": "pg-known",
                "source_url": source_url,
                "current_roles": [role],
            },
        },
    )
    with pytest.raises(PeopleGroveCurationError, match="Duplicate.*source_record_id"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )

    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-known": {
                "source_record_id": "pg-tampered",
                "source_url": source_url,
                "current_roles": [role],
            }
        },
    )
    with pytest.raises(PeopleGroveCurationError, match="does not match source_record_id"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )

    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-known": {
                "source_record_id": "pg-known",
                "source_url": source_url,
                "current_roles": [role],
            }
        },
        source_capture_sha256="0" * 64,
    )
    with pytest.raises(PeopleGroveCurationError, match="different capture SHA-256"):
        curate_peoplegrove_capture(
            capture,
            output_path=tmp_path / "curated.csv",
            enrichment_path=enrichment,
        )


def test_unparseable_headline_behavior_is_unchanged_without_enrichment(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    _write_capture(capture, [_profile("Known", "Product builder", "pg-known")])

    summary = curate_peoplegrove_capture(capture, output_path=output)

    assert summary["rows_accepted"] == 0
    assert summary["rejection_reasons"] == {"unparseable_current_role_company": 1}
    assert summary["enrichment_path"] == ""
    assert summary["enrichment_records"] == 0
    assert summary["roles_selected_from_enrichment"] == 0


def test_capture_dedupe_keeps_stronger_duplicate_record(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    _write_capture(
        capture,
        [
            _profile("Taylor Trojan", "Software Engineer at Useful", "same-record"),
            _profile("Taylor Trojan", "Product Manager at Useful", "same-record"),
        ],
    )

    summary = curate_peoplegrove_capture(capture, output_path=output)

    assert summary["rows_accepted"] == 1
    assert summary["rejection_reasons"] == {"duplicate_source_record_id": 1}
    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["title"] == "Product Manager"
    duplicate = next(
        decision for decision in summary["decisions"] if not decision["accepted"]
    )
    assert duplicate["duplicate_of_input_index"] == 2
    assert duplicate["original_reason"] == "no_high_signal_role"


def test_peoplegrove_query_profile_identity_is_preserved_for_dedupe(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    shared_path = "https://usc.peoplegrove.com/hub/usc-career-network/person"
    first = _profile(
        "First Product",
        "Product Manager at Google",
        "first",
        source_url=f"{shared_path}?modal=profile&userProfile=first&showBack=true",
    )
    second = _profile(
        "Second Product",
        "Product Manager at Stripe",
        "second",
        source_url=f"{shared_path}?modal=profile&userProfile=second&showBack=true",
    )
    _write_capture(capture, [first, second])

    summary = curate_peoplegrove_capture(capture, output_path=output)

    assert summary["rows_accepted"] == 2
    with output.open(newline="", encoding="utf-8") as handle:
        urls = {row["source_url"] for row in csv.DictReader(handle)}
    assert urls == {
        f"{shared_path}?userProfile=first",
        f"{shared_path}?userProfile=second",
    }


def test_optional_workspace_dedupe_reads_but_does_not_modify_tracker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    organization, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id("Google"),
            name="Google",
            organization_type=OrganizationType.COMPANY,
        )
    )
    contacts_path = workspace / "contacts.csv"
    organizations_path = workspace / "organizations.csv"
    contacts_before = contacts_path.read_bytes()
    organizations_before = organizations_path.read_bytes()

    # The existing organization alone is not enough to suppress a person.
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    _write_capture(capture, [_profile("Priya Product", "Product Manager at Google", "pg-2")])
    first = curate_peoplegrove_capture(capture, output_path=output, workspace=workspace)
    assert first["rows_accepted"] == 1

    workbook.upsert_contact(
        ContactRecord(
            contact_id=workbook.make_contact_id(organization.organization_id, "Priya Product"),
            organization_id=organization.organization_id,
            full_name="Priya Product",
            title="Product Manager",
        )
    )
    contacts_with_person = contacts_path.read_bytes()
    second = curate_peoplegrove_capture(capture, output_path=output, workspace=workspace)

    assert second["rows_accepted"] == 0
    assert second["rejection_reasons"] == {"already_in_workspace_person_company": 1}
    assert contacts_path.read_bytes() == contacts_with_person
    assert organizations_path.read_bytes() == organizations_before
    assert contacts_before != contacts_with_person


def test_cli_writes_curated_artifacts_without_execute_flag(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    output = tmp_path / "curated.csv"
    _write_capture(capture, [_profile("Priya Product", "Product Manager at Google", "pg-2")])

    result = CliRunner().invoke(
        app,
        [
            "curate-peoplegrove-capture",
            "--input-path",
            str(capture),
            "--output-path",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Accepted: 1 | rejected: 0" in result.output
    assert output.exists()
    assert output.with_suffix(".summary.json").exists()


def test_cli_accepts_capture_bound_enrichment_path(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    enrichment = tmp_path / "career-journey.json"
    output = tmp_path / "curated.csv"
    _write_capture(capture, [_profile("Priya Product", "Product builder", "pg-cli")])
    _write_enrichment(
        enrichment,
        capture,
        {
            "pg-cli": {
                "source_record_id": "pg-cli",
                "source_url": "https://usc.peoplegrove.com/profile/pg-cli",
                "current_roles": [
                    {
                        "title": "Product Manager",
                        "company": "Google",
                        "date_range": "2025 - Present",
                        "location": "Los Angeles, California",
                    }
                ],
            }
        },
    )

    result = CliRunner().invoke(
        app,
        [
            "curate-peoplegrove-capture",
            "--input-path",
            str(capture),
            "--enrichment-path",
            str(enrichment),
            "--output-path",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Accepted: 1 | rejected: 0" in result.output
    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["company"] == "Google"
    summary = json.loads(output.with_suffix(".summary.json").read_text(encoding="utf-8"))
    assert summary["enrichment_path"] == str(enrichment.resolve())
    assert summary["accepted_from_enrichment"] == 1


def test_capture_must_be_a_json_array(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    capture.write_text('{"profiles": []}', encoding="utf-8")

    with pytest.raises(PeopleGroveCurationError, match="JSON array"):
        curate_peoplegrove_capture(capture, output_path=tmp_path / "curated.csv")

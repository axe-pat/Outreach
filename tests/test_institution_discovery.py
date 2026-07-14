from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from outreach.config import OutreachSettings
from outreach.institution_discovery import (
    DEFAULT_INSTITUTION_SEARCHES,
    InstitutionDiscoveryError,
    LINKEDIN_US_GEO_URN,
    build_institution_capture_payload,
    curate_institution_capture,
    merge_institution_capture_payloads,
)
from outreach.services.linkedin import LinkedInScraper, missing_people_filter_url_params
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachWorkbook,
)


def _search(key: str, *results: dict[str, object]) -> dict[str, object]:
    spec = next(item for item in DEFAULT_INSTITUTION_SEARCHES if item.key == key)
    return {
        "key": key,
        "school_filter": spec.school_filter,
        "query": spec.query,
        "use_us_location": True,
        "limit": 100,
        "max_pages": 10,
        "termination_state": "bounded_sample_cap",
        "final_url": (
            "https://www.linkedin.com/search/results/people/"
            f"?keywords=product&geoUrn=%5B%22{LINKEDIN_US_GEO_URN}%22%5D"
            f"&schoolFilter=%5B%22{spec.school_urn}%22%5D"
        ),
        "raw_count": len(results),
        "results": list(results),
    }


def _person(
    name: str,
    title: str,
    slug: str,
    *,
    location: str = "San Francisco Bay Area",
    snippet: str = "",
) -> dict[str, object]:
    return {
        "name": name,
        "title": title,
        "connection_degree": "2nd",
        "location": location,
        "linkedin_url": f"https://www.linkedin.com/in/{slug}/?trk=search",
        "snippet": snippet,
        "raw_text": f"{name} {title} {location}",
    }


def _write_capture(path: Path, searches: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "captured_at": "2026-07-14T08:00:00+00:00",
                "captured_by": "test-capture",
                "searches": searches,
            }
        ),
        encoding="utf-8",
    )


def test_default_searches_cover_the_three_requested_institution_lanes() -> None:
    assert [item.key for item in DEFAULT_INSTITUTION_SEARCHES] == [
        "thapar_product_us",
        "usc_product_us",
        "usc_marshall_product_us",
    ]
    assert all(item.query == "product" for item in DEFAULT_INSTITUTION_SEARCHES)


def test_capture_merge_replaces_only_retried_search() -> None:
    base = build_institution_capture_payload(
        searches=[
            _search(spec.key, _person(spec.key, "Product Manager at BaseCo", spec.key))
            for spec in DEFAULT_INSTITUTION_SEARCHES
        ],
        captured_by="base",
    )
    retried = build_institution_capture_payload(
        searches=[
            _search(
                "usc_product_us",
                _person("Replacement", "Product Manager at NewCo", "replacement"),
            )
        ],
        captured_by="retry",
    )

    merged = merge_institution_capture_payloads(base, retried, captured_by="merged")

    by_key = {search["key"]: search for search in merged["searches"]}
    assert by_key["usc_product_us"]["results"][0]["name"] == "Replacement"
    assert by_key["thapar_product_us"]["results"][0]["name"] == "thapar_product_us"


def test_curator_dedupes_affiliations_suppresses_existing_and_surfaces_new_companies(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    airbyte, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-airbyte",
            name="Airbyte",
            organization_type=OrganizationType.COMPANY,
        )
    )
    existing_org, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-existing",
            name="Existing Co",
            organization_type=OrganizationType.COMPANY,
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="contact-existing",
            organization_id=existing_org.organization_id,
            full_name="Existing Person",
            title="Product Manager",
            linkedin_url="https://www.linkedin.com/in/existing-person/",
        )
    )

    alice = _person("Alice Alum", "Senior Product Manager at Airbyte", "alice-alum")
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "thapar_product_us",
                _person("Founder Friend", "Co-Founder @ SignalWorks", "founder-friend"),
                _person(
                    "Outside US",
                    "Product Manager at IndiaCo",
                    "outside-us",
                    location="Bengaluru, India",
                ),
            ),
            _search(
                "usc_product_us",
                alice,
                _person("Existing Person", "Product Manager at Existing Co", "existing-person"),
                _person("Ambiguous", "Product leader building useful things", "ambiguous"),
            ),
            _search("usc_marshall_product_us", alice),
        ],
    )
    output = workspace / "institution.csv"
    companies = workspace / "company_candidates.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=companies,
        workspace=workspace,
    )

    assert summary["raw_rows"] == 6
    assert summary["unique_profiles"] == 5
    assert summary["rows_accepted"] == 2
    assert summary["existing_contacts_suppressed"] == 1
    assert summary["reason_counts"]["explicit_non_us_location"] == 1
    assert summary["reason_counts"]["unparseable_current_role_company"] == 1
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    by_name = {row["full_name"]: row for row in rows}
    assert set(by_name) == {"Alice Alum", "Founder Friend"}
    assert by_name["Alice Alum"]["company"] == airbyte.name
    assert "usc-network" in by_name["Alice Alum"]["target_lists"]
    assert "marshall-network" in by_name["Alice Alum"]["target_lists"]
    assert by_name["Alice Alum"]["program"] == "USC Marshall School of Business"
    assert "company_discovery_candidate=true" in by_name["Founder Friend"]["notes"]
    assert "thapar-founder-executive" in by_name["Founder Friend"]["target_lists"]
    assert "usc-founder" not in by_name["Founder Friend"]["target_lists"]
    with companies.open(newline="", encoding="utf-8") as handle:
        company_rows = list(csv.DictReader(handle))
    assert [row["company"] for row in company_rows] == ["SignalWorks"]
    assert company_rows[0]["relationship_route"].startswith("promote through relationship lead")


def test_curator_rejects_search_without_us_filter(tmp_path: Path) -> None:
    search = _search("usc_product_us")
    search["use_us_location"] = False
    capture = tmp_path / "capture.json"
    _write_capture(capture, [search])

    with pytest.raises(InstitutionDiscoveryError, match="not bound to the US location filter"):
        curate_institution_capture(
            capture,
            output_path=tmp_path / "out.csv",
            company_candidates_path=tmp_path / "company-candidates.csv",
        )


def test_curator_canonicalizes_clear_divisions_and_rejects_bad_company_parse(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "thapar_product_us",
                _person(
                    "Amazon Lead",
                    "Lead Product Manager at Amazon driving global promotion platform growth through Gen AI powered tools",
                    "amazon-lead",
                ),
                _person(
                    "Bad Company",
                    "Head of Product at Howmet...",
                    "bad-company",
                ),
                _person(
                    "Wrong Lane",
                    "Program Manager at Acme",
                    "wrong-lane",
                ),
            )
        ],
    )
    output = tmp_path / "out.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=tmp_path / "company-candidates.csv",
    )

    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["full_name"], row["company"]) for row in rows] == [("Amazon Lead", "Amazon")]
    assert summary["reason_counts"]["invalid_current_company"] == 1
    assert summary["reason_counts"]["no_high_signal_product_role"] == 1


def test_curator_rejects_result_url_when_linkedin_dropped_filters(tmp_path: Path) -> None:
    search = _search("usc_product_us")
    search["final_url"] = "https://www.linkedin.com/search/results/people/?keywords=product"
    capture = tmp_path / "capture.json"
    _write_capture(capture, [search])

    with pytest.raises(InstitutionDiscoveryError, match="missing required filters"):
        curate_institution_capture(
            capture,
            output_path=tmp_path / "out.csv",
            company_candidates_path=tmp_path / "company-candidates.csv",
        )


@pytest.mark.parametrize(
    ("parameter", "replacement", "message"),
    [
        ("schoolFilter", "999", "school URN"),
        ("geoUrn", "999", "United States geo URN"),
    ],
)
def test_curator_rejects_wrong_filter_urn_even_when_parameter_is_present(
    tmp_path: Path,
    parameter: str,
    replacement: str,
    message: str,
) -> None:
    search = _search("usc_product_us")
    spec = next(item for item in DEFAULT_INSTITUTION_SEARCHES if item.key == "usc_product_us")
    expected = spec.school_urn if parameter == "schoolFilter" else LINKEDIN_US_GEO_URN
    search["final_url"] = str(search["final_url"]).replace(expected, replacement)
    capture = tmp_path / "capture.json"
    output = tmp_path / "out.csv"
    companies = tmp_path / "companies.csv"
    _write_capture(capture, [search])

    with pytest.raises(InstitutionDiscoveryError, match=message):
        curate_institution_capture(
            capture,
            output_path=output,
            company_candidates_path=companies,
        )

    assert not output.exists()
    assert not companies.exists()


def test_curator_records_exact_filter_urns(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_product_us",
                _person("USC Lead", "Product Manager at Acme", "usc-lead"),
            )
        ],
    )

    summary = curate_institution_capture(
        capture,
        output_path=tmp_path / "out.csv",
        company_candidates_path=tmp_path / "companies.csv",
    )

    coverage = summary["query_coverage"]["usc_product_us"]
    assert coverage["school_urn"] == "3084"
    assert coverage["us_geo_urn"] == LINKEDIN_US_GEO_URN


def test_explicit_current_company_conflict_fails_closed_and_never_becomes_candidate(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_marshall_product_us",
                _person(
                    "Stale Headline",
                    "Product Manager at Tesla",
                    "stale-headline",
                    snippet=("Current: Principal Product Manager - Technical at AFC (eCommerce)"),
                ),
            )
        ],
    )
    output = tmp_path / "out.csv"
    companies = tmp_path / "companies.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=companies,
    )

    assert summary["rows_accepted"] == 0
    assert summary["reason_counts"]["conflicting_current_company_evidence"] == 1
    with output.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []
    with companies.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []


def test_explicit_current_title_wins_when_company_agrees(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_product_us",
                _person(
                    "Precise Role",
                    "Product at Apple",
                    "precise-role",
                    snippet="Current: Senior Product Manager at Apple",
                ),
            )
        ],
    )
    output = tmp_path / "out.csv"

    curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=tmp_path / "companies.csv",
    )

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["title"] == "Senior Product Manager"
    assert row["company"] == "Apple"
    assert "outreach-hold" not in row["target_lists"]


def test_location_recovers_from_raw_card_text(tmp_path: Path) -> None:
    person = _person(
        "Mohinder Pahuja, CFA, FRM",
        "Sr. Product Manager at Moody's Analytics",
        "mohinder-pahuja",
        location="Mohinder Pahuja, CFA, FRM • 2nd",
    )
    person["raw_text"] = (
        "Mohinder Pahuja, CFA, FRM • 2nd Sr. Product Manager at Moody's Analytics "
        "Fremont, California, United States Connect"
    )
    capture = tmp_path / "capture.json"
    _write_capture(capture, [_search("thapar_product_us", person)])
    output = tmp_path / "out.csv"

    curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=tmp_path / "companies.csv",
    )

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["location"] == "Fremont, California, United States"
    assert "resolved_location=Fremont, California, United States" in row["notes"]


@pytest.mark.parametrize(
    "location",
    [
        "Greater Bengaluru Area",
        "Ho Chi Minh City, Vietnam",
        "Yerevan, Armenia",
        "Greater Delhi Area",
        "Profile Name • 2nd",
        "",
    ],
)
def test_curator_rejects_non_us_or_unverified_location(
    tmp_path: Path,
    location: str,
) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_product_us",
                _person(
                    "Location Fail", "Product Manager at Acme", "location-fail", location=location
                ),
            )
        ],
    )

    summary = curate_institution_capture(
        capture,
        output_path=tmp_path / "out.csv",
        company_candidates_path=tmp_path / "companies.csv",
    )

    assert summary["rows_accepted"] == 0
    assert (
        summary["reason_counts"].get("explicit_non_us_location", 0)
        + summary["reason_counts"].get("unverified_us_location", 0)
        == 1
    )


def test_product_engineering_gets_engineering_route_not_product_route(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_product_us",
                _person(
                    "Engineering Leader",
                    "Head of Product Eng at Aramas AI",
                    "engineering-leader",
                    snippet="Current: Head of Product Engineering at Aramas AI",
                ),
            )
        ],
    )
    output = tmp_path / "out.csv"
    companies = tmp_path / "companies.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=companies,
    )

    assert summary["accepted_category_counts"] == {"product_engineering": 1}
    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["contact_type"] == "Engineering"
    assert "institution-engineering" in row["target_lists"]
    assert "usc-engineering" in row["target_lists"]
    assert "institution-product" not in row["target_lists"]
    with companies.open(newline="", encoding="utf-8") as handle:
        candidate = next(csv.DictReader(handle))
    assert candidate["role_categories"] == "product_engineering"


def test_product_line_manager_remains_in_product_route(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_marshall_product_us",
                _person(
                    "Product Line Lead",
                    "Product at VMware",
                    "product-line-lead",
                    snippet="Current: Senior Product Line Manager at VMware",
                ),
            )
        ],
    )
    output = tmp_path / "out.csv"

    curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=tmp_path / "companies.csv",
    )

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["title"] == "Senior Product Line Manager"
    assert row["contact_type"] == "Product"
    assert "institution-product" in row["target_lists"]


def test_headline_only_role_is_durably_held_for_current_role_review(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_product_us",
                _person("Headline Only", "Product Manager at Acme", "headline-only"),
            )
        ],
    )
    output = tmp_path / "out.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=tmp_path / "companies.csv",
    )

    assert summary["current_role_review_required"] == 1
    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert {"current-role-review-required", "outreach-hold"}.issubset(
        set(row["target_lists"].split(";"))
    )
    assert "current_role_review_required=true" in row["notes"]


def test_semantic_existing_contact_match_is_emitted_as_enrichment_not_candidate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    organization, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-digital-room-inc",
            name="Digital Room Inc",
            organization_type=OrganizationType.COMPANY,
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="contact-jennifer-so",
            organization_id=organization.organization_id,
            full_name="Jennifer So",
            title="Head of Product Management",
        )
    )
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_product_us",
                _person(
                    "Jennifer So",
                    "Head of Product Management at Digital Room",
                    "sojennifer",
                    snippet="Current: Head of Product Management at Digital Room Inc",
                ),
            )
        ],
    )
    output = workspace / "institution.csv"
    companies = workspace / "companies.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=companies,
        workspace=workspace,
    )

    assert summary["rows_accepted"] == 1
    assert summary["existing_contact_enrichments"] == 1
    assert summary["known_company_leads"] == 1
    assert summary["new_company_leads"] == 0
    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["company"] == "Digital Room Inc"
    assert row["linkedin_url"] == "https://www.linkedin.com/in/sojennifer/"
    assert "existing_contact_enrichment=true" in row["notes"]
    with companies.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []


@pytest.mark.parametrize(
    ("captured_company", "canonical_company"),
    [
        ("The Walt Disney Company", "Disney"),
        ("Roku Inc.", "Roku"),
    ],
)
def test_semantic_company_aliases_match_existing_organizations(
    tmp_path: Path,
    captured_company: str,
    canonical_company: str,
) -> None:
    workspace = tmp_path / "workspace"
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id=f"org-{canonical_company.lower()}",
            name=canonical_company,
            organization_type=OrganizationType.COMPANY,
        )
    )
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_product_us",
                _person(
                    "Alias Lead",
                    f"Senior Product Manager at {captured_company}",
                    "alias-lead",
                    snippet=f"Current: Senior Product Manager at {captured_company}",
                ),
            )
        ],
    )
    output = workspace / "out.csv"
    companies = workspace / "companies.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=companies,
        workspace=workspace,
    )

    assert summary["known_company_leads"] == 1
    with output.open(newline="", encoding="utf-8") as handle:
        assert next(csv.DictReader(handle))["company"] == canonical_company
    with companies.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []


def test_ambiguous_composite_company_is_rejected_and_never_promoted(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "thapar_product_us",
                _person(
                    "Composite Lead",
                    "AI Product & Strategy at Comcast NBCUniversal",
                    "composite-lead",
                    snippet="Current: AI Product & Strategy at Comcast NBCUniversal",
                ),
            )
        ],
    )
    output = tmp_path / "out.csv"
    companies = tmp_path / "companies.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=companies,
    )

    assert summary["rows_accepted"] == 0
    assert summary["reason_counts"]["ambiguous_composite_company"] == 1
    with companies.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []


def test_legacy_or_acquired_company_requires_enrichment_and_is_never_promoted(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "thapar_product_us",
                _person(
                    "Legacy Employer Lead",
                    "Senior Product Manager at Conexant Systems Inc",
                    "legacy-employer-lead",
                ),
            )
        ],
    )
    output = tmp_path / "out.csv"
    companies = tmp_path / "companies.csv"

    summary = curate_institution_capture(
        capture,
        output_path=output,
        company_candidates_path=companies,
    )

    assert summary["rows_accepted"] == 0
    assert summary["reason_counts"]["legacy_or_acquired_company_requires_enrichment"] == 1
    with output.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []
    with companies.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []


def test_custom_output_path_derives_safe_sibling_company_queue(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    _write_capture(
        capture,
        [
            _search(
                "usc_product_us",
                _person("Safe Output", "Product Manager at Acme", "safe-output"),
            )
        ],
    )
    output = tmp_path / "custom.csv"

    summary = curate_institution_capture(capture, output_path=output)

    assert Path(summary["company_candidates_path"]) == tmp_path / "custom.company-candidates.csv"
    assert (tmp_path / "custom.company-candidates.csv").exists()


def test_people_filter_url_contract_detects_silent_filter_loss() -> None:
    assert missing_people_filter_url_params(
        "https://www.linkedin.com/search/results/people/?keywords=product",
        school="University of Southern California",
        use_us_location=True,
    ) == ["school", "United States location"]
    assert (
        missing_people_filter_url_params(
            "https://www.linkedin.com/search/results/people/"
            "?keywords=product&geoUrn=%5B1%5D&schoolFilter=%5B2%5D",
            school="University of Southern California",
            use_us_location=True,
        )
        == []
    )


def test_linkedin_filter_application_allows_institution_only_search(monkeypatch) -> None:
    scraper = LinkedInScraper(OutreachSettings())
    filled: list[tuple[str, str]] = []

    class _Button:
        def click(self) -> None:
            return None

    class _Page:
        def get_by_text(self, _text: str, exact: bool = False) -> _Button:
            return _Button()

    monkeypatch.setattr(scraper, "_click_filter_control", lambda *_args: None)
    monkeypatch.setattr(scraper, "_human_pause", lambda *_args: None)
    monkeypatch.setattr(
        scraper,
        "_fill_filter_typeahead",
        lambda _page, trigger, value: filled.append((trigger, value)),
    )

    scraper._apply_people_filters(
        _Page(),
        company="",
        school="University of Southern California",
        connection_degree=None,
        use_us_location=True,
    )

    assert filled == [
        ("Add a location", "United States"),
        ("Add a school", "University of Southern California"),
    ]

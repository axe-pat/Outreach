from __future__ import annotations

from pathlib import Path

from outreach.discovery.adapters import BuiltInCompaniesAdapter, YCombinatorCompanyDirectoryAdapter
from outreach.discovery.models import DiscoveredOrganization
from outreach.discovery.registry import get_source_definition, list_source_definitions
from outreach.tracking import OrganizationType, OutreachWorkbook


FIXTURE = Path(__file__).parent / "fixtures" / "yc_listing_sample.html"
DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "yc_detail_sample.html"
DETAIL_JOBS_FIXTURE = Path(__file__).parent / "fixtures" / "yc_detail_jobs_sample.html"
BUILTIN_LISTING_FIXTURE = Path(__file__).parent / "fixtures" / "builtin_companies_listing_sample.html"
BUILTIN_DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "builtin_company_detail_sample.html"


def test_registry_exposes_expected_sources() -> None:
    source_ids = [entry.definition.source_id for entry in list_source_definitions()]

    assert "yc_los_angeles" in source_ids
    assert "yc_sf_bay_hiring" in source_ids
    assert "builtin_la_companies" in source_ids


def test_yc_adapter_parses_company_cards() -> None:
    entry = get_source_definition("yc_los_angeles")
    html = FIXTURE.read_text(encoding="utf-8")
    adapter = YCombinatorCompanyDirectoryAdapter()

    items = adapter.discover(entry.definition, lambda _url: html, limit=10)

    assert len(items) == 2
    assert items[0].organization_name == "Mount"
    assert items[0].jobs_url.endswith("/companies/mount/jobs")
    assert items[0].opportunity_title == "Open roles via YC"
    assert items[0].tags == ["insurance", "ai"]
    assert items[1].organization_name == "Fifth Door"
    assert items[1].city == "Los Angeles"
    assert items[1].opportunity_title == ""


def test_yc_adapter_enriches_founders_and_company_facts() -> None:
    detail_html = DETAIL_FIXTURE.read_text(encoding="utf-8")
    adapter = YCombinatorCompanyDirectoryAdapter()
    base = DiscoveredOrganization(
        organization_name="Endstack",
        company_url="https://www.ycombinator.com/companies/endstack",
        source_page_url="https://www.ycombinator.com/companies/location/los-angeles",
        source_item_url="https://www.ycombinator.com/companies/endstack",
    )

    item = adapter.enrich_company(base, detail_html)

    assert item.website == "https://www.endstack.com"
    assert item.founded_year == "2024"
    assert item.jobs_count == 0
    assert [contact.full_name for contact in item.contacts] == ["Sam Park", "Josh Park"]


def test_yc_adapter_extracts_jobs_from_detail_page() -> None:
    detail_html = DETAIL_JOBS_FIXTURE.read_text(encoding="utf-8")
    adapter = YCombinatorCompanyDirectoryAdapter()
    base = DiscoveredOrganization(
        organization_name="Mount",
        company_url="https://www.ycombinator.com/companies/mount",
        source_page_url="https://www.ycombinator.com/companies/location/san-francisco-bay-area/hiring",
        source_item_url="https://www.ycombinator.com/companies/mount",
        jobs_url="https://www.ycombinator.com/companies/mount/jobs",
    )
    item = adapter.enrich_company(base, detail_html)

    assert len(item.opportunities) == 1
    assert item.opportunities[0].title == "Founding AI Engineer, Underwriting & AI Risk"
    assert item.opportunities[0].compensation_hint == "$170K - $210K"
    assert item.opportunities[0].equity_hint == "0.40% - 1.40%"
    assert item.opportunities[0].experience_hint == "Any (new grads ok)"


def test_yc_adapter_merges_full_jobs_page_with_preview_jobs() -> None:
    detail_html = """
    <html><body>
      <p>Jobs at Icarus</p>
      <a href="/companies/icarus/jobs">View all jobs</a>
      <a href="/jobs/preview-role">Preview Role</a>
      <p>Los Angeles, CA, US</p>
      <p>$120K - $220K</p>
      <p>1+ years</p>
      <a href="https://account.ycombinator.com/jobs/preview-role">Apply Now ›</a>
    </body></html>
    """
    jobs_html = """
    <html><body>
      <p>Jobs at Icarus</p>
      <a href="/jobs/full-role-a">Full Role A</a>
      <p>Los Angeles, CA, US</p>
      <p>$120K - $220K</p>
      <p>1+ years</p>
      <a href="https://account.ycombinator.com/jobs/full-role-a">Apply Now ›</a>
      <a href="/jobs/full-role-b">Full Role B</a>
      <p>Los Angeles, CA, US</p>
      <p>$120K - $220K</p>
      <p>1+ years</p>
      <a href="https://account.ycombinator.com/jobs/full-role-b">Apply Now ›</a>
      <p>Founded:2023</p>
    </body></html>
    """
    adapter = YCombinatorCompanyDirectoryAdapter()
    base = DiscoveredOrganization(
        organization_name="Icarus",
        company_url="https://www.ycombinator.com/companies/icarus",
        source_page_url="https://www.ycombinator.com/companies/location/los-angeles",
        source_item_url="https://www.ycombinator.com/companies/icarus",
        jobs_url="https://www.ycombinator.com/companies/icarus/jobs",
    )

    item = adapter.enrich_company(base, detail_html, jobs_html=jobs_html)

    assert len(item.opportunities) == 3
    assert [opportunity.title for opportunity in item.opportunities] == ["Preview Role", "Full Role A", "Full Role B"]


def test_yc_discover_fetches_jobs_page_after_detail_reveals_jobs_url() -> None:
    entry = get_source_definition("yc_los_angeles")
    listing_html = """
    <html><body>
      <a href="/companies/icarus">Icarus Y Combinator Logo Fall 2025 • Active • 16 employees • Los Angeles, CA</a>
      <p>hardware</p>
    </body></html>
    """
    detail_html = """
    <html><body>
      <a href="/companies/icarus/jobs">Jobs</a>8
      <p>Jobs at Icarus</p>
      <a href="/companies/icarus/jobs">View all jobs</a>
      <a href="/jobs/preview-role">Preview Role</a>
      <p>Los Angeles, CA, US</p>
      <p>$120K - $220K</p>
      <p>1+ years</p>
      <a href="https://account.ycombinator.com/jobs/preview-role">Apply Now ›</a>
      <p>Founded:2023</p>
      <p>Team Size:16</p>
      <p>Location:Los Angeles, CA</p>
    </body></html>
    """
    jobs_html = """
    <html><body>
      <p>Jobs at Icarus</p>
      <a href="/jobs/full-role-a">Full Role A</a>
      <p>Los Angeles, CA, US</p>
      <p>$120K - $220K</p>
      <p>1+ years</p>
      <a href="https://account.ycombinator.com/jobs/full-role-a">Apply Now ›</a>
      <a href="/jobs/full-role-b">Full Role B</a>
      <p>Los Angeles, CA, US</p>
      <p>$120K - $220K</p>
      <p>1+ years</p>
      <a href="https://account.ycombinator.com/jobs/full-role-b">Apply Now ›</a>
    </body></html>
    """

    html_map = {
        entry.definition.seed_urls[0]: listing_html,
        "https://www.ycombinator.com/companies/icarus": detail_html,
        "https://www.ycombinator.com/companies/icarus/jobs": jobs_html,
    }
    adapter = YCombinatorCompanyDirectoryAdapter()

    items = adapter.discover(entry.definition, lambda url: html_map[url], limit=1, enrich_details=True)

    assert len(items) == 1
    assert items[0].jobs_count == 8
    assert len(items[0].opportunities) == 3


def test_builtin_adapter_parses_listing_cards() -> None:
    entry = get_source_definition("builtin_la_companies")
    html = BUILTIN_LISTING_FIXTURE.read_text(encoding="utf-8")
    adapter = BuiltInCompaniesAdapter()

    items = adapter.discover(entry.definition, lambda _url: html, limit=10)

    assert len(items) == 2
    assert items[0].organization_name == "BuildOps"
    assert items[0].jobs_url.endswith("/company/buildops/jobs")
    assert items[0].team_size == "500 Employees"
    assert "software" in items[0].tags
    assert items[1].organization_name == "Doodle Labs"


def test_builtin_adapter_enriches_company_and_jobs() -> None:
    detail_html = BUILTIN_DETAIL_FIXTURE.read_text(encoding="utf-8")
    adapter = BuiltInCompaniesAdapter()
    base = DiscoveredOrganization(
        organization_name="BuildOps",
        company_url="https://www.builtinla.com/company/buildops",
        source_page_url="https://www.builtinla.com/companies",
        source_item_url="https://www.builtinla.com/company/buildops",
    )

    item = adapter.enrich_company(base, detail_html)

    assert item.website.startswith("http://buildops.com")
    assert item.city == "Santa Monica"
    assert item.founded_year == "2018"
    assert len(item.opportunities) == 2
    assert item.opportunities[0].title == "Enterprise Sales Development Representative"
    assert item.opportunities[1].location == "Hybrid | Los Angeles, CA, USA"
    assert item.opportunities[0].apply_url == "https://www.builtinla.com/job/enterprise-sales-development-representative/8515617"


def test_discovery_batch_import_writes_workbook_rows(tmp_path) -> None:
    workbook = OutreachWorkbook(tmp_path / "workspace")
    entry = get_source_definition("yc_sf_bay_hiring")

    summary = workbook.import_discovery_batch(
        source_id=entry.definition.source_id,
        source_label=entry.definition.label,
        source_kind=entry.definition.source_kind,
        base_url=entry.definition.seed_urls[0],
        extraction_method=entry.definition.adapter.value,
        target_lists=entry.definition.target_lists,
        organization_type=OrganizationType.STARTUP,
        opportunity_type=entry.definition.opportunity_type,
        items=[
            {
                "organization_name": "Mount",
                "target_lists": "yc;startup;sf;hiring",
                "city": "San Francisco",
                "company_url": "https://www.ycombinator.com/companies/mount",
                "jobs_url": "https://www.ycombinator.com/companies/mount/jobs",
                "description": "Insure and secure your AI Agents",
                "status": "Researching",
                "source_page_url": entry.definition.seed_urls[0],
                "source_item_url": "https://www.ycombinator.com/companies/mount",
                "tags": ["insurance", "ai"],
                "batch": "Spring 2026",
                "founded_year": "2024",
                "team_size": "2 employees",
                "location": "San Francisco, CA, USA",
                "jobs_count": 1,
                "contacts": [
                    {
                        "full_name": "Fabian Amherd",
                        "title": "Founder",
                        "linkedin_url": "https://www.linkedin.com/in/fabian-amherd",
                        "bio": "Founder at Mount",
                        "contact_type": "founder",
                    }
                ],
                "opportunities": [
                    {
                        "title": "Founding AI Engineer, Underwriting & AI Risk",
                        "location": "San Francisco, CA, US",
                        "compensation_hint": "$170K - $210K",
                        "equity_hint": "0.40% - 1.40%",
                        "experience_hint": "Any (new grads ok)",
                        "apply_url": "https://account.ycombinator.com/jobs/founding-ai-engineer",
                    }
                ],
            }
        ],
    )

    counts = workbook.summary_counts()

    assert summary.organizations_added == 1
    assert summary.opportunities_added == 1
    assert summary.contacts_added == 1
    assert counts["organizations"] == 1
    assert counts["opportunities"] == 1
    assert counts["contacts"] == 1
    assert counts["sources"] == 1
    assert "founded_year=2024" in workbook.list_organizations()[0].notes
    assert "jobs_count=1" in workbook.list_organizations()[0].notes

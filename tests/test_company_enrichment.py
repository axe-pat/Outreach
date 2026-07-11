from pathlib import Path

from outreach.account_tracker import build_account_rows
from outreach.company_enrichment import enrich_company_contexts, resolve_company_websites
from outreach.tracking import OpportunityRecord, OrganizationRecord, OrganizationType, OutreachWorkbook


class FakeFetcher:
    def __init__(self, html_by_url: dict[str, str]) -> None:
        self.html_by_url = html_by_url

    def fetch_text(self, url: str) -> str:
        if url not in self.html_by_url:
            raise ValueError(f"unexpected url: {url}")
        return self.html_by_url[url]


class RoutingFetcher:
    def __init__(self, html_by_url_part: dict[str, str]) -> None:
        self.html_by_url_part = html_by_url_part

    def fetch_text(self, url: str) -> str:
        for url_part, html in self.html_by_url_part.items():
            if url_part in url:
                return html
        raise ValueError(f"unexpected url: {url}")


def test_company_enrichment_updates_missing_context_from_job_rationale(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-centerfield",
            name="Centerfield",
            organization_type=OrganizationType.COMPANY,
            target_lists="jobs;resume_generator;pre_apply",
            notes="Imported from ResumeGenerator v1 jobs.xlsx | latest_resume_status=generated",
        )
    )
    workbook.upsert_opportunity(
        OpportunityRecord(
            opportunity_id="opp-centerfield",
            organization_id="org-centerfield",
            title="Product Manager Intern",
            opportunity_type="internship",
            source_url="https://www.linkedin.com/jobs/view/123/",
            notes=(
                "fit_rationale=[Proceed / High Priority] Structured PM internship with strong "
                "technical leverage from data/platform background and AI workflow ownership."
            ),
        )
    )

    results = enrich_company_contexts(tmp_path, execute=True, use_web_search=False)

    assert results[0].company == "Centerfield"
    assert results[0].status == "updated"
    assert results[0].confidence == "inferred_from_job"

    enriched = OutreachWorkbook(tmp_path).list_organizations()[0]
    assert "context_confidence=inferred_from_job" in enriched.notes
    assert "data-platform" in enriched.notes
    assert "artificial-intelligence" in enriched.notes

    row = build_account_rows(tmp_path)[0]
    assert "context_inferred_from_job" in row.data_quality_flags
    assert "needs_domain_enrichment" not in row.data_quality_flags


def test_company_enrichment_prefers_external_website_context(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-typeface",
            name="Typeface",
            organization_type=OrganizationType.COMPANY,
            website="https://www.typeface.ai",
            notes="Imported from ResumeGenerator v1 jobs.xlsx | latest_resume_status=generated",
        )
    )
    fetcher = FakeFetcher(
        {
            "https://www.typeface.ai": """
            <html>
              <head>
                <title>Typeface AI content platform</title>
                <meta name="description" content="Typeface is a generative AI platform for enterprise content workflows and brand-safe marketing automation.">
              </head>
              <body><p>Enterprise teams use Typeface to automate AI content workflows. Backed by Sequoia and GV.</p></body>
            </html>
            """
        }
    )

    results = enrich_company_contexts(
        tmp_path,
        execute=True,
        use_web_search=False,
        fetcher=fetcher,
    )

    assert results[0].company == "Typeface"
    assert results[0].confidence == "external_verified"

    enriched = OutreachWorkbook(tmp_path).list_organizations()[0]
    assert "context_confidence=external_verified" in enriched.notes
    assert "description=Typeface is a generative AI platform" in enriched.notes
    assert "artificial-intelligence" in enriched.notes
    assert "workflow-automation" in enriched.notes
    assert "prestige_signals=venture-backed,sequoia-backed,gv-backed" in enriched.notes


def test_company_enrichment_does_not_treat_customer_mentions_as_investors(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-workflow",
            name="WorkflowCo",
            organization_type=OrganizationType.COMPANY,
            website="https://www.workflowco.ai",
            notes="Imported from ResumeGenerator v1 jobs.xlsx",
        )
    )
    fetcher = FakeFetcher(
        {
            "https://www.workflowco.ai": """
            <html>
              <head>
                <title>WorkflowCo AI platform</title>
                <meta name="description" content="WorkflowCo builds AI workflow automation for operations teams.">
              </head>
              <body><p>Customers include Microsoft, finance teams, and enterprise operators. Institutional investors also use the product.</p></body>
            </html>
            """
        }
    )

    results = enrich_company_contexts(
        tmp_path,
        execute=True,
        use_web_search=False,
        fetcher=fetcher,
    )

    assert results[0].prestige_signals == []
    enriched = OutreachWorkbook(tmp_path).list_organizations()[0]
    assert "prestige_signals=" not in enriched.notes


def test_external_refresh_replaces_stale_prestige_signals(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-plain",
            name="Plain AI",
            organization_type=OrganizationType.COMPANY,
            website="https://www.plain.ai",
            notes=(
                "tags=artificial-intelligence | description=Old context. | "
                "prestige_signals=microsoft-backed,venture-backed | "
                "prestige_evidence_url=https://old.example.com | "
                "context_confidence=external_verified"
            ),
        )
    )
    fetcher = FakeFetcher(
        {
            "https://www.plain.ai": """
            <html>
              <head>
                <title>Plain AI workflow platform</title>
                <meta name="description" content="Plain AI builds workflow automation software for internal teams.">
              </head>
              <body><p>Plain AI helps teams automate internal operations.</p></body>
            </html>
            """
        }
    )

    results = enrich_company_contexts(
        tmp_path,
        execute=True,
        use_web_search=False,
        fetcher=fetcher,
        force=True,
    )

    assert results[0].status == "updated"
    enriched = OutreachWorkbook(tmp_path).list_organizations()[0]
    assert "prestige_signals=" not in enriched.notes
    assert "prestige_evidence_url=" not in enriched.notes


def test_company_website_resolution_from_search_writes_verified_website(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-typeface",
            name="Typeface",
            organization_type=OrganizationType.COMPANY,
            source_url="https://www.linkedin.com/jobs/view/123/",
            notes="Imported from ResumeGenerator v1 jobs.xlsx | context_confidence=inferred_from_job",
        )
    )
    fetcher = RoutingFetcher(
        {
            "duckduckgo.com/html": """
            <html><body>
              <a href="/l/?uddg=https%3A%2F%2Fwww.typeface.ai%2F">Typeface official website</a>
            </body></html>
            """,
            "https://www.typeface.ai": """
            <html>
              <head><title>Typeface enterprise AI</title></head>
              <body><p>Typeface helps enterprise marketing teams create on-brand AI campaigns.</p></body>
            </html>
            """,
        }
    )

    results = resolve_company_websites(tmp_path, execute=True, fetcher=fetcher)

    assert results[0].status == "resolved"
    assert results[0].website == "https://www.typeface.ai"
    enriched = OutreachWorkbook(tmp_path).list_organizations()[0]
    assert enriched.website == "https://www.typeface.ai"
    assert "website_resolution_source=web_search" in enriched.notes


def test_company_website_resolution_rejects_generic_short_domain_match(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-gen",
            name="Gen",
            organization_type=OrganizationType.COMPANY,
            source_url="https://www.linkedin.com/jobs/view/123/",
            notes="Imported from ResumeGenerator v1 jobs.xlsx | context_confidence=inferred_from_job",
        )
    )
    fetcher = RoutingFetcher(
        {
            "duckduckgo.com/html": """
            <html><body>
              <a href="/l/?uddg=https%3A%2F%2Fgen.example.com%2F">Generic tools</a>
            </body></html>
            """,
            "https://gen.example.com": """
            <html>
              <head><title>Generic tools homepage</title></head>
              <body><p>Generic tools for creators and operators.</p></body>
            </html>
            """,
        }
    )

    results = resolve_company_websites(
        tmp_path,
        execute=True,
        fetcher=fetcher,
        use_web_search=False,
        allow_domain_guess=True,
    )

    assert results[0].status == "no_url_found"
    assert OutreachWorkbook(tmp_path).list_organizations()[0].website == ""


def test_company_website_resolution_keeps_meaningful_short_prefix(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-d-matrix",
            name="d-Matrix",
            organization_type=OrganizationType.COMPANY,
            source_url="https://www.linkedin.com/jobs/view/123/",
            notes="Imported from ResumeGenerator v1 jobs.xlsx | context_confidence=inferred_from_job",
        )
    )
    fetcher = RoutingFetcher(
        {
            "https://dmatrix.com": """
            <html><head><title>Spreadsheet tools</title></head>
            <body><p>Data matrix helpers for spreadsheets.</p></body></html>
            """,
            "https://dmatrix.ai": """
            <html><head><title>Analytics dashboards</title></head>
            <body><p>Matrix dashboards for analytics teams.</p></body></html>
            """,
            "https://d-matrix.com": """
            <html><head><title>d-Matrix AI compute</title></head>
            <body><p>d-Matrix builds AI compute platforms for inference workloads.</p></body></html>
            """,
        }
    )

    results = resolve_company_websites(
        tmp_path,
        execute=True,
        fetcher=fetcher,
        use_web_search=False,
        allow_domain_guess=True,
    )

    assert results[0].status == "resolved"
    assert results[0].website == "https://d-matrix.com"
    enriched = OutreachWorkbook(tmp_path).list_organizations()[0]
    assert enriched.website == "https://d-matrix.com"


def test_company_website_resolution_rejects_generic_domain_for_compound_name(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-d-matrix",
            name="d-Matrix",
            organization_type=OrganizationType.COMPANY,
            source_url="https://www.linkedin.com/jobs/view/123/",
            notes="Imported from ResumeGenerator v1 jobs.xlsx | context_confidence=inferred_from_job",
        )
    )
    fetcher = RoutingFetcher(
        {
            "duckduckgo.com/html": """
            <html><body>
              <a href="/l/?uddg=https%3A%2F%2Fmatrix.com%2F">Matrix official website</a>
            </body></html>
            """,
            "https://dmatrix.com": "<html><body>No public website here.</body></html>",
            "https://dmatrix.ai": "<html><body>No public website here.</body></html>",
            "https://d-matrix.com": "<html><body>No public website here.</body></html>",
            "https://d-matrix.ai": "<html><body>No public website here.</body></html>",
            "https://matrix.com": """
            <html>
              <head><title>Matrix hair care</title></head>
              <body><p>Matrix professional hair care and styling products.</p></body>
            </html>
            """,
        }
    )

    results = resolve_company_websites(tmp_path, execute=True, fetcher=fetcher)

    assert results[0].status == "no_url_found"
    assert OutreachWorkbook(tmp_path).list_organizations()[0].website == ""


def test_company_website_resolution_rejects_parked_domain_guess(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-stride",
            name="Stride, Inc.",
            organization_type=OrganizationType.COMPANY,
            source_url="https://www.linkedin.com/search/results/people/",
            notes="Imported from LinkedIn outreach",
        )
    )
    fetcher = RoutingFetcher(
        {
            "https://stride.com": "",
            "https://stride.ai": "",
            "https://stride.io": """
            <html>
              <head><title>Stride.io premium domain</title></head>
              <body>
                <p>Stride.io is a verified premium domain available for purchase.</p>
                <p>Buy now or pay in installments. Free transaction support, no extra fees.</p>
              </body>
            </html>
            """,
        }
    )

    results = resolve_company_websites(
        tmp_path,
        execute=True,
        fetcher=fetcher,
        use_web_search=False,
        allow_domain_guess=True,
    )

    assert results[0].status == "no_url_found"
    assert OutreachWorkbook(tmp_path).list_organizations()[0].website == ""


def test_company_website_resolution_disables_uncorroborated_guesses_by_default(
    tmp_path: Path,
) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-two-labs",
            name="Two Labs",
            organization_type=OrganizationType.COMPANY,
            source_url="https://www.linkedin.com/jobs/view/123/",
            notes="Imported from ResumeGenerator v1 jobs.xlsx",
        )
    )
    fetcher = RoutingFetcher(
        {
            "https://two.com": """
            <html><head><title>Two Labs official website</title></head>
            <body><p>Two Labs builds software products.</p></body></html>
            """,
        }
    )

    results = resolve_company_websites(
        tmp_path,
        execute=True,
        fetcher=fetcher,
        use_web_search=False,
    )

    assert results[0].status == "no_url_found"
    assert OutreachWorkbook(tmp_path).list_organizations()[0].website == ""


def test_company_enrichment_rejects_parked_context_page(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-trucker-path",
            name="Trucker Path",
            organization_type=OrganizationType.COMPANY,
            website="https://trucker-path.com",
            notes="Imported from LinkedIn outreach",
        )
    )
    fetcher = FakeFetcher(
        {
            "https://trucker-path.com": """
            <html>
              <head><title>trucker-path.com</title></head>
              <body>
                <p>trucker-path.com is your first and best source for information about trucker path.</p>
                <p>Here you will also find topics relating to issues of general interest.</p>
                <p>We hope you find what you are looking for!</p>
              </body>
            </html>
            """,
        }
    )

    results = enrich_company_contexts(
        tmp_path,
        execute=True,
        use_web_search=False,
        fetcher=fetcher,
        force=True,
        fallback_to_jobs=False,
    )

    assert results[0].status == "no_context_found"
    enriched = OutreachWorkbook(tmp_path).list_organizations()[0]
    assert "context_confidence=external_verified" not in enriched.notes


def test_company_website_resolution_requires_full_identity_for_generic_suffix(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-advantage-solutions",
            name="Advantage Solutions",
            organization_type=OrganizationType.COMPANY,
            source_url="https://www.linkedin.com/jobs/view/123/",
            notes="Imported from ResumeGenerator v1 jobs.xlsx",
        )
    )
    fetcher = RoutingFetcher(
        {
            "https://advantagesolutions.com": "",
            "https://advantagesolutions.ai": "",
            "https://advantagesolutions.io": "",
            "https://advantagesolutions.co": "",
            "https://advantage-solutions.com": "",
            "https://advantage-solutions.ai": "",
            "https://advantage-solutions.io": "",
            "https://advantage.com": """
            <html>
              <head><title>Advantage Rent a Car</title></head>
              <body><p>Find cheap car rentals with Advantage. Serving airports and offering no deposit options.</p></body>
            </html>
            """,
        }
    )

    results = resolve_company_websites(tmp_path, execute=True, fetcher=fetcher)

    assert results[0].status == "no_url_found"
    assert OutreachWorkbook(tmp_path).list_organizations()[0].website == ""


def test_company_enrichment_rejects_javascript_only_page(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path)
    workbook.initialize()
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-bandwidth",
            name="Bandwidth",
            organization_type=OrganizationType.COMPANY,
            website="https://bandwidth.co",
            notes="Imported from ResumeGenerator v1 jobs.xlsx",
        )
    )
    fetcher = FakeFetcher(
        {
            "https://bandwidth.co": """
            <html>
              <body><p>This form requires JavaScript. Please enable JavaScript and reload this page or switch to a browser that supports it!</p></body>
            </html>
            """,
        }
    )

    results = enrich_company_contexts(
        tmp_path,
        execute=True,
        use_web_search=False,
        fetcher=fetcher,
        force=True,
        fallback_to_jobs=False,
    )

    assert results[0].status == "no_context_found"
    enriched = OutreachWorkbook(tmp_path).list_organizations()[0]
    assert "context_confidence=external_verified" not in enriched.notes

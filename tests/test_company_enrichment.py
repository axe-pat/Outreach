from pathlib import Path

from outreach.account_tracker import build_account_rows
from outreach.company_enrichment import enrich_company_contexts
from outreach.tracking import OpportunityRecord, OrganizationRecord, OrganizationType, OutreachWorkbook


class FakeFetcher:
    def __init__(self, html_by_url: dict[str, str]) -> None:
        self.html_by_url = html_by_url

    def fetch_text(self, url: str) -> str:
        if url not in self.html_by_url:
            raise ValueError(f"unexpected url: {url}")
        return self.html_by_url[url]


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

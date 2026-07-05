from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from outreach.company_enrichment import format_notes_parts, parse_notes_parts
from outreach.tracking import DiscoverySourceRecord, OrganizationRecord, OrganizationType, OutreachWorkbook, SourceKind


@dataclass(frozen=True)
class StrategicAccountSeed:
    name: str
    website: str
    account_track: str
    tags: str
    description: str
    team_size: str = "1000+ employees"
    priority: str = "wishlist"


DEFAULT_STRATEGIC_ACCOUNTS: tuple[StrategicAccountSeed, ...] = (
    StrategicAccountSeed("Google", "https://www.google.com", "Large Company", "artificial-intelligence,data-platform,developer-tools,saas", "Global technology company with AI, cloud, search, ads, developer, and enterprise product surfaces.", priority="dream"),
    StrategicAccountSeed("Meta", "https://www.meta.com", "Large Company", "artificial-intelligence,machine-learning,marketplace,developer-tools", "Global social, AI, ads, messaging, and consumer platform company.", priority="dream"),
    StrategicAccountSeed("Apple", "https://www.apple.com", "Large Company", "artificial-intelligence,developer-platform,productivity,hardware", "Consumer technology company building hardware, software, services, developer platforms, and AI-enabled product experiences.", priority="dream"),
    StrategicAccountSeed("Amazon", "https://www.amazon.com", "Large Company", "marketplace,logistics,data-platform,artificial-intelligence,cloud", "Global commerce, logistics, cloud, ads, and AI platform company.", priority="dream"),
    StrategicAccountSeed("Netflix", "https://www.netflix.com", "Large Company", "data,analytics,machine-learning,consumer,productivity", "Streaming entertainment and consumer technology company with strong experimentation, data, and personalization culture.", priority="dream"),
    StrategicAccountSeed("Microsoft", "https://www.microsoft.com", "Large Company", "artificial-intelligence,developer-tools,productivity,cloud,data-platform", "Enterprise software, cloud, developer platform, productivity, and AI company.", priority="dream"),
    StrategicAccountSeed("LinkedIn", "https://www.linkedin.com", "Large Company", "hiring,marketplace,data-platform,artificial-intelligence", "Professional network and hiring marketplace with data, AI, recruiting, and creator/product surfaces.", priority="dream"),
    StrategicAccountSeed("Intuit", "https://www.intuit.com", "Large Company", "fintech,artificial-intelligence,data,workflow-automation,saas", "Financial technology platform behind TurboTax, QuickBooks, Credit Karma, and Mailchimp.", priority="dream"),
    StrategicAccountSeed("Adobe", "https://www.adobe.com", "Large Company", "generative-ai,creative-tools,saas,workflow-automation,data", "Creative, document, marketing, and generative AI software company.", priority="dream"),
    StrategicAccountSeed("Salesforce", "https://www.salesforce.com", "Large Company", "saas,artificial-intelligence,workflow-automation,data-platform", "Enterprise CRM and cloud software company with AI, workflow, analytics, and platform products.", priority="dream"),
    StrategicAccountSeed("Oracle", "https://www.oracle.com", "Large Company", "cloud,data-platform,infrastructure,saas", "Enterprise cloud, database, infrastructure, and business software company."),
    StrategicAccountSeed("SAP", "https://www.sap.com", "Large Company", "saas,data-platform,workflow-automation,enterprise-software", "Enterprise applications and business process software company."),
    StrategicAccountSeed("Workday", "https://www.workday.com", "Large Company", "saas,hiring,workflow-automation,data-platform", "Enterprise HR, finance, workforce, and planning software company."),
    StrategicAccountSeed("NVIDIA", "https://www.nvidia.com", "Large Company", "artificial-intelligence,infrastructure,developer-platform,data", "Accelerated computing, AI infrastructure, GPU, developer, and platform company.", priority="dream"),
    StrategicAccountSeed("Stripe", "https://stripe.com", "Growth / Mid-Market", "fintech,payments,developer-tools,api,saas", "Payments and financial infrastructure platform for internet businesses.", priority="dream"),
    StrategicAccountSeed("Databricks", "https://www.databricks.com", "Growth / Mid-Market", "data-platform,artificial-intelligence,machine-learning,infrastructure", "Data and AI platform company built around lakehouse, analytics, ML, and enterprise data workflows.", priority="dream"),
    StrategicAccountSeed("Snowflake", "https://www.snowflake.com", "Large Company", "data-platform,data-infrastructure,analytics,artificial-intelligence", "Cloud data platform for analytics, applications, AI, and enterprise data collaboration.", priority="dream"),
    StrategicAccountSeed("Datadog", "https://www.datadoghq.com", "Large Company", "observability,monitoring,infrastructure,developer-tools", "Observability and cloud monitoring platform for engineering and operations teams.", priority="dream"),
    StrategicAccountSeed("MongoDB", "https://www.mongodb.com", "Large Company", "data-platform,developer-tools,infrastructure,saas", "Developer data platform and database company."),
    StrategicAccountSeed("Atlassian", "https://www.atlassian.com", "Large Company", "productivity,collaboration,developer-tools,workflow-automation", "Team collaboration and software development platform behind Jira, Confluence, Trello, and related products."),
    StrategicAccountSeed("ServiceNow", "https://www.servicenow.com", "Large Company", "workflow-automation,saas,artificial-intelligence,enterprise-software", "Enterprise workflow and AI platform for IT, employee, customer, and business operations."),
    StrategicAccountSeed("HubSpot", "https://www.hubspot.com", "Large Company", "saas,data,workflow-automation,marketing-tech", "CRM, marketing, sales, service, and operations software platform."),
    StrategicAccountSeed("Shopify", "https://www.shopify.com", "Large Company", "marketplace,fintech,developer-platform,saas", "Commerce platform for merchants, payments, logistics, apps, and retail workflows."),
    StrategicAccountSeed("Twilio", "https://www.twilio.com", "Large Company", "api,developer-tools,communications,workflow-automation", "Customer engagement and communications API platform."),
    StrategicAccountSeed("Okta", "https://www.okta.com", "Large Company", "security,saas,developer-platform,identity", "Identity and access management platform for workforce and customer identity."),
    StrategicAccountSeed("Cloudflare", "https://www.cloudflare.com", "Large Company", "infrastructure,developer-tools,security,edge-computing", "Connectivity cloud and internet infrastructure platform."),
    StrategicAccountSeed("GitHub", "https://github.com", "Large Company", "developer-tools,developer-platform,artificial-intelligence,collaboration", "Developer collaboration platform with code hosting, DevEx, security, and AI coding products.", priority="dream"),
    StrategicAccountSeed("OpenAI", "https://openai.com", "Growth / Mid-Market", "artificial-intelligence,generative-ai,developer-platform,api", "AI research and product company building models, APIs, ChatGPT, agents, and developer platforms.", priority="dream"),
    StrategicAccountSeed("Anthropic", "https://www.anthropic.com", "Growth / Mid-Market", "artificial-intelligence,generative-ai,developer-platform,api", "AI safety and product company building Claude and enterprise/developer AI workflows.", priority="dream"),
    StrategicAccountSeed("Perplexity", "https://www.perplexity.ai", "Growth / Mid-Market", "artificial-intelligence,search,consumer,productivity", "AI answer engine and consumer/productivity search company."),
    StrategicAccountSeed("Hugging Face", "https://huggingface.co", "Growth / Mid-Market", "artificial-intelligence,machine-learning,developer-platform,open-source", "AI community, model, dataset, and developer platform."),
    StrategicAccountSeed("Figma", "https://www.figma.com", "Growth / Mid-Market", "collaboration,productivity,developer-tools,workflow-automation", "Collaborative design, product development, and creative workflow software company.", priority="dream"),
    StrategicAccountSeed("Notion", "https://www.notion.so", "Growth / Mid-Market", "productivity,collaboration,artificial-intelligence,workflow-automation", "Connected workspace for notes, docs, projects, knowledge, and AI-enabled workflows.", priority="dream"),
    StrategicAccountSeed("Airtable", "https://www.airtable.com", "Growth / Mid-Market", "workflow-automation,data-platform,productivity,collaboration", "App platform and collaborative database for operational workflows.", priority="dream"),
    StrategicAccountSeed("Asana", "https://asana.com", "Large Company", "productivity,collaboration,workflow-automation,saas", "Work management and collaboration software company."),
    StrategicAccountSeed("Miro", "https://miro.com", "Growth / Mid-Market", "collaboration,productivity,workflow-automation,saas", "Visual collaboration and product/design workflow platform."),
    StrategicAccountSeed("Monday.com", "https://monday.com", "Large Company", "workflow-automation,productivity,collaboration,saas", "Work operating system for project, CRM, product, and operational workflows."),
    StrategicAccountSeed("Rippling", "https://www.rippling.com", "Growth / Mid-Market", "saas,hiring,workflow-automation,fintech,data-platform", "Workforce, HR, IT, finance, and operations platform.", priority="dream"),
    StrategicAccountSeed("Ramp", "https://ramp.com", "Growth / Mid-Market", "fintech,payments,workflow-automation,data", "Corporate card, spend management, procurement, and finance automation platform.", priority="dream"),
    StrategicAccountSeed("Brex", "https://www.brex.com", "Growth / Mid-Market", "fintech,payments,workflow-automation,saas", "Corporate spend, card, travel, and finance platform."),
    StrategicAccountSeed("Plaid", "https://plaid.com", "Growth / Mid-Market", "fintech,api,data-platform,developer-tools", "Financial data API and payments infrastructure platform."),
    StrategicAccountSeed("DoorDash", "https://www.doordash.com", "Large Company", "marketplace,logistics,delivery,data,consumer", "Local commerce, delivery, logistics, marketplace, and ads platform."),
    StrategicAccountSeed("Uber", "https://www.uber.com", "Large Company", "marketplace,logistics,mobility,data-platform", "Mobility, delivery, logistics, marketplace, and consumer platform company."),
    StrategicAccountSeed("Airbnb", "https://www.airbnb.com", "Large Company", "marketplace,consumer,data,trust-safety", "Travel marketplace and consumer product company."),
    StrategicAccountSeed("Instacart", "https://www.instacart.com", "Large Company", "marketplace,logistics,delivery,data,retail-media", "Grocery delivery, marketplace, ads, and retail technology platform."),
    StrategicAccountSeed("Pinterest", "https://www.pinterest.com", "Large Company", "consumer,marketplace,artificial-intelligence,ads", "Visual discovery, recommendations, ads, and consumer product company."),
    StrategicAccountSeed("Reddit", "https://www.reddit.com", "Large Company", "consumer,marketplace,data,artificial-intelligence", "Community, content, ads, and consumer platform company."),
    StrategicAccountSeed("Roblox", "https://www.roblox.com", "Large Company", "developer-platform,marketplace,consumer,gaming", "User-generated gaming, creator marketplace, and developer platform."),
    StrategicAccountSeed("Discord", "https://discord.com", "Growth / Mid-Market", "consumer,communications,developer-platform,community", "Communications and community platform."),
    StrategicAccountSeed("Duolingo", "https://www.duolingo.com", "Large Company", "consumer,artificial-intelligence,education,data", "Consumer learning and AI-powered education product company."),
    StrategicAccountSeed("Grammarly", "https://www.grammarly.com", "Growth / Mid-Market", "artificial-intelligence,productivity,workflow-automation,writing", "AI writing, communication, and productivity assistant."),
    StrategicAccountSeed("Retool", "https://retool.com", "Growth / Mid-Market", "developer-tools,workflow-automation,data-platform,saas", "Developer platform for internal tools, apps, workflows, and AI agents."),
    StrategicAccountSeed("Linear", "https://linear.app", "Growth / Mid-Market", "developer-tools,productivity,collaboration,workflow-automation", "Product development and issue tracking platform for software teams."),
    StrategicAccountSeed("Anysphere", "https://www.cursor.com", "Growth / Mid-Market", "artificial-intelligence,developer-tools,developer-experience,agent", "AI coding tools company behind Cursor."),
    StrategicAccountSeed("Render", "https://render.com", "Growth / Mid-Market", "developer-tools,infrastructure,cloud,platform-engineering", "Cloud application hosting and developer platform."),
    StrategicAccountSeed("Cohere", "https://cohere.com", "Growth / Mid-Market", "artificial-intelligence,generative-ai,developer-platform,api", "Enterprise AI model and developer platform company."),
    StrategicAccountSeed("Groq", "https://groq.com", "Growth / Mid-Market", "artificial-intelligence,infrastructure,developer-platform", "AI inference hardware and developer platform company."),
)


def import_strategic_accounts(workbook_dir: Path, *, execute: bool = False) -> dict[str, int]:
    workbook = OutreachWorkbook(workbook_dir)
    workbook.initialize()
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    source_id = workbook.make_source_id("strategic-account-seeds", "built-in")
    added = 0
    updated = 0
    if execute:
        workbook.upsert_source(
            DiscoverySourceRecord(
                source_id=source_id,
                label="Built-in strategic account seed list",
                source_kind=SourceKind.MANUAL,
                base_url="built-in://strategic-account-seeds",
                extraction_method="manual_seed",
                owner="outreach-engine",
                last_run_at=now,
                notes="MAANG, major SaaS, AI, data, fintech, marketplace, and developer-platform targets.",
            )
        )

    for seed in DEFAULT_STRATEGIC_ACCOUNTS:
        org_id = workbook.make_organization_id(seed.name)
        notes = _seed_notes(seed)
        existing = next((item for item in workbook.list_organizations() if item.organization_id == org_id), None)
        if not execute:
            if existing:
                updated += 1
            else:
                added += 1
            continue
        if existing:
            freeform, metadata = parse_notes_parts(existing.notes)
            _, seed_metadata = parse_notes_parts(notes)
            merged_metadata = {**metadata, **seed_metadata}
            merged_metadata["tags"] = _merge_csv(metadata.get("tags", ""), seed_metadata.get("tags", ""))
            if metadata.get("context_confidence") == "external_verified":
                for key in (
                    "context_source",
                    "context_confidence",
                    "context_evidence_url",
                    "context_enriched_at",
                    "context_refresh_after",
                    "prestige_signals",
                    "prestige_evidence_url",
                ):
                    if metadata.get(key):
                        merged_metadata[key] = metadata[key]
                if metadata.get("description"):
                    merged_metadata["description"] = metadata["description"]
            target_lists = _merge_semicolon(existing.target_lists, _seed_target_lists(seed))
            workbook.update_organization(
                org_id,
                target_lists=target_lists,
                status=existing.status or "Strategic target",
                website=existing.website or seed.website,
                source_kind=getattr(existing.source_kind, "value", existing.source_kind) or SourceKind.MANUAL.value,
                source_url=existing.source_url or seed.website,
                notes=format_notes_parts(freeform or ["Strategic account seed"], merged_metadata),
                last_updated_at=now,
            )
            updated += 1
        else:
            workbook.upsert_organization(
                OrganizationRecord(
                    organization_id=org_id,
                    name=seed.name,
                    organization_type=OrganizationType.COMPANY,
                    target_lists=_seed_target_lists(seed),
                    status="Strategic target",
                    website=seed.website,
                    source_kind=SourceKind.MANUAL,
                    source_url=seed.website,
                    discovered_at=now,
                    last_updated_at=now,
                    notes=notes,
                )
            )
            added += 1
    return {"count": len(DEFAULT_STRATEGIC_ACCOUNTS), "added": added, "updated": updated}


def _seed_target_lists(seed: StrategicAccountSeed) -> str:
    parts = ["strategic", "wishlist", "track-2", "relationship"]
    if seed.priority == "dream":
        parts.append("dream")
    if seed.account_track == "Large Company":
        parts.append("large-company")
    elif seed.account_track == "Growth / Mid-Market":
        parts.append("growth")
    return ";".join(parts)


def _seed_notes(seed: StrategicAccountSeed) -> str:
    _, metadata = parse_notes_parts("")
    metadata["seed_source"] = "built_in_strategic_accounts"
    metadata["context_source"] = "manual_seed"
    metadata["context_confidence"] = "manual_seed"
    metadata["context_evidence_url"] = seed.website
    metadata["account_track_hint"] = seed.account_track
    metadata["team_size"] = seed.team_size
    metadata["tags"] = seed.tags
    metadata["description"] = seed.description
    metadata["manual_priority"] = seed.priority
    return format_notes_parts(["Strategic account seed"], metadata)


def _merge_semicolon(existing: str, new: str) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in [existing, new]:
        for item in (value or "").split(";"):
            clean = item.strip()
            normalized = clean.lower()
            if not clean or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(clean)
    return ";".join(merged)


def _merge_csv(existing: str, new: str) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in [existing, new]:
        for item in (value or "").split(","):
            clean = item.strip()
            normalized = clean.lower()
            if not clean or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(clean)
    return ",".join(merged)

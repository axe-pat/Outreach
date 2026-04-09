from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from outreach.artifacts import write_artifact
from outreach.config import OutreachSettings
from outreach.scoring import score_candidate
from outreach.services.linkedin import LinkedInScraper
from outreach.services.notes import NoteGenerator
from outreach.models import CandidateProfile
from outreach.tracking import (
    ContactRecord,
    OpportunityRecord,
    OpportunityType,
    OrganizationRecord,
    OrganizationType,
    OutreachChannel,
    OutreachWorkbook,
    SourceKind,
    TouchpointRecord,
    utc_now_iso,
)


def resolve_pass_definitions(
    settings: OutreachSettings,
    include_passes: tuple[str, ...] = (),
    exclude_passes: tuple[str, ...] = (),
    enable_marshall: bool = False,
    force_broad_fallback: bool = False,
) -> dict[str, dict[str, str | int | bool]]:
    include_set = {item.strip() for item in include_passes if item.strip()}
    exclude_set = {item.strip() for item in exclude_passes if item.strip()}
    pass_definitions = {
        name: dict(config) for name, config in settings.search.pass_definitions.items()
    }

    if enable_marshall:
        for name in ("product_usc_marshall", "engineering_usc_marshall"):
            if name in pass_definitions:
                pass_definitions[name]["enabled"] = True

    if force_broad_fallback and "broad_fallback" in pass_definitions:
        pass_definitions["broad_fallback"]["enabled"] = True
        pass_definitions["broad_fallback"].pop("run_if_below_pool_size", None)

    if include_set:
        for name, config in pass_definitions.items():
            config["enabled"] = name in include_set

    for name in exclude_set:
        if name in pass_definitions:
            pass_definitions[name]["enabled"] = False

    return pass_definitions


def infer_role_bucket(title: str, raw_text: str, settings: OutreachSettings) -> str:
    title_lower = title.lower()
    raw_text_lower = raw_text.lower()

    recruiter_keywords = ["recruiter", "sourcer", "talent", "campus recruiting", "university recruiting"]
    university_keywords = ["usc", "university", "campus", "marshall school of business", "career center"]
    adjacent_override_keywords = ["solution engineer", "solutions engineer", "solutions architect", "solution architect"]

    if any(keyword in title_lower for keyword in recruiter_keywords):
        if any(keyword in raw_text_lower for keyword in university_keywords):
            return "University Recruiting"
        return "Recruiting"

    if any(keyword in title_lower for keyword in adjacent_override_keywords):
        return "Adjacent"

    if any(keyword.lower() in title_lower for keyword in settings.search.role_keywords_product):
        if "productivity engineering" not in title_lower:
            return "Product"

    if any(keyword.lower() in title_lower for keyword in settings.search.role_keywords_engineering):
        return "Engineering"

    if any(keyword.lower() in title_lower for keyword in settings.search.adjacent_titles):
        return "Adjacent"

    return "Other"


def detect_usc_marshall(raw_text: str) -> bool:
    text = raw_text.lower()
    return "usc marshall" in text or "marshall school of business" in text


def detect_usc(raw_text: str) -> bool:
    text = raw_text.lower()
    return "usc" in text or "university of southern california" in text


def detect_shared_history(raw_text: str, settings: OutreachSettings) -> bool:
    text = raw_text.lower()
    if any(keyword in text for keyword in settings.search.shared_history_keywords):
        return True
    return any(company.lower() in text for company in settings.search.ex_companies)


def pass_relevance(pass_name: str, role_bucket: str, title: str, raw_text: str) -> bool:
    title_lower = title.lower()

    product_text_signals = [
        "product manager",
        "product ",
        "product@",
        "product @",
        "tpm",
        "technical product manager",
        "product management",
        "group product",
        "director of product",
    ]
    engineering_text_signals = [
        "software engineer",
        "swe",
        "sde",
        "staff engineer",
        "senior engineer",
        "ml engineer",
        "machine learning engineer",
        "data engineer",
        "platform engineer",
        "infra engineer",
        "engineering at",
        "developer",
    ]

    if pass_name == "existing_connections":
        return True
    if pass_name.startswith("product_"):
        if role_bucket == "Product":
            return True
        return any(signal in title_lower for signal in product_text_signals)
    if pass_name.startswith("engineering_"):
        if role_bucket == "Engineering":
            return True
        return any(signal in title_lower for signal in engineering_text_signals)
    return role_bucket != "Other"

app = typer.Typer(help="Outreach engine CLI")


@app.command()
def doctor() -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        typer.echo("Copy .env.example to .env and fill in the required values.")
        raise typer.Exit(code=1)

    typer.echo("Environment check")
    typer.echo(f"- Chrome user data dir: {settings.resolved_linkedin_user_data_dir}")
    typer.echo(f"- Chrome profile name: {settings.linkedin_profile_name}")
    typer.echo(f"- Chrome debug port: {settings.linkedin_debug_port}")
    typer.echo(f"- Anthropic key configured: {bool(settings.anthropic_api_key)}")
    typer.echo(f"- Notion token configured: {bool(settings.notion_api_token)}")
    typer.echo(f"- Notion database configured: {bool(settings.notion_database_id)}")


@app.command("prepare-browser-manual")
def prepare_browser_manual() -> None:
    settings = OutreachSettings()
    user_data_dir = settings.resolved_linkedin_user_data_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)
    typer.echo("Use this Chrome window to log into LinkedIn normally, including Google if needed.")
    typer.echo(f"User data dir: {user_data_dir}")
    typer.echo(f"Launch Chrome with remote debugging on port {settings.linkedin_debug_port}.")


@app.command("prepare-browser")
def prepare_browser(
    headless: Annotated[
        bool,
        typer.Option(help="Run without opening a visible browser window"),
    ] = False,
) -> None:
    settings = OutreachSettings()
    scraper = LinkedInScraper(settings)
    typer.echo("Opening dedicated automation browser for LinkedIn login.")
    typer.echo(f"User data dir: {settings.resolved_linkedin_user_data_dir}")
    scraper.prepare_browser(headless=headless)


@app.command("check-linkedin")
def check_linkedin(
    headless: Annotated[
        bool,
        typer.Option(help="Run without opening a visible browser window"),
    ] = False,
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        typer.echo("Copy .env.example to .env and fill in the required values.")
        raise typer.Exit(code=1)

    scraper = LinkedInScraper(settings)
    result = scraper.check_session(headless=headless)
    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-check",
        {
            "ok": result.ok,
            "current_url": result.current_url,
            "title": result.title,
            "logged_in": result.logged_in,
            "details": result.details,
            "steps": result.steps,
            "screenshots": result.screenshot_paths,
        },
    )

    if result.ok:
        typer.echo("LinkedIn session check passed.")
        typer.echo(f"Page title: {result.title}")
        typer.echo(f"Current URL: {result.current_url}")
        typer.echo(f"Artifact: {artifact}")
        return

    typer.echo("LinkedIn session check failed.")
    typer.echo(result.details)
    typer.echo(f"Artifact: {artifact}")
    raise typer.Exit(code=1)


@app.command("check-linkedin-live")
def check_linkedin_live() -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)

    scraper = LinkedInScraper(settings)
    result = scraper.check_session_via_cdp()
    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-live-check",
        {
            "ok": result.ok,
            "current_url": result.current_url,
            "title": result.title,
            "logged_in": result.logged_in,
            "details": result.details,
            "steps": result.steps,
            "screenshots": result.screenshot_paths,
        },
    )
    if result.ok:
        typer.echo("LinkedIn live session check passed.")
        typer.echo(f"Page title: {result.title}")
        typer.echo(f"Current URL: {result.current_url}")
        typer.echo(f"Artifact: {artifact}")
        return

    typer.echo("LinkedIn live session check failed.")
    typer.echo(result.details)
    typer.echo(f"Artifact: {artifact}")
    raise typer.Exit(code=1)


@app.command("extract-company")
def extract_company(
    company: Annotated[str, typer.Option(help="Target company name")],
    limit: Annotated[int, typer.Option(help="Maximum visible people cards to capture")] = 10,
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)

    scraper = LinkedInScraper(settings)
    typer.echo(f"Extracting visible LinkedIn people results for {company}")
    results = scraper.extract_company_people_live(company=company, limit=limit)
    artifact = write_artifact(
        settings.artifacts_dir,
        "company-search",
        {
            "company": company,
            "limit": limit,
            "count": len(results),
            "results": [item.model_dump() for item in results],
        },
    )
    typer.echo(f"Captured {len(results)} visible candidates.")
    typer.echo(f"Artifact: {artifact}")


@app.command()
def run(
    company: Annotated[str, typer.Option(help="Target company name")],
    dry_run: Annotated[bool, typer.Option(help="Skip writes and external side effects")] = True,
    company_mode: Annotated[
        str,
        typer.Option(help="How to tune note ask style: default, startup, or big_company"),
    ] = "default",
    include_pass: Annotated[
        list[str] | None,
        typer.Option("--include-pass", help="Only run the named pass or passes"),
    ] = None,
    exclude_pass: Annotated[
        list[str] | None,
        typer.Option("--exclude-pass", help="Skip the named pass or passes"),
    ] = None,
    enable_marshall: Annotated[
        bool,
        typer.Option(help="Enable USC Marshall passes for this run"),
    ] = False,
    force_broad_fallback: Annotated[
        bool,
        typer.Option(help="Force the broad fallback pass even if the pool is already healthy"),
    ] = False,
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)

    scraper = LinkedInScraper(settings)
    note_generator = NoteGenerator()
    deduped: dict[str, dict] = {}
    pass_summaries: list[dict] = []
    pass_definitions = resolve_pass_definitions(
        settings,
        include_passes=tuple(include_pass or []),
        exclude_passes=tuple(exclude_pass or []),
        enable_marshall=enable_marshall,
        force_broad_fallback=force_broad_fallback,
    )
    ordered_passes = sorted(
        pass_definitions.items(),
        key=lambda item: int(item[1].get("priority", 999)),
    )
    for pass_name, pass_config in ordered_passes:
        if not bool(pass_config.get("enabled", True)):
            typer.echo(f"- Pass {pass_name}: skipped (disabled)")
            continue
        pool_floor = pass_config.get("run_if_below_pool_size")
        if pool_floor is not None and len(deduped) >= int(pool_floor):
            typer.echo(f"- Pass {pass_name}: skipped (pool already at {len(deduped)})")
            continue
        pass_query = str(pass_config.get("query", "")).strip()
        limit = int(pass_config.get("limit", settings.search.default_limit))
        query = pass_query
        filter_run = scraper.extract_people_with_filters_live(
            company=company,
            search_query=query,
            limit=limit,
            school=str(pass_config.get("school")) if pass_config.get("school") else None,
            connection_degree=str(pass_config.get("connection_degree")) if pass_config.get("connection_degree") else None,
            use_us_location=bool(pass_config.get("use_us_location", True)),
        )
        raw_candidates = filter_run.candidates
        kept_count = 0
        pass_artifact = write_artifact(
            settings.artifacts_dir,
            f"pass-{pass_name}",
            {
                "company": company,
                "pass_name": pass_name,
                "query": query,
                "school": pass_config.get("school"),
                "connection_degree": pass_config.get("connection_degree"),
                "use_us_location": pass_config.get("use_us_location", True),
                "final_url": filter_run.final_url,
                "visible_filter_text": filter_run.visible_filter_text,
                "screenshot": filter_run.screenshot_path,
                "raw_count": len(raw_candidates),
                "limit": limit,
                "results": [item.model_dump() for item in raw_candidates],
            },
        )
        pass_summaries.append(
            {
                "pass_name": pass_name,
                "query": query,
                "school": pass_config.get("school"),
                "connection_degree": pass_config.get("connection_degree"),
                "use_us_location": pass_config.get("use_us_location", True),
                "final_url": filter_run.final_url,
                "screenshot": filter_run.screenshot_path,
                "limit": limit,
                "raw_count": len(raw_candidates),
                "kept_count": 0,
                "artifact": str(pass_artifact),
            }
        )
        typer.echo(f"- Pass {pass_name}: {len(raw_candidates)} raw results")
        for raw in raw_candidates:
            title = raw.title or ""
            raw_text = raw.raw_text or ""
            role_bucket = infer_role_bucket(title, raw_text, settings)
            if not pass_relevance(pass_name, role_bucket, title, raw_text):
                continue
            kept_count += 1
            connection_degree = raw.connection_degree or "3rd"
            pass_school = str(pass_config.get("school") or "")
            pass_implies_usc = "southern california" in pass_school.lower()
            pass_implies_marshall = "marshall" in pass_school.lower()
            pass_implies_existing_connection = pass_name == "existing_connections"
            profile = CandidateProfile(
                name=raw.name,
                title=raw.title or "",
                company=company,
                linkedin_url=raw.linkedin_url or "https://www.linkedin.com/",
                connection_degree=connection_degree,
                mutual_connections=1 if raw.snippet and "mutual connection" in raw.snippet else 0,
                existing_connection=pass_implies_existing_connection or connection_degree == "1st",
                usc_marshall=pass_implies_marshall or detect_usc_marshall(raw_text),
                usc_alumni=pass_implies_usc or detect_usc(raw_text),
                shared_history=detect_shared_history(raw_text, settings),
                indian_background=False,
                university_recruiter=role_bucket == "University Recruiting",
                role_bucket=role_bucket,
            )
            scored = score_candidate(profile, settings.scoring)
            key = raw.linkedin_url or f"{raw.name}:{title}"
            entry = deduped.get(
                key,
                {
                    "name": raw.name,
                    "title": raw.title,
                    "location": raw.location,
                    "linkedin_url": raw.linkedin_url,
                    "subtitle": raw.subtitle,
                    "connection_degree": raw.connection_degree,
                    "snippet": raw.snippet,
                    "role_bucket": role_bucket,
                    "score": scored.score,
                    "tier": scored.tier.value,
                    "triggers": scored.triggers,
                    "passes": [],
                    "existing_connection": profile.existing_connection,
                    "usc_marshall": profile.usc_marshall,
                    "usc": profile.usc_alumni,
                    "shared_history": profile.shared_history,
                },
            )
            entry["passes"] = sorted(set([*entry["passes"], pass_name]))
            if scored.score > entry["score"]:
                entry.update(
                    {
                        "role_bucket": role_bucket,
                        "score": scored.score,
                        "tier": scored.tier.value,
                        "triggers": scored.triggers,
                        "existing_connection": profile.existing_connection,
                        "usc_marshall": profile.usc_marshall,
                        "usc": profile.usc_alumni,
                        "shared_history": profile.shared_history,
                    }
                )
            deduped[key] = entry

        pass_summaries[-1]["kept_count"] = kept_count
        typer.echo(f"  kept {kept_count} after pass relevance filtering")

        if len(deduped) >= settings.search.hard_company_limit:
            typer.echo(f"Reached hard company limit of {settings.search.hard_company_limit}; stopping early.")
            break

    scored_candidates = list(deduped.values())
    for candidate in scored_candidates:
        if candidate["existing_connection"]:
            candidate["priority_bucket"] = "Direct Message Now"
        else:
            candidate["priority_bucket"] = candidate["tier"]

    scored_candidates.sort(
        key=lambda item: (item["existing_connection"], item["score"], item["name"]),
        reverse=True,
    )
    scored_candidates = scored_candidates[: settings.search.final_company_limit]
    scored_candidates = note_generator.generate_batch(
        scored_candidates,
        company=company,
        company_mode=company_mode,
    )

    artifact = write_artifact(
        settings.artifacts_dir,
        "dry-run-pipeline",
        {
            "company": company,
            "company_mode": company_mode,
            "dry_run": dry_run,
            "passes": pass_definitions,
            "pass_summaries": pass_summaries,
            "count": len(scored_candidates),
            "results": scored_candidates,
        },
    )
    typer.echo(f"Starting outreach pipeline for {company}")
    typer.echo(f"Dry run: {dry_run}")
    typer.echo(f"Timezone: {settings.timezone}")
    typer.echo(f"Captured and scored {len(scored_candidates)} candidates.")
    typer.echo(f"Artifact: {artifact}")


@app.command("generate-notes")
def generate_notes(
    artifact_path: Annotated[Path, typer.Option(help="Path to a prior dry-run pipeline artifact")],
    company_mode: Annotated[
        str,
        typer.Option(help="How to tune note ask style: default, startup, or big_company"),
    ] = "default",
    ai_polish: Annotated[
        bool,
        typer.Option(help="Run AI polish on the top slice of generated notes"),
    ] = False,
    top_n: Annotated[
        int,
        typer.Option(help="How many top notes to polish with AI"),
    ] = 10,
    polish_model: Annotated[
        str,
        typer.Option(help="Anthropic model to use for note polish"),
    ] = "claude-sonnet-4-6",
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)

    with artifact_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    company = payload["company"]
    candidates = payload["results"]
    note_generator = NoteGenerator()
    annotated = note_generator.generate_batch(candidates, company=company, company_mode=company_mode)
    summary = {
        "send": sum(1 for item in annotated if item["note_qc"]["verdict"] == "send"),
        "review": sum(1 for item in annotated if item["note_qc"]["verdict"] == "review"),
        "revise": sum(1 for item in annotated if item["note_qc"]["verdict"] == "revise"),
    }
    polished_summary: dict[str, int] | None = None

    if ai_polish:
        if not settings.anthropic_api_key:
            typer.echo("ANTHROPIC_API_KEY is required for --ai-polish.")
            raise typer.Exit(code=1)
        annotated = note_generator.polish_batch(
            annotated,
            company=company,
            api_key=settings.anthropic_api_key,
            top_n=top_n,
            model=polish_model,
            company_mode=company_mode,
        )
        polished_candidates = [item for item in annotated if "polished_note_qc" in item]
        polished_summary = {
            "send": sum(1 for item in polished_candidates if item["polished_note_qc"]["verdict"] == "send"),
            "review": sum(1 for item in polished_candidates if item["polished_note_qc"]["verdict"] == "review"),
            "revise": sum(1 for item in polished_candidates if item["polished_note_qc"]["verdict"] == "revise"),
        }

    artifact = write_artifact(
        settings.artifacts_dir,
        "notes-batch",
        {
            "source_artifact": str(artifact_path),
            "company": company,
            "company_mode": company_mode,
            "count": len(annotated),
            "qc_summary": summary,
            "ai_polish": ai_polish,
            "polish_top_n": top_n if ai_polish else 0,
            "polish_model": polish_model if ai_polish else None,
            "polished_qc_summary": polished_summary,
            "results": annotated,
        },
    )
    typer.echo(f"Generated notes for {len(annotated)} candidates.")
    typer.echo(f"QC summary: {summary}")
    if polished_summary is not None:
        typer.echo(f"Polished QC summary: {polished_summary}")
    typer.echo(f"Artifact: {artifact}")


@app.command("init-workbook")
def init_workbook() -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    paths = workbook.initialize()
    typer.echo(f"Initialized outreach workbook in {settings.resolved_tracking_workspace_dir}")
    for table_name, path in paths.items():
        typer.echo(f"- {table_name}: {path}")


@app.command("workbook-summary")
def workbook_summary() -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    counts = workbook.summary_counts()
    typer.echo(f"Workbook: {settings.resolved_tracking_workspace_dir}")
    for table_name, count in counts.items():
        typer.echo(f"- {table_name}: {count} rows")


@app.command("add-organization")
def add_organization(
    name: Annotated[str, typer.Option(help="Organization name")],
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="Organization bucket for the master list"),
    ] = OrganizationType.COMPANY,
    target_lists: Annotated[
        str,
        typer.Option(help="Semicolon-separated tracks such as jobs;yc;hacker_house"),
    ] = "",
    status: Annotated[str, typer.Option(help="Pipeline status")] = "New",
    city: Annotated[str, typer.Option(help="City or region")] = "",
    website: Annotated[str, typer.Option(help="Website URL")] = "",
    linkedin_url: Annotated[str, typer.Option(help="Company LinkedIn URL")] = "",
    source_kind: Annotated[SourceKind, typer.Option(help="Where the lead came from")] = SourceKind.MANUAL,
    source_url: Annotated[str, typer.Option(help="Source page URL")] = "",
    notes: Annotated[str, typer.Option(help="Free-form notes")] = "",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    organization, created = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(name),
            name=name,
            organization_type=organization_type,
            target_lists=target_lists,
            status=status,
            city=city,
            website=website,
            linkedin_url=linkedin_url,
            source_kind=source_kind,
            source_url=source_url,
            notes=notes,
        )
    )
    typer.echo(f"{'Created' if created else 'Already had'} organization {organization.name}")
    typer.echo(f"- organization_id: {organization.organization_id}")
    typer.echo(f"- workbook: {settings.resolved_tracking_workspace_dir}")


@app.command("add-opportunity")
def add_opportunity(
    organization: Annotated[str, typer.Option(help="Organization name")],
    title: Annotated[str, typer.Option(help="Opportunity title")],
    opportunity_type: Annotated[
        OpportunityType,
        typer.Option(help="Type such as internship, research, or residency"),
    ] = OpportunityType.OTHER,
    target_lists: Annotated[str, typer.Option(help="Semicolon-separated track tags")] = "",
    location: Annotated[str, typer.Option(help="Location text")] = "",
    status: Annotated[str, typer.Option(help="Opportunity status")] = "Discovered",
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="Organization type if the organization needs to be created"),
    ] = OrganizationType.COMPANY,
    source_kind: Annotated[SourceKind, typer.Option(help="Where the lead came from")] = SourceKind.MANUAL,
    source_url: Annotated[str, typer.Option(help="Source page URL")] = "",
    compensation_hint: Annotated[str, typer.Option(help="Stipend or pay notes")] = "",
    notes: Annotated[str, typer.Option(help="Free-form notes")] = "",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    organization_record, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(organization),
            name=organization,
            organization_type=organization_type,
            target_lists=target_lists,
            source_kind=source_kind,
            source_url=source_url,
        )
    )
    opportunity, created = workbook.upsert_opportunity(
        OpportunityRecord(
            opportunity_id=workbook.make_opportunity_id(
                organization_record.organization_id,
                title,
                source_url=source_url,
            ),
            organization_id=organization_record.organization_id,
            title=title,
            opportunity_type=opportunity_type,
            target_lists=target_lists,
            location=location,
            status=status,
            source_kind=source_kind,
            source_url=source_url,
            compensation_hint=compensation_hint,
            notes=notes,
        )
    )
    typer.echo(f"{'Created' if created else 'Already had'} opportunity {opportunity.title}")
    typer.echo(f"- opportunity_id: {opportunity.opportunity_id}")
    typer.echo(f"- organization_id: {opportunity.organization_id}")


@app.command("add-contact")
def add_contact(
    organization: Annotated[str, typer.Option(help="Organization name")],
    full_name: Annotated[str, typer.Option(help="Contact full name")],
    title: Annotated[str, typer.Option(help="Role or title")] = "",
    contact_type: Annotated[str, typer.Option(help="Founder, PM, professor, recruiter, etc.")] = "",
    target_lists: Annotated[str, typer.Option(help="Semicolon-separated track tags")] = "",
    preferred_channel: Annotated[
        OutreachChannel,
        typer.Option(help="Preferred outreach channel"),
    ] = OutreachChannel.LINKEDIN,
    status: Annotated[str, typer.Option(help="Contact status")] = "Discovered",
    linkedin_url: Annotated[str, typer.Option(help="LinkedIn profile URL")] = "",
    email: Annotated[str, typer.Option(help="Email address")] = "",
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="Organization type if the organization needs to be created"),
    ] = OrganizationType.COMPANY,
    source_kind: Annotated[SourceKind, typer.Option(help="Where the lead came from")] = SourceKind.MANUAL,
    source_url: Annotated[str, typer.Option(help="Source page URL")] = "",
    notes: Annotated[str, typer.Option(help="Free-form notes")] = "",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    organization_record, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(organization),
            name=organization,
            organization_type=organization_type,
            target_lists=target_lists,
            source_kind=source_kind,
            source_url=source_url,
        )
    )
    contact, created = workbook.upsert_contact(
        ContactRecord(
            contact_id=workbook.make_contact_id(
                organization_record.organization_id,
                full_name,
                linkedin_url=linkedin_url,
                email=email,
            ),
            organization_id=organization_record.organization_id,
            full_name=full_name,
            title=title,
            contact_type=contact_type,
            target_lists=target_lists,
            preferred_channel=preferred_channel,
            status=status,
            linkedin_url=linkedin_url,
            email=email,
            source_kind=source_kind,
            source_url=source_url,
            notes=notes,
        )
    )
    typer.echo(f"{'Created' if created else 'Already had'} contact {contact.full_name}")
    typer.echo(f"- contact_id: {contact.contact_id}")
    typer.echo(f"- organization_id: {contact.organization_id}")


@app.command("log-touchpoint")
def log_touchpoint(
    organization: Annotated[str, typer.Option(help="Organization name")],
    message_text: Annotated[str, typer.Option(help="Exact outbound or draft message text")],
    full_name: Annotated[str, typer.Option(help="Optional contact name")] = "",
    title: Annotated[str, typer.Option(help="Optional contact title")] = "",
    linkedin_url: Annotated[str, typer.Option(help="Optional LinkedIn URL")] = "",
    email: Annotated[str, typer.Option(help="Optional email address")] = "",
    channel: Annotated[OutreachChannel, typer.Option(help="Outreach channel")] = OutreachChannel.LINKEDIN,
    status: Annotated[str, typer.Option(help="Draft, Sent, Replied, etc.")] = "Draft",
    message_kind: Annotated[str, typer.Option(help="Short label for this message")] = "outreach",
    target_lists: Annotated[str, typer.Option(help="Semicolon-separated track tags")] = "",
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="Organization type if the organization needs to be created"),
    ] = OrganizationType.COMPANY,
    source_artifact: Annotated[str, typer.Option(help="Optional artifact path or external reference")] = "",
    notes: Annotated[str, typer.Option(help="Free-form notes")] = "",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    organization_record, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(organization),
            name=organization,
            organization_type=organization_type,
            target_lists=target_lists,
        )
    )

    contact_id = ""
    if full_name.strip():
        contact, _ = workbook.upsert_contact(
            ContactRecord(
                contact_id=workbook.make_contact_id(
                    organization_record.organization_id,
                    full_name,
                    linkedin_url=linkedin_url,
                    email=email,
                ),
                organization_id=organization_record.organization_id,
                full_name=full_name,
                title=title,
                target_lists=target_lists,
                preferred_channel=channel,
                linkedin_url=linkedin_url,
                email=email,
            )
        )
        contact_id = contact.contact_id

    touchpoint, created = workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id=workbook.make_touchpoint_id(
                organization_record.organization_id,
                contact_id,
                channel.value,
                message_text,
                source_artifact=source_artifact,
            ),
            organization_id=organization_record.organization_id,
            contact_id=contact_id,
            channel=channel,
            status=status,
            message_kind=message_kind,
            message_text=message_text,
            sent_at=utc_now_iso() if status.lower() == "sent" else "",
            source_artifact=source_artifact,
            notes=notes,
        )
    )
    typer.echo(f"{'Logged' if created else 'Already had'} touchpoint {touchpoint.touchpoint_id}")
    typer.echo(f"- organization_id: {touchpoint.organization_id}")
    if touchpoint.contact_id:
        typer.echo(f"- contact_id: {touchpoint.contact_id}")


@app.command("import-linkedin-artifact")
def import_linkedin_artifact(
    artifact_path: Annotated[Path, typer.Option(help="Path to a dry-run-pipeline or notes artifact")],
    target_lists: Annotated[
        str,
        typer.Option(help="Semicolon-separated track tags for imported records"),
    ] = "referrals;linkedin",
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="How to classify the imported organization"),
    ] = OrganizationType.COMPANY,
    touchpoint_status: Annotated[
        str,
        typer.Option(help="How to log generated notes, typically Draft or Prepared"),
    ] = "Draft",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    summary = workbook.import_linkedin_artifact(
        artifact_path=artifact_path,
        target_lists=target_lists,
        organization_type=organization_type,
        touchpoint_status=touchpoint_status,
    )
    typer.echo(f"Imported LinkedIn artifact into {settings.resolved_tracking_workspace_dir}")
    typer.echo(f"- organization_id: {summary.organization_id}")
    typer.echo(f"- source_id: {summary.source_id}")
    typer.echo(f"- contacts_added: {summary.contacts_added}")
    typer.echo(f"- touchpoints_added: {summary.touchpoints_added}")


@app.command("send-invites")
def send_invites(
    artifact_path: Annotated[Path, typer.Option(help="Path to a notes-batch artifact")],
    limit: Annotated[int, typer.Option(help="Maximum number of candidates to process")] = 5,
    start_at: Annotated[int, typer.Option(help="Start offset into the eligible queue")] = 0,
    verdict: Annotated[str, typer.Option(help="Only include notes with this QC verdict")] = "send",
    execute: Annotated[
        bool,
        typer.Option(help="Actually send invites instead of doing a dry run"),
    ] = False,
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)

    with artifact_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    company = payload["company"]
    all_candidates = payload["results"]
    eligible: list[dict] = []
    for item in all_candidates:
        qc = item.get("polished_note_qc") or item.get("note_qc") or {}
        item_verdict = qc.get("verdict")
        if verdict and item_verdict != verdict:
            continue
        if item.get("existing_connection"):
            continue
        if not item.get("linkedin_url"):
            continue
        item = dict(item)
        if "polished_note" in item:
            item["note"] = item["polished_note"]
        eligible.append(item)

    batch = eligible[start_at : start_at + limit]
    if not batch:
        typer.echo("No eligible candidates matched the current filters.")
        raise typer.Exit(code=1)

    scraper = LinkedInScraper(settings)
    typer.echo(f"Processing {len(batch)} invite candidates for {company}")
    typer.echo(f"Mode: {'execute' if execute else 'dry run'}")
    results = scraper.send_connection_requests(batch, execute=execute)
    artifact = write_artifact(
        settings.artifacts_dir,
        "invite-send-batch",
        {
            "source_artifact": str(artifact_path),
            "company": company,
            "execute": execute,
            "limit": limit,
            "start_at": start_at,
            "verdict": verdict,
            "count": len(results),
            "results": [result.__dict__ for result in results],
        },
    )
    status_counts: dict[str, int] = {}
    for result in results:
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
    typer.echo(f"Status summary: {status_counts}")
    typer.echo(f"Artifact: {artifact}")


if __name__ == "__main__":
    app()

# Outreach Engine

Phase 1 foundation for a LinkedIn outreach workflow, now extended with a CSV-backed
target workbook for broader outreach discovery:

- ingest a target company
- scrape candidate profiles from LinkedIn in a logged-in browser session
- score and rank candidates
- generate personalized connection notes
- push the review queue to Notion
- store organizations, opportunities, contacts, touchpoints, and source metadata

## Recommended Stack

- Python 3.11
- Playwright for browser automation
- Anthropic SDK for note generation
- Notion client for queue persistence
- Pydantic for typed config and data models
- Typer for a small CLI
- Pytest for unit tests

## Why This Stack

Python is the cleanest fit for browser automation, API integration, scoring logic, and fast iteration in a solo project. The key constraint here is not frontend complexity, it is reliability around LinkedIn behavior. That makes a small CLI pipeline a better starting point than a web app.

The app is structured so the LinkedIn layer is isolated. If selectors break or we later decide to split scraping and note generation into separate jobs, the rest of the pipeline stays stable.

Note on AI usage: base LinkedIn note generation is deterministic. The optional `--ai-polish` rewrite layer now defaults to `claude-haiku-4-5-20251001`, which keeps the expensive model reserved for places where it matters more.

## Project Layout

```text
src/outreach/
  cli.py
  config.py
  discovery/
  models.py
  tracking.py
  scoring.py
  services/
    linkedin.py
    notes.py
    notion.py
docs/
  architecture.md
  system_overview.md
tests/
```

## Architecture Docs

- `docs/system_overview.md`: current-state architecture with diagrams
- `docs/architecture.md`: earlier design notes and original component split
- `docs/discovery_strategy.md`: how the multi-source discovery model maps into the workbook

## Getting Started

1. Create a virtual environment.
2. Install dependencies with `pip install -e ".[dev]"`.
3. Install Playwright browsers with `playwright install chromium`.
4. Copy `.env.example` to `.env` and fill in your secrets.
5. Run `python main.py doctor`.
6. Set `LINKEDIN_CHROME_USER_DATA_DIR` to the absolute path of the signed-in Chrome profile you want Outreach to reuse, then run `./scripts/launch_outreach_browser.sh`.
7. Log into LinkedIn in the dedicated browser window and keep that window open.
8. Run `python main.py check-linkedin-live`.
9. Run `python main.py run --company Snowflake --dry-run`.
10. Run `python main.py init-workbook`.

## Workbook Layout

The outreach workbook lives in `workspace/` by default and is intentionally split by
entity, not by source bucket:

- `organizations.csv`: master list of companies, startups, incubators, labs, hacker houses
- `opportunities.csv`: internships, research roles, residencies, or other openings
- `contacts.csv`: people tied to an organization, with LinkedIn and email fields
- `touchpoints.csv`: every draft, sent message, reply, and follow-up note
- `sources.csv`: discovery source definitions and provenance

Use `target_lists` as a semicolon-separated tag field such as `jobs;yc;la_startups`
instead of creating one sheet per avenue.

## New Commands

- `python main.py init-workbook`
- `python main.py workbook-summary`
- `python main.py build-resume-outreach-queue`
- `python main.py import-resume-jobs`
- `python main.py list-discovery-sources`
- `python main.py discover-source --source-id yc_los_angeles --limit 25`
- `python main.py discover-source --source-id yc_los_angeles --require-jobs-url --max-team-size 50 --min-batch-year 2024`
- `python main.py discover-source --source-id yc_sf_bay_hiring --enrich-details --max-team-size 50 --min-batch-year 2025`
- `python main.py discover-source --source-id builtin_la_companies --require-jobs-url --remote-only --include-tag robotics --max-team-size 200`
- `python main.py build-linkedin-company-queue --limit 20 --include-target-list yc --include-target-list built_in --require-hiring-signal`
- `python main.py dispatch-linkedin-company-queue --limit 5 --include-target-list yc --include-target-list built_in --require-hiring-signal`
- `python main.py build-target-action-queue --limit 25 --include-target-list yc --include-target-list built_in`
- `python main.py add-organization --name "Y Combinator" --organization-type accelerator`
- `python main.py add-opportunity --organization "Figma" --title "Summer PM Intern"`
- `python main.py add-contact --organization "Figma" --full-name "Avery Product"`
- `python main.py log-touchpoint --organization "Figma" --full-name "Avery Product" --message-text "..."`
- `python main.py import-linkedin-artifact --artifact-path artifacts/...json`

## ResumeGenerator Bridge

ResumeGenerator v1 can stay the upstream job tracker while Outreach consumes only
fresh, apply-worthy jobs for pre-application outreach.

- upstream file: `../ResumeGenerator v1/discovery/jobs.xlsx`
- sheet read: `Jobs` only
- default import filter: `status in {queued, generated}`, `fit_score >= 7.0`,
  `date_found <= 10 days old`
- dedupe: `url_hash` first, otherwise normalized `company + role_title`

Outreach-specific prioritization lives downstream. Use
`workspace/company_overrides.csv` to manually bias certain companies toward
startup outreach or deprioritize big-company outreach without changing
`jobs.xlsx`.

## Current Status

This repo now covers:

- LinkedIn discovery, scoring, note generation, and invite sending workflows
- a reusable outreach workbook for multi-source discovery and contact tracking
- target-action classification for `apply_now`, `outreach_now`, and `skip`

The next layer is source-specific discovery for startup directories, hacker houses,
university labs, and job feeds.

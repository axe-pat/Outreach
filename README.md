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

## Project Layout

```text
src/outreach/
  cli.py
  config.py
  models.py
  tracking.py
  scoring.py
  services/
    linkedin.py
    notes.py
    notion.py
docs/
  architecture.md
tests/
```

## Getting Started

1. Create a virtual environment.
2. Install dependencies with `pip install -e ".[dev]"`.
3. Install Playwright browsers with `playwright install chromium`.
4. Copy `.env.example` to `.env` and fill in your secrets.
5. Run `python main.py doctor`.
6. Run `./scripts/launch_outreach_browser.sh`.
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
- `python main.py add-organization --name "Y Combinator" --organization-type accelerator`
- `python main.py add-opportunity --organization "Figma" --title "Summer PM Intern"`
- `python main.py add-contact --organization "Figma" --full-name "Avery Product"`
- `python main.py log-touchpoint --organization "Figma" --full-name "Avery Product" --message-text "..."`
- `python main.py import-linkedin-artifact --artifact-path artifacts/...json`

## Current Status

This repo now covers:

- LinkedIn discovery, scoring, note generation, and invite sending workflows
- a reusable outreach workbook for multi-source discovery and contact tracking

The next layer is source-specific discovery for startup directories, hacker houses,
university labs, and job feeds.

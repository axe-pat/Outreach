# Architecture Notes

## Recommendation

Build this as a small Python service-oriented CLI first, not a web app.

That gives us:

- faster iteration on browser automation
- easier local debugging with a real Chrome session
- lower operational overhead while the workflow is still changing

## Core Design

The pipeline should be split into five separable services:

1. `LinkedInScraper`
   Owns browser startup, pacing, selectors, and profile extraction.
2. `Scoring`
   Pure deterministic logic with unit tests.
3. `NoteGenerator`
   Wraps LLM prompting and output validation.
4. `NotionPublisher`
   Owns queue writes and idempotency.
5. `OutreachWorkbook`
   Owns the local system of record for organizations, contacts, opportunities,
   touchpoints, and discovery sources.

## Important Implementation Choices

### 1. Browser strategy

Use Playwright with a persistent Chromium context that points to the existing Chrome user data directory. That is the safest Day 1 approach because it preserves the real logged-in session and real browser fingerprint.

### 2. Config strategy

Keep secrets in `.env`, not in `config.yaml`. YAML is still useful later for templates and weights, but API keys and local profile paths should stay out of version control.

### 3. Observability

Every run should write structured artifacts locally:

- raw scraped candidates
- scored candidate list
- generated note outputs
- publish results

That will make debugging selector breaks and note quality much easier.

### 4. Safe rollout

Phase 1 should stop at a review queue. Do not automate sending until we have confidence in:

- selector reliability
- note quality
- false positive rate in candidate targeting

### 5. Multi-source expansion

As the system expands beyond posted jobs, the data model should stay entity-first:

- store all organizations in one master sheet
- store all people in one contacts sheet
- store all messages in one touchpoints sheet
- use typed columns and `target_lists` tags to segment YC, LA startups, hacker houses,
  USC researchers, and referral-driven job targets

This keeps the workbook usable in Excel or Google Sheets without creating a new sheet
every time a new discovery avenue appears.

## Suggested Near-Term Milestones

1. Build and verify the local config loader and CLI.
2. Implement a dry-run LinkedIn scraper that captures candidate rows without sending anything.
3. Add scoring tests with sample profiles.
4. Add note generation with strict 300-character validation.
5. Add Notion sync with idempotent upserts.
6. Add local artifact logging for every run.
7. Add CSV workbook importers so LinkedIn, job feeds, startup directories, and university
   sources all land in one place.

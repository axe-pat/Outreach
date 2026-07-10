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
- `docs/linkedin_browser_playbook.md`: canonical Chrome / LinkedIn session rules shared with ResumeGenerator v1
- `../ResumeGenerator v1/docs/RECRUITING_ENGINE.md`: combined Recruiting Engine operating guide

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
- `python main.py import-story-fit-targets --source-path workspace/story_fit_targets.csv`
- `python main.py init-relationship-leads`
- `python main.py import-relationship-leads --source-path workspace/relationship_leads.csv`
- `python main.py list-discovery-sources`
- `python main.py discover-source --source-id yc_los_angeles --limit 25`
- `python main.py discover-source --source-id yc_los_angeles --require-jobs-url --max-team-size 50 --min-batch-year 2024`
- `python main.py discover-source --source-id yc_sf_bay_hiring --enrich-details --max-team-size 50 --min-batch-year 2025`
- `python main.py discover-source --source-id builtin_la_companies --require-jobs-url --remote-only --include-tag robotics --max-team-size 200`
- `python main.py build-linkedin-company-queue --limit 20 --include-target-list yc --include-target-list built_in --require-hiring-signal`
- `python main.py dispatch-linkedin-company-queue --limit 5 --include-target-list yc --include-target-list built_in --require-hiring-signal`
- `python main.py build-target-action-queue --limit 25 --include-target-list yc --include-target-list built_in`
- `python main.py audit-track-2-core`
- `python main.py build-track-2-daily-plan`
- `python main.py run-track-2-daily-plan`
- `python main.py run-supervised-e2e`
- `python main.py research-linkedin-contact-info-emails --limit 5`
- `python main.py research-external-contact-emails --limit 5`
- `python main.py draft-track-2-emails --max-email-drafts 5`
- `python main.py review-linkedin-followup-drafts --draft-artifact artifacts/...track-2-linkedin-followup-drafts.json`
- `python main.py review-track-2-email-drafts --draft-artifact artifacts/...track-2-email-drafts.json`
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
- default import filter: `status in {queued, generated, applied}`,
  `fit_score >= 7.0`, `date_found <= 10 days old`
- dedupe: `url_hash` first, otherwise normalized `company + role_title`
- daily supervised E2E default: `--resume-season-focus fall_ft_transition`,
  which keeps FT/new-grad/APM and fall/co-op/off-cycle roles while suppressing
  old summer/generic internship rows. Use `--resume-season-focus all` for a
  broad one-off refresh.

Outreach-specific prioritization lives downstream. Use
`workspace/company_overrides.csv` to manually bias certain companies toward
startup outreach or deprioritize big-company outreach without changing
`jobs.xlsx`.

In the combined **Recruiting Engine**, this repo is the **Outreach Lane**:
relationship targets come from application-plus-outreach jobs, startup org
discovery, and the relationship buffer; Outreach handles people search, note
generation, invite sends, and touchpoint logging.

## Story-Fit Targets

`workspace/story_fit_targets.csv` is the curated Track 2 source for companies where
Akshat has a real pitch even when no role is posted. The importer dedupes against
`organizations.csv`, enriches existing accounts with `story-fit` tags and story
metadata, and creates new company rows when needed.

The importer writes operational metadata into organization notes:
`source=story_fit_targets`, `why_this_company`, `story_angle`,
`profile_evidence`, `target_roles`, and `priority`. It remains backward-compatible
with the older `why_you_have_a_case` column.

Preview:

```bash
python main.py import-story-fit-targets --source-path workspace/story_fit_targets.csv
```

Write to the tracker:

```bash
python main.py import-story-fit-targets --source-path workspace/story_fit_targets.csv --execute
```

## Relationship Leads

`workspace/relationship_leads.csv` is the one-time import lane for high-signal
people sources: PeopleGrove, Handshake, USC founders/C-suite, recent MBA PMs, and
manual LinkedIn finds. It writes companies into `organizations.csv`, people into
`contacts.csv`, and preserves source tags like `peoplegrove`, `recent-mba-pm`, and
`usc-founder`.

Create/verify the template:

```bash
python main.py init-relationship-leads
python main.py init-relationship-leads --source-key peoplegrove_usc
python main.py init-relationship-leads --source-key recent_mba_pm
```

Preview or import:

```bash
python main.py import-relationship-leads --source-path workspace/relationship_leads.csv
python main.py import-relationship-leads --source-path workspace/relationship_leads.csv --execute
python main.py import-relationship-leads --source-key peoplegrove_usc --execute
python main.py import-relationship-leads --source-key recent_mba_pm --execute
```

The source-key templates create clean one-time capture files and companion guides:

- `workspace/relationship_leads_peoplegrove_usc.csv` for PeopleGrove/Trojan Network/USC founders, operators, product leaders, and recruiters
- `workspace/relationship_leads_recent_mba_pm.csv` for recent MBA grads who moved into PM/product/product strategy

## Communication Style

`workspace/communication_style_profile.yml` is the local voice/style control file.
LinkedIn invite notes and Track 2 email drafts now use it for banned phrases,
recipient-specific asks, and review metadata. Email drafting remains artifact-only:
the engine can draft reviewed emails, but it does not send them.

For high-stakes emails, use the communication lab loop:

```bash
python main.py build-communication-lab
python main.py review-linkedin-followup-drafts --draft-artifact artifacts/...track-2-linkedin-followup-drafts.json
python main.py draft-track-2-emails --max-email-drafts 5
python main.py review-track-2-email-drafts --draft-artifact artifacts/...track-2-email-drafts.json
python main.py import-communication-feedback --feedback-path artifacts/...draft-review.csv --execute
```

The lab reads the coffee-chat dump, sent touchpoints, and story-bank material, then
scores LinkedIn follow-ups and email drafts for generic networking language,
unsupported callbacks, generic company-fit lines, seniority mismatches, missing
earned background, weak asks, and lack of human/friction lines. Review commands
also emit a CSV beside the JSON artifact. Fill `user_decision`, `user_reason`,
`user_edit`, or `user_notes` in that CSV and import it into
`workspace/communication_feedback.csv` with `import-communication-feedback`.

Story-fit metadata is now used directly in communication drafts. If an account has
`story_fit_reason` or `profile_evidence` in its organization notes, Track 2 email
and senior/founder/product LinkedIn drafts prefer that angle over generic company
fit language.

## Supervised Daily E2E

`run-supervised-e2e` is the guarded daily wrapper for the whole Outreach lane. In
default mode it previews/import-checks the source lanes, rebuilds the account
tracker, audits Track 2, builds the campaign plan, and runs the bounded Track 2
daily plan without sending or writing live LinkedIn updates.

```bash
python main.py run-supervised-e2e
```

During the July/August transition this defaults to:

```bash
python main.py run-supervised-e2e --resume-season-focus fall_ft_transition
```

Use `--resume-season-focus all` when intentionally rebuilding the broad
ResumeGenerator account universe.

Safe write mode:

```bash
python main.py run-supervised-e2e --execute
```

Safe write mode imports source rows, rebuilds the tracker, audits/scopes Track 2,
runs non-browser enrichment, and queues LinkedIn browser work unless live flags
are explicitly enabled:

```bash
python main.py run-supervised-e2e --execute --live-linkedin
python main.py run-supervised-e2e --execute --refresh-linkedin
```

LinkedIn sends stay behind a second explicit flag:

```bash
python main.py run-supervised-e2e --execute --send-linkedin
```

The attended/manual Outreach debug runner is:

```bash
scripts/run_manual_supervised_e2e_debug.sh
```

`scripts/run_daily_supervised_e2e.sh` is retained only as a compatibility shim
that prints a warning and delegates to the debug runner.

The installed daily runner is the macOS LaunchAgent
`com.akshat.resumegenerator.nightly`, which calls ResumeGenerator's
`discovery/scripts/run_nightly_pipeline.py` at `1am`. That is the blessed daily
path because it already runs the live ResumeGenerator discovery/generation lane,
Outreach relationship discovery, bounded app-queue invite sends, account
maintenance, campaign planning, and the live bounded Track 2 daily plan.

Current cycle config is `offcycle_light`:

- ResumeGenerator/app-queue LinkedIn invites are capped at `5` total because
  fall/off-cycle app volume is low.
- Track 2 is the primary relationship lane and runs with up to `25` new
  LinkedIn invites, `25` LinkedIn follow-ups/replies, `15` company mapping
  tasks, and `10` LinkedIn Contact Info/email research tasks.
- The old ResumeGenerator follow-up sender is skipped in nightly mode so Track
  2 owns follow-up execution and the tracker remains the source of truth.
- Switch the nightly pipeline to `--cycle-config normal` when app volume returns;
  that raises the app-queue invite target back to `25` while preserving the
  Track 2 relationship caps unless intentionally retuned.

At the end of that LaunchAgent path, ResumeGenerator now calls:

```bash
python main.py write-daily-run-report --workspace workspace --since <run-start> --nightly-summary <summary-json>
```

That command refreshes the Outreach HTML/Markdown report from the actual
artifacts produced by that nightly run. It has two modes: the scheduled
run-scoped mode (pass both `--since` and `--nightly-summary`) is the source of
truth; an ad-hoc mode without them is only for local troubleshooting and is
clearly labeled as a workspace snapshot. The report has a first-class Source
Breakdown for LinkedIn, Handshake, JobSpy, startup sources, the
ResumeGenerator/app queue, and Track 2 imports/maintenance. Sources that did
not run are shown as `skipped` with zeroes rather than being inferred from old
artifacts.

Each run also writes a small reusable LinkedIn comms-learning corpus under
`workspace/comms_learning/`: manual messages are `gold`, generated drafts
replaced by those messages are `negative`, and sent approved/automatic drafts
are `silver`. The report shows only that run's learning counts and links its
per-run artifact. The standalone debug runner remains
useful for attended troubleshooting or a manual full Outreach run, but it is not
the scheduled daily source of truth.

Latest user-facing report:

```text
workspace/reports/daily_html/daily_run_report.html
```

For compatibility, the same latest HTML/Markdown report is also mirrored to
`workspace/reports/daily_run_report.html`, `workspace/daily_run_report.html`,
and `workspace/daily_run_report.md`.

## Current Status

This repo now covers:

- LinkedIn discovery, scoring, note generation, and invite sending workflows
- a reusable outreach workbook for multi-source discovery and contact tracking
- target-action classification for `apply_now`, `outreach_now`, and `skip`

The next layer is source-specific discovery for startup directories, hacker houses,
university labs, and job feeds.

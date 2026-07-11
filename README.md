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
  cadence.py
  cli.py
  company_news.py
  company_watchlist.py
  config.py
  discovery/
  email_delivery.py
  linkedin_signals.py
  linkedin_affinity.py
  models.py
  outcome_learning.py
  role_surface_monitor.py
  shared_discovery.py
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
- `python main.py stage-relationship-leads --source-path workspace/relationship_leads.csv`
- `python main.py review-relationship-leads --staged-path workspace/relationship_leads.staged.csv --reviewer Akshat`
- `python main.py import-relationship-leads --source-path workspace/relationship_leads.staged.csv --execute`
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
- `python main.py capture-linkedin-intelligence --max-scrolls 5 --max-items 100`
- `python main.py capture-company-news --per-source-limit 30`
- `python main.py review-linkedin-feed-signal <signal-id> --disposition company_candidate`
- `python main.py build-company-discovery-review --run-id <run-id>`
- `python -m outreach.shared_discovery --nightly-summary '../ResumeGenerator v1/discovery/source_validation/...nightly-pipeline-summary.json'`
- `python main.py build-role-surface-report --source-metrics <run-source-metrics.json> --run-id <run-id>`
- `python main.py build-outreach-cadence-report`
- `python main.py build-outcome-learning-report`
- `python main.py send-track-2-emails --draft-artifact artifacts/...track-2-email-drafts.json`
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

The cross-repo decision surface is `workspace/shared_discovery/shared_daily_queue.{json,csv}`.
It consumes one exact ResumeGenerator nightly summary or action-queue artifact,
merges application roles with YC/Built In candidates, approved company-watchlist
entries, and warm Outreach contacts, then dedupes at company level. It never
authorizes a send or writes back into `jobs.xlsx`; every row carries its gate,
recommended action, and source provenance. The nightly runner now refreshes this
queue after its current-run action queue is available.

Strategic companies stay company-level tracker rows even when no current PM role
exists. The shared queue mirrors them as low-priority, buffered `role_watch` tasks.
If the exact ResumeGenerator run surfaces a strong Product Strategy,
BizOps/Strategy, Program/Operations, or narrowly defined Growth-adjacent opening,
the same company row becomes a human-gated `application_research` task. Each role
records its family, classification rule, source, source URL, upstream queue bucket,
decision, and write gate. The fail-closed recovery path considers an omitted scored
role only when it has an exact URL, fit score of at least 7, and upstream
`Proceed`/`accepted` evidence; generic growth marketing, blocklisted, dropped, and
rejected rows do not trigger the watch.

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

For a signed-in PeopleGrove browser pull, capture a JSON array with
`full_name`, `headline`, `program`, `grad_year`, `member_type`, `source_url`,
`source_record_id`, `queries`, and `labels`, then curate it before staging:

```bash
python main.py curate-peoplegrove-capture \
  --input-path artifacts/peoplegrove_capture.json \
  --enrichment-path artifacts/peoplegrove_career_journey.json \
  --output-path workspace/relationship_leads_peoplegrove_curated.csv \
  --workspace workspace
```

The curator accepts only an explicit current `TITLE at COMPANY`, `TITLE @
COMPANY`, or founder/company separator. It retains product/product strategy,
BizOps/strategy, program/operations leadership, founder/C-suite,
recruiting/talent, and venture/startup-operator lanes. Students, interns, job
seekers, vague or irrelevant titles, ambiguous companies, and duplicates are
rejected. Its companion `.summary.json` records every decision, category,
score, rejection reason, and source identity. The optional workspace argument
is read-only and suppresses already-imported contacts.

`--enrichment-path` is optional. Use it only for exact current roles captured
from signed-in PeopleGrove Career Journey profiles when a directory-card headline
does not expose a parseable current role/company. The enrichment is a strict,
capture-bound JSON object: `schema_version`, `source_capture_sha256`,
`captured_at`, `captured_by`, and a `profiles` mapping keyed by the exact
`source_record_id` or canonical `source_url`. Each mapping contains those source
identities plus non-empty `current_roles` entries with exactly `title`, `company`,
`date_range`, and `location`. Unknown identities, duplicate IDs/URLs, mismatched
ID/URL pairs, malformed roles, duplicate JSON keys, or a capture-hash mismatch
block the whole curation before outputs are replaced. A parseable card headline
still wins; otherwise the curator selects only an explicit enriched current role
that passes the same role and irrelevance gates. It never derives an employer or
title. The original headline, selected role evidence, enrichment operator/time,
and artifact hash remain in the notes and decision audit. If an existing explicit
company alias is used for account dedupe, the exact captured title and company
remain separately recorded as enrichment evidence.

That loader is deliberately limited to signed-in PeopleGrove Career Journey
data bound to the exact raw-capture hash. Public-web corroboration is not a
valid `--enrichment-path` input. Research from official pages, LinkedIn public
profiles, or professional directories must be manually reviewed, converted
into a separate staged relationship batch with its evidence URL/status intact,
and imported under `peoplegrove_public_web` university-directory provenance.

Keep a capture manifest beside the raw JSON with each query's advertised count,
profiles captured, card batches/scrolls, and an honest termination state. An
exact-count query may be marked exhausted; a high-volume query stopped at a
deliberate review-yield budget must remain `bounded_sample_cap`, not be described
as exhaustive.

Stage, review, then import. Raw captures cannot be executed directly:

```bash
python main.py stage-relationship-leads \
  --source-path workspace/relationship_leads_peoplegrove_curated.csv \
  --source-key peoplegrove_usc
python main.py review-relationship-leads \
  --staged-path workspace/relationship_leads_peoplegrove_curated.staged.csv \
  --reviewer Akshat \
  --decision-artifact workspace/peoplegrove_review_decisions.json
python main.py import-relationship-leads \
  --source-path workspace/relationship_leads_peoplegrove_curated.staged.csv \
  --source-key peoplegrove_usc \
  --execute
```

Staging validates required identity/provenance, URL shape, email shape, source
records, duplicates, and batch fingerprints. For a reviewed batch, the decision
artifact must contain `schema_version`, `batch_id`, the original `source_sha256`,
the pre-review `staged_sha256`, exact `approved_row_ids`/`rejected_row_ids`, and
matching `rows_total`/`rows_approved`/`rows_rejected` counts. Those lists must
partition every staged row exactly once. The review manifest records the
decision artifact's own SHA-256 and
its source/stage binding, so later edits fail import. Bulk flags affect only
pending rows; changing a finalized decision requires the explicit
`--override-finalized` flag. Imports never create or rewrite their source and
stop on missing, empty, raw, unreviewed, tampered, or ambiguous inputs.

The source-key templates create clean one-time capture files and companion guides:

- `workspace/relationship_leads_peoplegrove_usc.csv` for PeopleGrove/Trojan Network/USC founders, operators, product leaders, and recruiters
- `workspace/relationship_leads_recent_mba_pm.csv` for recent MBA grads who moved into PM/product/product strategy

Current baseline: the earlier 28-profile USC proof-of-flow seed and 11-profile
public USC/MBA seed remain valid history. The July 11 signed-in pull captured
1,845 unique profiles across 12 targeted role/education queries; seven exact-count
queries were exhausted and five broad queries remain bounded best-match samples,
not exhaustive directory coverage. Structural curation retained 154 and rejected
1,691. Final signed-in review approved 104 and rejected 50; a separate public
corroboration pass approved 31 additional profiles from 111 researched ambiguous
cards. All 135 reviewed people are now imported without guessed emails or
LinkedIn URLs. Exact import reruns made zero organization/contact changes. The
tracker now contains 174 relationship-source contacts including the prior seeds,
while 1,710 profiles from the capture remain excluded. This stays a low-frequency
source pull, not a daily scraper.

## Communication Style

`workspace/communication_style_profile.yml` is the local voice/style control file.
LinkedIn invite notes and Track 2 email drafts now use it for banned phrases,
recipient-specific asks, and review metadata. Email work remains artifact-first.
Live delivery exists only through the separately gated `send-track-2-emails`
command described below; drafting or running the normal daily plan does not by
itself send an email.

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

## Recruiting Intelligence, Coverage, and Channel Controls

The recruiting-intelligence layer implements the discovery-to-learning loop
without turning every signal into an automatic action. Its generated CSV/JSON
files are operating artifacts under `workspace/`; they are not source files and
should not be committed.

### Daily LinkedIn feed and weekly viewer capture

Run the read-only LinkedIn pass against the same dedicated, signed-in Chrome
session used by the other live LinkedIn commands:

```bash
python main.py capture-linkedin-intelligence \
  --max-scrolls 5 \
  --max-items 100 \
  --max-duration-seconds 0 \
  --profile-viewers-every-days 7
```

The feed pass is intended to run daily. Its budget is configurable: `0` means
no time cap, so the operating limit can be tuned from real runs instead of being
fixed at 60 seconds. It records hiring, jobs, funding, launches, warm-network
activity, good startups/new-company candidates, and other relevant posts in
`workspace/linkedin_feed_signals.csv`, preserving post URL when LinkedIn exposes
one plus author/company/context fallback and explicit URL-coverage counts when
it does not. It never sends a message.

Profile viewers are checked weekly by default and retained in
`workspace/linkedin_profile_viewers.csv` as passive context. The ledger dedupes
repeat observations and annotates target-company or role relevance; a profile
view never creates or sends outreach by itself. Set
`--profile-viewers-every-days 0` only for an intentional every-run capture.

### Company discovery and reviewed watchlist

The feed ledger is an independent discovery source; a company does not need to
already exist in `organizations.csv`. Review an individual feed signal first if
needed:

```bash
python main.py review-linkedin-feed-signal <signal-id> \
  --disposition company_candidate \
  --note "Why this deserves company review"
python main.py build-company-discovery-review \
  --run-id <run-id> \
  --capture-artifact artifacts/...linkedin-intelligence-capture.json \
  --source-metrics ../ResumeGenerator\ v1/discovery/source_validation/...source-run-metrics.json
python main.py capture-company-news --per-source-limit 30
python main.py build-company-discovery-review \
  --run-id <same-run-id> \
  --news-capture-artifact artifacts/...company-news-capture.json
```

The builder dedupes candidates, preserves provenance, and writes an editable
review queue plus JSON/CSV watchlist payloads under
`workspace/company_discovery/`. Its explicit 15-point rubric covers domain fit,
the technical-MBA story, geography/remote viability, growth/quality, and a
plausible Product or adjacent-role surface. A strong score is only a
recommendation: promotion requires a human `approved` review state. After
editing the review CSV, rebuild it and use `--promote-approved` to write only
approved, rubric-qualified entries into the company-level account tracker:

```bash
python main.py build-company-discovery-review \
  --run-id <run-id> \
  --promote-approved
```

Current ingestion combines the exact nightly LinkedIn-feed capture, the same-run
ResumeGenerator startup relationship/apply report (including YC and Built In),
and the exact company/news capture. The default public feeds are TechCrunch
Startups and Crunchbase News; Hacker News is opt-in because it is noisier.
`--input-path` on `capture-company-news` also adapts reviewed CSV, JSON, or JSONL
directory/news exports to the same typed candidate contract. The tracker remains
memory after discovery, not the source of discovery. Run summaries include only
exact current inputs, while the editable review queue remains cumulative.

### High-affinity LinkedIn expansion

The canary-only affinity mode keeps exact-company search as the base and can add
bounded Intuit, Gojek, USC, Marshall, Thapar, Hevo, Optum, and role-family
searches for top role-backed accounts. It is disabled by default in both the
nightly runner and standalone company runs; enable it deliberately with
`--enable-affinity-expansion` during a bounded validation run. When enabled, a
per-company invite cap can rise from 3 to at most 5 only when real scored
affinity candidates exist and the Track 2 daily budget has unused headroom.
Lifted slots additionally require structured current-company evidence and a
defensible role route; past-company matches, executive-support/CISO routes, and
polluted off-company results fail closed. Mapping never enables these expansion
passes. The same employer gate protects the standalone, app-queue, and Track 2
send surfaces: an exact-company filter failure blocks the batch, while startup or
coverage-only candidates need independent structured current-employer evidence.
Candidate names, cached match flags, and search pass labels are not evidence. The
July 11 Delinea dry-run found four raw affinity hits, only two of
which qualified for lift, so its recommendation correctly stayed at the base
cap of three.

### Role-family coverage

The account tracker stays company-level. A separate, run-scoped monitor reads
the current ResumeGenerator source-metrics artifact and reports discovered,
scored, surfaced, and acted-on roles for:

- Product / PM, the primary lane
- Product Strategy
- BizOps / Strategy
- Program / Operations
- narrowly defined Growth/GTM-adjacent roles

```bash
python main.py build-role-surface-report \
  --source-metrics <run-source-metrics.json> \
  --run-id <same-run-id>
```

Outputs live under `workspace/role_surface/` as reusable JSON and CSV tables by
family, source, and source/family. Sources that were skipped or failed remain
visible with zeroes. The monitor rejects mixed-run inputs and surfaces
unclassified titles for audit; it does not add role rows to the company tracker.

### Target-role-aware messaging

The role being pursued is now separate from the recipient's job. Invite notes,
accepted-invite follow-ups, reply drafts, review CSV suggestions, and Track 2
initial/follow-up/final emails use one target-role resolver for Product/PM,
Product Strategy, BizOps/Strategy, Program/Operations, narrow Growth/GTM, and a
general-business fallback. This includes existing-connection, USC/Marshall, and
shared-history invite paths; a concrete non-Product target no longer gets
"pivoting into PM" copy.

Resolution is deterministic and provenance-preserving: an explicit selected
plan role wins; concrete opportunity titles come next (Product remains primary
inside a mixed opportunity set); structured `target_roles` notes are the next
tier; Product/PM is the fallback only when no concrete target exists. Recipient
facts such as a contact's Product background remain intact while the candidate's
pursued-role language changes.

Draft artifacts and communication-review CSVs include `target_role_family`,
label, source, matched text/rule, and whether the evidence was concrete. Sent
invite touchpoints retain the
family so message reconciliation and later follow-ups reuse the role actually
targeted instead of guessing from the contact title. The daily Track 2 plan also
passes its selected `target_role` into both LinkedIn invite generation and email
drafting. This is execution context only: it does not add role rows or role-bound
contacts to the company-level account tracker.

### Tracker-backed cadence and cold email

The touchpoint ledger is the source of truth for both channels. Inspect the
current decisions with:

```bash
python main.py build-outreach-cadence-report
```

The implemented defaults are:

- accepted LinkedIn invite, no reply: first useful follow-up on day 4
- one final LinkedIn value-add 4–5 days later; generic or near-duplicate nudges
  are blocked
- cold email: initial note, first follow-up after 4 days, optional final note
  4–5 days later, with at most three email touches in 90 days
- any reply/engagement pauses automated cadence; rejection, unsubscribe,
  bounce, invalid address, or do-not-contact stops it
- a same-day LinkedIn/email double-tap is suppressed

Cold-email delivery is preview-only unless every gate passes. The contact needs
a verified address, the tracker must say the matching email action is due, the
draft needs an explicit approval marker, and a bounded live command must include
`--execute`:

```bash
# Safe preview: no SMTP connection and no send.
python main.py send-track-2-emails \
  --draft-artifact artifacts/...track-2-email-draft-review.json \
  --approval-csv artifacts/...track-2-email-draft-review.csv \
  --limit 5

# Live only after review and SMTP setup.
python main.py send-track-2-emails \
  --draft-artifact artifacts/...track-2-email-draft-review.json \
  --approval-csv artifacts/...track-2-email-draft-review.csv \
  --limit 5 \
  --execute
```

Live delivery requires `SMTP_HOST` and `SMTP_FROM_EMAIL`; configure
`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, and
`SMTP_USE_SSL` as appropriate. These values are read from the environment or
the repo-local `.env`; `SMTP_TIMEOUT_SECONDS` defaults to 30. The approval is
bound to the exact review artifact, recipient, subject, and reviewed body, and
the reviewed email must match the tracker address. Successful sends are written
back to `touchpoints.csv`; an interrupted uncertain attempt is held for manual
reconciliation instead of being blindly retried. SMTP delivery is not silently
enabled by the nightly run.

### Comms and outcome learning

The run report's reusable comms corpus keeps manual sends as `gold`, sent
approved/automatic drafts as `silver`, and generated drafts replaced or cleared
by manual messages as `negative`. Build the outcome view with:

```bash
python main.py build-outcome-learning-report
```

`workspace/comms_learning/outcome_learning.json` combines those labels with
tracker accepts, replies, rejections, message types, audiences, and accounts.
Gold/silver examples sync as bounded strong examples and negative rows as weak
examples in `workspace/communication_style_profile.yml`, so later draft
polishing actually learns from the corpus. Aggregate outcome recommendations
remain advisory: they do not rewrite prompt rules, rubrics, selection policy,
or send messages. Human review remains the gate before those rules change.

### Activation and manual gates

The code paths above are implemented. Reliable operation still requires:

- a dedicated Chrome session with a current LinkedIn login and successful
  `check-linkedin-live` preflight; the current feed/viewer selectors were live
  validated on July 10, 2026 and should be monitored for LinkedIn DOM changes
- daily review dispositions for useful feed signals and explicit human approval
  before any candidate is promoted into `organizations.csv`
- a same-run ResumeGenerator source-metrics artifact for trustworthy role
  coverage; missing sources must stay `skipped`/zero rather than borrowing an
  older artifact
- verified email addresses, reviewed drafts, working SMTP credentials, and an
  explicit bounded `--execute` command before the first live email batch
- real touchpoint outcomes before acting on learning recommendations

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
- The live inbox is scanned whenever the LinkedIn refresh lane is enabled, even
  when the campaign planner selected zero follow-up companies. Unanswered
  inbound replies are processed first; unmatched threads are surfaced as
  contact-mapping/user actions instead of disappearing.
- Track 2 mapping uses a bounded cross-functional search pass set and imports
  the resulting contacts with exact per-company counts. Full affinity expansion
  remains an invite-campaign concern, not a 15-company maintenance multiplier.
- The old ResumeGenerator follow-up sender is skipped in nightly mode so Track
  2 owns follow-up execution and the tracker remains the source of truth.
- Switch the nightly pipeline to `--cycle-config normal` when app volume returns;
  that raises the app-queue invite target back to `25` while preserving the
  Track 2 relationship caps unless intentionally retuned.

At the end of that LaunchAgent path, ResumeGenerator now calls:

```bash
python main.py write-daily-run-report --workspace workspace --since <run-start> --nightly-summary <summary-json> --run-id <run-id>
```

That command refreshes the Outreach HTML/Markdown report from the selected
nightly summary. In run-scoped mode, `--run-id` must match the ID recorded in
the summary and names each exact report artifact; `--since` identifies the run
start but is never used to discover artifacts. Every claimed action must come from one of
these explicit pointers:

- `nightly_summary.daily_engine_manifest` for ResumeGenerator/app-queue invite,
  follow-up, reply, and email-send artifacts
- `nightly_summary.outreach_maintenance.track_2_daily_run_artifact` and that
  artifact's exact phase/child-artifact pointers
- named source, maintenance, and intelligence artifacts in the same nightly
  summary

An unrelated manual command, test, or concurrent run can create an artifact in
the same directory and time window without affecting the report. It has two
modes: the scheduled
run-scoped mode (pass both `--since` and `--nightly-summary`) is the source of
truth; an ad-hoc mode without them is only for local troubleshooting and is
clearly labeled as a workspace snapshot. The report has a first-class Source
Breakdown for LinkedIn, Handshake, JobSpy, startup sources, the
ResumeGenerator/app queue, Track 2 imports/maintenance, and the cold-email
channel. Sources that did
not run are shown as `skipped`/`not_run` with zeroes rather than being inferred
from old artifacts. Startup sources also show adapter and lane stages separately
(`fetched`, `discovered`, and `selected/new`), so YC/Built In company discovery
cannot be confused with startup job discovery.

Overall run health is derived from those same exact-run rows. A required
LinkedIn, Handshake, JobSpy, startup, app-queue, or Track 2 row with status
`failed`, `timed_out`, `timeout`, `partial_failed`, or `incomplete` prevents the
report from claiming `completed`. An explicitly configured `skipped` source and
a successful source with zero results remain valid and visible.

The report is action-first: it separates outcomes actually executed in that
run (including per-company counts, such as invites sent or contacts mapped)
from the next campaign plan. It has distinct contracts:

- `What needs you` contains only concrete human actions, such as a resume/email
  request, routing decision, message-review batch, or explicit SMTP/configuration
  blocker.
- `Messages to review` contains unsent drafts behind a human-review gate and
  shows the channel, recipient, subject/inbound context, draft, gate, and whether
  it came from this run or the carried-over queue. Track 2 email drafts belong
  here until approved; a draft is never counted as sent.
- `Auto-handled messages` contains only exact send results with `status=sent`.
- `LinkedIn actions` puts invite sends, inbox refresh/triage, follow-ups,
  replies, mapping, Contact Info research, feed capture, viewer capture,
  review holds, skips, and failures in one place.
- `Execution by company` is derived from actual result artifacts; plan budgets
  and campaign recommendations never count as completed work.
- `Cold email actions` records reviewed delivery results separately. Email totals
  increase only from an exact send artifact whose delivery status is `sent`;
  manifest-reported SMTP blockers and sent/draft-count mismatches stay visible.

Open inbox items persist in `workspace/linkedin_inbox_actions.csv` until marked
done, snoozed, not actionable, or resolved by an exact auto-send result. Track 2
with no return code/artifact is `not_run`, not `completed`; phase failures and
queued/planned live work keep the run from being reported as green.
An exact-company-filter failure and an invite whose delivery is unknown are
also non-green. Every live invite is reserved before a killable worker attempts
delivery; an uncertain slot is never retried automatically and appears in
`What needs you` until signed-in reconciliation resolves it. App-queue prep/send
failures and zero-send company attempts remain visible in LinkedIn Actions and
Execution by company even when no send artifact exists.

### Production promotion contract

The scheduled end-to-end path is the protected production path. New stages stay
disabled there until they have isolated unit/contract tests, a fixture-backed
end-to-end report test, and an explicit run-manifest schema. Production report
tests must use temporary artifact/workspace directories and must prove that an
unreferenced concurrent artifact cannot change totals. After those gates pass,
the change can be merged and enabled in the scheduled config. A production run
is complete only when its final nightly summary, daily-engine manifest, required
stage results, Track 2 phase artifact, and daily report all exist and reconcile.

Each run also writes a small reusable LinkedIn comms-learning corpus under
`workspace/comms_learning/`: manual messages are `gold`, generated drafts
replaced by those messages are `negative`, and sent approved/automatic drafts
are `silver`. The report shows only that run's learning counts and links its
per-run artifact. The standalone debug runner remains
useful for attended troubleshooting or a manual full Outreach run, but it is not
the scheduled daily source of truth.

The new intelligence sections retain their natural scopes instead of being
blurred together. Feed capture status and the role-surface report are run-scoped
when built from that run's capture/source-metrics artifacts. Company review and
watchlist counts, the cadence plan, profile-viewer history, and outcome learning
are cumulative workspace-state snapshots. A current nightly maintenance pointer
proves that one of those snapshots was refreshed during the run; it does not
turn its historical rows into events from that run. The report must label these
as workspace state and must never use them to claim that a source ran or that
all counted actions happened in the current cycle.

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
- configurable LinkedIn home-feed discovery and a passive profile-viewer ledger
- independent company-candidate review and human-approved watchlist promotion
- public company/news feeds plus reviewed structured imports through that same
  candidate contract, with source-aware tracker promotion
- the run-stamped shared ResumeGenerator/Outreach daily company queue
- default-off, bounded high-affinity LinkedIn canary passes for role-backed
  priority accounts
- tamper-bound stage/review/import gates for low-frequency relationship-source
  pulls, including a synthetic 150-row idempotency regression test
- run-scoped role-family coverage with explicit source status
- tracker-enforced LinkedIn/email cadence and explicitly gated SMTP delivery
- gold/silver/negative communication examples plus advisory outcome learning

The next operating layer is live validation: run the new passes for several
cycles, review the resulting queues, activate a small SMTP batch only after the
manual gates pass, and use observed coverage/outcomes to tune source and role
breadth. Additional company-discovery inputs can then reuse the same review
contract.

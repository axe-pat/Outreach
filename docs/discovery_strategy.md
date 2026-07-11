# Discovery Strategy

## Recommendation

Do not create a separate sheet for each avenue like YC, hacker houses, incubators,
professors, and job-derived companies. That looks tidy for a week and becomes messy
very quickly.

Use one workbook split by entity:

- `organizations.csv`
- `opportunities.csv`
- `contacts.csv`
- `touchpoints.csv`
- `sources.csv`

Then classify rows with:

- `organization_type`
- `opportunity_type`
- `target_lists`
- `status`

Example `target_lists` values:

- `jobs;referrals`
- `yc;startup`
- `la_startups;founder_outreach`
- `usc;research`
- `hacker_house;summer`

## Why This Grouping Works

It solves two problems at once:

1. You keep one system of record for every target.
2. You can still filter the workbook into the exact slices you care about.

That means one company can belong to multiple tracks without duplication. A startup
found from a YC directory can later also appear in the resume-generator jobs feed and
still remain one organization row with shared contacts and touchpoints.

## Suggested Source Tracks

### 1. Job-driven companies

Owner: resume generator pipeline

Flow:

- job feed discovers opening
- company is added to `organizations.csv`
- opening is added to `opportunities.csv`
- LinkedIn people search adds warm contacts to `contacts.csv`
- generated notes land in `touchpoints.csv`

### 2. Startup and YC discovery

Flow:

- discover startup from directory, batch, or founder list
- add startup to `organizations.csv` with `target_lists=yc;startup`
- add any visible internship or founder-call opening to `opportunities.csv`
- run LinkedIn discovery against the company and add contacts

### 3. Local ecosystem discovery

Examples:

- LA startup ecosystem lists
- SF early-stage startup lists
- accelerator portfolio pages
- incubators and builder communities

These should usually create organization-first records, then branch into LinkedIn or
email outreach based on who is reachable.

### 4. Hacker houses and entrepreneurial programs

These are usually a mix of organization and opportunity:

- organization: the house, residency, or program
- opportunity: summer residency, builder slot, fellowship, stipend, or sponsorship
- contact: organizer, founder, operator, or resident lead

### 5. USC professors and labs

This is person-first outreach but should still map into the same structure:

- organization: USC or the lab
- opportunity: research assistantship, project collaboration, summer role
- contact: professor, lab manager, PhD, research lead
- touchpoint: email-first in most cases

## Channel Strategy

Default channels by target type:

- startups and companies: LinkedIn first, email second
- professors and labs: email first, LinkedIn second
- hacker houses and communities: X, email, or website form plus LinkedIn
- warm referrals: direct LinkedIn message or email, depending on relationship strength

## Shared Daily Decision Queue

The entity workbook remains Outreach's relationship system of record and
ResumeGenerator's `jobs.xlsx` remains the application system of record. The cross-repo
layer is deliberately a run-stamped decision artifact rather than a third tracker.
Its scope labels the ResumeGenerator input as exact-run evidence and the Outreach
workbook/watchlist inputs as snapshots:

```bash
python -m outreach.shared_discovery \
  --nightly-summary "../ResumeGenerator v1/discovery/source_validation/<run>-nightly-pipeline-summary.json" \
  --workspace workspace
```

The builder rejects an unscoped/stale nightly pointer, normalizes every application
and startup-company bucket, merges duplicates by conservative company identity, then
adds approved watchlist and warm-contact evidence. Each JSON/CSV row carries roles,
source provenance, the recommended action, and one of three gates:
`ready_for_next_stage`, `human_review_required`, or `buffered`. It never sends and
never writes a company directly into `jobs.xlsx`.

## Review-Gated Company and News Sources

Public TechCrunch Startups and Crunchbase News RSS feeds now enter the same candidate
rubric as LinkedIn feed, YC, and Built In signals. Hacker News is available only as an
explicit opt-in source. Reviewed accelerator/directory exports can be supplied as
CSV, JSON, or JSONL with `capture-company-news --input-path`; all paths preserve the
source URL/run and remain pending until a human approves a rubric-qualified candidate.
Each exact capture embeds canonical signal snapshots plus a SHA-256 binding, so replay
cannot silently read newer rows from the mutable ledger. Approved watchlist entries are
durable across idempotent rebuilds; an explicit rejection still removes one.

## Build Order

1. Keep the current LinkedIn pipeline as the person-discovery and note-generation engine.
2. Use the workbook as the single local source of truth.
3. Keep the shared run-stamped queue and importers from existing artifacts and the
   ResumeGenerator job list healthy.
4. Add new source-specific discoverers one at a time through the reviewed candidate contract:
   startup directories, USC faculty pages, incubator lists, hacker house lists.
5. Add email outreach as another touchpoint channel instead of building a parallel system.

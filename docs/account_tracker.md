# Account Tracker — Operational Reference

Scoring design decisions live in `docs/relationship_engine.md` → **Scoring Philosophy**.

## How To Run

```bash
outreach account-tracker
# with explicit paths:
outreach account-tracker --workspace workspace --output workspace/account_tracker.xlsx
```

Track 2's company universe is built from these seeded/import sources plus the
normal YC/BuiltIn/startup discovery commands:

```bash
# Built-in strategic/dream accounts: MAANG, major SaaS, AI/data/devtool,
# fintech, collaboration/productivity, and selected high-fit platform companies.
outreach import-strategic-accounts --workspace workspace --execute

# Story-fit accounts: companies selected because Akshat has a real pitch,
# not because they posted a role.
outreach import-story-fit-targets \
  --workspace workspace \
  --source-path workspace/story_fit_targets.csv \
  --execute

# Relationship leads: PeopleGrove, Handshake, USC founder/C-suite,
# recent MBA PM, and manual LinkedIn finds. Adds both companies and contacts.
outreach init-relationship-leads
outreach init-relationship-leads --source-key peoplegrove_usc
outreach init-relationship-leads --source-key recent_mba_pm
outreach import-relationship-leads \
  --workspace workspace \
  --source-path workspace/relationship_leads.csv \
  --execute
outreach import-relationship-leads --workspace workspace --source-key peoplegrove_usc --execute
outreach import-relationship-leads --workspace workspace --source-key recent_mba_pm --execute

# Broad ResumeGenerator company universe. This intentionally ignores Lane 1's
# score/age gates so applied/discovered companies are still available for
# account-level ranking and future campaigns.
outreach import-resume-jobs \
  --jobs-xlsx "../ResumeGenerator v1/discovery/jobs.xlsx" \
  --account-universe \
  --resume-season-focus all

# Current-cycle intake for the July/August transition. This keeps FT/new-grad/APM
# and fall/co-op/off-cycle roles, while filtering old summer/generic internships.
outreach import-resume-jobs \
  --jobs-xlsx "../ResumeGenerator v1/discovery/jobs.xlsx" \
  --account-universe \
  --resume-season-focus fall_ft_transition
```

Before trusting Track 2 tiers for newly imported companies, run company-context
enrichment:

```bash
# Preview missing/stale context using public pages/search first
outreach enrich-company-context --limit 50

# Write verified public context for a focused batch
outreach enrich-company-context --limit 50 --execute

# Verify every company that still has only inferred/unverified context
outreach enrich-company-context --limit 300 --verify-all --execute

# Force-refresh the selected slice, useful after changing enrichment logic
outreach enrich-company-context --limit 100 --start-at 0 --verify-all --force --execute --timeout-seconds 6

# Fast source-first cleanup: refresh only companies with known public URLs
outreach enrich-company-context --limit 300 --verify-all --force --require-direct-url --no-web-search --no-job-fallback --execute --timeout-seconds 2

# Fast all-company preview from local ResumeGenerator fit rationales only
outreach enrich-company-context --limit 300 --no-network
```

## Output

`workspace/account_tracker.xlsx` — core tracker sheets plus operational views:

- **Account Tracker** — all companies, full detail, auto-filter on every column
- **Tier A — Active Campaign** — active startup/growth relationship campaigns only
- **Action Queue** — Relationship A/B and Large L1/L2 companies with actionable stages, sorted by urgency
  (conversation → connected → active outreach → people mapped → priority target)
- **Campaign Plan** — concrete Track 2 actions with channel, reason, and Lane 1 policy
- **Startup Founder-Led** — smaller/startup accounts where founder/product routing is plausible
- **Growth Mid-Market** — scaled startups/mid-market accounts where product/recruiting mapping matters more
- **Large Company** — 1000+ employee accounts that need wider org/referral mapping
- **Large Company Priority** — L1/L2 large-company referral/routing accounts
- **Strategic Wishlist** — manual/dream/priority accounts, regardless of company size
- **Needs Enrichment** — accounts whose domain/company context is not externally strong enough yet

## Key Columns

| Column | Notes |
|--------|-------|
| Account Score | Track 2 strategic priority: profile/domain fit + reachability + brand/manual priority + small traction bonus + capped hiring/path signal + pitch strength + team gate/data penalties |
| Fit Score | Opportunity-weighted score retained for context: profile fit + role fit + team gate + reachability + raw hiring/path signal + relationship depth − no-domain penalty |
| Tier | `A/B/C` for startup/growth relationship campaigns; `L1/L2/L3` for separate large-company referral/routing priority |
| Account Stage | Derived from contact/touchpoint state — see relationship_engine.md |
| Why Fit | Top signals that drove the score |
| Next Action | Recommended move based on account stage |
| Campaign Action | Concrete Track 2 action such as `expand_linkedin_wave`, `map_more_contacts`, or `find_email_path` |
| Daily Action Priority | Execution urgency for today. Replies/accepts can rise here without distorting Account Score |
| Lane 1 Policy | How normal outreach should treat the company: `track_2_owns`, `fresh_role_only`, or `lane_1_allowed` |
| Account Track | Operational segment: `Startup / Founder-Led`, `Growth / Mid-Market`, `Large Company`, or `Needs Enrichment` |
| Score: Profile / Role / Team / Reach / Hiring / Rel / Momentum / Brand / Pitch / Account Hiring | Per-component breakdown for transparency. `Rel` is the small strategic bonus; `Momentum` drives daily action urgency |

## Operational Views

These views are partly filters and partly playbook boundaries:

| View | Operational meaning |
|------|---------------------|
| Startup Founder-Led | Track toward founder/product conversations, quick routing, and compact account waves |
| Growth Mid-Market | Map product/recruiting/referral paths; wider than a startup, narrower than a giant company |
| Large Company | Use a large-company playbook: hiring-adjacent PM leaders/recruiters plus separate referral-path contacts |
| Strategic Wishlist | Override/spotlight accounts Akshat explicitly cares about; still obeys the right company-size playbook |
| Needs Enrichment | Research queue before bespoke Track 2 touches; role-only context should not drive priority accounts |

Relationship Tier A is intentionally startup/growth-only:

| Track | Tier A target |
|------|---------------|
| Startup Founder-Led | 20 |
| Growth Mid-Market | 12 |

Large companies do not compete for Relationship Tier A. They get `L1/L2/L3` labels
instead because the large-company motion is different: the main outcome is referral,
recruiter routing, alumni/internal advocate, or hiring-manager visibility. A bespoke
company thesis is useful as context, but it is not the primary conversion lever the
way it can be at a startup or growth company.

Roles are now treated as path signals rather than the core reason to run Track 2.
FT/new-grad/APM/product-path roles count most. Fall/co-op internships count only when
they are LA-compatible or remote; unknown-location fall roles get weak credit, and
in-person/hybrid fall roles outside LA should not lift Track 2 priority. Summer
internships remain visible in Fit Score, but should not by themselves push a company
into the relationship-campaign priority pool.

## Company Context Enrichment

`enrich-company-context` fills the `tags=...`, `description=...`, and `context_*`
metadata that Account Score depends on.

The current default operating shape is:

- every daily supervised run imports strategic account seeds and the
  `fall_ft_transition` slice of the ResumeGenerator account universe
- every nightly run resolves/enriches a small bounded batch, then rebuilds the
  tracker and campaign plan
- broad ResumeGenerator account-universe refreshes should be explicit/manual
  (`--resume-season-focus all`) while summer/generic internship rows are noisy
- larger cleanup passes should still run manually in visible batches so bad
  website guesses can be caught quickly

Selection rules:

- new job-imported companies with no tags/description are selected automatically
- stale context is selected again after `context_refresh_after`
- targeted refresh is available with repeated `--company` options
- `--verify-all` selects companies that still lack `external_verified` context
- `--force` refreshes selected companies even if they are already
  `external_verified`, which is useful when the extraction logic changes
- `--start-at` lets an all-company force refresh run in stable bounded slices
- `--require-direct-url` is the fast first pass: it skips search-only companies
- `--no-job-fallback` prevents a failed public fetch from downgrading verified
  company context into local-only inference

Confidence levels:

| Confidence | Meaning |
|------------|---------|
| `external_verified` | Context came from a fetched public company/source page |
| `manual_seed` | Canonical built-in strategic account with hand-written tags/context; useful for ranking, but not fetched evidence |
| `inferred_from_job` | Context came from ResumeGenerator job fit rationale; useful, but discounted by Account Score |
| `missing` | No trustworthy company/domain context yet; use Needs Enrichment before bespoke campaign work |

Funding/prestige signals such as `techcrunch-covered`, `crunchbase-profile`,
`series-a`, `series-b`, `series-c-plus`, `yc-backed`, and top-investor backing are
stored as `prestige_signals=...` and feed the Brand component of Account Score.
Investor tags require funding/backing language near the investor name, so a customer
or integration mention should not inflate the account score.

Use `--no-network` for a fast local backfill. Use default network mode for verified
context, preferably in bounded batches because public pages/search can be slower.

Run website resolution/enrichment in small visible batches when the result will affect
Relationship A/B, Large L1/L2, or the campaign plan. Compound names with short
prefixes, such as `d-Matrix`, must preserve the full company identity and should not
be resolved to generic domains like `matrix.com`.

## Campaign Plan

Build the JSON/action artifact without opening Excel:

```bash
outreach build-account-campaign-plan --limit 30
```

Audit whether Track 2 has coherent actions/channels:

```bash
outreach audit-track-2-core
```

Build today's bounded Track 2 execution queue:

```bash
outreach build-track-2-daily-plan
```

Run the daily plan in phase order. By default this writes review artifacts only; use
`--execute` for live non-send work, and add `--send-linkedin` only when you want the
send phases to actually send:

```bash
outreach run-track-2-daily-plan
outreach run-track-2-daily-plan --refresh-linkedin --linkedin-message-limit 25
outreach run-track-2-daily-plan --execute
outreach run-track-2-daily-plan --execute --live-linkedin
outreach run-track-2-daily-plan --execute --send-linkedin
```

`--execute` is safe for unattended cron runs: it writes imports/local updates and
runs non-browser phases, while live LinkedIn browser work stays queued unless
`--live-linkedin`, `--refresh-linkedin`, or `--send-linkedin` is passed.

Draft cold emails from selected Track 2 email actions. This is draft-only and uses
`workspace/communication_style_profile.yml` for banned phrases and recipient-specific
asks:

```bash
outreach build-communication-lab
outreach review-linkedin-followup-drafts --draft-artifact artifacts/...track-2-linkedin-followup-drafts.json
outreach draft-track-2-emails --max-email-drafts 5
outreach review-track-2-email-drafts --draft-artifact artifacts/...track-2-email-drafts.json
outreach import-communication-feedback --feedback-path artifacts/...draft-review.csv --execute
```

The communication path is intentionally iterative: build a corpus-backed brief,
review LinkedIn follow-ups and email drafts for slop/specificity, then edit the
style profile or draft rules based on what fails. Email should stay artifact-only
until drafts are consistently strong under review.

Each review command writes both JSON and a markup-ready CSV. The CSV carries the
message, verdict, flags, quality labels, and rewrite guidance, plus blank
`user_decision`, `user_reason`, `user_edit`, and `user_notes` fields. Importing
marked rows appends them to `workspace/communication_feedback.csv`; it does not
silently mutate the style profile.

For story-fit accounts, communication drafts read `story_fit_reason` and
`profile_evidence` from organization notes. That lets curated story-led targets
produce sharper first-pass drafts than generic account-list companies.

Inspect LinkedIn Contact Info overlays for mapped contacts whose selected daily action
needs email research:

```bash
outreach research-linkedin-contact-info-emails --limit 5
outreach research-linkedin-contact-info-emails --limit 5 --execute
```

Email research order is intentionally conservative:

1. use emails already stored on contacts
2. inspect LinkedIn Contact Info for mapped contacts who expose an email
3. only then use a configured external finder

The external finder is opt-in because it can spend credits. Configure one or both:

```bash
PROSPEO_API_KEY=...
HUNTER_API_KEY=...
EMAIL_FINDER_PROVIDER=auto   # auto, prospeo, or hunter
EMAIL_FINDER_MIN_CONFIDENCE=80
EMAIL_FINDER_ONLY_VERIFIED=true
```

Run external research as its own supervised source:

```bash
outreach research-external-contact-emails --limit 5
outreach research-external-contact-emails --limit 5 --execute --provider auto
```

Or include it in Track 2 execution after LinkedIn Contact Info misses:

```bash
outreach run-track-2-daily-plan --execute --external-email-finder --email-finder-provider auto
```

Action meanings:

| Action | Meaning |
|--------|---------|
| `enrich_company_context` | Role exists, but company/domain context is too thin for bespoke campaign motion |
| `map_more_contacts` | Strong account, but not enough relevant people mapped |
| `send_initial_multichannel_outreach` | Relevant contact has email; send LinkedIn + email in parallel |
| `send_initial_invites` | Relevant people are mapped, but no first wave has been sent |
| `expand_linkedin_wave` | Some LinkedIn outreach sent; expand toward a fuller account wave |
| `follow_up_connected_contact` | Someone accepted; send accepted-invite follow-up before new outreach |
| `continue_conversation` | Real conversation exists; move toward coffee chat, routing, or referral |
| `send_cold_email_followup` | LinkedIn wave failed and an email contact exists |
| `find_email_path` | LinkedIn wave is not converting; inspect LinkedIn Contact Info/email path before another blind wave |
| `pause_account` | Do not spend relationship-engine budget right now |

Channel meanings:

| Channel | Meaning |
|---------|---------|
| `linkedin` | LinkedIn-only action |
| `email` | Email-only action because an email contact exists |
| `linkedin+email` | Parallel LinkedIn and email outreach to mapped contacts |
| `linkedin+email_research` | Build both LinkedIn contact map and email path before first touch |
| `email_research` | Inspect LinkedIn Contact Info and other email/contact paths; no usable email is stored yet |
| `research` | Company/context research before outreach |
| `none` | Paused account |

## Source Files

| File | Purpose |
|------|---------|
| `src/outreach/account_tracker.py` | All scoring and Excel generation logic |
| `workspace/account_tracker.xlsx` | Generated output — regenerate freely, not a source of truth |
| `docs/relationship_engine.md` | Scoring philosophy and design decisions |

# Account Tracker — Operational Reference

Scoring design decisions live in `docs/relationship_engine.md` → **Scoring Philosophy**.

## How To Run

```bash
outreach account-tracker
# with explicit paths:
outreach account-tracker --workspace workspace --output workspace/account_tracker.xlsx
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

`workspace/account_tracker.xlsx` — four sheets:

- **Account Tracker** — all companies, full detail, auto-filter on every column
- **Tier A — Active Campaign** — top 20 companies
- **Action Queue** — Tier A/B companies with actionable stages, sorted by urgency
  (conversation → connected → active outreach → people mapped → priority target)
- **Campaign Plan** — concrete Track 2 actions with channel, reason, and Lane 1 policy

## Key Columns

| Column | Notes |
|--------|-------|
| Account Score | Track 2 relationship-campaign priority: profile/domain fit + reachability + brand/manual priority + relationship traction + capped hiring + pitch strength + team gate/data penalties |
| Fit Score | Opportunity-weighted score retained for context: profile fit + role fit + team gate + reachability + raw hiring + relationship depth − no-domain penalty |
| Tier | A (top 20) / B (next 40) / C (rest) — rank-based by Account Score, not threshold |
| Account Stage | Derived from contact/touchpoint state — see relationship_engine.md |
| Why Fit | Top signals that drove the score |
| Next Action | Recommended move based on account stage |
| Campaign Action | Concrete Track 2 action such as `expand_linkedin_wave`, `map_more_contacts`, or `switch_to_email_or_wellfound` |
| Lane 1 Policy | How normal outreach should treat the company: `track_2_owns`, `fresh_role_only`, or `lane_1_allowed` |
| Score: Profile / Role / Team / Reach / Hiring / Rel / Brand / Pitch / Account Hiring | Per-component breakdown for transparency |

## Company Context Enrichment

`enrich-company-context` fills the `tags=...`, `description=...`, and `context_*`
metadata that Account Score depends on.

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
| `inferred_from_job` | Context came from ResumeGenerator job fit rationale; useful, but discounted by Account Score |

Funding/prestige signals such as `techcrunch-covered`, `crunchbase-profile`,
`series-a`, `series-b`, `series-c-plus`, `yc-backed`, and top-investor backing are
stored as `prestige_signals=...` and feed the Brand component of Account Score.
Investor tags require funding/backing language near the investor name, so a customer
or integration mention should not inflate the account score.

Use `--no-network` for a fast local backfill. Use default network mode for verified
context, preferably in bounded batches because public pages/search can be slower.

## Campaign Plan

Build the JSON/action artifact without opening Excel:

```bash
outreach build-account-campaign-plan --limit 30
```

Action meanings:

| Action | Meaning |
|--------|---------|
| `enrich_company_context` | Role exists, but company/domain context is too thin for bespoke campaign motion |
| `map_more_contacts` | Strong account, but not enough relevant people mapped |
| `send_initial_invites` | Relevant people are mapped, but no first wave has been sent |
| `expand_linkedin_wave` | Some LinkedIn outreach sent; expand toward a fuller account wave |
| `follow_up_connected_contact` | Someone accepted; send accepted-invite follow-up before new outreach |
| `continue_conversation` | Real conversation exists; move toward coffee chat, routing, or referral |
| `switch_to_email_or_wellfound` | LinkedIn wave is not converting; add/switch channel |
| `pause_account` | Do not spend relationship-engine budget right now |

## Source Files

| File | Purpose |
|------|---------|
| `src/outreach/account_tracker.py` | All scoring and Excel generation logic |
| `workspace/account_tracker.xlsx` | Generated output — regenerate freely, not a source of truth |
| `docs/relationship_engine.md` | Scoring philosophy and design decisions |

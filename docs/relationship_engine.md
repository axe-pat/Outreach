# Relationship Engine Roadmap

The outreach system should not optimize for sending invites. It should optimize for
creating real conversations, warm advocates, referrals, and eventual internship/job
paths at companies where Akshat has a credible fit.

## Product Shape

The system has two lanes.

### Lane 1: Volume Apply + Opportunistic Outreach

This is the existing machine:

- find roles
- apply when relevant
- identify nearby LinkedIn contacts
- send targeted connection notes
- log send results

This lane stays useful because recruiting is still partly a volume game.

### Lane 2: Priority Account Relationship Engine

This is the new strategic layer:

- maintain a smaller dynamic account list
- rank companies by fit, reachability, and hiring likelihood
- map multiple people per priority company
- keep working the company until there is a conversation or a reason to pause
- draft and eventually send follow-ups, replies, and channel switches

The outcome is not "5 LinkedIn messages." The outcome is a growing network of people
who know Akshat, understand his product/operator/technical-PM fit, and may route or
vouch for him when the right role appears.

## North Star

Every daily run should answer and execute:

> What are the highest-leverage relationship moves Akshat should make today?

At first the system should produce reviewed sends. Over time, low-risk messages should
be sent automatically.

## Company Account Model

Company stages:

- `unqualified`: not worth relationship-engine attention yet
- `priority_target`: strong fit or manually promoted
- `people_mapped`: relevant contacts exist
- `outreach_active`: invites/messages are in flight
- `connected_no_conversation`: someone accepted but has not replied
- `conversation_started`: real reply or exchange exists
- `coffee_chat`: chat scheduled or completed
- `warm_advocate`: person is warm enough to route, refer, or advise
- `referral_path`: explicit referral/process path exists
- `paused`: no near-term path or no longer a strong fit

Contact stages:

- `identified`
- `invite_sent`
- `accepted`
- `follow_up_drafted`
- `follow_up_sent`
- `replied`
- `coffee_chat_scheduled`
- `coffee_chat_done`
- `warm_relationship`
- `referral_or_advocate`

## Priority Company Scoring

The account list should be dynamic, not a static handpicked sheet.

Signals:

- profile fit: data/platform, marketplaces, applied AI, dev tools, product ops,
  infrastructure, fintech/security systems, technical PM work
- reachability: USC/Marshall, India/Delhi, shared employer, LA, and 2nd-degree paths
- brand/prestige/manual priority: top-tier brand, strong product company, YC/funded
  startup, or Akshat explicitly marks it priority/core/dream
- relationship traction: accepted invites and real replies matter because they are
  already partway toward the outcome
- hiring likelihood: active roles and internships matter, but should be capped so a
  role-only company does not outrank a better long-term relationship target
- pitch strength: whether the outreach can be grounded in real work rather than
  forced specificity
- team gate/data quality: very tiny teams and missing company context should slow
  the relationship campaign until the account is better understood

Context enrichment should run across the whole company universe, not only current
top-ranked accounts. A company can be artificially buried when it lacks tags,
description, website, or funding/prestige signals; verified public context is what
lets Track 2 promote it into the right tier.

The account universe should be intentionally broad:

- strategic seed accounts: MAANG, major SaaS, AI/data/devtool, fintech, and other
  dream/high-fit companies Akshat wants in the relationship system even before a
  specific application exists
- ResumeGenerator job imports: every credible applied/discovered company should flow
  into Track 2 account state, even when Lane 1 would not send outreach today
- discovery/referral artifacts: YC, BuiltIn, LinkedIn/referral/imported sources

Lane 1 can stay strict about which people to message today. Track 2 should preserve
the company universe and let ranking, enrichment confidence, and campaign action
decide whether it deserves relationship effort.

Tiers:

- Relationship Tier A: startup/growth companies actively campaigned
- Relationship Tier B: startup/growth companies monitored and touched periodically
- Large Company L1/L2/L3: separate referral/routing priority track
- Tier C: normal volume apply/outreach

Excel/CSV is the right first visual surface. The account view should include:

- company
- tier
- account score
- fit score
- why fit
- target role/path
- hiring signal
- people mapped
- invites sent
- accepted
- replies
- coffee chats
- advocates/referrals
- next action
- next due date
- status

## Daily Loop

The daily run should be small and action-oriented.

1. Reconcile LinkedIn deltas.
   - read the LinkedIn message thread list from the last stored offset
   - detect newly accepted invites because accepted contacts appear in messages
   - detect accepted contacts who also sent a reply
   - use the connections/profile page only as a fallback audit when the message
     offset looks incomplete

2. Draft and send safe follow-ups.
   - accepted invite with no reply
   - recruiter/process follow-up
   - engineer referral/pointer follow-up
   - junior PM/APM "how to get on radar" follow-up

3. Update company account state.
   - promote companies with traction
   - expand companies with fit but no replies
   - switch channel when LinkedIn volume is not converting
   - pause weak accounts

4. Continue normal discovery/apply/outreach.

## Follow-Up Playbooks

Most accepts do not reply. The system should treat an accepted invite as the start of
the relationship, not as a successful outcome.

Inputs:

- original invite note
- company context
- contact type
- current account goal
- prior thread text if present
- Akshat communication style profile

Default accepted-invite actions:

- founder: ask for a quick read on where a technical MBA/operator could be useful
- senior product: ask a product/team-direction question tied to fit
- junior product/APM: ask how to get on the hiring team's radar
- engineer in India: ask for referral or pointer if the fit looks reasonable
- engineer elsewhere: ask how builders influence product or how technical PMs stand out
- recruiter: ask for process/screening guidance

The system should start with human review, then graduate low-risk follow-ups to
auto-send.

## Communication Style Profile

Because this system will write a lot of communication, it needs a local style profile.

Store:

- strong messages Akshat liked
- weak messages Akshat rejected
- preferred directness
- preferred casualness
- banned phrases
- common self-intro variants
- approved asks by recipient type

This should be read by future note, follow-up, and reply generators.

The communication engine now has a separate feedback ledger:

- review commands emit JSON plus a CSV that Akshat can mark up
- marked rows are imported into `workspace/communication_feedback.csv`
- imported feedback is deduped by draft identity plus user decision/edit
- the ledger stores `user_decision`, `user_reason`, `user_edit`, and `user_notes`
- style-profile changes should be deliberate follow-up work, not an automatic side
  effect of importing one review CSV

Review metadata should travel with every generated message: score, verdict,
recommended action, flags, strengths, quality labels, and rewrite guidance.

Story-fit account metadata should also travel into communication. When an
organization has `story_fit_reason` or `profile_evidence`, generators should use
that as the first company-fit angle before falling back to generic account or title
heuristics.

## Build Order

### One-Time Relationship Source Imports

PeopleGrove/USC and recent-MBA-to-PM pulls are low-frequency, high-signal batches,
not daily scrapers. Their safe path is now explicit:

1. Capture the signed-in PeopleGrove directory into a JSON array with stable source
   IDs/URLs and the visible professional fields. Write a companion coverage manifest
   with per-query advertised/captured counts, batch/scroll observations, and either
   `exhausted_exact_count` or an honest bounded/error termination.
2. Run `curate-peoplegrove-capture --workspace workspace` to reject students,
   interns, job seekers, vague/irrelevant roles, ambiguous current companies,
   capture duplicates, and already-imported contacts. The command never writes the
   tracker. When a card omits a parseable current role/company, an optional
   capture-hash-bound `--enrichment-path` may supply exact Career Journey
   `current_roles`; identity mismatches or malformed mappings fail closed, and only
   explicit roles that pass the normal relevance gates can be selected.
   This enrichment loader accepts only signed-in, capture-hash-bound PeopleGrove
   data. Public-web corroboration is incompatible with it and must remain a
   separately reviewed/staged relationship batch with its evidence URL and
   resolution status plus `peoplegrove_public_web` university-directory
   provenance intact.
3. Inspect its curated relationship-lead CSV and JSON decision audit. The audit
   records category, score, source identity, and an explicit rejection reason for
   every captured profile.
4. Run `stage-relationship-leads` to normalize, validate, fingerprint, and dedupe.
5. Reconcile every staged row into one decision JSON containing the batch/source/
   pre-review staged hashes, complete approved/rejected row-ID lists, and matching
   counts. Seal it with `review-relationship-leads --decision-artifact ...`.
6. Run `import-relationship-leads --execute` only against that reviewed staged file.

Execution fails closed on missing provenance, invalid URLs/email shape, edited rows
after review, duplicate rows, changed decision artifacts, incomplete row partitions,
and ambiguous workbook identity conflicts. Bulk decisions affect pending rows only;
changing a finalized row requires `--override-finalized`. Import never creates or
rewrites its input. No email is guessed, and staging/review never writes the
company/contact tracker.

Existing July 4 PeopleGrove rows remain valid tracker history, but the 28-profile set
is only an early seed. The July 11 signed-in capture and curation are complete: 1,845
unique profiles across 12 queries produced 153 curated candidates and 1,692 explicit
rejections. Seven exact-count surfaces were exhausted and five high-volume surfaces
were deliberately bounded best-match samples. The 104-approved/49-rejected manual
partition must still be reconciled into one complete SHA-bound decision artifact;
the July 11 staged batch is not approved for import until that reconciliation passes.

### 1. LinkedIn Reconcile

Highest immediate value. Detect and record what happened after sends:

- accepted invite with no reply
- accepted invite with a message/reply
- existing thread already seen
- unmatched message thread that needs manual mapping
- stale pending invite only as a secondary audit

This creates the closed loop. Without this, every next step is guessing.

The primary implementation should maintain a LinkedIn message offset, not poll every
recently invited profile. The reason is simple: if Akshat sends invites to everyone,
accepted invites appear in LinkedIn messages. The daily loop should scan new message
threads since the last offset, match them back to workbook contacts, and classify them
as `connected` or `replied`.

Profile/connection-page checks are still useful, but only as fallback:

- when a message thread cannot be matched
- when LinkedIn messages look incomplete
- when we need to audit old pending invites
- when a contact's profile state conflicts with the message offset

### 2. Accepted-Invite Follow-Up Engine

Generate next messages for accepted invites with no reply. Start with review, then
auto-send safe cases.

### 3. Communication Style Profile

Build a reusable style layer before expanding generated communication volume.

### 4. Priority Company Account View

Create the Excel/CSV account sheet with tiers, fit, relationship state, metrics, and
next actions.

### 5. Account Campaign Planner

For Relationship A/B and Large L1/L2 companies, decide when to:

- send another LinkedIn wave
- follow up
- switch to email
- use Wellfound
- ask for coffee chat
- ask for referral
- pause the account

The planner should output concrete action records, not just a dashboard row. Current
actions:

- `enrich_company_context`
- `map_more_contacts`
- `send_initial_invites`
- `expand_linkedin_wave`
- `follow_up_connected_contact`
- `continue_conversation`
- `find_email_path`
- `pause_account`

Each action carries a `lane_1_policy`:

- `track_2_owns`: normal ranked outreach should not independently touch this account.
- `fresh_role_only`: Lane 1 may touch only if there is a fresh applied/generated role.
- `lane_1_allowed`: normal outreach can handle it.

### 5a. Company Context Enrichment

Track 2 scoring depends on company-level context, so the system now includes a
separate enrichment pass:

```bash
outreach import-strategic-accounts --execute
outreach import-resume-jobs --jobs-xlsx "../ResumeGenerator v1/discovery/jobs.xlsx" --account-universe
outreach resolve-company-websites --limit 20 --execute
outreach enrich-company-context --limit 50
outreach enrich-company-context --limit 50 --execute
outreach enrich-company-context --limit 300 --verify-all --execute
outreach enrich-company-context --limit 300 --no-network
```

The command selects companies with missing/stale `tags` or `description`, including
new companies imported from ResumeGenerator jobs. It writes:

- `tags`
- `description`
- `context_enriched_at`
- `context_refresh_after`
- `context_source`
- `context_confidence`
- `context_evidence_url`
- `prestige_signals`
- `prestige_evidence_url`

Confidence matters:

- `external_verified`: public company/source page was fetched; safest for Account Score
- `manual_seed`: built-in strategic account context, useful but not externally fetched
- `inferred_from_job`: ResumeGenerator job rationale was used; helpful, but Account
  Score discounts it so a job rationale does not masquerade as verified company truth
- `missing`: no reliable company/domain context yet; route to Needs Enrichment

Funding and market-quality evidence is part of enrichment. Signals from company
pages, TechCrunch, Crunchbase, YC, and investor/funding language feed the Brand /
Prestige component of Account Score, capped with manual priority and known-brand
signals so prestige does not double-count.

### 6. Reply + Conversation Agent

Classify incoming replies and draft responses:

- "happy to connect"
- "what would you like to know?"
- referral openness
- recruiter process answer
- coffee-chat openness
- rejection/no path

Eventually, safe replies should send automatically.

### 7. Email + Wellfound Expansion

Add non-LinkedIn channels only after the LinkedIn loop is stable.

## Implementation Principle

The system should not hand Akshat a generic to-do list. It should do the work and
surface only the parts that need judgment:

- safe to send
- needs review
- blocked by missing data
- asks for a strategic decision

Every artifact should be reviewable, but the default direction is automation with
guardrails.

---

## Scoring Philosophy

This section records the design decisions behind the priority company scorer
(`src/outreach/account_tracker.py`). Update here when scoring logic changes.

### Two Scores, Two Jobs

The tracker intentionally keeps two different scores:

| Score | Purpose |
|-------|---------|
| Account Campaign Score | Track 2 ranking/tiering. Answers: "Should we persistently build relationships at this company?" |
| Daily Action Priority | Track 2 execution queue. Answers: "What needs attention today because momentum exists?" |
| Fit Score | Opportunity context. Answers: "Is there a role/application signal worth preserving or enriching?" |

Campaign actions are a separate layer from strategic score. Account Score decides
durable priority and Tier A/B/C for startup/growth relationship campaigns. Daily
Action Priority decides today's queue. A company with a reply can top the action
queue without becoming a better strategic account than Scale AI, Vercel, Stripe, etc.

### Fit Domains

Domains where Akshat has a real, earned credential — not a stretch:

| Domain | Credential |
|--------|-----------|
| AI-powered SaaS products | FlairX (current AI PM intern), L'Oréal AI workflow project |
| Data infrastructure / data platforms | Hevo Data — ETL pipelines, enterprise data onboarding |
| API / integration platforms | Hevo is a data connector/integration tool at its core |
| Observability / monitoring | Hevo 2.0 AI monitoring platform (H-MONITORING-AI story) |
| Developer tools / DevEx | Engineering depth + internal tooling built; profile lists DevEx as target |
| Hiring tech / workflow automation | FlairX + ResumeGenerator side project — built and interned in this space |
| Consumer marketplace / logistics | Gojek — fare pricing, fleet API, marketplace dynamics |
| Supply chain / ops tech | Gojek fleet and logistics work |
| Enterprise SaaS with product culture | Notion, Rippling, Ramp, Airtable type companies — technical PM fit |
| FinTech / billing | Intuit — SMB billing, financial data systems |
| Payments infrastructure | Intuit billing; Stripe, Recurly adjacents are fair |
| Healthcare IT | Optum — regulated AI, clinical workflows (weaker interest, real credential) |
| AI agents / autonomous workflow | ResumeGenerator and Outreach system are working agentic products |

Broad terms are allowed only when they are earned and token-matched, not substring
matched. For example, `api` cannot match `Capital`, `llm` cannot match `fulfillment`,
and `ai` cannot match random word fragments. Generic enterprise wording alone should
not create profile fit.

### Profile Fit Scoring

Profile fit is tag-matching against company description and tags. Scoring should be
domain-specific and weighted by credential strength:

- Highest weight (5 pts): AI/ML products, data infrastructure, developer tools, hiring tech
- Medium weight (4 pts): observability, integration platforms, marketplace/logistics, fintech
- Lower weight (3 pts): healthcare IT, payments, ops tech, supply chain

Cap profile fit contribution at 25 pts before any bonus.

### Account Campaign Score

Track 2 Account Campaign Score is:

| Component | Range | Notes |
|-----------|-------|-------|
| Profile/domain fit | 0–25 | Earned domains only; token-aware matching |
| Reachability | 0–12 | Akshat-specific paths only |
| Brand/prestige/manual priority | 0–12 | Manual priority can override brand inside this cap; it does not stack beyond 12 |
| Relationship traction | 0–6 | Small bonus for accepted/replied contacts; should not dominate strategic rank |
| Account hiring signal | 0–6 | Capped hiring/path nudge, not the center of Track 2 |
| Pitch strength | 0–10 | Strong story + warm path + workable team size |
| Team gate | −10–0 | Penalizes teams too small for meaningful PM/operator path |
| Missing domain context | −8 or −5 | Prevents role-only imports from becoming priority accounts |

This is deliberately not role-fit-heavy. A PM internship with no company thesis should
trigger enrichment, not automatically become Tier A.

Relationship momentum is still very important, but it belongs in **Daily Action
Priority**, not as a heavy Account Score component. A reply/accepted invite should
make the system act today; it should not make a weaker company look like a better
long-term target.

### Team Size — Maturity Gate

Team size is NOT a reachability signal. It is a company maturity gate that affects
whether a real PM internship is likely to exist and be meaningful.

| Team size | Score adjustment |
|-----------|-----------------|
| < 10 | Hard penalty (−10): too small, no real PM structure |
| 10–15 | Slight discount (−5): marginal, very hands-on |
| 15–200 | No adjustment: sweet spot for startup internships |
| 200–1000 | Neutral: mid/large startup |
| 1000+ | Large company track: different campaign logic |

Mount at 2 people should not rank #1. A 2-person company has no PM internship in
any meaningful sense.

### Reachability

Reachability should capture only Akshat's specific network advantages — not generic
startup size or general approachability. Max 12 pts.

| Signal | Points | How detected |
|--------|--------|-------------|
| USC / Marshall connection | +5 | `usc` or `marshall` in contact notes/triggers |
| Indian / Delhi background in contacts | +4 | `india`, `indian`, `delhi` in contact notes |
| Shared past employer (Intuit/Gojek/Hevo/Optum founder or exec) | +4 | keywords in org description or contact notes |
| LA location | +3 | city contains `Los Angeles`, `LA`, `Santa Monica`, etc. |
| 2nd-degree connection density | +3 if ≥2 2nd-degree contacts | `2nd Degree` in contact triggers |

Things removed from reachability:
- Team size (moved to maturity gate above)
- YC visibility (not a reachability advantage, moved to prestige/brand signal)

### Brand / Prestige / Manual Priority Signal

Current implementation uses an automatic first pass:

| Score | Company type |
|-------|-------------|
| 12 | Top-tier product/company brand or explicit `dream` / `tier-a` manual tag |
| 10 | Explicit `priority`, `core`, `relationship`, or `target` manual tag |
| 8 | Strong known product company |
| 5 | YC signal |
| 3 | Recognizable source signal such as BuiltIn/growth-stage/funding text |
| 0 | No brand/manual-priority signal |

Manual priority and brand live in the same capped component so they can promote an
account without double-counting.

### Hiring Likelihood

- FT/product-path role, including new-grad/APM/rotational/product roles: +18
- Other FT role posted: +14
- LA-compatible or remote fall/spring/winter/co-op/off-cycle internship: +12
- Fall/co-op internship with unknown location: +6
- In-person/hybrid fall/co-op internship outside LA/remote: +1
- Generic internship with no current-season signal: +6
- Summer internship: +3
- Any other opportunity discovered: +5
- Nothing: 0

Track 2 is role-aware, not role-driven. Roles are timing/path signals for relationship
work; they should not decide whether a company is strategically worth building into.
Summer internships are weak Track 2 signals now because the summer cycle is mostly
over. Fall/co-op/current internships matter only when they are compatible with being
in LA for school, or clearly remote. In-person/hybrid fall roles outside LA should
not lift Track 2 priority; they can remain visible for context, but they are not a
real fall path.

Use `date_posted` from `jobs.xlsx` (via ResumeGenerator bridge) when available.
Recency decay for stale roles should still be added, but the core rule is already:
active relationship priority should not be dominated by old summer internship imports.
The ResumeGenerator bridge now exposes `--resume-season-focus`; daily supervised
E2E defaults to `fall_ft_transition`, while broad historical universe refreshes
should use `--resume-season-focus all` explicitly.

For Account Campaign Score, hiring is deliberately compressed into `Account Hiring`
(0–6). Raw hiring still appears in Fit Score so active roles remain visible for
Lane 1/apply-driven follow-up.

### Relationship Depth

Relationship progress is split into two components. A small strategic traction bonus
feeds Account Score, while the larger momentum score drives Daily Action Priority.

| Contact state | Account Score bonus | Daily momentum |
|--------------|---------------------|----------------|
| 1+ replied/warm contact | +6 | +15 to +20 |
| 3+ accepted contacts | +4 | +12 |
| 1–2 accepted contacts | +3 | +8 |
| No connections yet | 0 | 0 |

A company where you have an active conversation should jump today's action queue, but
it should not automatically become a better long-term strategic account than a higher
fit company with no traction yet.

### No-Domain-Data Penalty

Companies imported from `jobs.xlsx` (ResumeGenerator) often have no tags or description.
These get role fit + hiring credit (the PM intern listing is real) but are docked **−8 pts**
since there's no domain signal to confirm fit. Stopgap until those imports are enriched
with tags from YC/BuiltIn discovery or `company_overrides.csv`.

### Tier Assignment

Tier assignment is split by operating model.

Startup and growth companies compete for relationship-campaign tiers because a
bespoke company thesis, proof of fit, and founder/product routing can materially
change outcomes there. Large companies do **not** compete inside the same Tier A.
At Salesforce/Adobe/Meta-scale, the likely useful outcomes are referral, recruiter
routing, alumni/internal advocate, or hiring-manager visibility. No one is evaluating
whether Akshat has a uniquely sharp thesis on Salesforce's future; the pitch mainly
makes the referral/routing ask feel credible instead of random.

Relationship Tier A is up to 32 active startup/growth campaign accounts:

| Track | Tier A target |
|-------|---------------|
| Startup / Founder-Led | 20 |
| Growth / Mid-Market | 12 |

Relationship Tier B is the next 50 qualified startup/growth accounts. Large companies
receive separate `L1` / `L2` / `L3` priority labels and remain in the Large Company
view and Campaign Plan, but they do not consume relationship Tier A slots.

The **Action Queue** sort order also weights relationship stage —
`conversation_started` and `connected_no_conversation` always surface above
higher-scoring companies with zero relationship progress.

This keeps large companies in the system without letting them clog the highest-touch
startup/growth relationship funnel.

### Large Company Track (1000+ employees)

Large companies follow different campaign logic:
- Do not optimize for a bespoke company thesis as the primary conversion lever; use
  the thesis as context for a referral/routing ask
- Map the org hierarchy first — prioritize PM managers, Directors of Product, and
  recruiters over ICs
- Target ~5–7 people closest to the hiring decision (PM managers, Dir of Product,
  recruiters who post PM roles)
- Target ~5 people for referral path (any engineer or PM who can submit internally)
- Multi-touchpoint still valid; direct referral ask is the primary outcome
- Account stage derivation same as startups; next action copy differs

### Open Decisions / Still To Build

- Strategic account seeds and ResumeGenerator account-universe import are wired.
  Daily maintenance should use the transition-focused season slice; broad historical
  refreshes remain available via `import-resume-jobs --account-universe --resume-season-focus all`.
- Enrichment guardrails and Relationship A/B cleanup are mostly in place: compound-name
  website resolution now preserves meaningful short prefixes (`d-Matrix` must not
  collapse to generic `matrix.com`), generic suffixes such as "Solutions" require
  stronger identity matches, and JavaScript/anti-bot placeholder pages are rejected.
  Continue running visible batches, use **Needs Enrichment** as the work queue, and
  regenerate the tracker after each batch to inspect rank/action movement.
- Company enrichment is wired into the nightly orchestrator as a small bounded
  resolve/enrich pass plus tracker/campaign-plan rebuild. A separate fortnightly
  stale-context refresh can still be added once daily operations are stable.
- Prestige score: enrichment now captures funding/investor/source signals, but the
  source-quality rubric can still be improved as we see real examples.
- 2nd-degree density: wired into reachability scoring (≥2 contacts with "2nd degree" in
  notes → +3 pts). Populated automatically by the daily LinkedIn Playwright pipeline.
- `date_posted` recency decay: jobs.xlsx has the field; bridge to account tracker
  not yet built.
- Large company track: team size and view generation are wired. The next useful build
  is contact-mapping strategy per view: PM/recruiter/referral-path mapping for large
  companies, founder/product routing for startups, and narrower product/recruiting
  mapping for growth/mid-market accounts.
- Manual priority currently comes from account target-list/notes tags. A structured
  `company_overrides.csv` priority/brand field would be cleaner.

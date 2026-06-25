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

Tiers:

- Tier A: 15-20 companies actively campaigned
- Tier B: 30-50 strong-fit companies monitored and touched periodically
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

## Build Order

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

For Tier A/B companies, decide when to:

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
- `switch_to_email_or_wellfound`
- `pause_account`

Each action carries a `lane_1_policy`:

- `track_2_owns`: normal ranked outreach should not independently touch this account.
- `fresh_role_only`: Lane 1 may touch only if there is a fresh applied/generated role.
- `lane_1_allowed`: normal outreach can handle it.

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
| Fit Score | Opportunity context. Answers: "Is there a role/application signal worth preserving or enriching?" |

Campaign actions are a separate layer from score. The score decides durable priority;
the action decides the next move based on state. A company can have low Account Score
but still get `enrich_company_context` if it has a strong role signal and missing
domain data.

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
| Relationship traction | 0–20 | Active/replied relationships beat cold accounts |
| Account hiring signal | 0–8 | Capped hiring/role nudge, not the center of Track 2 |
| Pitch strength | 0–10 | Strong story + warm path + workable team size |
| Team gate | −10–0 | Penalizes teams too small for meaningful PM/operator path |
| Missing domain context | −8 or −5 | Prevents role-only imports from becoming priority accounts |

This is deliberately not role-fit-heavy. A PM internship with no company thesis should
trigger enrichment, not automatically become Tier A.

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

- Internship posted (recent, within 45 days): +20
- Internship posted (older, 45–90 days): +12
- FT role posted: +10
- Any opportunity discovered: +5
- Nothing: 0

Use `date_posted` from `jobs.xlsx` (via ResumeGenerator bridge) when available.
Recency decay: roles older than 90 days should not get full hiring signal credit.

For Account Campaign Score, hiring is deliberately compressed into `Account Hiring`
(0–8). Raw hiring still appears in Fit Score so active internships remain visible for
Lane 1/apply-driven follow-up.

### Relationship Depth

Relationship progress is a direct score component (not just a sort tiebreaker):

| Contact state | Points |
|--------------|--------|
| 3+ contacts Warm (replied) | +20 |
| 1–2 contacts Warm | +15 |
| 3+ contacts Connected (accepted, no reply) | +12 |
| 1–2 contacts Connected | +8 |
| No connections yet | 0 |

A company where you have an active conversation outranks a company with better raw fit
but zero relationship progress.

### No-Domain-Data Penalty

Companies imported from `jobs.xlsx` (ResumeGenerator) often have no tags or description.
These get role fit + hiring credit (the PM intern listing is real) but are docked **−8 pts**
since there's no domain signal to confirm fit. Stopgap until those imports are enriched
with tags from YC/BuiltIn discovery or `company_overrides.csv`.

### Tier Assignment

Tier assignment (A/B/C) is based on Account Campaign Score rank. The **Action Queue** sort order
also weights relationship stage — `conversation_started` and `connected_no_conversation`
always surface above higher-scoring companies with zero relationship progress.

Tier A is the top 20 by score. Tier B is the next 40. Rank-based, not threshold-based.

### Large Company Track (1000+ employees)

Large companies follow different campaign logic:
- Map the org hierarchy first — prioritize PM managers, Directors of Product, and
  recruiters over ICs
- Target ~5–7 people closest to the hiring decision (PM managers, Dir of Product,
  recruiters who post PM roles)
- Target ~5 people for referral path (any engineer or PM who can submit internally)
- Multi-touchpoint still valid; direct referral ask is the primary outcome
- Account stage derivation same as startups; next action copy differs

### Open Decisions / Still To Build

- Company enrichment: many `jobs.xlsx` imports still lack tags/description, so their
  campaign action should often be `enrich_company_context`.
- Prestige score: automatic list is still lightweight. Crunchbase/funding/investor tier
  enrichment would make the 0–12 brand component much stronger.
- 2nd-degree density: wired into reachability scoring (≥2 contacts with "2nd degree" in
  notes → +3 pts). Populated automatically by the daily LinkedIn Playwright pipeline.
- `date_posted` recency decay: jobs.xlsx has the field; bridge to account tracker
  not yet built.
- Large company track: team size now parses correctly, but channel/playbook fork logic
  is still not fully implemented.
- Manual priority currently comes from account target-list/notes tags. A structured
  `company_overrides.csv` priority/brand field would be cleaner.

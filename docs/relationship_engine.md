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
- role fit: PM intern, APM, product ops, founder's associate, strategy/operator,
  technical PM, high-fit adjacent internships
- reachability: smaller team, YC/startup, visible founders, product/engineering
  density, USC/Marshall/India/referral paths
- hiring likelihood: active roles, internships, recent jobs, growth/team-size signal
- pitch strength: whether the outreach can be grounded in real work rather than
  forced specificity
- manual priority: companies Akshat explicitly marks as target/core/dream/priority

Tiers:

- Tier A: 15-20 companies actively campaigned
- Tier B: 30-50 strong-fit companies monitored and touched periodically
- Tier C: normal volume apply/outreach

Excel/CSV is the right first visual surface. The account view should include:

- company
- tier
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
   - accepted invites
   - still pending invites
   - replies
   - already connected profiles
   - stale outreach

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

- invite accepted
- invite still pending
- profile already connected
- reply received
- no connect path

This creates the closed loop. Without this, every next step is guessing.

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

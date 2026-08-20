# Reply Engine Design — T0 lane

Status: **settled, ready to sample**
Date: 2026-08-07
Validated against: `artifacts/20260807-014600-linkedin-followup-drafts-backlog.json` (185 drafts, 26 `T0_replies`), `workspace/organizations.csv` (692), `workspace/opportunities.csv` (443)

---

## 1. Why not a case list

The obvious fix is "add more branches." It fails structurally: the case list is unbounded on one axis and tiny on another, and the current design conflates them.

**Situations are unbounded.** "He called me Akshay and said he can't help." "He's a contractor, not FTE." "He left the company." "He mass-blasted me two Product Hunt launches." You will never finish enumerating these.

**Decisions are not.** Across all 26 real threads, five questions determine what happens next:

1. Is a message even the right action? (vs. schedule, vs. create a contact row, vs. nothing)
2. Did they ask me something? — answering precedes any ask
3. What can this person actually give me?
4. Is the thread alive, stalled, or closed?
5. What is the smallest thing they can do in 30 seconds?

"They're legal counsel," "they're `#OpenToWork`," "they left," "they're a non-FTE contractor" are not four branches. They are four values of question 3.

**AI reads and writes; deterministic code decides and checks.** The current engine is inverted — it enumerates situations deterministically (`infer_followup_audience`, seniority branches, campaign flags) and leaves composition loose.

The ~25 cases don't become code. They become the **eval suite**.

---

## 2. What exists and is half-built

| Field | State | Problem |
|---|---|---|
| `reply_intent` | 9 values, on 26/185 | Only runs on replies. `needs_routing_ask` is a 13/26 catch-all. |
| `action_items` | **0/185 populated** | Dead field. 7 named humans in T0, zero extracted. |
| `message_window` | 185/185 | **Not chronologically sorted.** |

Misclassifications in the current enum:

- **Kunal** named Jean Georges Perres → `needs_routing_ask`. **Deepak** and **SHOBHIT**, doing the identical thing → `routed_to_named_contacts`. Same situation, two labels.
- **Harsha**: *"we currently have an opening for Product role"* → `needs_routing_ask`. Highest-value signal in the backlog, flattened.
- **Raymond** asked a direct question → `needs_routing_ask`. Never answered.
- **Thirunaavukkarasu** asked for a resume → `needs_routing_ask`.
- **Austin** mass-blasted two launches → `needs_routing_ask`.

---

## 3. Settled decisions

| Decision | Resolution |
|---|---|
| Resume attach | Human step. Message + paired attach task. Volume is manageable. |
| Pursuit mode | `fall_internship` now → `full_time` later. **Config switch, not hardcoded copy.** Summer is over; `profile.md` is stale and needs updating. |
| CLOSE_WARM | Stop outbound, **keep the mapping**, record reopen condition. Merged with `awaiting_trigger` — same mechanism. |
| Mass blast | **Transact** for decision-makers (upvote + real feedback, ask separately days later), **defer** for everyone else. Emits a human task. |
| Touch limit | **Two by default. Third unlocked only by a real external trigger, never a timer.** |
| Written take | Keep the idea, invert the burden — **bring the take** for top tier, don't ask them to supply a problem. Cut everywhere else. |

---

## 4. Layer 0 — Fix the thread record first

Nothing below works on bad input. All deterministic, no model:

1. **Sort `message_window` chronologically.** `original_invite` rows carry empty `timestamp_text` and splice in arbitrarily. Austin's window reads `11:52 AM → [invite] → Jul 12 → Jul 23`. Sandeep's invite lands *after* the messages replying to it.
2. **Never put telemetry in a message window.** 71/185 have a "latest message" of `invite_result=send_unknown_reserved | detail=Invite worker returned ambiguous status...`. Filter `invite_result=` out entirely; mark the thread `context: none`.
3. **Emit `thread_order_confidence`.** Unreliable → route to hold, not to the composer.

---

## 5. Layer 1 — Thread state (deterministic)

| State | Meaning |
|---|---|
| `no_context` | Accepted, zero real messages. **139 of 185 today.** |
| `they_replied_unanswered` | Ball in our court |
| `you_replied_last` | Ball in their court — do not draft |
| `parked` | No outbound; contact retained; reopen condition recorded |
| `closed_hard` | Do not contact |

`parked` absorbs both "they can't help" and "ping me when a role opens." Same machinery, different reopen conditions.

---

## 6. Layer 2 — Read (AI, structured extraction only)

The model **never writes prose here.** It reads the ordered thread and returns fixed JSON.

```jsonc
{
  "question_asked_of_me":   "string | null",   // Raymond: defense sector experience?
  "named_people":           [{"name","role_hint","why"}],
  "named_opening":          "string | null",   // Harsha: "opening for Product role"
  "explicit_request":       "resume | call | feedback | upvote | intro_material | none",
  "offer_made":             "intro | referral | route_to_recruiter | advice | none",
  "capability":             "can_create | can_refer | can_name | can_opine | cannot_help | no_longer_there",
  "sentiment":              "warm | neutral | dismissive | transactional",
  "is_mass_blast":          true,
  "their_need":             "string | null",   // Hemang: layoff. Manogna: #OpenToWork
  "factual_errors_about_me":["called me Akshay"],
  "commitments_i_made":     ["only send a fit if there's a real match"]
}
```

Eleven fields, covering all 26 threads. They should cover the next 200 without schema change, because they are *questions about the person*, not *descriptions of the scenario*.

`commitments_i_made` matters: in the **Harsh Ranjan** thread you wrote *"if a PM/product opening comes up later, would it be okay if I reached back out with the specific link?"* — he agreed. A fully-specified reopen condition sitting in plain text, currently unextracted, which is why `wait_for_trigger` has nothing to wait on.

---

## 7. The ask ladder

Selected by **recipient authority × company size**, never defaulted.

| Ask | Requires | Costs them | Yields |
|---|---|---|---|
| **CREATE** — name the work, ask for a conversation | `can_create`: founder/CEO/CPO/Head of Product **and** small company | a decision | a role that didn't exist |
| **REFER** — referral to a specific live req | `can_refer` + a **citable** req | 5–10 min, real capital | the most direct path |
| **FORWARD** — "would you pass this to [named person]?" | `can_refer` + a known name | 30 sec | uses a little of their capital |
| **NAME** — "who runs product for X?" | `can_name` | real thought | a pointer |
| **INTEL** — "does this place take product interns? is the fall cycle open?" | `can_opine`: anyone inside | nothing | a real decision input |
| *(none)* | `cannot_help` / `no_longer_there` | — | park |

**NAME was the previous engine's only ask** — used 103 times — and it's a poor default: it costs them thought and yields the least. It is now the fallback, not the opener.

**INTEL is the bottom rung and it matters more than it looks.** People without authority can't route you and shouldn't be asked to. But they reliably know how hiring works where they are. Asking a designer one year out of undergrad how the ranking model surfaces to candidates is not a genuine question — it's a pose with no end goal. Asking her whether Mercor takes product interns is cheap for her and actionable for you.

**CREATE is never framed as "do you have an internship."** At a six-person company that category doesn't exist, and the question invites a no. The structure is: one concrete observation about their product → one line of proof tied to it → ask to work on it. Ask for a *conversation*, not a job. "Easy to try" carries the whole economics argument in three words; explaining the value of an intern to a founder is a status inversion and reads badly. That argument is strong as a **rebuttal on touch two** if they say they're too small — because then they raised it.

*Confirm-a-req-is-live* isn't a rung; it's a precondition of REFER.

Authority resolution uses `organizations.csv` notes (`team_size=`, `batch=`, `organization_type=startup`) plus title. `team_size` is present on only **97/185**, so fall back to org type and the curated briefs.

---

## 8. Opportunity freshness gate (new — this changes priorities)

A req may only be cited in a message if it passes a gate. Today nothing checks this, and the consequences are live in the current pack.

Of **161 product-ish opportunities**:

| Season marker | Count |
|---|---|
| `fall` | **9** |
| `summer` (dead as of Aug 2026) | 27 |
| `2027` | 3 |
| intern, season unspecified | 94 |
| no marker | 28 |

Only 5 rows are marked `expired`. So the engine happily cites dead reqs — and **it already did**: the current Raymond Chan draft links *"2026 Summer Intern — Strategy & Operations (R5065)"* at a man in August, for a summer that is over. Waabi's cited req is `Commercial Product MBA Intern, Summer 2026`. Revolut's is `Internship Programme 2027`.

Three states:

- **`citable`** — season matches `pursuit_mode`, not expired, recently seen → may appear in a message
- **`needs_verification`** — no season marker → emits a check task, **never** goes in a message
- **`stale`** — wrong season or expired → suppressed entirely

**Consequence:** with only 9 citable fall reqs across 692 orgs, REFER will rarely fire. That is exactly why your CREATE ask matters — it is the only ask that doesn't depend on a req existing. It should be the primary play for small companies, not an edge case.

---

## 9. Layer 3 — Decide (deterministic, priority-ordered)

First match wins.

Rule 0 is the deterministic state/cadence gate before this table: closed,
off-channel, already-answered, parked, unreliable capture/order, and accepted-
silent contacts at the outbound touch cap are suppressed or held before any
scenario rule can draft. The default cap is two prior follow-ups. A fired
durable reopen condition permits exactly one third touch; elapsed time does not.

| # | Condition | Action | Message? |
|---|---|---|---|
| 1 | `named_people` non-empty | `CREATE_CONTACTS` + referral provenance | 2-line thanks |
| 2 | `explicit_request = call` | `SCHEDULE` | none |
| 3 | `explicit_request = resume` | `SEND_ATTACHMENT` + attach task | 2 lines |
| 4 | `named_opening` present | `RESOLVE_REQ` → REFER on that req | after resolution |
| 5 | `question_asked_of_me` | `ANSWER` — ask nothing this turn | answer only |
| 6 | `capability ∈ {cannot_help, no_longer_there}` | `PARK` + reopen condition | 1 line, no ask |
| 6b | `acknowledged_standing_ask` | `SUPPRESS` + reopen condition | **none** |
| 7 | `is_mass_blast` | `TRANSACT` (decision-maker) / `SUPPRESS` | never a pitch |
| 8 | `their_need` present | `RECIPROCATE` first | yes |
| 9 | `offer_made ≠ none` | `ACCEPT_OFFER`, one-click | yes |
| 10 | no authority + no context, or parked band | `SUPPRESS` | none |
| 11 | default | `ASK` via the ladder | per ask budget |

Rule 6b was found by running the engine on live data. Midun said *"Absolutely"*, Harsh said *"Yes that will be great"*, Hemang said *"Sure, let me know"* — all three were agreeing to an ask already made. Acknowledging an acknowledgement is noise. The previous engine drafted a fresh pitch at all three.

Ordering does the work. Rule 5 before any ask rule fixes Raymond. Rule 1 producing a **contact row** rather than prose recovers seven warm leads.

Rules 1–4 produce **actions that are not messages**. The current engine has exactly one output type, which is why *"Hold. Deepak routed you to specific people…"* gets rendered in the message slot and then scored `needs_rewrite (60)` against an outbound-copy rubric.

---

## 10. Layer 4 — Write (AI, constrained)

Input: ordered thread, Layer 2 read, the single permitted move, a word budget, and **sentences already used elsewhere in this batch**.

Budgets: `PARK` ≤ 25 · `ASK_SMALLEST` ≤ 40 · `ANSWER` ≤ 60 · `ASK_CREATE` ≤ 70 · rest ≤ 70.
Current pack: mean 73, median 71, max 128.

Facts may only come from: the thread, `organizations.csv` notes (`description=` present on **178/185**), the curated company briefs, and `profile.md`. Nothing invented.

---

## 11. Layer 5 — Check (deterministic critic)

| Check | Catches today |
|---|---|
| Answered `question_asked_of_me`? | Raymond |
| Any sentence appearing >2× in batch? | 103 drafts share one sentence |
| Raw URL > 100 chars? | Hiten (300-char Handshake URL) |
| Cites a non-`citable` req? | **Raymond (dead summer req), Waabi, Revolut** |
| Claims attachment without a paired attach task? | Thirunaavukkarasu, Hemang |
| Asks help from `cannot_help`? | Vincent B, Pratik, Manogna, Hiten |
| Pitches into `is_mass_blast`? | Austin |
| Violates `commitments_i_made`? | Hemang |
| Over word budget? | ~60% of pack |

Fail → regenerate once → hold for human. **Never silently fall back to a template.** Raymond's draft is `AI: fallback` — the model failed validation and the template fired, producing "Doing well, thanks for asking" to a man asking about defense sector experience. The fallback *is* the bug.

---

## 12. Validation against all 26 T0 threads

| # | Contact | Rule | Current engine | Verdict |
|---|---|---|---|---|
| 1 | Kunal / Actian | 1 | 461-char re-pitch, no contact row | ✗ loses a lead |
| 2 | Nagendra / Actian | 2 | suppresses, no scheduling task | ~ |
| 3 | Pranav / Anam | 9 | ignores his offer, asks for a different intro | ✗ fumbles live offer |
| 4 | Kaia / Cloudflare | 11 | job link + referral ask after "Fight On!!!" | ✗ far too fast |
| 5 | Shashwat / ConverzAI | 11 | 3 paragraphs | ✗ length |
| 6 | Ular / HeyGen | 11 | reasonable | ✓ |
| 7 | **Harsha / HireVue** | 4 | generic "who's on product side" | ✗ **best signal discarded** |
| 8 | Hiten / Keck | 6 | 300-char URL + ask to someone who can't help | ✗ worst draft |
| 9 | Makaela / Mercor | 9 | asks a designer "who owns product" | ✗ |
| 10 | SOUHAIL / Micro1 | 6 park | correct hold, no reopen condition | ~ |
| 11 | Midun / Ottimate | 6 park | correct hold, no reopen condition | ~ |
| 12 | Amritansh / Retool | 11 | reasonable | ✓ |
| 13 | Vincent B / Revolut | 6 | asks anyway | ✗ |
| 14 | Pratik / SLAC | 6 | re-asks after an explicit no | ✗ |
| 15 | Vincent Z / iDirect | 6 | asks an intern for a product leader | ✗ |
| 16 | **Austin / Salestrics** | 7 | engages blast *and* pitches | ✗ |
| 17 | Kirk / SentinelOne | 9 | good, slightly long | ✓ tighten |
| 18 | Sandeep / ServiceNow | — | **order unreliable**, assumes referral | ⚠ hold |
| 19 | **Raymond / Shield AI** | 5 | ignores question, cites **dead summer req** | ✗ **total miss** |
| 20 | Manogna / Snorkel | 6 + 8 | asks for intro anyway | ✗ |
| 21 | Hemang / Snyk | 3 + 8 | "attaching my resume", nothing attaches | ✗ breaks own promise |
| 22 | Harsh / Sortly | 6 park | trigger is *in the text*, unextracted | ~ |
| 23 | Thirunaavukkarasu / Turing | 3 | 400 chars asking three other things | ✗ |
| 24 | Deepak / Ventura | 1 | hold note, no rows | ✗ loses 2 leads |
| 25 | SHOBHIT / Ventura | 1 + org update | hold note, no rows | ✗ loses 2 leads |
| 26 | KIRAN / Waabi | 11 | **order inverted**, decent copy | ⚠ luck |

**3 keep · 5 partial · 16 wrong move · 2 blocked on ordering.**

**Rule 1 alone creates 7 warm contacts** — Jean Georges Perres, Ajit Bhave, Shashank Masurkar, Manu Monga, Jay Parab, Sushil, plus Pranav's recruiting lead. All pre-warmed by a mutual connection, all currently plain text nothing reads.

---

## 13. Build order

1. **Layer 0** — sort windows, strip telemetry, order confidence. No model.
2. **Freshness gate** — stop citing dead reqs. No model.
3. **Layer 3 + 5** — decision table and critic against *existing* drafts. No model.
4. **Layer 2** — structured read replaces flat `reply_intent`; populate `action_items`.
5. **Rule 1 pipeline** — named people become contact rows. Highest ROI item here.
6. **Layer 4** — constrained writer with batch-level sentence exclusion.
7. **Reopen watcher** — give `parked` something to wait on.

Steps 1–3 are pure determinism and fix roughly half the observed failures. Only step 6 changes how copy is generated.

---

## 14. Implementation

Built as `src/outreach/reply_engine/`, a standalone package rather than more
surgery on the 18.5k-line `cli.py`.

| Module | Layer |
|---|---|
| `thread.py` | 0 + 1 — ordering, telemetry filter, thread state |
| `context.py` | company facts, authority resolution, ask ladder, freshness gate |
| `extract.py` | 2 — structured read (AI, with a conservative regex fallback) |
| `decide.py` | 3 — the decision table |
| `compose.py` | 4 — constrained writer |
| `critic.py` | 5 — deterministic referee |
| `touches.py` | tracker-backed outbound touch count and cap |
| `pipeline.py` | orchestration + batch-level critic state |

The legacy `cli.py` writer is disabled for every LinkedIn follow-up lane. Its
public builder returns no copy, the recurring run never enables its send
branch, and the old send worker fences any artifact passed to it. Invitation
sending remains a separate, explicitly governed path.

Runner: `scripts/run_reply_engine.py`. Without `--live` the model is never
called, which still exercises ordering, state, the decision table and
collisions — the fastest way to see what the engine decided and why. Copy-level
critic checks require candidate copy, so they run in focused tests and live
composition rather than this no-copy dry run.

```bash
python scripts/run_reply_engine.py --backlog artifacts/<...>-backlog.json
python scripts/run_reply_engine.py --backlog artifacts/<...>-backlog.json --live
```

`tests/test_reply_engine.py` uses contact-named regression tests, each tied to
the draft that went wrong, so a regression says which real failure it reintroduced.

### What running it on live data changed

Four defects only appeared once the engine ran against the real backlog:

1. **Invite touchpoints are re-logged by reconcile passes.** Taking the latest
   one placed the invite after replies it actually preceded. Fixed by taking
   the earliest per contact.
2. **LinkedIn timestamps cannot produce a total order.** Older messages get
   `Jul 9`, today's get `1:16 AM`, and a same-day reply to a timestamped invite
   sorts before it. The scraper already emits threads in order, so raw order is
   now the base signal and the *only* thing repositioned is the undated invite.
3. **"great to connect with you" was read as a referral**, producing a contact
   named "Hey Akshat". Routing verbs now require an object that isn't you/me/us.
4. **"I don't believe we're hiring any product roles" was read as an opening.**
   Negation is now checked per clause.

### Sandeep P. — resolved

With real invite timestamps, his profile link is dated **Jul 9** and the invite
**Jul 29**. He sent that link three weeks *before* we ever contacted him, so it
was never a referral. The previous draft thanked him for one. The thread now
resolves to `you_replied_last` and sends nothing.

### Current decision distribution (185 threads, deterministic read only)

| | count |
|---|---|
| suppressed | 114 |
| would compose | 71 |
| `no_context` | 158 |
| CREATE asks | 9 |
| INTEL asks | 4 |
| NAME asks | 73 |
| contacts to create | 4 |
| human tasks | 3 |

The NAME count stays high under the regex fallback because it can't tell a
Staff PM from a Support Engineer without reading the thread. The AI read
redistributes these toward CREATE and INTEL; the gap between the two runs is
worth watching as a measure of how much the model is actually contributing.

---

## 15. How this runs now

The recurring path is ResumeGenerator's `run_nightly_pipeline.py`, which calls
Outreach's `run-track-2-daily-plan --execute --refresh-linkedin
--no-send-linkedin-followups`. Track 2 refreshes the LinkedIn inbox first and
applies that reconcile to the durable contact and touchpoint ledger. Only after
the fresh state is written does it invoke
`scripts/run_reply_engine_all_lanes.py --live`. That runner combines
accepted-silent contacts, unanswered replies, and usable contacts created from
unmatched threads, but only when the ledger or captured thread proves Akshat
already sent a LinkedIn message. A contact with no prior outbound LinkedIn
message is first outreach, never a follow-up. Those rows are removed before any
model call and written without copy to a separate warm/never-invited manual-review
artifact. The locked
`artifacts/20260814-approved-sends.md` threads are excluded before the model is
called.

The engine orders and classifies every thread, performs the structured read,
chooses one move through the existing decision table, writes constrained copy,
and runs the deterministic critic across the whole combined batch. A failing
draft is regenerated once and then held; it is never replaced with template
copy. The only follow-up copy artifact is
`artifacts/<timestamp>-linkedin-followup-review.md`, grouped CREATE, REFER,
direct replies, NAME, then INTEL. Critic and bad-data holds are marked inline;
large-company contacts at the touch cap are listed separately for 2027
re-entry. The companion
`artifacts/<timestamp>-linkedin-followup-review-warm-never-invited-manual-review.md`
contains identity and relationship context only, never generated copy. Reconcile
and run-health JSON remain operational state, not message review artifacts.

Nothing in this path sends or creates a send queue. The public legacy builder
returns no copy, the legacy send command exits disabled, and the lower legacy
send executor fences every supplied draft. Akshat still reviews and copies each
unheld message, creates any named contact rows listed in the pack, attaches
requested resumes, and resolves bad-data or critic holds. The recurring runner
keeps follow-up sending off even when separately governed invitation sending is
enabled.

The recurring code path is wired, but scheduling is an operating-system concern:
on a machine where the ResumeGenerator nightly LaunchAgent is not installed and
loaded, someone must activate that service before a scheduled execution can
occur. Once active, the next execution follows the reconcile, draft, critic,
hold, and Markdown-artifact sequence above without a separate reply-engine
command.

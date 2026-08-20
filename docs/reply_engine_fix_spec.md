# Reply engine — fix spec

Date: 2026-08-14
Against: `artifacts/20260814-reply-engine-review.json` (29 live threads, `--live` run)
Design context: `docs/reply_engine_design.md`

Every item below is a real failure observed in that run. Each has a symptom, the
evidence, the expected behaviour, and a test name. Work them in order — P0 items
invalidate the others' evidence if left broken.

**Do not regenerate any thread listed in `artifacts/20260814-approved-sends.md`.**
Those are hand-finalised and being sent. Once their touchpoints are logged the next
pull resolves them to `you_replied_last` and the engine suppresses them on its own.

---

# P0-1 — Message capture is dropping conversation

**Symptom.** The pull captures roughly the latest message per thread instead of the
thread. In `artifacts/20260814-unanswered-inbound-backlog.json`, **25 of 29 threads
have exactly one inbound message**; window lengths are `{2: 21, 3: 3, 4: 5}`.

**Evidence it drops real content.** Dhruvi Sonani's captured window is only:

> btw my husband got his MBA at USC Marshall too

Her actual thread also contains:

> Please feel free to pass along any positions that might be a good fit for you!
> would be happy to refer you

An unprompted referral offer — the single most valuable message in the batch — was
never seen by the engine. The resulting draft asks her a generic "who owns product"
question.

**Expected.** The reconcile should capture the full visible thread per conversation,
not the preview line. Where LinkedIn paginates, scroll until the thread start or a
bounded limit is hit, and record how many messages were captured versus expected.

**Also emit** `capture_confidence` on each thread: `full` when the thread start was
reached, `partial` otherwise. `partial` threads with a single inbound message should
route to `HOLD`, not to the composer — the same fail-closed treatment as unreliable
ordering.

**Tests.** `test_partial_capture_holds_instead_of_drafting`,
`test_full_thread_capture_records_all_inbound_messages`

> Until this is fixed, no judgement about draft quality is trustworthy. For 25 of 29
> threads the engine is writing against one line of a conversation.

---

# P0-2 — Wrong-company org rows

**Symptom.** Org enrichment matches on name string and collides on common names. Two
confirmed, so treat it as systemic rather than incidental.

**`org-shield-ai`** — description is *"a real-time data protection and compliance
platform that uses Generative AI to safeguard sensitive information."* Raymond Chan is
a Staff Technical PM at the **defence autonomy** company and asked specifically about
defence-sector experience. The engine faithfully used the workbook and produced a draft
telling an Army veteran you're drawn to their data-compliance work.

**`org-ventura`** — description is *"Ventura builds the AI workforce for distributors
and manufacturers"*, `team_size=2`, `batch=W2026`, San Francisco. All ten contacts are
**Ventura Securities Ltd**, an Indian brokerage — three say "Ventura Securities" in
their titles, and the roster is Flutter developers and an ex-Jio engineer.

**Expected.**
1. Fix both rows.
2. Add a `scripts/audit_org_identity.py` sweep flagging orgs where contact signals
   contradict the description — contact locations clustering in a different country
   from `location=`, or titles naming a different legal entity than `name`.
3. Emit a report; do not auto-fix.

**Tests.** `test_contact_titles_naming_a_different_entity_are_flagged`,
`test_contact_geography_conflicting_with_org_location_is_flagged`

---

# P0-3 — 19 live threads have no workbook contact

**Symptom.** `artifacts/20260814-114545-linkedin-message-reconcile.json` reports
`"missing_contact": 19` across 201 threads. Those conversations never reach the reply
engine at all — they aren't suppressed, they're invisible.

Some are genuinely noise (sponsored InMail, a paid research study, a Product Hunt
blast). Several are not:

| Thread | Latest message |
|---|---|
| **Suresh Mergu** | *"I know there is an internship program that Optum hires for every year. Given you…"* |
| Andre de la Cruz | *"Akshat - thanks for connecting! The product teams we've worked with lately have…"* |
| Aaron Allen | *"Application for PM Intern role"* |
| Ravi Kant Jha | *"Yes I did"* |
| Chandni Mittal | *"Yes, for sure!"* |

Suresh Mergu is the one to look at first: an internship-program lead at **Optum**,
where Akshat previously worked, sitting unread and unrouted.

**Expected.**
1. Emit `artifacts/<stamp>-unmatched-threads.md` listing every `missing_contact` thread
   with its latest message, so none of them can sit invisible again.
2. Classify each as `noise` (sponsored / InMail / broadcast / recruiter spam) or
   `needs_contact_row`.
3. For `needs_contact_row`, create the contact and organization from the thread so the
   next run picks them up normally. Do not draft for them in the same pass — they have
   no invite history and no org context yet.

**Tests.** `test_unmatched_threads_are_reported_not_dropped`,
`test_sponsored_and_inmail_threads_classify_as_noise`

---

# P1-3 — `req_actionability` on opportunities

**Why.** Akshat cannot take a full-time role until he graduates. Requisition freshness
currently answers "is this real" but nothing answers "what can I do about it", so the
engine treats every open PM req as equally useful. It is not.

Add a second field alongside `requisition_state`:

| Value | When | What it means for the message |
|---|---|---|
| `apply_now` | fall internship / co-op, or a 2027 new-grad or MBA req | Apply directly. Outreach is a referral ask on that req. |
| `create_wedge` | active **full-time** PM req at a **small** company (`CompanyFacts.is_small`) | Proof of unmet demand plus an existing budget line. Strengthens the CREATE ask — the req becomes the observation: more product work than people. |
| `pipeline_signal` | active **full-time** PM req at a **large** company | An approved headcount slot at a set level. It cannot become an intern seat, and asking signals you don't understand their org. Use it as evidence the team is growing: ask whether an intern or co-op path exists and when the 2027 cycle opens. |
| `not_actionable` | wrong function, wrong level, expired, or `stale` per the freshness gate | Suppressed entirely. |

This is the same authority × company-size axis already in `context.py`, applied to the
requisition instead of the person.

**Downstream rule.** The intern-conversion argument ("cheaper, fewer hours, convert
later") is only ever permitted at `create_wedge`, and only as a **rebuttal** after they
raise the objection — never as an opener, never at a large company. Add it to
`ASK_GUIDANCE` as a second-touch note, not first-touch copy.

**Tests.** `test_fulltime_req_at_small_company_is_create_wedge`,
`test_fulltime_req_at_large_company_is_pipeline_signal`,
`test_fall_internship_req_is_apply_now`,
`test_intern_economics_never_appears_at_large_company`

---

# P1-4 — `declined_referral` is not `cannot_help`

**Symptom.** Colin Williams (Doximity, Product Manager) said he cannot submit referrals
for people he hasn't worked with — then gave detailed, engaged help: an Exponent
recommendation, a case-study heads-up, SQL test notes. The AI read returned
`can_opine`, his title resolved to `can_refer`, rule 9 fired, and the draft asked him
"who runs product for physician workflows."

Two separate problems:

**(a) The refusal wasn't detected.** `_CANNOT_HELP` in `extract.py` covers "can't help"
but not "not able to submit referrals". `validate_ai_read` only ever *downgrades*
`cannot_help` → `can_opine`; it never upgrades. Deterministic refusal patterns should
be a **floor the model cannot override**, matching referral-specific phrasing:
"not able to refer", "can't refer", "only refer people I've worked with", "don't
submit referrals".

**(b) The taxonomy is too coarse.** Declining a *referral* is not declining to *help*.
Add `Capability.DECLINED_REFERRAL`, ranked between `can_name` and `cannot_help`:
`select_ask` returns `NAME`, never `REFER` or `FORWARD`. The critic's
`asks_help_from_cannot_help` should not fire on it — asking for a name is a different
ask from asking for a referral.

**Tests.** `test_referral_refusal_phrasing_is_detected`,
`test_deterministic_refusal_overrides_optimistic_ai_read`,
`test_declined_referral_gets_name_ask_not_refer`,
`test_declined_referral_is_not_flagged_for_asking_a_name`

---

# P1-5 — ACCEPT_OFFER asks a different favour

**Symptom.** Two cases, both slipping past the critic because each has only one
question mark.

**Pranav Shikarpur** offered: *"happy to shoot your linkedin over to the recruiting
lead."* The draft accepts, then asks *"who owns product for the interview experience
side of things?"*

**SOUHAIL KHEZZANE** said to go to the recruiting team. The draft asks *"who runs
product for the contractor-management side?"*

`ACTION_GUIDANCE[ACCEPT_OFFER]` already says "do not ask for a different favour than
the one offered" — nothing enforces it.

**Expected.** New critic check `asks_beyond_the_offer`: when
`decision.action is ACCEPT_OFFER` and `read.offer_made` is a routing-type offer
(`intro`, `referral`, `route_to_recruiter`), fail if the body contains a *new* ask
pattern — `who (owns|runs|leads)`, `point me to`, `intro to` — targeting something
other than what was offered.

**Tests.** `test_accept_offer_cannot_bolt_on_a_second_ask`,
`test_accept_offer_accepting_only_passes`

---

# P1-6 — ANSWER guidance produces background dumps

**Symptom.** Erin Overland (Workday) asked *"are you still looking at workday roles?"* —
a yes/no. The draft answered in three words and spent the rest re-pitching background
she already had from the intro, then closed with the templated "does that background
feel useful?"

The current guidance hardcodes "close by handing the judgement back: ask whether that
background is materially useful there." That is correct for **Raymond**, because he
asked about background. It is wrong for anyone who didn't.

**Expected.** The follow-on must be derived from what they asked, not templated:

- If they asked about your **background or fit** → answer, then ask whether it
  translates on their side.
- If they asked about your **interest, availability or intent** → answer, then state
  the constraint they need in order to help (timing, role shape), then one ask they can
  actually action.

Never re-state biography they already have from the invite.

**Tests.** `test_answer_to_interest_question_states_timing_not_biography`,
`test_answer_to_background_question_still_hands_judgement_back`

---

# P1-7 — Word budget holds on trivial overage

**Symptom.** Chirag Jain held at `over_budget:46>45`. One word.

**Expected.** Allow a 10% tolerance before failing, or on the regeneration pass instruct
a trim rather than a rewrite. A one-word overage should never block a good draft.

**Test.** `test_marginal_overage_is_trimmed_not_held`

---

# P1-8 — Wire the resume into the writer

**Why.** `compose.py` `_SYSTEM` carries a two-line generic bio. The resume at
`Profile/resume_2026-04-03_r8.7.docx` contains far stronger material: a billing failure
caught across 1,500+ businesses, 50,000 accounts with invisible errors reconciled,
restored billing for 80K+ Intuit businesses, reliability tradeoffs on 120K+ pipelines at
Hevo. None of it reaches the model.

**The specific risk this creates.** The Erin draft said "Built billing systems at
Intuit." That is *true* — and the model could not have known it. It inferred from
Intuit's brand and got lucky. Next time it produces "tax filing software at Intuit"
with identical confidence and nothing catches it, because the existing grounding checks
only validate claims about the **company**. Claims about **Akshat** are ungoverned.

**Expected.**
1. Extract a bounded set of verified proof beats from the resume into
   `workspace/proof_beats.yml` — one line each, tagged by domain (data infra,
   reliability, billing/fintech, marketplace/logistics, AI product).
2. Pass the two or three beats matching the recipient's domain into the compose prompt
   as `USABLE PROOF`.
3. Add critic check `unsourced_self_claim`: fail if the body names an employer alongside
   a capability term not present in either the proof beats or `profile.md`.

**Tests.** `test_proof_beats_load_from_resume`,
`test_unsourced_employer_claim_is_flagged`,
`test_claim_matching_a_proof_beat_passes`

---

# P1-9 — `reopen_condition` is written and read by nothing

**Symptom.** `PARK` and rule 6b record a reopen condition, the runner prints it into the
artifact, and no code anywhere consumes it. Seven threads are parked right now with
conditions that will never fire:

| Contact | Reopen when |
|---|---|
| Hemang Sarkar | only send a fit if there's a real match |
| Harsh Ranjan | keep an eye on Sortly roles |
| Midun Raju C | concrete Ottimate role |
| Shashwat Das, Makaela Jarnagin, Kaia Wing-Alpert, KIRAN V. | concrete role at their company |

Parking without a watcher is just forgetting politely. Harsh's case is the sharpest:
you explicitly promised to come back with a specific link if a PM role opened, and he
agreed. Nothing will ever check.

**Expected.** A `scripts/check_reopen_conditions.py` pass that runs after the
opportunity refresh:
1. Load parked contacts and their reopen conditions.
2. For conditions naming a company, check whether that org now has an opportunity with
   `req_actionability` of `apply_now` or `create_wedge` (see P1-3).
3. Emit `artifacts/<stamp>-reopen-candidates.md`. **Do not auto-draft** — surface the
   trigger and let the next reply-engine run handle it once unparked.

Store the condition durably on the contact record, not only in the run artifact.

**Tests.** `test_parked_contact_reopens_when_matching_req_appears`,
`test_parked_contact_stays_parked_without_a_trigger`,
`test_reopen_condition_persists_on_the_contact_record`

---

# P1-10 — "Let me know how I can help" is an offer

**Symptom.** Kirk Hanson (Sr. Director, SentinelOne) wrote exactly that. The AI read
returned `offer_made: "none"`, so rule 9 never fired and the thread fell through to rule
11 as a cold ask. Outcome was similar here, so this is low severity — but an explicit
open offer from a Senior Director is the highest-value thing in a thread and the read
should not miss it.

**Expected.** Add open-ended offer phrasing to both the extraction prompt's `offer_made`
guidance and the deterministic `_OFFER` pattern: "let me know how I can help", "happy to
help", "anything I can do", "let me know if you need anything".

**Test.** `test_open_ended_offer_is_read_as_an_offer`

---

# P1-11 — Two live threads at one company still need coordinating

**Symptom.** Pulkit Kumar and Ramashish Pandey are both at ConverzAI, both replied, and
both got a message in the same batch. The collision policy exempts live threads from
suppression — correct, since answering two people who wrote to you is normal — but it
does nothing about *what* they're asked. Both were routed toward the same product ask.

**Expected.** Collision policy should still coordinate the **ask** across live threads
at one company, even though it suppresses neither:

- One live thread per company carries the substantive ask (`NAME`, `REFER`, `FORWARD`,
  `CREATE`). Pick by capability rank, then by engagement — someone who volunteered help
  outranks someone who only said hello.
- Other live threads at that company are downgraded to `INTEL` or a pure acknowledgement.

Ramashish volunteered to keep an eye out; Pulkit said "Hey, Akshat". Ramashish should
carry the ask.

**Tests.** `test_one_live_thread_per_company_carries_the_ask`,
`test_other_live_threads_downgrade_to_intel`

---

# P1-12 — `profile.md` is stale

`Profile/profile.md` line 16 still reads:

> **Internship target:** Summer 2026 (May–August). Full-time availability.

Summer 2026 has passed. Current target is a **fall internship**, transitioning to
full-time later. This file feeds the fit scorer and the note generator as well as the
reply engine, so it is wrong in more places than this one.

Update the target line and confirm nothing else in the file assumes a summer cycle.
Group with P1-8 — same surface, "what the system believes about Akshat."

---

# Verification

```bash
PYTHONPATH=src python -m pytest tests/test_reply_engine.py -q
python scripts/run_reply_engine.py --backlog artifacts/<latest>-backlog.json
```

Run the dry run (no `--live`) first — it exercises capture, ordering, state, the
decision table, collisions and the critic without spending a token.

**What good looks like on the next live run:**

- `capture_confidence: full` on the large majority of threads
- zero threads where a referral offer or named opening appears in the thread but not in
  the read
- NAME asks down as a share of total, CREATE and INTEL up — the gap between the dry run
  and the live run is the measure of what the model is contributing
- no draft citing a company fact from an org row flagged by the identity sweep

---

# Round 2 — from the live sends, 2026-08-15/16

Found by sending the approved pack by hand and watching the replies. All five are
real failures with a named thread behind them.

---

## P2-1 — Duplicate contact names bind to the wrong row

**Symptom.** There are two `Chirag Jain` rows: `org-idler` ("swe @ idler (yc s25)")
and `org-d-matrix` ("Software Engineer"). The live thread belongs to the d-Matrix
Chirag; the engine bound it to Idler and drafted a message about reinforcement
learning environments to someone who works on inference compute.

**Expected.** Thread-to-contact matching must key on the LinkedIn profile URL, never
on display name alone. When a name matches more than one contact and no URL is
available, emit `ambiguous_contact_match` and route to `HOLD` rather than guessing.

Sweep the workbook for other duplicate `full_name` values and report them.

**Tests.** `test_duplicate_name_without_profile_url_holds`,
`test_profile_url_binds_to_the_correct_duplicate_contact`

---

## P2-2 — Org identity audit is mostly false positives

**Symptom.** 95 findings across 62 organizations is unreviewable, and the signal is
buried. Actual output includes:

- **Real:** `org-clara` has 8 findings — contacts titled "Senior Software Engineer
  @Intuit" and "SDE IV at Hevo Data". They do not work at Clara.
- **Real:** `org-mount` — "AI Product Manager @ Iron Mountain" matched on "Mount".
- **Noise:** also `org-mount` — a contact flagged for naming "Scale", because his
  headline reads "Automation at Scale".

**Expected.**
1. Suppress a match when the other company follows `ex-`, `prev`, `previously`,
   `formerly`, `alum`, or `ex ` in the title — those are past employers, not evidence
   of a mis-filed contact.
2. Suppress single generic tokens (`scale`, `mount`, `clara`, `apply`, `notion`) unless
   they appear adjacent to `@` or `at `.
3. Rank the report by **findings per organization**, not alphabetically. Concentration
   is the signal: one contact naming another company is noise, five is a collision.
4. Split the report into `likely_collision` (2+ findings) and `low_signal` (1 finding).

Target: a human reviews roughly a dozen rows, not ninety-five.

**Tests.** `test_ex_employer_in_title_is_not_a_collision`,
`test_generic_token_without_at_marker_is_not_a_collision`,
`test_report_ranks_by_findings_per_org`

---

## P2-3 — The availability qualifier is mandatory, not optional

**Symptom.** Repeatedly dropped from drafts because it competes for word budget.
Dhruvi Sonani offered to refer Akshat to "any positions that might be a good fit" —
without the qualifier she would search full-time listings he cannot accept, and the
referral is wasted.

**Expected.** Whenever the recipient may act on Akshat's behalf — `REFER`, `FORWARD`,
`ACCEPT_OFFER` on a routing offer, `CREATE_CONTACTS`, or any `RESOLVE_REQ` — the
message MUST state the availability constraint from `pursuit_mode`: currently a fall
internship or co-op, not full-time, because he is mid-MBA.

Treat it as a required field, not as copy. Add critic check
`missing_availability_qualifier` on those actions. It is *not* required for `PARK`,
`TRANSACT`, or `INTEL`.

**Tests.** `test_referral_message_must_state_availability`,
`test_park_message_does_not_require_availability`

---

## P2-4 — INTEL must be one question, answerable from their seat

**Symptom.** Harsha Singla (Senior SDET, HireVue) received: *"Does HireVue run a
product internship or co-op track, and do you know who'd own that?"* She replied
"I'll let you know if I see anything" — the polite exit.

Two questions, and **neither was answerable from her role.** An SDET does not know the
internship programme structure and definitely does not know who owns it.

The current critic explicitly exempts `INTEL` from `multiple_asks` on the theory that
two cheap questions cost nothing. That was wrong: cheap for *us* to ask is not cheap
for *them* to answer.

**Expected.** Remove the INTEL exemption — one question, same as everything else. Add
guidance that the question must be answerable from the recipient's own vantage point:
existence and timing of a programme is fair game for anyone inside; ownership and org
structure is not, unless they are senior.

**Tests.** `test_intel_is_limited_to_one_question`,
`test_intel_does_not_ask_a_junior_contact_about_org_structure`

---

## P2-5 — NAME should ask for the right person, not an org-chart slot

**Symptom.** Kirk Hanson (Sr. Director, SentinelOne) was going to be asked *"who owns
product for data protection or cloud security?"* — which guesses at an area Akshat does
not actually target, and asks Kirk to map an org chart.

**Expected.** Ask for **the right person for the goal**, and let the insider do the
mapping. They know the org; we do not. "Who should I be talking to about fall product
internships at X?" beats "who owns product for Y?" on every axis: it cannot pick the
wrong area, it is answerable in one line, and it routes to whoever actually matters —
which may be a recruiter rather than a product lead.

Update `ASK_GUIDANCE[Ask.NAME]` accordingly and stop deriving the area from the company
description.

**Tests.** `test_name_ask_targets_the_goal_not_an_org_position`,
`test_name_ask_does_not_invent_a_focus_area_from_the_company_description`

---

## P2-6 — No touch counting before the accepted-silent lane runs

**Symptom.** `docs/reply_engine_design.md` states the cadence policy — two touches by
default, a third only when a real external trigger fires, never on a timer. **Nothing
implements it.** There is no touch counter anywhere in `reply_engine/`.

This has been harmless so far because every live run has been on the reply lane, where
the recipient's own message drives the decision. It stops being harmless the moment the
engine runs against accepted-but-silent contacts, where rule 11 will draft an ask
without knowing whether two already went out.

**Scale of the exposure.** The workbook holds ~1,226 contacts with an invite touchpoint
and only 59 with a logged reply. Every live run to date has covered 29 threads. The
accepted-silent lane is the large one and it is the lane the original engine damaged
with a single sentence repeated 103 times.

**Expected.**
1. Count prior outbound follow-up touchpoints per contact.
2. `decide` suppresses when the count is at or above the cap: 2 by default.
3. A third touch unlocks only when a reopen condition has fired (P1-9) — never from
   elapsed time alone.
4. Surface `touch_count` and `touch_cap_reached` on the draft so the artifact shows why
   something was suppressed.

**Also confirm** the legacy follow-up path in `cli.py` — the one that produced the
103-use sentence — can no longer run against these contacts. Either route it through the
reply engine or fence it off. Two engines writing to the same lane is worse than either.

**Tests.** `test_contact_at_touch_cap_is_suppressed`,
`test_third_touch_requires_a_fired_reopen_condition`,
`test_first_touch_on_a_silent_accept_is_allowed`

---

## P2-7 — Outbound that reads as a response implies an unlogged inbound

**Symptom.** Agent A noticed manual outbound like *"Thanks anyways"* on contacts the
ledger still classifies as accepted-silent. Verified: **5 confirmed cases** —

| Contact | Our logged outbound |
|---|---|
| Bhavin Gajjar | "Thanks anyways!" |
| Ankit Sheoran | "Oh okay, thanks anyway!" |
| Ankit Chaurasia | "Thanks anyways" |
| Kanhaya Yadav | "Thank you so much Kanhaya!!😃" |
| Bratee Podder | "Thanks so much Bratee for making the effort to share all this!" |

You do not write "thanks so much for making the effort to share all this" to somebody
who has said nothing. These people engaged; the inbound was lost to the pre-P0-1
capture bug and the reconcile has not recovered it — their threads fall outside the
pull window.

The consequence is worse than a miscount: the silent lane would send a cold "who runs
product" ask to somebody who already helped.

**Expected.**
1. Deterministic check — an outbound whose text is purely responsive ("thanks anyway",
   "no worries", "thanks so much for", "appreciate you sending") with **no logged
   inbound before it** marks the contact `inbound_probably_missing`.
2. Exclude those contacts from the accepted-silent backlog.
3. Emit them as a targeted re-pull list; their threads need a deeper scroll than the
   standard window.

Note the pattern must not fire on opening messages — "Hi X! How are you doing?" and
"Hi X, I'm a 1Y MBA at USC" both contain courtesy words and are cold openers, not
responses. Match on responsive intent, not politeness vocabulary.

**Tests.** `test_thanks_anyway_without_inbound_flags_missing_inbound`,
`test_cold_opener_with_courtesy_words_is_not_flagged`

---

## P2-8 — NAME is the fallthrough default in the silent lane

**Symptom.** The accepted-silent dry run produced **77 would-draft, of which 63 are
NAME asks (82%)**. Agent B's authority audit split them: **31 plausible, 32 individual
contributors with no visible authority.**

NAME was supposed to be the fallback rung, not the default. It is the default again
because `resolve_capability` falls through to `CAN_NAME` for anyone who is not clearly
founder, senior, or junior — which is most people — and in the silent lane there is no
conversation to correct it.

The result is the original failure mode in new clothing: one ask, sent to nearly
everyone, asking the recipient to do org-chart research they may not be able to do.

**Judgement to encode.** Asking "who runs product?" of somebody who accepted an invite
and then said nothing for weeks is the worst available trade — highest cost to them,
lowest yield to us, zero engagement signal to justify it. In `NO_CONTEXT` state a
NAME ask should require positive evidence of authority, not the absence of evidence
against it.

**Expected.**
1. In `NO_CONTEXT`, `CAN_NAME` must be *earned* — seniority markers, a product/ops
   function, or a small company where anyone plausibly knows the org. Absent those,
   resolve to `CAN_OPINE`, which yields `INTEL`.
2. Keep the fallthrough as-is for `THEY_REPLIED_UNANSWERED`: a reply is itself the
   engagement signal that makes a NAME ask reasonable.
3. Expected effect on the current backlog: NAME drops from 63 to roughly 31, INTEL
   rises from 2 to roughly 34. Report the actual split after the change.

**Tests.** `test_silent_ic_with_no_authority_gets_intel_not_name`,
`test_silent_senior_contact_still_gets_name`,
`test_replied_ic_can_still_get_name`

---

## P2-9 — Resolve org membership per contact, not per organisation

**Symptom.** The P2-2 audit asks a human to judge 17 organisations by reading contact
lists. That is the wrong unit of work and mostly not a judgement call. Measured across
the 266 contacts at those 17 orgs:

| Bucket | Count |
|---|---|
| Title names **this** employer | 97 |
| Title names a **different** employer | 45 |
| Title names **no** employer | 123 |
| No title at all | 1 |

**142 of 266 are decidable from the workbook alone.** Asking a human to read them is
waste. Only the 124 with no employer in the title are genuinely ambiguous — and
**263 of 266 have a LinkedIn URL**, so a read-only profile pull answers most of those
too.

**Reframe.** The unit is the contact, not the organisation: *does this person currently
work here?* The org-level verdict then falls out — if most contacts at an org work
elsewhere, the row is a scrape artifact rather than a company.

**Expected.**

1. **Auto-classify from titles.**
   - `confirmed_here` — title names this employer. Leave.
   - `works_elsewhere` — title names a current affiliation but not this organization.
     This is evidence, not an automatic mutation. `organization_id` also routes warm
     referral paths: contacts tagged `referrals`, or contacts with no sent
     `linkedin_invite`, remain mapped and eligible. If a sent invite treated someone
     as an employee of the target company, emit an explicit conflict flag; never
     detach or reassign silently. Confirmed name collisions such as Mount/Iron
     Mountain stay visible for reviewed reassignment. Skip `ex-`/`prev`/`former`
     mentions and education (`USC`, `CMU`, …), which are not current employment.
   - `unknown` — no employer named. Goes to step 2.

2. **Resolve unknown membership lazily at the live boundary.** First run the
   deterministic accepted-silent lane. A profile read is eligible only when that
   contact is in the resulting would-draft set; suppressed contacts stay deferred
   until they become live. For this round that is five contacts: one each at Yondu,
   Mercor, Micro1, Snorkel AI and Invisible Technologies. Add only a three-contact
   diagnostic sample at Clara, Yondu and Anthropic to distinguish real company rows
   from discovery artifacts. Reads remain read-only and one visit per selected
   contact. Cuauhtli Padilla is the motivating stale-membership case — title reads
   "Founder", he was at Clara, he has since left, and nothing in the workbook can
   know that. This is the same just-in-time shape as the requisition freshness gate:
   identity evidence is acquired when it can affect an imminent draft, not eagerly
   across dormant workbook rows.

3. **Flag garbage rows.** `contacts.csv` contains a row whose `full_name` is
   **"Building Clara."** Detect non-person names — trailing punctuation, verb-initial
   phrases, company names in the name field — and report them.

4. **Emit a short human queue.** Only selected live/sample contacts still unresolved
   after steps 1–3, garbage rows, plus any sampled org whose evidence remains
   inconclusive. Keep all other unknowns in a deferred appendix, not the human queue.
   Target: under 20 rows.

**Tests.** `test_sidharth_other_affiliation_detaches_without_reassigning`,
`test_ex_employer_mention_does_not_reassign`,
`test_education_mention_is_not_an_employer`,
`test_non_person_name_is_flagged`

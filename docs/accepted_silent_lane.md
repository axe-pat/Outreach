# Accepted-silent lane — prep brief

Status: data prep only, no engine changes
Date: 2026-08-16
Owner: separate agent, runs in parallel with `docs/reply_engine_fix_spec.md` Round 2

---

## What this lane is

Contacts who accepted the LinkedIn invite and **never said anything**. Distinct from
the reply lane, which is everything worked so far.

| | count |
|---|---|
| Connected, never replied | **268** |
| — with zero follow-ups sent | 245 |
| — with one follow-up sent | 22 |
| — at the two-touch cap | 1 |

For scale: every live run to date has covered **29** threads. This lane is roughly
nine times larger, and it is the lane the original engine damaged — one sentence
appeared in 103 of 185 drafts, all of them here.

---

## Hard boundary

**Do not modify anything under `src/outreach/reply_engine/`.** Another agent is
working `docs/reply_engine_fix_spec.md` Round 2 in those files, including touch
counting (P2-6). Two implementations of the same gate is worse than none.

This brief is **selection, verification and segmentation only**. New standalone
scripts and artifacts are fine. Engine edits are not.

---

## Task 0 — Reconcile first

Everything below depends on this. Roughly twenty replies were sent by hand on
2026-08-15/16 and none are in the ledger, so "connected and never replied" is currently
wrong — some of those contacts have now been answered, and the reply engine would
re-draft them.

Run a full reconcile and apply it. This picks up:
- the hand-sent replies, which become `you_replied_last` and self-suppress
- any new inbound since the last pull
- connection-state drift on the 268

Two constraints:

1. **Only one agent uses the Chrome/LinkedIn session at a time.** Confirm the other
   agent is not running anything live before you start.
2. **Confirm the repo is clean and tests pass first.** The other agent shipped the
   full-thread capture fix (P0-1) and is still editing; you want their finished capture
   code, not a half-edited state.

Report what changed: touchpoints added, statuses moved, threads newly visible.

---

## Task 1 — Build the backlog

`scripts/build_accepted_silent_backlog.py`, emitting an artifact in the same shape
`scripts/run_reply_engine.py --backlog` already consumes.

Selection: contact is connected, has no logged reply, is not `Do not contact`, and is
not already parked with an unmet reopen condition.

Include per row: contact, org, title, band, opportunities, **the original invite note**,
invite date, and count of follow-ups already sent.

---

## Task 2 — How many will the engine actually draft for?

This is the question that shapes everything else, and nobody has answered it.

Of 268, a large share will be suppressed before composition — no authority plus no
context (rule 10), parked bands, or touch cap. Run the existing dry run over the
backlog and report:

- suppressed vs would-draft, with the rule that fired
- would-draft split by ask: CREATE / REFER / FORWARD / NAME / INTEL
- how many CREATE candidates — founders and execs at small companies, the
  highest-value segment and probably the place to start
- concentration by company, since collision policy will thin those

If the real drafting population is 60 rather than 268, the plan changes.

---

## Task 3 — Audit the invite notes *(the one unique to this lane)*

On the reply lane the recipient's own message supplies context. Here **the only
context is what Akshat's invite said** — so the follow-up has to build on it without
repeating it, and it has to be true.

Neither is currently guaranteed:

- Invite notes are themselves templated. The 2026-08-07 pack is full of
  *"I've been interested in X, especially its work in Y"*.
- **At least one is factually wrong.** Both Surge AI contacts received *"especially its
  work in robotics."* Surge AI is RLHF and data labelling. There is no robotics. Katie
  Makarska and Matt Anger both read that.

Deliver:
1. Frequency table of invite-note templates across the 268 — which phrasings repeat and
   how often.
2. **Every invite note whose claimed focus area is not supported by the org
   description.** Same class of error as Surge AI. This is the priority output.
3. Flag contacts whose invite is wrong: a follow-up cannot build on a false premise, and
   the right move there may be a correction rather than an ask.

---

## Task 4 — Verify they are still connected

Workbook state is stale. Some of the 268 may have withdrawn, changed jobs, or already
been messaged outside the system. Read-only reconcile against LinkedIn; report drift,
change nothing.

---

## Not in scope

- Touch counting — agent A, P2-6
- Any composition or sending
- Org identity fixes — agent A, P2-2, though flag anything you hit

---

## When this lane runs live

**Twenty first, not 268.** Start with the CREATE segment: founders and execs at small
companies, where the ask is strongest and the population is small enough to read every
draft by hand. Everything learned there applies to the rest.

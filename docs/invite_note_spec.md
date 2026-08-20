# Invite notes — what makes one work

Date: 2026-08-18
Derived from rewriting `artifacts/20260818-091125-fall-sprint-invite-notes-preview.md` into `artifacts/20260818-invite-notes-rewritten.md`.

This is a document about *why*, not a checklist. The bugs are at the end because they matter less.

---

## The one thing

**Before:** "Hi Hassaan, I've been interested in Tavus, especially its work in recruiting workflows. I've worked on AI recruiting products after several years in engineering and have been deep in product for a while now. I'm looking closely at PM/product roles there. Would love to connect."

**After:** "Hi Hassaan, I've spent the last several months building on Tavus. I'm PM on an AI video-interview product at FlairX and I ran the integration, so I've pushed CVI through a lot of ugly edge cases and come out a fan. I'm mid-MBA at USC and looking for a fall product role. Would love to connect!"

The first **asserts interest**. The second **demonstrates exposure**.

"I've been interested in X" is a claim about an internal state. It is costless, unverifiable, and true of everyone who sends an invite — which makes it worth nothing. "I've spent months building on X" is a claim about something that happened. It can be checked, and almost nobody else can say it.

Everything below is a consequence of that.

---

## The four consequences

### 1. Evidence of contact beats declaration of interest

Strongest to weakest, all better than "I've been interested in you":

1. I used your product in production and hit its edges — *Tavus, Anam*
2. I competed against you — *Airbyte, via Hevo*
3. I studied you closely for work — *Apriora, Ribbon, ConverzAI*
4. I built a worse version of this myself — *Jobright*
5. I read your docs / went through the product / followed a specific launch — **available for any company, requires ten minutes**

Tier 5 is the important row. The pattern does not depend on having a relationship. Where no relationship exists, contact is still manufacturable by actually looking at the product.

### 2. Specificity costs no extra characters

| Generic | Specific |
|---|---|
| "AI recruiting products" | "an AI video-interview product at FlairX" |
| "several years in engineering" | "five years at Hevo Data on pipeline reliability" |
| "its work in recruiting workflows" | "latency and turn-taking in real interviews" |

The rewritten pack came in *shorter* than the originals — 245–299 characters against 215–297 — while carrying far more content. Vague writing is long because it needs many words to say nothing. This is why the length flag and the quality problem were the same problem.

### 3. Close the inference loop

The correction that produced the biggest jump in quality. A fact is not an argument.

> "We run avatar generation on Tavus. I ran the vendor evaluation that got us there."

True, and it leaves the recipient to work out why they should care. State the implication:

> "…so I've pushed CVI through a lot of ugly edge cases and come out a fan."

**Every fact in the note must be followed by what it means for them.** If a sentence could end with "so what?", it isn't finished.

Related: name a specific surface you'd want to work on. Generic enthusiasm is unanswerable; a named surface is a conversation. Matching quality (Turing), signal quality (Micro1), the marketplace side (Mercor), latency and turn-taking (Tavus).

### 4. Compliments must cost the sender something

Three failures and the fix:

| Reject | Why |
|---|---|
| "Rated it highly enough to push for it internally and we came close to signing" | Positions Akshat as benefactor. Implies a debt. Not the point. |
| "Voice-first for staffing is a genuinely different bet from ours" | Categorises them. Costs nothing, says nothing. |
| "The emotional feedback loop you're building is the real insight" | Grades a founder's judgement from outside, uninvited. |

| Accept | Why |
|---|---|
| "Ribbon's voice-first bet was the one that made me rethink our own approach" | Concedes they changed his mind. Costs something, so it reads as true. |
| "Staffing is the one segment where voice genuinely beats video" | An opinion about their market that shows he understands why their bet is right. |
| "Got properly into the weeds with it and loved what you've built" | First-person testimony from a user. Not a grade. |

The distinction that matters: **first-person testimony is not third-person grading.** "I used it and loved it" is something only he can say. "That's the real insight" is a verdict he has no standing to deliver.

---

## The test

**Swap the company name. Does the note still read fine?**

If yes, it is generic and it fails. The original Tavus note was *verbatim identical* to the Micro1, ConverzAI and Anam notes — the same paragraph sent to four companies with a name changed. None of the rewrites survive the swap.

This is checkable in code: compare each note against the rest of the batch after stripping the greeting and the close. High overlap on the remaining content means nothing company-specific was said.

---

## Close

Fold availability into the ask so the MBA is the *reason* for it, not a fact set beside it.

- "I'm mid-MBA at USC and looking for a fall product role. Would love to connect!"
- Tight variant: "Mid-MBA at USC, looking for a fall product role. Would love to connect!"

One closing exclamation is approved.

---

## Inputs the composer needs

Where a real relationship exists, the composer cannot invent it. A `personal_connection` field is needed **only for companies where one exists** — currently about eleven — not as a required field for every company. Where it is absent, tier 5 above applies instead.

Verified connections: Tavus (FlairX runs avatar generation on it; Akshat ran the integration) · Anam AI (evaluated in depth choosing an avatar layer) · Apriora, Ribbon, ConverzAI (FlairX competitive analysis) · Airbyte (direct Hevo competitor) · Jobright.ai (built an equivalent agent for himself).

---

## Bugs, separately

1. **`notes.py` surface lookup:** `(("recruiting", "hiring workflow", "interview"), "recruiting workflows")`. The `interview` trigger matches every avatar and conversational-video company, because interviewing is a use case of their technology, not their business — so Tavus and Anam were told they work on recruiting workflows. Same class as the `automation` → `robotics` bug that reached 57 people. The surface must describe what the company does, not the use case linking Akshat to it.

2. **Reversed biography:** "Worked on AI interview products at FlairX, then an MBA." The MBA is in progress; FlairX was summer 2026, during it.

3. **QC fallback inverts quality:** `AI note failed QC; kept the sendable template note` fired on Ali Ansari (CEO, Micro1), Hassaan Raza (CEO, Tavus) and Eloi du Bois — the highest-value contacts in the run — with nearly every note at 275–297 of 300 characters. The composer writes at the ceiling, QC rejects, and the best targets receive the weakest copy. Target 245–299 and the problem dissolves, because specific notes are shorter.

4. **No internal product names.** "Ziva" is an internal name for an avatar and means nothing outside the company. FlairX may be named.

5. **Never tell a vendor you evaluated their competitors.** Rejected: "I ran our avatar evaluation across Tavus, HeyGen and Anam," addressed to Tavus.

6. **No em dashes.** Universal rule, and the invite path has no check for it.

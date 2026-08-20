# Follow-up copy spec — before / after

Date: 2026-08-17
Source of "before": `artifacts/20260817-linkedin-followup-review.md`

Every "before" below is real copy from the current pack. Nothing here is hypothetical.

---

## The rules these examples encode

Deterministic critic checks (hard fails, not prompt guidance):

1. **No em dashes.** Currently 495 in the pack. The rule lives only as a soft line in `_SYSTEM`; models ignore it. Move to `critic.py` as a regex.
2. **No meta-text.** Reject any draft containing `---`, "here's the message", or the recipient's name in the third person.
3. **No evaluative predicates about their company, product or judgment.** Ban `is smart`, `is sharp`, `is clever`, `is genuinely`, `is solid`, `is the real insight`, `is exactly the ... I`, `most X are just Y`, `most X still Y`. Referencing what they build is fine. Grading it is not.
4. **Open with a human beat.** 219 of 312 drafts currently open cold on a bare question. Require a greeting or acknowledgement before the ask.
5. **Name the company explicitly.** Ban bare `here` / `there` as the company referent. It hides wrong org bindings.
6. **No proof beats on NAME or INTEL asks.** Those questions need no evidence; the proof reads as a pitch bolted to a favour.
7. **Ask must be consistent with the invite.** If the invite said BizOps, the follow-up cannot silently become product.
8. **Word budget scales down with touch count.** Third touch gets roughly half of first.
9. **Terminal touch says it is terminal, warmly.** Use "I'll stop bugging you after this one, promise!" or open with "Last note from me on this" and end with a warm sign-off. Do not use "This is my last note on it"; it reads like a countdown rather than politeness.
10. **Ask must be sized to actual authority.** Recruiters and SVPs must not receive INTEL asks.

---

## Case 1 — CREATE, founder, second touch

### Andrew Pekin, Bellagent — Founder | GenAI

**Before**

> The zero-touch integration angle is smart—most enterprise platforms still require months of setup work. I've spent five years diagnosing these friction points: at Hevo, I mapped reliability tradeoffs across 120K+ pipelines and saw how trust gaps kill adoption.
>
> Want to dig into a specific product problem you're sitting on right now?

**After**

> Hi Andrew, following up on my earlier note. More directly this time: would you be open to bringing on a part-time product intern at Bellagent this fall? Happy to start on whatever's most annoying right now!

**Why.** "Is smart" grades a founder's judgment uninvited. "Most enterprise platforms still require months of setup" explains his own market to him.

On the ask itself: the original repeated the offer to do free work. That offer *sounds* cheap but is not — he has to invent a problem, write it up, then read the response, which is half an hour of a founder's time. It also already failed once with this exact person. On a second touch the goal is to reduce the cost of replying, so the direct proposal wins: it is answerable in one word.

Critically, this proposes a role rather than querying whether one exists. "Do you have an internship" asks about a category that does not exist at his size and invites a no. Do not raise intern economics unless the recipient has raised budget first.

*(Supersedes the earlier version of this case, which repeated the work offer. The direct proposal is the rule for all second-touch CREATE.)*

---

## Case 2 — CREATE, third touch, already pitched twice

### Ryan Samadi, Alt-X — CEO, Co-Founder

**Before**

> The Excel agent angle is sharp—trillions in deals still move through manual spreadsheets because analysts can't trust automation. I've caught similar trust gaps before: at Intuit, I found a billing failure affecting 1,500+ businesses and coordinated Engineering, QA, Support to resolve it in hours instead of days. Open to working through a problem you're sitting with?

**After**

> Hi Ryan, last note from me on this. More directly: would you be open to bringing on a part-time product intern at Alt-X this fall? Either way, wishing you the best with Alt-X!

**Why.** Two long pitches have already gone unanswered, but both made Ryan invent a problem before he could respond. The terminal note should finally put the real, one-word decision in front of him. "Would you be open to bringing on" proposes a role without pretending an internship programme already exists, and the warm sign-off makes the close genuinely terminal.

---

## Case 3 — CREATE, weak proof link (currently held)

### Sean Wu, Synphony — ex-NVIDIA AI | CEO

**Before**

> The bed-level analytics + data pipeline angle is exactly the systems problem I've built for—diagnosed reliability tradeoffs across 120K+ pipelines at Hevo, caught billing failures at Intuit affecting thousands. Would love a conversation about what you're solving for strawberry picking at scale.

**After**

> Hi Sean, following up on my note. I spent five years on data pipeline reliability at Hevo, which sounds adjacent to the analytics side of what you're building at Synphony. If it's ever useful to have someone take a run at a product problem there, happy to pick one up.

**Why.** "Is exactly the systems problem I've built for" asserts the match instead of showing it, and stacks two proof beats to do it. "Sounds adjacent" is the honest version and is more persuasive for being hedged. "Strawberry picking at scale" landed as jarring because it was reaching for specificity the rest of the message hadn't earned.

---

## Case 4 — REFER, named requisition (mostly fine, shown as the baseline)

### Ryan Liu, Jobright.ai — Founding Engineer

**Before**

> Ryan, saw the Product Manager Intern role and it's exactly what I'm looking for this fall. Five years building backend and data systems, now deep in product on an AI interview tool. Would you be open to referring me for that specific requisition? I'm looking for a fall internship while finishing my MBA.

**After**

> Hi Ryan, thanks for connecting. I saw the Product Manager Intern role at Jobright and it lines up well with what I'm after this fall. Five years building backend and data systems, now doing product on an AI interview tool. Would you be open to referring me for that req? It'd be a fall internship while I finish the MBA.

**Why.** This one was already close. REFER is the one ask where a proof beat belongs, because he has to vouch for you and needs material to do it with. Changes are only the greeting beat and naming the company. Included here so the spec is clear that the fix is not "make everything shorter."

---

## Case 5 — NAME, senior product person, accepted then silent

### Kelly McDonald, Abridge — Staff Product Lead

**Before**

> Hey Kelly—I'm targeting a fall product internship and suspect you'd know who I should connect with here, whether that's recruiting, a product lead, or someone else entirely.

**After**

> Hi Kelly, thanks for connecting a while back. I'm looking for a fall product internship at Abridge. Do you know who I'd be best off talking to, whether that's recruiting or someone on the product side? Happy to go through the normal process.

**Why.** Em dash, bare "here", and no acknowledgement that time has passed. "Happy to go through the normal process" is the load-bearing addition: it signals you are not asking her to bend anything, which is most of what makes these read as presumptuous.

---

## Case 6 — NAME, meta-text leak (P0)

### Angela Lee, Adobe — Senior Product Manager

**Before**

> Since you're already connected with Angela and she accepted, here's the message:
>
> ---
>
> Who should I be talking to about fall product internship opportunities? Figured you'd know the right person to loop in.

**After**

> Hi Angela, thanks for connecting, fellow Trojan. I'm looking for a fall product internship at Adobe. Do you know who I should be talking to? Fight On.

**Why.** The model's preamble was captured as the message body. Sending it discloses the automation in one line. One instance out of 312, but there is no check for it, so the count is luck rather than control. The invite used "Fight On!", so continuing that is natural rather than decorative.

---

## Case 7 — NAME, wrong org binding → must hold, not send

### Stephen Lin, filed under Advanced Metal Research — "Software Engineer at NASA | Machine Learning Engineer"

**Before**

> Who should I talk to about a fall product internship here? You'd probably know who's hiring on the product side.

**After**

> *(no message — HOLD: org binding unverified)*

**Why.** He works at NASA per his own title and is filed under a different company. "Here" resolves silently to whatever the binding says, so a wrong binding produces a confidently wrong question and he has no idea what is being asked. The correct output is a hold, not better phrasing. Same class: **Samuel Pullman** (Talent @ Armadin, executive search, filed under Alchemy) and **Praneel Khiantani** (CS @ Harvard, SWE @ Janet AI, filed under Anthropic).

Rule: if the recipient's title names an employer that does not match the bound organisation, hold for human confirmation.

---

## Case 8 — NAME, ask contradicts the invite

### Tommy Joyner, Amperesand — SVP of Engineering

**Before**

> Who'd be the right person to talk to about fall product internship openings here?

**After**

> Hi Tommy, thanks for connecting. Following up on my earlier note about BizOps and strategy roles at Amperesand. Do you know who I'd be best off talking to about internship openings on that side?

**Why.** The invite said BizOps/Strategy. The follow-up asked about product. Switching what you want between touches, without acknowledging it, is the kind of thing a recipient notices and reads as mass outreach.

---

## Case 9 — NAME, ask inflation plus irrelevant proof

### Jairo Camacho, 1Password — Product Marketing

**Before**

> Jairo—thanks for connecting. I caught a billing failure at Intuit affecting 1,500+ businesses and got Engineering, QA, Support aligned to resolve it in hours instead of days. Now I'm looking at the Product Management Intern role for Unified Access this fall. Would you be open to a quick intro to whoever's hiring?

**After**

> Hi Jairo, thanks for connecting. I'm looking at the Product Management Intern role for Unified Access at 1Password this fall. Do you know who owns hiring for that team?

**Why.** Two failures. The Intuit story is a pitch attached to a question that needs no evidence, delivered to someone in product marketing who has no reason to care about billing reconciliation. And "a quick intro" is a FORWARD, a materially larger favour than the NAME ask the decision layer selected. The copy layer silently escalated the ask.

---

## Case 10 — INTEL sent to someone with real authority

### Vamshi Ramarapu, Actian — SVP Engineering

**Before**

> Thanks for connecting. I built data platforms at Optum and Intuit before moving into product—now working on an AI interview tool. Curious: when did product intern recruiting start for you, or have you seen interns come through the engineering side?

**After**

> Hi Vamshi, thanks for connecting. I'm looking for a fall product internship at Actian. Do you know who I'd be best off talking to on the product side?

**Why.** An SVP of Engineering can name someone or route you. Asking him whether he has *noticed interns around* wastes the highest-authority contact in the block on the lowest-value question. This is under-asking, and it is a decision-layer bug, not a copy bug.

---

## Case 11 — INTEL sent to a recruiter

### 大林彩乃, Arches — 人事・採用担当者 (HR / recruiting)

**Before**

> Have you seen product interns come through Arches, or remember when recruiting typically kicks off for fall?

**After**

> Hi, thanks for connecting. I'm looking for a fall product internship at Arches. Since you're on the recruiting side, is there a process I should be going through, or someone you'd point me to?

**Why.** She *runs* the thing the question asks whether she has noticed. Recruiters must never receive INTEL; they are the single most routable contact type. Additionally flag: her profile is entirely in Japanese, and an English message may not be the right call. Worth a human decision rather than an automated one.

---

## Case 12 — INTEL with a proof beat bolted on

### Wissem Gamra, Airtable — Software Engineer

**Before**

> Thanks for accepting. Five years in backend and data systems at Gojek, Hevo, and Intuit—now deep in product on an AI interview tool. Airtable's platform caught my eye. Have you seen product interns come through, or remember when recruiting typically kicks off?

**After**

> Hi Wissem, thanks for connecting. Have you seen product interns come through Airtable, or know roughly when recruiting opens? Trying to time things right for fall.

**Why.** Nothing in the CV recital helps him answer whether he has seen interns. "Airtable's platform caught my eye" is filler that reads as flattery. The question is the whole message.

---

## Case 13 — INTEL, clean baseline

### Vitid Nakareseisoon, Adobe — Software Engineer

**Before**

> Thanks for connecting. I'm hunting for a fall product internship at Adobe—have you seen product interns come through, or do you remember when recruiting typically kicks off?

**After**

> Hi Vitid, thanks for connecting. I'm looking for a fall product internship at Adobe. Have you seen interns come through, or know roughly when recruiting opens?

**Why.** Already close. Only the em dash and the missing name change. Included so the spec does not imply everything needs rewriting: roughly this shape is the target for the bulk of the 187 INTEL drafts.

---

## Case 14 — thread with prior incident, must hold

### Naman Kothari, Amplitude — Software Engineer

**Before**

> Have you seen product interns come through Amplitude, or remember when recruiting typically kicks off? Want to time outreach right.

**Prior message in this thread was Akshat's own apology:**

> "Sorry Naman, I've been working on an AI-LinkedIn followup agent and it looks like a few draft follow-ups got pasted here by mistake…"

**After**

> *(HOLD for manual — or, if sending: "Hi Naman, sorry again about the clutter earlier. Whenever you have a moment, have you seen product interns come through Amplitude?")*

**Why.** The last thing in this thread was an apology for automated spam. Following it with an automated form question is the exact repeat of the thing apologised for. Rule: if the last outbound message contains an apology or a correction, hold the thread for a human.

---

## Case 15 — direct reply that must actually answer

### Suresh Mergu, Optum

**What he said**

> "I know there is an internship program that Optum hires for every year. Given your experience, I don't think you would want to go that route. It's mostly FTEs for Product as far as I've seen and also there's no sponsorship these days that Optum is willing to provide for new hires. In any case, send me your resume, I'll check to see if there's any path to help get you in"

**Before**

> Thanks for checking, Suresh. Resume attached. Quick summary: five years building backend and data systems at Gojek, Hevo, Intuit, and Optum, now doing product on an AI interview tool. Appreciate you looking into options.

**After**

> Thanks Suresh, that's genuinely useful to know, particularly on the sponsorship side. Resume attached anyway in case something opens up. Five years on backend and data systems including a stretch at Optum, now doing product on an AI interview tool. Really appreciate you taking a look.

**Why.** He volunteered three substantive things and the draft acknowledged none of them. This is the only real reply in the file and therefore the one that most needs to read like a person. Note the distinction from the standing rule about not raising visa questions unprompted: he raised it, so acknowledging it is normal, and ignoring volunteered material information is what would read as odd.

---

## Open items not resolved by this spec

- **Warm / never-invited lane (65 drafts).** No sample reviewed here. These people were never sent an invite, so any "thanks for connecting" or "thanks for accepting" opener is false. `critic.py` has `warm_contact_false_acceptance_premise` for this; confirm it covers the greeting beat that rule 4 now mandates, since the two requirements interact.
- **Season wording.** Every draft says "fall". Confirm what that means from 17 August onward before 312 messages inherit the answer.

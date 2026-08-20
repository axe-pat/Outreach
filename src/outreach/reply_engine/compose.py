"""Layer 4: write the message, tightly constrained.

The writer receives the ordered thread, the structured read, the *single*
permitted move, a word budget, and the sentences already used elsewhere in
this batch.  It may only use facts from the thread, the company record, or the
candidate profile.

Per-action guidance is deliberately specific about what NOT to do, because the
observed failures were tonal as often as structural.
"""

from __future__ import annotations

from typing import Any

from .context import (
    APPLY_NOW,
    CREATE_WEDGE,
    LARGE_COMPANY_TRACK,
    PIPELINE_SIGNAL,
    CompanyFacts,
    campaign_goal,
)
from .models import (
    Action,
    Ask,
    Decision,
    ThreadRead,
    availability_qualifier_for,
    requires_availability_qualifier,
)
from .proof import ProofBeat, render_usable_proof
from .thread import Message
from .extract import transcript

#: Guidance injected per action.  Written as instructions to a person.
ACTION_GUIDANCE: dict[Action, str] = {
    Action.CREATE_CONTACTS: (
        "They pointed you at someone. Thank them in one or two lines and ask "
        "whether you may mention their name. Do NOT re-pitch yourself - they "
        "have already routed you, the pitch belongs in the next conversation."
    ),
    Action.SCHEDULE: (
        "They offered a call. Propose concrete availability. Nothing else."
    ),
    Action.SEND_ATTACHMENT: (
        "They asked for your resume. Say it is attached and give a two-line "
        "summary. Ask for nothing else - they asked for one thing."
    ),
    Action.RESOLVE_REQ: (
        "They mentioned an opening. If it is a full-time posting and you need "
        "an internship, acknowledge it in ONE clause, then convert to asking "
        "who runs product - a team hiring full-time has more work than people, "
        "and that is the real signal. Do not ask them to find the link for you."
    ),
    Action.ANSWER: (
        "They asked you something directly. Answer it plainly and first. Do NOT "
        "congratulate yourself on being honest and do NOT add lines like 'I'd "
        "rather not pretend' - just answer. Do not repeat biography already present "
        "in the invitation. The follow-on must serve the question they actually asked."
    ),
    Action.PARK: (
        "They cannot help. Close warmly in one line. No ask, no pitch. Do NOT "
        "correct anything they got wrong, including your name - it reads petty."
    ),
    Action.TRANSACT: (
        "They broadcast a launch. Give one piece of specific, real feedback on "
        "it. Do NOT pitch yourself in this message at all - the ask goes "
        "separately in a few days."
    ),
    Action.RECIPROCATE: (
        "They need something themselves. Acknowledge their situation and offer "
        "something useful before asking for anything. If they cannot help you, "
        "do not ask."
    ),
    Action.ACCEPT_OFFER: (
        "They offered help. Accept it and make saying yes take thirty seconds - "
        "hand them the exact wording or the exact link. Do not ask for a "
        "different favour than the one offered."
    ),
    Action.ASK: "",  # supplied per-ask below
}

ASK_GUIDANCE: dict[Ask, str] = {
    Ask.CREATE: (
        "This person can make a role exist. Structure: one concrete observation "
        "about their product that proves you looked, one line of proof tied to "
        "that observation, then ask to work on it. Ask for a CONVERSATION, not "
        "a job. Never ask 'do you have an internship' - at their size there is "
        "no such category and it invites a no. Signal you are low-friction in "
        "a few words; do NOT explain the economics of hiring an intern to a "
        "founder, and do not mention converting to full-time."
    ),
    Ask.REFER: (
        "Ask for a referral to the specific requisition named below. Include "
        "one compact line of background - they need enough to vouch for you."
    ),
    Ask.FORWARD: (
        "Ask them to pass your profile to the named person. Give them the "
        "wording so it takes thirty seconds."
    ),
    Ask.NAME: (
        "Ask who Akshat should be talking to about the structured goal below. "
        "Let the insider map the org. Do "
        "NOT ask who owns, runs, or leads product for a guessed area, and do not "
        "name a product focus from the company description. The right person may "
        "be a recruiter rather than a product leader. Keep it answerable in one line."
    ),
    Ask.INTEL: (
        "This person is an individual contributor, so ask exactly ONE casual "
        "routing question answerable from their own seat: do they know who owns "
        "product hiring at the target company? Name the company in the question. "
        "Ask only for the person, never an introduction, referral, programme-existence "
        "check, timing question, or account of how they got hired."
    ),
}

INTEL_TIMING_GUIDANCE = (
    "THREAD-SPECIFIC INTEL FOCUS: recruiting timing is the genuine unresolved fact. "
    "Ask exactly ONE timing question about the structured goal. Do not also ask who "
    "owns hiring, whether a programme exists, or how the recipient got in."
)

REQUISITION_GUIDANCE: dict[str, str] = {
    APPLY_NOW: (
        "This requisition is directly actionable. Name it and make the one ask a "
        "referral to that exact role."
    ),
    CREATE_WEDGE: (
        "Use the full-time product requisition only as evidence that this small "
        "company has funded product work, then make the CREATE ask. Do not ask to "
        "apply to or convert that full-time seat. SECOND TOUCH ONLY: if the recipient "
        "has explicitly raised a budget or small-team capacity objection in this "
        "conversation, you may rebut with a lower-cost, fewer-hours internship that "
        "can convert later. Never volunteer that economics argument in the opener."
    ),
    PIPELINE_SIGNAL: (
        "This full-time requisition is only a large-company growth signal. Do not ask "
        "for that seat or imply it can become an internship. Ask whether an intern or "
        "co-op path exists and when the next graduate cycle opens."
    ),
}

ANSWER_GUIDANCE: dict[str, str] = {
    "background_fit": (
        "They asked about background or fit. Give the direct answer and only the "
        "minimum missing evidence, then hand judgement back: ask whether that "
        "background translates or is materially useful on their side."
    ),
    "interest_availability_intent": (
        "They asked about interest, availability, or intent. Answer yes/no first, "
        "then state the actionable constraint: a fall product internship or co-op "
        "rather than a full-time role before graduation. End with one concrete ask "
        "they can act on. Do not restate employers, years of experience, the MBA, or "
        "the engineering biography already in the invitation."
    ),
    "other": (
        "Answer only what they asked. Add at most one directly relevant follow-on; "
        "do not use the answer as a reason to re-pitch your background."
    ),
}

_SYSTEM = """\
You are drafting one short LinkedIn message for Akshat Pathak.

Akshat: USC Marshall MBA (STEM), five years engineering before that - backend
and data platform systems at Gojek, Hevo, Intuit, Optum. Currently doing
product on an AI interview tool.

GOAL: {goal}
CAMPAIGN TRACK: {track}

Voice: direct, warm, unfussy. Sounds like a person typing, not a template.
One light exclamation is allowed; never use more than one. No corporate filler.
Never use an em dash. Never start with "I hope this finds you well". Do not use
the recipient's full name in the greeting if it is long; first name only.

HARD RULES:
- Maximum {budget} words. Shorter is better.
- Use ONLY facts given below. Invent nothing about the company or the person.
- Any claim about Akshat's work must come from USABLE PROOF. If no matching
  proof is supplied, do not infer work from an employer's brand.
- Do not reuse any sentence listed under BANNED.
- One ask maximum. Sometimes zero.
- Open with a human beat: a greeting, thanks, or truthful acknowledgement.
- Name the target company explicitly. Never use bare "here" or "there" for it.
- Write only recipient-facing copy. Never add separators or commentary such as
  "here's the message" and never discuss the recipient in the third person.
- Do not grade their judgment or product with "is smart", "is sharp", or similar.
- NAME and INTEL asks contain no resume proof. A NAME ask requests a name only;
  never silently upgrade it to an introduction, forward, or referral.
- Only use an in-group phrase or school slogan (for example "Fight On") if the
  recipient used it first in this conversation. Borrowing it otherwise is false
  familiarity.
- Never write a placeholder such as [your LinkedIn URL] or <name>. You cannot
  fill it in later - if you do not have the value, leave the sentence out.
- Never write a URL, profile link, handle or email address unless it appears
  verbatim above. If they asked for your profile, say you will send it rather
  than guessing the address.
"""

_USER = """\
CONVERSATION (chronological):
{transcript}

RELATIONSHIP CONTEXT:
{relationship_context}

{recipient_line}
{company_fact}
{req_line}

AVAILABILITY CONSTRAINT:
{availability_qualifier}

USABLE PROOF (choose at most one; copy the substance faithfully):
{usable_proof}

WHAT TO DO:
{guidance}

BANNED SENTENCES (already used elsewhere in this batch, do not reuse):
{banned}

Write only the message body. No greeting line like "Hi X," is required if the
opening already addresses them naturally.
"""


def build_prompt(
    *,
    messages: list[Message],
    decision: Decision,
    read: ThreadRead,
    name: str,
    title: str,
    company: str,
    facts: CompanyFacts,
    banned: list[str],
    usable_proof: list[ProofBeat] | None = None,
    season: str = "fall",
    relationship_context: str = "",
) -> tuple[str, str]:
    guidance = ACTION_GUIDANCE.get(decision.action, "")
    if decision.action is Action.ASK or decision.ask is not Ask.NONE:
        ask_guidance = ASK_GUIDANCE.get(decision.ask, "")
        if decision.ask is Ask.INTEL and read.intel_focus == "timing":
            ask_guidance = INTEL_TIMING_GUIDANCE
        guidance = f"{guidance}\n{ask_guidance}".strip()
    if decision.req_actionability in REQUISITION_GUIDANCE:
        guidance = (
            f"{guidance}\n{REQUISITION_GUIDANCE[decision.req_actionability]}"
        ).strip()
    if decision.action is Action.ANSWER:
        guidance = (
            f"{guidance}\n{ANSWER_GUIDANCE.get(read.question_kind, ANSWER_GUIDANCE['other'])}"
        ).strip()

    goal = decision.goal or campaign_goal(
        facts,
        role_family=decision.goal_role_family,
        season=season,
    )
    guidance = (
        f"STRUCTURED GOAL (do not change role family): {goal}.\n{guidance}"
    ).strip()
    if decision.campaign_track == LARGE_COMPANY_TRACK:
        guidance = (
            "LARGE-COMPANY TRACK: route toward full-time or 2027 new-grad hiring. "
            "Do not ask about a fall internship.\n" + guidance
        )
        if decision.ask is Ask.INTEL:
            if read.intel_focus == "timing":
                track_intel = (
                    "LARGE-COMPANY INTEL: ask the one thread-grounded timing question "
                    "about full-time or 2027 new-grad recruiting."
                )
            else:
                track_intel = (
                    "LARGE-COMPANY INTEL: ask only whether they know who owns product "
                    "hiring for full-time or 2027 new-grad roles at this company. "
                    "Request a name, not an introduction or referral."
                )
            guidance = f"{track_intel}\n{guidance}"
    if decision.ask is Ask.CREATE and decision.create_direct_ask:
        guidance = (
            "PRIOR CREATE PITCH ALREADY RAN: do not re-offer a written take or ask "
            "for a product problem. Ask directly whether they would take on a "
            f"{season} product intern. Propose the role; never ask whether an "
            "internship programme exists. Do not mention intern economics unless "
            "they raised budget first.\n" + guidance
        )
    if decision.terminal_touch:
        guidance = (
            "TERMINAL TOUCH: use the warm sentence 'I'll stop bugging you after "
            "this one, promise!' or open with 'Last note from me on this' and end "
            "with a warm sign-off. Never write 'This is my last note on it.' "
            "Remove pressure and do not imply another follow-up.\n" + guidance
        )

    availability_qualifier = "not required for this move"
    if requires_availability_qualifier(decision, read):
        availability_qualifier = (
            decision.availability_qualifier
            or (
                "full-time roles after MBA graduation and the 2027 new-grad cycle"
                if decision.campaign_track == LARGE_COMPANY_TRACK
                else availability_qualifier_for(season)
            )
        )
        guidance = (
            f"REQUIRED FIELD: State this availability constraint in substance: "
            f"{availability_qualifier}.\n{guidance}"
        ).strip()

    if decision.ask is Ask.NAME:
        # Company descriptions tempted the writer to invent an org-chart slot
        # ("who owns cloud security?"). NAME is goal-based routing, so the
        # description is deliberately not exposed for this ask.
        company_fact = (
            "COMPANY FACT: withheld for NAME routing - do not infer a product "
            "area or org-chart position."
        )
    else:
        company_fact = (
            f"COMPANY FACT (usable): {facts.description}"
            if facts.has_usable_description
            else "COMPANY FACT: none available - do NOT write a company observation."
        )
    req_line = (
        f"REQUISITION SIGNAL ({decision.req_actionability}): "
        f"{decision.citable_req} {decision.citable_req_url}".strip()
        if decision.citable_req
        else "REQUISITION SIGNAL: none - do not name any specific job posting."
    )

    system = _SYSTEM.format(
        season=season,
        budget=decision.word_budget,
        goal=goal,
        track=decision.campaign_track,
    )
    warm_uninvited = relationship_context == "warm_uninvited_referral"
    if warm_uninvited:
        relationship_note = (
            "Existing connection or PeopleGrove relationship; no prior LinkedIn "
            "invite note or message. This is first substantive outreach. The "
            "recipient is a possible referral path into the target company and "
            "may work elsewhere. Never thank them for accepting and never imply "
            "they work at the target company."
        )
        guidance = (
            f"WARM OPENER: start with 'Hi {name.split()[0]},' and a truthful reason "
            f"for reaching out about {company}. Do not thank them for connecting or "
            "accepting; no invite was sent.\n" + guidance
        )
        recipient_line = (
            f"RECIPIENT: {name} - {title or 'unknown'}\n"
            f"TARGET COMPANY FOR REFERRAL PATH: {company}"
        )
        empty_transcript = "(no prior LinkedIn message)"
    else:
        relationship_note = relationship_context or "Accepted LinkedIn connection or active thread."
        guidance = (
            f"OPENING BEAT: start with 'Hi {name.split()[0]},' plus thanks for "
            "connecting, or a truthful acknowledgement of the prior exchange.\n"
            + guidance
        )
        recipient_line = f"RECIPIENT: {name} - {title or 'unknown'} at {company}"
        empty_transcript = "(no messages yet - invite was accepted)"

    user = _USER.format(
        transcript=transcript(messages) or empty_transcript,
        relationship_context=relationship_note,
        recipient_line=recipient_line,
        company_fact=company_fact,
        req_line=req_line,
        availability_qualifier=availability_qualifier,
        usable_proof=(
            render_usable_proof(usable_proof or [])
            or "(none matched - make no specific work claim)"
        ),
        guidance=guidance or "Write a short, useful message.",
        banned="\n".join(f"- {s}" for s in banned[:40]) or "(none yet)",
    )
    return system, user


def compose(
    *,
    messages: list[Message],
    decision: Decision,
    read: ThreadRead,
    name: str,
    title: str,
    company: str,
    facts: CompanyFacts,
    banned: list[str],
    usable_proof: list[ProofBeat] | None = None,
    client: Any | None = None,
    model: str = "claude-haiku-4-5-20251001",
    season: str = "fall",
    relationship_context: str = "",
) -> tuple[str, str]:
    """Return ``(message, source)``.

    There is no template fallback.  If the model is unavailable the draft is
    held for a human rather than replaced with generic copy - the previous
    engine's silent template fallback is what produced its worst message.
    """

    if client is None:
        return "", "held_no_composer"

    system, user = build_prompt(
        messages=messages,
        decision=decision,
        read=read,
        name=name,
        title=title,
        company=company,
        facts=facts,
        banned=banned,
        usable_proof=usable_proof,
        season=season,
        relationship_context=relationship_context,
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=500,
            temperature=0.7,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            getattr(block, "text", "") for block in getattr(response, "content", [])
        ).strip()
        return text, "ai"
    except Exception as exc:  # noqa: BLE001
        # Preserve the error class for artifact-run diagnostics without
        # leaking request bodies, credentials, or provider response text.
        return "", f"held_composer_error:{type(exc).__name__}"

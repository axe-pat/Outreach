"""Layer 2: read the thread into a fixed schema.

The model does not write prose here.  It answers eleven questions about the
*person*, which is why a bounded schema absorbs an unbounded variety of
situations: "they're legal counsel", "they're #OpenToWork", "they left the
company" and "they're a contractor" are not four branches, they are four
values of ``capability``.

Falls back to deterministic regex extraction whenever the model is
unavailable or returns something unusable.  The fallback never invents a
richer read than it can prove.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import Capability, NamedPerson, ThreadRead
from .thread import Message

_EXTRACTION_PROMPT = """\
You are reading one LinkedIn conversation and returning structured facts.
Do NOT write a reply. Do NOT give advice. Return JSON only.

Conversation (chronological, "You" is the job seeker):
{transcript}

Recipient title: {title}
Company: {company}

Return exactly this JSON shape:
{{
  "question_asked_of_me": string or null,
  "question_kind": "background_fit" | "interest_availability_intent" | "other" | "none",
  "intern_economics_objection": boolean,
  "intel_focus": "routing" | "timing",
  "named_people": [{{"name": string, "role_hint": string, "why": string}}],
  "named_opening": string or null,
  "explicit_request": "resume" | "call" | "feedback" | "upvote" | "intro_material" | "none",
  "offer_made": "intro" | "referral" | "route_to_recruiter" | "advice" | "none",
  "offer_target": string or null,
  "capability": "can_create" | "can_refer" | "can_name" | "declined_referral" | "can_opine" | "cannot_help" | "no_longer_there",
  "sentiment": "warm" | "neutral" | "dismissive" | "transactional",
  "is_mass_blast": boolean,
  "acknowledged_standing_ask": boolean,
  "their_need": string or null,
  "factual_errors_about_me": [string],
  "commitments_i_made": [string]
}}

Rules:
- question_asked_of_me: only a real question directed at the job seeker that
  is still unanswered. "How are you?" does not count.
- question_kind: classify that question by what the recipient needs answered.
  Interest, availability, timing, and intent are not requests for biography.
- intern_economics_objection: true only if the recipient themselves raised a
  lack of budget, headcount, bandwidth, or small-team capacity.
- intel_focus: default to "routing". Use "timing" only when this conversation
  explicitly establishes recruiting timing as the unresolved fact. Never use
  timing merely because it is an easy generic question.
- named_people: ONLY a third party HUMAN the recipient pointed us toward, by
  name. Never a company, product, tool, website, course or program - a
  recommended resource is not a referral.
  Never include the recipient themselves, and never include the job seeker.
  If they simply greeted the job seeker by name, that is not a named person.
  If they pointed at nobody, return an empty list.
- named_opening: only if they said a role exists. Quote their words.
- explicit_request: only something THEY asked YOU for. If the job seeker
  offered to send a resume and they merely agreed or said nothing about it,
  that is "none" - an offer is not a request.
- offer_target: the person, role, or team the offered intro/referral/routing
  would reach. Do not infer a different target.
- offer_made: open-ended help such as "let me know how I can help", "happy to
  help", or "anything I can do" is still an advice offer, not none.
- capability: what THEY said, not what their title implies. Use cannot_help
  ONLY if they actually declined or said they cannot help. A greeting, a
  thank-you, or friendly small talk is NOT cannot_help - use can_opine when
  they have not said what they can do. Use declined_referral when they refuse
  to submit a referral but remain able to advise or name someone.
- is_mass_blast: true for broadcast promotion sent to many people.
- acknowledged_standing_ask: true if their last message is just agreement to
  an ask the job seeker already made ("sure", "yes that would be great").
- their_need: something THEY need (job hunting, laid off, hiring help).
- commitments_i_made: promises the job seeker made in this thread.
"""

_NAME_MENTION = re.compile(r"@?\b([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b")

#: Capitalised pairs that look like names and are not.  "Akshat" appears here
#: because people address him by name in the same breath as routing him on.
_NOT_A_CONTACT = {
    "fight on",
    "product hunt",
    "thank you",
    "best regards",
    "good luck",
    "linked in",
}
#: "connect with Jay" routes us.  "great to connect with you" does not, which
#: is why the object of the verb matters.
_ROUTING_HINT = re.compile(
    r"(?:reach out to|connect with|talk to|speak (?:to|with)|contact|find)\s+"
    r"(?!you\b|me\b|us\b)",
    re.I,
)

#: Leading words that make a capitalised pair a greeting, not a person.
_GREETING_LEAD = {
    "hey", "hi", "hello", "dear", "thanks", "thank", "please", "sure",
    "yes", "no", "great", "good", "best", "kind", "warm", "fight",
}
_CANNOT_HELP = re.compile(
    r"not sure how i can help|can'?t help|cannot help|not really sure|"
    r"may not have much insight|don'?t have much insight|wishing you the best|"
    r"not sure i would be able to help|i'?m not sure about this",
    re.I,
)
_DECLINED_REFERRAL = re.compile(
    r"not able to (?:submit (?:a )?referral|refer)|can'?t refer|cannot refer|"
    r"(?:do not|don'?t) submit referrals?|"
    r"only refer (?:people|someone|candidates?) (?:who|that)?\s*i(?:'|’)?ve worked with|"
    r"(?:not comfortable|unable) (?:submitting (?:a )?referral|referring)",
    re.I,
)
_LEFT_COMPANY = re.compile(r"i have left|i left|no longer (?:at|with)|used to work", re.I)
_RESUME_REQUEST = re.compile(r"share (?:me )?your resume|send (?:me )?your resume|your resume|cv\b", re.I)
_CALL_REQUEST = re.compile(r"chat over (?:a )?(?:phone )?call|hop on a call|phone call|let'?s talk|schedule", re.I)
_OFFER = re.compile(
    r"happy to (?:shoot|send|pass|refer|introduce)|let me know how i can help|"
    r"feel free to|happy to connect you|i can connect you|open to referring|"
    r"happy to help|anything i can do|let me know if you need anything|"
    r"(?:i would|i will|i'?ll) get in touch if",
    re.I,
)
_OFFER_TARGET = re.compile(
    r"(?:shoot|send|pass|forward) (?:your|the|it|this)?\s*(?:linkedin|profile|resume)?\s*"
    r"(?:over )?to\s+(?:the )?([^.!?]{2,80})|"
    r"(?:introduce|connect) you to\s+(?:the )?([^.!?]{2,80})",
    re.I,
)
_MASS_BLAST = re.compile(
    r"product hunt|upvote|we just launched|would love your support|"
    r"quick favor|check us out|🚀",
    re.I,
)
_OPENING = re.compile(
    r"we (?:currently )?have an opening|we'?re hiring|there'?s an opening|role is open",
    re.I,
)
#: "I don't believe we're hiring any product roles" is not an opening.
_NEGATED = re.compile(r"\b(?:not|don'?t|doesn'?t|aren'?t|no longer|isn'?t)\b", re.I)

#: A short affirmation to a standing ask we already made.  There is nothing
#: to say next: the ball is with the world, not with us.
_ACKNOWLEDGEMENT = re.compile(
    r"^(?:sure|yes|absolutely|of course|okay|ok|great|perfect|will do|"
    r"sounds good|happy to)\b[\s,.!]*(?:that (?:will|would) be great|"
    r"let me know|i will|thanks)?[\s,.!]*$",
    re.I,
)
_QUESTION = re.compile(r"\?\s*$|\?\s")
_BACKGROUND_QUESTION = re.compile(
    r"\b(?:background|experience|fit|worked (?:in|on|with)|exposure|sector|"
    r"industry|domain|technical|qualification)\b",
    re.I,
)
_INTENT_QUESTION = re.compile(
    r"\b(?:still looking|looking (?:at|for)|interested|interest|available|"
    r"availability|intent|timing|when (?:can|could|would|do)|start date|"
    r"fall|spring|summer|winter|internship|co[- ]?op)\b",
    re.I,
)
_INTERN_ECONOMICS_OBJECTION = re.compile(
    r"\b(?:no|lack(?:ing)?|don'?t have|do not have|without)\s+"
    r"(?:budget|headcount|bandwidth|capacity|resources?)\b|"
    r"\b(?:too small|can'?t afford|cannot afford|not hiring|no room on the team)\b",
    re.I,
)
_TIMING_GENUINELY_UNRESOLVED = re.compile(
    r"\b(?:not sure|don'?t know|unclear|unknown|wondering)\b.{0,45}"
    r"\b(?:when|timing|cycle|recruiting|applications?)\b|"
    r"\b(?:when|timing)\b.{0,35}\b(?:open|start|begin|recruit)\b",
    re.I,
)
_NEED = re.compile(r"opentowork|laid off|layoff|looking for (?:a )?(?:job|role|work)|job search", re.I)

#: Language that actually declines.  A declared ``cannot_help`` is honoured only
#: when the recipient said something like this: the model returns that value off
#: bare greetings ("Hey, Akshat") and off friendly small talk, and rule 6 then
#: parks a live thread with a goodbye.
_DECLINED = re.compile(
    r"not sure how i can help|can'?t help|cannot help|not really sure|"
    r"may not have much insight|don'?t have much insight|"
    r"not able to (?:help|do)|"
    r"don'?t have (?:any )?(?:openings|referrals)|"
    r"(?:are|'?re) not hiring|no openings|don'?t believe we'?re hiring|"
    r"not (?:as )?an? fte|wishing you the best|best of luck|"
    r"not sure i would be able to help|i'?m not sure about this",
    re.I,
)

#: What the *recipient* must have said for an ``explicit_request`` to be real.
#: The model readily attributes the job seeker's own offer - "if I send a tight
#: resume + 3-line blurb" - to the recipient, which fires rule 3 and produces a
#: resume drop nobody asked for.
_REQUEST_SUPPORT: dict[str, re.Pattern[str]] = {
    "resume": re.compile(
        r"share (?:me )?your resume|send (?:me )?your resume|your resume|your cv\b|"
        r"can you (?:please )?share|attach your|forward your",
        re.I,
    ),
    "intro_material": re.compile(
        r"your resume|your cv\b|your profile|send (?:me )?(?:your|a)|share (?:me )?your",
        re.I,
    ),
    "call": re.compile(
        r"over (?:a )?(?:phone )?call|phone call|hop on a call|let'?s talk|"
        r"chat (?:over|on)|schedule|calendar",
        re.I,
    ),
    "feedback": re.compile(r"feedback|thoughts|what do you think|would love your", re.I),
    "upvote": re.compile(r"upvote|product hunt|support us|would love your support", re.I),
}

#: Language that actually hands us a person.  Colin wrote "I would also highly
#: recommend Exponent!" about an interview-prep course and the model returned it
#: as a referral, which would have created a contact row for a product.  A
#: referral needs routing language, an @mention, or a statement of what the
#: person owns - recommending a resource is none of those.
_REFERRAL_EVIDENCE = re.compile(
    r"reach(?:ing)? out to|connect (?:with|you)|talk(?:ing)? to|speak(?:ing)? (?:to|with)|"
    r"\bping\b|\bcontact\b|\bfind\b|introduce you|put you in touch|shoot your|"
    r"pass (?:your|it|this)|forward (?:your|it|this)|@\w|"
    r"(?:he|she|they) (?:handles|handle|runs|run|owns|own|leads|lead|is|are)|"
    r"\bhandles\b|\bruns product\b|\bowns product\b|\bleads product\b",
    re.I,
)

#: Words that describe a role rather than name a person.  "happy to shoot your
#: linkedin over to the recruiting lead" is a routing promise, not a referral to
#: somebody called "recruiting lead", and it must not become a contact row.
_ROLE_WORDS = {
    "the", "their", "our", "a", "an", "someone", "somebody", "folks", "team",
    "recruiting", "recruiter", "recruiters", "recruitment", "hiring", "talent",
    "acquisition", "manager", "managers", "lead", "leads", "head", "director",
    "product", "engineering", "hr", "people", "ops", "staff", "contact",
    "partner", "person", "side", "guy", "colleague", "colleagues",
}


def transcript(messages: list[Message], *, limit: int = 12) -> str:
    return "\n".join(
        f"[{m.sender or 'contact'}] {m.text}" for m in messages[-limit:]
    )


def classify_question_kind(question: str | None) -> str:
    if not question:
        return "none"
    if _BACKGROUND_QUESTION.search(question):
        return "background_fit"
    if _INTENT_QUESTION.search(question):
        return "interest_availability_intent"
    return "other"


def deterministic_read(
    messages: list[Message],
    *,
    contact_title: str = "",
    candidate_first_name: str = "Akshat",
) -> ThreadRead:
    """Regex fallback.  Conservative by design - it under-claims."""

    read = ThreadRead(source="deterministic")
    inbound = [m for m in messages if not m.is_from_us]
    outbound = [m for m in messages if m.is_from_us]
    if not inbound:
        return read

    latest = inbound[-1].text
    joined = " ".join(m.text for m in inbound)
    read.intern_economics_objection = bool(_INTERN_ECONOMICS_OBJECTION.search(joined))

    if _LEFT_COMPANY.search(joined):
        read.capability = Capability.NO_LONGER_THERE
    elif _DECLINED_REFERRAL.search(joined):
        read.capability = Capability.DECLINED_REFERRAL
    elif _CANNOT_HELP.search(joined):
        read.capability = Capability.CANNOT_HELP

    # Names are only harvested from the clause that does the routing, and never
    # across a sentence boundary - "Im not sure about this Akshat. Probably
    # reach out to someone" previously yielded a contact called "Akshat Please".
    seen: set[str] = set()
    for clause in re.split(r"(?<=[.!?])\s+|\n+", joined):
        if not _ROUTING_HINT.search(clause):
            continue
        for candidate in _NAME_MENTION.findall(clause):
            key = candidate.lower()
            if key in seen or key in _NOT_A_CONTACT:
                continue
            if candidate_first_name and candidate_first_name.lower() in key:
                continue
            if key.split()[0] in _GREETING_LEAD:
                continue
            seen.add(key)
            read.named_people.append(NamedPerson(name=candidate, why=clause.strip()[:120]))

    for clause in re.split(r"(?<=[.!?])\s+|\n+", joined):
        if _OPENING.search(clause) and not _NEGATED.search(clause):
            read.named_opening = clause.strip()[:200]
            break

    # They acknowledged an ask we already made and nothing has changed since.
    if len(latest.split()) <= 8 and _ACKNOWLEDGEMENT.match(latest.strip()):
        if outbound:
            read.acknowledged_standing_ask = True

    if _RESUME_REQUEST.search(joined):
        read.explicit_request = "resume"
    elif _CALL_REQUEST.search(joined):
        read.explicit_request = "call"
    elif _MASS_BLAST.search(joined):
        read.explicit_request = "upvote"

    if _OFFER.search(joined):
        if re.search(r"recruit(?:er|ing|ment)|talent|hiring (?:lead|team)", joined, re.I):
            read.offer_made = "route_to_recruiter"
        elif re.search(r"\brefer(?:ral|ring)?\b", joined, re.I):
            read.offer_made = "referral"
        elif re.search(r"introduce|connect you", joined, re.I):
            read.offer_made = "intro"
        else:
            read.offer_made = "advice"
        if target_match := _OFFER_TARGET.search(joined):
            read.offer_target = next(
                (group.strip() for group in target_match.groups() if group and group.strip()),
                None,
            )

    read.is_mass_blast = bool(_MASS_BLAST.search(joined))
    if _NEED.search(f"{joined} {contact_title}"):
        read.their_need = "appears to be job hunting"

    if _QUESTION.search(latest) and "?" in latest:
        sentence = next(
            (s.strip() for s in re.split(r"(?<=[.!?])\s+", latest) if s.strip().endswith("?")),
            None,
        )
        # "How are you?" is not a question that needs answering.
        if sentence and not re.match(r"how (?:are|'?s) (?:you|it going)", sentence, re.I):
            read.question_asked_of_me = sentence
            read.question_kind = classify_question_kind(sentence)

    for message in outbound:
        if re.search(r"only send you a fit|if a .* opening comes up|i'?ll keep an eye", message.text, re.I):
            read.commitments_i_made.append(message.text[:200])

    return read


def _name_tokens(value: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{2,}", (value or "").lower())}


def is_self_or_recipient(
    name: str, *, recipient: str = "", candidate_first_name: str = "Akshat"
) -> bool:
    """True when a "named person" is really us or the person we are talking to.

    Rule 1 routes on ``named_people``, so a recipient echoed back as a referral
    silently converts every live thread into a thank-you note.  The model does
    this readily: a bare "Hey, Akshat" came back as a referral to the sender.
    """

    tokens = _name_tokens(name)
    if not tokens:
        return True
    if candidate_first_name and _name_tokens(candidate_first_name) & tokens:
        return True
    recipient_tokens = _name_tokens(recipient)
    if not recipient_tokens:
        return False
    return tokens <= recipient_tokens or recipient_tokens <= tokens


def is_role_placeholder(name: str) -> bool:
    """True when every token describes a role rather than naming a person."""

    tokens = _name_tokens(name)
    return bool(tokens) and tokens <= _ROLE_WORDS


def validate_ai_read(read: ThreadRead, messages: list[Message]) -> ThreadRead:
    """Hold the model's read to what the recipient actually said.

    The model is the only component allowed to absorb unbounded variety, but two
    of its errors are load-bearing because rules 3 and 6 fire on them directly:
    it declares ``cannot_help`` off a bare greeting, and it reports the job
    seeker's own offer as the recipient's request.  Both are downgrades only -
    this never invents a richer read than the model returned.
    """

    inbound = " ".join(m.text for m in messages if not m.is_from_us)

    referral_refusal = _DECLINED_REFERRAL.search(inbound)
    if referral_refusal:
        # A referral-specific refusal is a deterministic floor.  The model may
        # not promote it back to can_refer/can_opine from the recipient's title
        # or from other helpful advice in the same message.
        read.capability = Capability.DECLINED_REFERRAL
    elif read.capability in {Capability.CANNOT_HELP, Capability.NO_LONGER_THERE}:
        declined = _DECLINED.search(inbound) or _CANNOT_HELP.search(inbound)
        left = _LEFT_COMPANY.search(inbound)
        if read.capability is Capability.NO_LONGER_THERE and not left:
            read.capability = Capability.CANNOT_HELP if declined else Capability.CAN_OPINE
        elif read.capability is Capability.CANNOT_HELP and not declined:
            read.capability = Capability.CAN_OPINE

    support = _REQUEST_SUPPORT.get(read.explicit_request)
    if support is not None and not support.search(inbound):
        read.explicit_request = "none"

    # Rule 1 creates contact rows, so a "referral" with no routing language in
    # the thread must not survive: it is usually a tool or company they liked.
    if read.named_people and not _REFERRAL_EVIDENCE.search(inbound):
        read.named_people = []

    # Rule 5 outranks every ask rule, so an invented question silently converts
    # a refusal into a reply that asks for something anyway.  Vincent wrote "not
    # sure how I can help" - no question - and still received one.
    if read.question_asked_of_me and "?" not in inbound:
        read.question_asked_of_me = None
        read.question_kind = "none"
    elif read.question_asked_of_me:
        # This classification is cheap and deterministic; the model may quote
        # the question, but it cannot turn an interest question into a request
        # for a biography dump.
        read.question_kind = classify_question_kind(read.question_asked_of_me)

    if read.offer_made == "none" and _OFFER.search(inbound):
        # Explicit offers are another deterministic floor.  The model missed
        # Kirk's exact "let me know how I can help" and fell through to a cold
        # ask despite the highest-value signal in the thread.
        deterministic_offer = deterministic_read(messages)
        read.offer_made = deterministic_offer.offer_made
        read.offer_target = deterministic_offer.offer_target

    if _INTERN_ECONOMICS_OBJECTION.search(inbound):
        read.intern_economics_objection = True
    if read.intel_focus == "timing" and not _TIMING_GENUINELY_UNRESOLVED.search(
        inbound
    ):
        read.intel_focus = "routing"

    return read


def _coerce(
    payload: dict[str, Any],
    *,
    recipient: str = "",
    candidate_first_name: str = "Akshat",
) -> ThreadRead:
    def _string_or_none(key: str) -> str | None:
        value = payload.get(key)
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    people = []
    for entry in payload.get("named_people") or []:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip():
            name = str(entry["name"]).strip()
            if is_self_or_recipient(
                name, recipient=recipient, candidate_first_name=candidate_first_name
            ) or is_role_placeholder(name):
                continue
            people.append(
                NamedPerson(
                    name=name,
                    role_hint=str(entry.get("role_hint") or "").strip(),
                    why=str(entry.get("why") or "").strip(),
                )
            )

    try:
        capability = Capability(str(payload.get("capability") or "can_opine"))
    except ValueError:
        capability = Capability.CAN_OPINE

    intel_focus = str(payload.get("intel_focus") or "routing").strip().casefold()
    if intel_focus not in {"routing", "timing"}:
        intel_focus = "routing"

    return ThreadRead(
        question_asked_of_me=_string_or_none("question_asked_of_me"),
        question_kind=str(payload.get("question_kind") or "none"),
        intern_economics_objection=bool(payload.get("intern_economics_objection")),
        intel_focus=intel_focus,
        named_people=people,
        named_opening=_string_or_none("named_opening"),
        explicit_request=str(payload.get("explicit_request") or "none"),
        offer_made=str(payload.get("offer_made") or "none"),
        offer_target=_string_or_none("offer_target"),
        capability=capability,
        sentiment=str(payload.get("sentiment") or "neutral"),
        is_mass_blast=bool(payload.get("is_mass_blast")),
        acknowledged_standing_ask=bool(payload.get("acknowledged_standing_ask")),
        their_need=_string_or_none("their_need"),
        factual_errors_about_me=[
            str(x) for x in (payload.get("factual_errors_about_me") or []) if str(x).strip()
        ],
        commitments_i_made=[
            str(x) for x in (payload.get("commitments_i_made") or []) if str(x).strip()
        ],
        source="ai",
    )


def read_thread(
    messages: list[Message],
    *,
    client: Any | None = None,
    model: str = "claude-haiku-4-5-20251001",
    contact_title: str = "",
    company: str = "",
    contact_name: str = "",
) -> ThreadRead:
    fallback = deterministic_read(messages, contact_title=contact_title)
    # An invitation note is outbound context, not a conversation for Layer 2
    # to interpret. With no inbound message there are no recipient facts for a
    # model to read; asking it anyway lets it hallucinate cannot_help, offers,
    # or mass-blast intent and changes deterministic silent-lane decisions.
    if client is None or not any(not message.is_from_us for message in messages):
        return fallback

    prompt = _EXTRACTION_PROMPT.format(
        transcript=transcript(messages),
        title=contact_title or "unknown",
        company=company or "unknown",
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=700,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            getattr(block, "text", "") for block in getattr(response, "content", [])
        )
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return fallback
        read = _coerce(json.loads(match.group(0)), recipient=contact_name)
        return validate_ai_read(read, messages)
    except Exception:  # noqa: BLE001 - fail closed to the deterministic read
        return fallback

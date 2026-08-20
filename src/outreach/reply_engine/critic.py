"""Layer 5: a deterministic critic over generated copy.

This is where determinism earns its keep - as the referee, not the author.
Every check here catches a real failure observed in the 2026-08-07 backlog,
and none of them need a model.

A failing draft is regenerated once and then held for a human.  It is never
replaced with a template.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from .context import (
    CREATE_WEDGE,
    LARGE_COMPANY_TRACK,
    ROLE_BIZOPS_STRATEGY,
    ROLE_ENGINEERING,
    ROLE_PRODUCT,
)
from .models import (
    Action,
    Ask,
    Capability,
    Decision,
    ThreadRead,
    requires_availability_qualifier,
)
from .proof import (
    ProofBeat,
    domains_for_text,
    normalized_evidence_text,
    observation_before_proof,
    used_proof_beats,
)

MAX_URL_CHARS = 100
WORD_BUDGET_TOLERANCE = 0.10
BATCH_REPETITION_PRIOR_LIMIT = 3
PROOF_BEAT_PRIOR_LIMIT = 2
COMPANY_ASK_PRIOR_LIMIT = 1

#: Phrases the previous engine repeated to exhaustion.  "Who owns product
#: there" appeared in 103 of 185 drafts.
LEGACY_PHRASES = (
    "i'd rather get in front of them with something concrete",
    "happy to write up a take on a problem they care about",
    "rather than pitch myself",
    "than wait for a posting",
    "i build ai agents that do real work",
    "before the mba, 5 years on data and platform systems",
)

#: Self-congratulatory hedging that reads as preachy rather than honest.
PREACHY_PHRASES = (
    "i'd rather not pretend",
    "i won't pretend",
    "to be completely honest with you",
    "i'll be blunt",
)

_ROUTING_OFFERS = {"intro", "referral", "route_to_recruiter"}
_NEW_ROUTING_ASK = re.compile(
    r"who (?:owns|runs|leads)\s+(?P<who>[^?.,;]{2,100})|"
    r"point me to\s+(?P<point>[^?.,;]{2,100})|"
    r"(?:intro(?:duction)?|introduce me) to\s+(?P<intro>[^?.,;]{2,100})",
    re.I,
)
_TARGET_STOP = {
    "a", "an", "the", "someone", "somebody", "person", "people", "team",
    "lead", "leader", "side", "group", "product",
}
_SELF_CAPABILITY_TERMS = (
    "tax filing", "tax preparation", "fraud detection", "payments", "billing",
    "reconciliation", "data platform", "pipeline", "reliability", "observability",
    "monitoring", "audit", "marketplace", "logistics", "fleet", "pricing",
    "conversion", "api", "machine learning", "predictive", "clinical",
    "onboarding", "schema", "product strategy", "roadmap",
)
_INTERN_ECONOMICS = re.compile(
    r"\b(?:cheaper|lower[- ]cost|fewer hours|convert later|convert to full[- ]time|"
    r"fraction of the cost|part[- ]time budget)\b",
    re.I,
)

_INTERNSHIP_AVAILABILITY = re.compile(r"\b(?:intern(?:ship)?|co[- ]?op)\b", re.I)
_SEASONAL_AVAILABILITY = re.compile(
    r"\b(?:fall|spring|summer|winter|off[- ]?cycle)\b",
    re.I,
)
_NOT_FULL_TIME_CONSTRAINT = re.compile(
    r"\bnot\s+(?:a\s+)?full[- ]?time\b|"
    r"\brather than\s+(?:a\s+)?full[- ]?time\b|"
    r"\b(?:can(?:not|'t)|unable)\b.{0,30}\bfull[- ]?time\b|"
    r"\bmid[- ]?mba\b|\bwhile\b.{0,30}\bmba\b|\bbefore graduation\b",
    re.I,
)
_SENIOR_AUTHORITY_TITLE = re.compile(
    r"\b(?:founder|co[- ]?founder|chief|ceo|cto|cpo|coo|vp|vice president|"
    r"head of|director|manager|executive chairman|engineering leader|lead|"
    r"principal|senior principal)\b",
    re.I,
)
_ORG_STRUCTURE_QUESTION = re.compile(
    r"\bwho(?:\s+(?:would|'d))?\s+(?:own|owns|run|runs|lead|leads|handle|handles|manage|manages)\b|"
    r"\bwho\s+(?:should|would|could)\s+i\s+(?:talk|speak|reach out)\s+to\b|"
    r"\b(?:right|best)\s+person\b|\bwhich\s+team\s+(?:owns|runs|handles)\b",
    re.I,
)
_PERMITTED_INTEL_HIRING_ROUTING = re.compile(
    r"\bwho\b.{0,35}\b(?:own|owns|run|runs|lead|leads|handle|handles|manage|manages)\b"
    r".{0,20}\bproduct\s+(?:hiring|recruiting|recruitment)\b|"
    r"\bwho\b.{0,35}\b(?:product\s+)?(?:hiring|recruiting|recruitment)\b",
    re.I,
)
_INTEL_TIMING_OR_PERSONAL_ENTRY = re.compile(
    r"\bwhen\b.{0,45}\b(?:recruit|hiring|applications?|cycle|open|start|begin)\b|"
    r"\b(?:recruiting|hiring|applications?|cycle)\b.{0,45}\bwhen\b|"
    r"\bhow\b.{0,20}\b(?:you|they)\b.{0,20}\b(?:got|joined|landed|hired)\b",
    re.I,
)
_ORG_CHART_SLOT = re.compile(
    r"\bwho(?:\s+(?:would|'d))?\s+(?:own|owns|run|runs|lead|leads|handle|handles|manage|manages)\b|"
    r"\b(?:owner|head|lead|leader)\s+of\s+(?:product|the\s+product)\b",
    re.I,
)
_DIRECT_ASK_OPENING = re.compile(
    r"^(?:quick question\s*[:,-]?\s*)?(?:who|what|when|where|why|how|"
    r"have|has|had|do|does|did|can|could|would|will|is|are|was|were)\b|"
    r"^(?:open to|any chance|i(?:'m| am) curious (?:if|whether)|"
    r"i wanted to ask|would love (?:an?|to))\b",
    re.I,
)
_EVALUATIVE_PREDICATE = re.compile(
    r"\b(?:angle|approach|idea|insight|platform|product|work|what you(?:'re| are) building)\b"
    r".{0,35}\bis\s+(?:smart|sharp|clever|genuinely\b[^.!?]*|solid|the real insight)\b|"
    r"\bis exactly the\b[^.!?]{0,80}\bi\b|"
    r"\bmost\s+[^.!?]{1,55}\s+(?:are just|still)\b",
    re.I,
)
_BARE_COMPANY_REFERENT = re.compile(
    r"\b(?:work|working|interns?|internships?|roles?|openings?|hiring|team|"
    r"product(?: side)?)\s+(?:here|there)\b|\b(?:here|there)\s*[?.!]",
    re.I,
)
_SELF_PROOF_SIGNAL = re.compile(
    r"\b(?:gojek|hevo|intuit|optum)\b|\b(?:five|5) years\b|"
    r"\b(?:backend|data platform|data systems|billing failure|reconciliation)\b|"
    r"\bai interview tool\b|\b(?:120k|50k|80k|1,?500|3,?000)\+?\b",
    re.I,
)
_NAME_ESCALATION = re.compile(
    r"\b(?:would|could|can) you\b[^?]{0,45}\b(?:intro(?:duce|duction)?|"
    r"refer|forward|pass (?:me|my)|loop me in|put me in touch)\b|"
    r"\bopen to (?:a )?(?:quick )?intro(?:duction)?\b",
    re.I,
)
_FILLER_CLOSER = re.compile(
    r"\blow lift\b|\bwould love to talk through how i could help\b",
    re.I,
)
_TERMINAL_LANGUAGE = re.compile(
    r"\b(?:last|final) (?:note|message|follow[- ]?up)\b|"
    r"\bwon't follow up again\b|\bwill not follow up again\b|"
    r"\bstop bugging you after this one\b",
    re.I,
)
_COLD_TERMINAL_SENTENCE = re.compile(
    r"\bthis is my last note on it\.?(?=\s|$)",
    re.I,
)
WARM_TERMINAL_SENTENCE = "I'll stop bugging you after this one, promise!"
_CREATE_WORK_OFFER = re.compile(
    r"\bwritten take\b|\btake a run at\b|\bwork through a problem\b|"
    r"\bsend me (?:a |one )?(?:product )?problem\b",
    re.I,
)
_CREATE_DIRECT_ROLE = re.compile(
    r"\b(?:take|bring|have|use|consider)\b[^?.]{0,35}\bproduct intern\b|"
    r"\bproduct intern\b[^?.]{0,35}\b(?:this fall|for fall)\b",
    re.I,
)
_CREATE_EXISTENCE_QUERY = re.compile(
    r"\b(?:do you|does [^?]{1,30}|are there|is there)\b[^?]{0,45}\bintern",
    re.I,
)

_COMPANY_TOKEN_STOP = {
    "ai", "co", "company", "corp", "corporation", "inc", "incorporated",
    "llc", "ltd", "limited", "technologies", "technology",
}
_INVITE_OVERLAP_STOP = {
    "about", "accepted", "background", "company", "connect", "connecting",
    "connection", "explore", "exploring", "fall", "from", "hello", "here",
    "intern", "internship", "looking", "love", "product", "role", "roles",
    "thanks", "there", "this", "with", "would", "your",
}
_PERSONAL_HOOKS = (
    "fight on", "fellow trojan", "fellow marshall", "fellow thapar",
    "usc marshall", "thapar alum",
)
_MATERIAL_REPLY_TOPICS: dict[str, re.Pattern[str]] = {
    "sponsorship": re.compile(r"\bsponsor(?:ship)?\b|\bvisa\b", re.I),
    "full_time": re.compile(r"\bfull[- ]?time\b|\bfte\b", re.I),
    "intern_program": re.compile(r"\bintern(?:ship)? program\b|\binterns?\b", re.I),
    "budget": re.compile(r"\bbudget\b|\bheadcount\b|\bcapacity\b", re.I),
    "sector_fit": re.compile(r"\bsector\b|\bindustry\b|\bdomain\b", re.I),
}


@dataclass
class CriticResult:
    passed: bool = True
    flags: list[str] = field(default_factory=list)
    normalized_message: str = ""

    def fail(self, flag: str) -> None:
        self.passed = False
        self.flags.append(flag)


def sentences(text: str) -> list[str]:
    return [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", text or "")
        if len(s.strip().split()) >= 5
    ]


_COORDINATING_CONJUNCTIONS = {"and", "but", "or", "so", "yet"}
_EM_DASH_LEADING_WORD = re.compile(
    r"(?P<lead>[\"'“”‘’(\[]*)(?P<word>[A-Za-z]+)"
)
_EM_DASH_MARKER = re.compile(r"\ue000em_dash_break_\d+\ue001")
_FRAGMENT_WORD = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")


def _without_em_dash_markers(text: str) -> str:
    return _EM_DASH_MARKER.sub("", text)


def normalize_em_dashes(text: str) -> tuple[str, list[str]]:
    """Apply the review-pack punctuation rule before any critic checks.

    A dash before a coordinating conjunction becomes a comma. Every other
    dash becomes a sentence boundary and the following word is capitalized.
    The returned fragments are only short sentences created by the new full
    stop; pre-existing short sentences are outside this normalization guard.
    """

    normalized = text or ""
    break_markers: list[str] = []
    while "—" in normalized:
        before, after = normalized.split("—", 1)
        left = before.rstrip()
        right = after.lstrip()
        leading = _EM_DASH_LEADING_WORD.match(right)
        word = leading.group("word") if leading else ""
        if word.casefold() in _COORDINATING_CONJUNCTIONS:
            assert leading is not None
            start, end = leading.span("word")
            right = right[:start] + word.casefold() + right[end:]
            normalized = f"{left}, {right}"
            continue

        if leading is not None:
            start, end = leading.span("word")
            right = right[:start] + word[:1].upper() + word[1:] + right[end:]
        marker = f"\ue000em_dash_break_{len(break_markers)}\ue001"
        break_markers.append(marker)
        normalized = f"{left}{marker}. {right}"

    fragments: list[str] = []
    for marker in break_markers:
        marker_index = normalized.index(marker)
        prefix = normalized[:marker_index]
        prior_boundary = list(re.finditer(r"[.!?](?:[\"'”’\)\]]*)\s+", prefix))
        left_start = prior_boundary[-1].end() if prior_boundary else 0
        left_sentence = _without_em_dash_markers(prefix[left_start:]).strip()

        suffix = normalized[marker_index + len(marker):]
        # This marker always precedes the full stop introduced by the rule.
        right_source = suffix[1:].lstrip() if suffix.startswith(".") else suffix.lstrip()
        next_boundary = re.search(
            r"[.!?](?:[\"'”’\)\]]*)(?=\s|$)",
            right_source,
        )
        if next_boundary:
            right_sentence = _without_em_dash_markers(
                right_source[:next_boundary.end()]
            ).strip()
        else:
            right_sentence = _without_em_dash_markers(right_source).strip()

        candidates = (
            f"{left_sentence}." if left_sentence else "",
            right_sentence,
        )
        for candidate in candidates:
            if candidate and len(_FRAGMENT_WORD.findall(candidate)) < 5:
                fragments.append(candidate)

    return _without_em_dash_markers(normalized), list(dict.fromkeys(fragments))


def normalize_terminal_close(text: str, decision: Decision) -> str:
    """Apply the prescribed warm terminal language deterministically."""

    normalized = _COLD_TERMINAL_SENTENCE.sub(WARM_TERMINAL_SENTENCE, text or "")
    if not decision.terminal_touch or _TERMINAL_LANGUAGE.search(normalized):
        return normalized
    separator = "" if not normalized.strip() else " "
    return f"{normalized.rstrip()}{separator}{WARM_TERMINAL_SENTENCE}"


def batch_repetition_sentences(text: str, decision: Decision) -> list[str]:
    """Sentences that belong in the cross-recipient repetition gate.

    NAME and INTEL intentionally prescribe a cheap, answerable ask, and a
    terminal touch must explicitly say it is the last note. Repeating either
    mandated sentence across unrelated recipients is compliance, not evidence
    of generic copy, so question sentences and prescribed terminal language are
    excluded. Observation and proof sentences remain exact-matched signals of
    whether the writer found specific content.
    """

    values = [
        sentence
        for sentence in sentences(text)
        if not (
            decision.terminal_touch
            and _TERMINAL_LANGUAGE.search(sentence)
        )
    ]
    if decision.ask is Ask.NONE:
        return values
    return [sentence for sentence in values if "?" not in sentence]


def company_ask_sentences(text: str, decision: Decision) -> list[str]:
    """Return normalized question clauses for the within-company ask gate."""

    if decision.ask is Ask.NONE:
        return []
    clauses: list[str] = []
    for segment in re.split(r"(?<=\?)\s+|\n+", text or ""):
        if "?" not in segment:
            continue
        question = segment[:segment.rfind("?") + 1].strip()
        match = re.search(
            r"\b(?:who|what|when|where|why|how|have|has|had|do|does|did|"
            r"can|could|would|will|is|are|was|were)\b.*\?",
            question,
            re.I,
        )
        value = (match.group(0) if match else question).strip()
        if len(value.split()) >= 3:
            clauses.append(value)
    return clauses


def company_ask_key(company: str, question: str) -> tuple[str, str]:
    """Canonical key for one exact ask within one organization."""

    return (
        " ".join(_normalized_tokens(company)),
        " ".join(_normalized_tokens(question)),
    )


def _answers_question(message: str, question: str) -> bool:
    """Cheap overlap test: did we engage with the nouns they asked about?"""

    stop = {
        "do", "you", "have", "any", "the", "in", "a", "an", "is", "are", "of",
        "and", "or", "to", "for", "with", "your", "my", "i", "not", "see",
        "that", "this", "it", "what", "how", "why", "would", "could", "can",
    }
    terms = {
        w for w in re.findall(r"[a-z]{4,}", (question or "").lower()) if w not in stop
    }
    if not terms:
        return True
    body = (message or "").lower()
    hits = sum(1 for term in terms if term in body)
    return hits >= max(1, len(terms) // 3)


def _target_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", (value or "").casefold())
        if token not in _TARGET_STOP
    }


def _effective_question_kind(read: ThreadRead) -> str:
    if read.question_kind != "none":
        return read.question_kind
    question = read.question_asked_of_me or ""
    if re.search(
        r"\b(background|experience|fit|exposure|sector|industry|domain|qualification)\b",
        question,
        re.I,
    ):
        return "background_fit"
    if re.search(
        r"\b(still looking|looking (?:at|for)|interested|available|availability|"
        r"intent|timing|fall|spring|summer|winter|internship|co[- ]?op)\b",
        question,
        re.I,
    ):
        return "interest_availability_intent"
    return "other" if question else "none"


def _question_count(message: str) -> int:
    """Count explicit questions plus interrogative clauses joined under one '?'."""

    marks = message.count("?")
    joined = len(
        re.findall(
            r"\band\s+(?:do|does|did|can|could|would|will|is|are|"
            r"who|what|when|where|why|how)\b",
            message,
            re.I,
        )
    )
    return marks + joined


def _normalized_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (value or "").casefold())


def _company_named(message: str, company: str) -> bool:
    company_tokens = [
        token for token in _normalized_tokens(company)
        if token not in _COMPANY_TOKEN_STOP
    ]
    if not company_tokens:
        return False
    body = " ".join(_normalized_tokens(message))
    return " ".join(company_tokens) in body


def _without_target_company(message: str, company: str) -> str:
    """Remove the required target-company referent before scanning for CV proof."""

    tokens = [
        token for token in _normalized_tokens(company)
        if token not in _COMPANY_TOKEN_STOP
    ]
    if not tokens:
        return message
    phrase = r"\b" + r"[\s.\-]*".join(re.escape(token) for token in tokens) + r"\b"
    return re.sub(phrase, " ", message, flags=re.I)


def _recipient_used_in_third_person(message: str, recipient_name: str) -> bool:
    first_name = (recipient_name or "").strip().split()[0] if recipient_name.strip() else ""
    if not first_name:
        return False
    matches = list(re.finditer(re.escape(first_name), message, re.I))
    if not matches:
        return False
    for match in matches:
        prefix = message[max(0, match.start() - 24):match.start()]
        suffix = message[match.end():match.end() + 3]
        greeting_prefix = bool(
            re.search(r"(?:^|\b)(?:hi|hey|hello|thanks|thank you)\s*$", prefix, re.I)
        )
        direct_vocative = match.start() == 0 or prefix.rstrip().endswith(",")
        if (greeting_prefix or direct_vocative) and re.match(r"\s*[,!:.\-–—]", suffix):
            continue
        return True
    return False


def _opens_directly_on_ask(message: str, recipient_name: str) -> bool:
    """True when greeting/vocative decoration leads straight into the ask."""

    opening = (message or "").lstrip()
    first_name = (recipient_name or "").strip().split()[0] if recipient_name.strip() else ""
    if first_name:
        opening = re.sub(
            rf"^(?:(?:hi|hey|hello)\s+)?{re.escape(first_name)}\s*[,!:—-]\s*",
            "",
            opening,
            flags=re.I,
        )
    else:
        opening = re.sub(r"^(?:hi|hey|hello)\b[^,!:—-]*[,!:—-]\s*", "", opening, flags=re.I)
    return bool(_DIRECT_ASK_OPENING.search(opening))


def _invite_overlap(message: str, invite_text: str, company: str) -> str:
    message_key = " ".join(_normalized_tokens(message))
    invite_key = " ".join(_normalized_tokens(invite_text))
    for hook in _PERSONAL_HOOKS:
        if hook in message_key and hook in invite_key:
            return hook

    company_tokens = set(_normalized_tokens(company))
    stop = _INVITE_OVERLAP_STOP | company_tokens
    message_tokens = [
        token for token in _normalized_tokens(message)
        if len(token) >= 3 and token not in stop
    ]
    invite_tokens = [
        token for token in _normalized_tokens(invite_text)
        if len(token) >= 3 and token not in stop
    ]
    if len(message_tokens) < 4 or len(invite_tokens) < 4:
        return ""
    invite_ngrams = {
        tuple(invite_tokens[index:index + 4])
        for index in range(len(invite_tokens) - 3)
    }
    for index in range(len(message_tokens) - 3):
        ngram = tuple(message_tokens[index:index + 4])
        if ngram in invite_ngrams:
            return " ".join(ngram)
    return ""


def _engages_material_reply(message: str, inbound: str) -> bool:
    topics = {
        topic
        for topic, pattern in _MATERIAL_REPLY_TOPICS.items()
        if pattern.search(inbound or "")
    }
    if not topics:
        return True
    return any(_MATERIAL_REPLY_TOPICS[topic].search(message) for topic in topics)


def review(
    *,
    message: str,
    decision: Decision,
    read: ThreadRead,
    capability: Capability,
    batch_sentence_counts: Counter[str] | None = None,
    batch_company_ask_counts: Counter[tuple[str, str]] | None = None,
    has_attachment_task: bool = False,
    proof_beats: list[ProofBeat] | None = None,
    proof_beat_counts: Counter[tuple[Ask, str]] | None = None,
    profile_text: str = "",
    recipient_title: str = "",
    relationship_context: str = "",
    recipient_name: str = "",
    company: str = "",
    invite_text: str = "",
    last_inbound_message: str = "",
) -> CriticResult:
    normalized_message, em_dash_fragments = normalize_em_dashes(message)
    normalized_message = normalize_terminal_close(normalized_message, decision)
    body = normalized_message.strip()
    result = CriticResult(normalized_message=body)

    if not body:
        result.fail("empty_message")
        return result

    for fragment in em_dash_fragments:
        result.fail(f"em_dash_fragment:{fragment[:40]}")

    words = len(body.split())
    hard_word_ceiling = math.ceil(
        decision.word_budget * (1 + WORD_BUDGET_TOLERANCE)
    )
    if words > hard_word_ceiling:
        result.fail(
            f"over_budget:{words}>{decision.word_budget}"
            f"(tolerance_ceiling={hard_word_ceiling})"
        )

    lowered = body.lower()

    if "---" in body or re.search(r"\bhere(?:'s| is) the message\b", body, re.I):
        result.fail("meta_text")
    if _recipient_used_in_third_person(body, recipient_name):
        result.fail("meta_recipient_third_person")
    if _EVALUATIVE_PREDICATE.search(body):
        result.fail("evaluative_predicate")
    copy_contract_context = bool(recipient_name or company or invite_text)
    if copy_contract_context and _opens_directly_on_ask(body, recipient_name):
        result.fail("missing_opening_beat")
    if (
        relationship_context == "warm_uninvited_referral"
        and not re.match(r"^(?:hi|hey|hello)\b", body, re.I)
        and "missing_opening_beat" not in result.flags
    ):
        result.fail("missing_opening_beat")
    if company and not _company_named(body, company):
        result.fail("company_not_named")
    if _BARE_COMPANY_REFERENT.search(body):
        result.fail("bare_company_referent")
    if body.count("!") > 1:
        result.fail("too_many_exclamations")
    if _FILLER_CLOSER.search(body):
        result.fail("filler_closer")
    if invite_text:
        overlap = _invite_overlap(body, invite_text, company)
        if overlap:
            result.fail(f"invite_overlap:{overlap[:40]}")

    if relationship_context == "warm_uninvited_referral" and re.search(
        r"\bthanks?\s+for\s+(?:accepting|connecting)\b|"
        r"\bglad\s+(?:we|to)\s+connect(?:ed)?\b",
        body,
        re.I,
    ):
        result.fail("warm_contact_false_acceptance_premise")

    if requires_availability_qualifier(decision, read):
        if decision.campaign_track == LARGE_COMPANY_TRACK:
            has_large_track_availability = bool(
                re.search(r"\bfull[- ]?time\b", body, re.I)
                and re.search(r"\b2027\b|\bnew[- ]?grad\b|\bgraduate\b", body, re.I)
            )
            if not has_large_track_availability:
                result.fail("missing_availability_qualifier")
        elif not (
            _INTERNSHIP_AVAILABILITY.search(body)
            and _SEASONAL_AVAILABILITY.search(body)
            and _NOT_FULL_TIME_CONSTRAINT.search(body)
        ):
            result.fail("missing_availability_qualifier")

    # Employer brands are not evidence of work.  When a draft names one of the
    # resume employers alongside a concrete capability, that capability must
    # occur in either the verified proof catalog or the operator profile.
    available_proof = proof_beats or []
    used_proof = used_proof_beats(body, available_proof)
    if decision.ask in {Ask.NAME, Ask.INTEL} and (
        used_proof
        or _SELF_PROOF_SIGNAL.search(_without_target_company(body, company))
    ):
        result.fail(f"proof_not_allowed_for_{decision.ask.value}")
    evidence = normalized_evidence_text(available_proof, profile_text)
    checked_employers: set[str] = set()
    for beat in available_proof:
        if beat.employer.casefold() in checked_employers:
            continue
        checked_employers.add(beat.employer.casefold())
        employer_aliases = {beat.employer.casefold()}
        first_token = beat.employer.casefold().split()[0]
        if len(first_token) >= 4:
            employer_aliases.add(first_token)
        if not any(
            re.search(rf"\b{re.escape(alias)}\b", lowered)
            for alias in employer_aliases
        ):
            continue
        for term in _SELF_CAPABILITY_TERMS:
            if re.search(rf"\b{re.escape(term)}s?\b", lowered):
                normalized_term = re.sub(r"[^a-z0-9]+", " ", term.casefold()).strip()
                if normalized_term not in evidence:
                    result.fail(f"unsourced_self_claim:{beat.employer}:{term}")
                    break

    # CREATE copy uses a company observation to justify why a resume beat is
    # relevant.  A true resume claim is still misleading in that position if
    # its tagged domain has no overlap with the observation it is supporting.
    if decision.ask is Ask.CREATE and used_proof:
        observation_domains = domains_for_text(
            observation_before_proof(body, used_proof)
        )
        for beat in used_proof:
            if not observation_domains.intersection(beat.domains):
                result.fail(f"proof_domain_mismatch:{beat.beat_id}")

    # Proof diversity is enforced inside each ask block. Two uses are allowed;
    # the third is held so a single resume story cannot stand in for contact-
    # specific grounding across the block.
    if proof_beat_counts is not None and decision.ask is not Ask.NONE:
        for beat in used_proof:
            if proof_beat_counts[(decision.ask, beat.beat_id)] >= PROOF_BEAT_PRIOR_LIMIT:
                result.fail(f"proof_beat_reuse:{beat.beat_id}")

    for phrase in LEGACY_PHRASES:
        if phrase in lowered:
            result.fail(f"legacy_template_phrase:{phrase[:32]}")
    for phrase in PREACHY_PHRASES:
        if phrase in lowered:
            result.fail(f"preachy:{phrase[:32]}")

    if _INTERN_ECONOMICS.search(body) and not (
        decision.req_actionability == CREATE_WEDGE
        and read.intern_economics_objection
    ):
        result.fail("intern_economics_without_small_company_objection")

    # They asked something and we talked past it.
    if read.question_asked_of_me and not _answers_question(body, read.question_asked_of_me):
        result.fail("did_not_answer_their_question")

    question_kind = _effective_question_kind(read)
    if decision.action is Action.ANSWER and question_kind == "interest_availability_intent":
        biography_hits = sum(
            bool(re.search(pattern, body, re.I))
            for pattern in (
                r"\bmba\b|marshall",
                r"\b(?:five|5) years\b|years (?:of|in) engineering",
                r"backend|data platform|systems engineering",
                r"\b(?:gojek|hevo|intuit|optum)\b",
            )
        )
        if biography_hits >= 2:
            result.fail("biography_dump_in_interest_answer")
        if not re.search(
            r"\b(fall|spring|summer|winter|intern(?:ship)?|co[- ]?op|graduate|"
            r"2027|available|timing|before graduation)\b",
            body,
            re.I,
        ):
            result.fail("interest_answer_missing_actionable_constraint")

    if decision.action is Action.ANSWER and question_kind == "background_fit":
        if not (
            "?" in body
            and re.search(
                r"\b(translate|useful|relevant|count|fit|matter|hard gate|on your side|there)\b",
                body,
                re.I,
            )
        ):
            result.fail("background_answer_did_not_hand_judgement_back")

    # Long raw URLs with query strings look broken in a chat window.
    for url in re.findall(r"https?://\S+", body):
        if len(url) > MAX_URL_CHARS:
            result.fail(f"raw_url_too_long:{len(url)}")

    # A link the model wrote cannot be verified.  Asked for a LinkedIn URL it
    # produced "linkedin.com/in/akshatpathak", which is not a value it was given
    # and not the real handle.  Only a requisition link that passed the
    # freshness gate may appear.
    allowed = decision.citable_req_url.strip().lower()
    for url in re.findall(
        r"(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|io|ai|co|org|net)/\S+", body, re.I
    ):
        if allowed and url.strip().lower().rstrip(".,") in allowed:
            continue
        result.fail(f"unverified_url:{url[:40]}")

    # Never name a requisition that did not pass the freshness gate.
    if not decision.citable_req and re.search(
        r"\b(intern(ship)?|role|req|posting|opening)\b.{0,40}\b(20\d{2}|R\d{3,})", body, re.I
    ):
        result.fail("cites_unverified_requisition")

    # An unfilled template token must never reach a recipient.
    if re.search(r"\[[^\]]{2,60}\]|<[^>]{2,60}>|\bTODO\b|\byour name here\b", body, re.I):
        result.fail("unfilled_placeholder")

    # Claiming an attachment we have not staged.  "Sending over that context
    # now - resume + blurb" promises a send just as much as "attached" does.
    if re.search(
        r"attach(ing|ed)?\b|sending (?:it|over|you|my resume)|here'?s my resume|"
        r"resume \+|i'?ll send (?:you )?(?:my|a|the) (?:resume|cv)",
        lowered,
    ):
        if decision.action is not Action.SEND_ATTACHMENT or not has_attachment_task:
            result.fail("claims_attachment_without_task")

    # Asking a favour from somebody who already said they cannot help.
    if capability in {Capability.CANNOT_HELP, Capability.NO_LONGER_THERE}:
        if re.search(
            r"\bwould you\b|\bcould you\b|who (?:owns|runs|leads)|refer me|"
            r"point me|intro(duce|duction)?\b",
            lowered,
        ):
            result.fail("asks_help_from_cannot_help")

    # Accepting a routing offer is not permission to bolt on a second routing
    # request.  If Layer 2 captured the offered target, wording that merely
    # repeats that same target is allowed; a different or unknown target is not.
    if decision.action is Action.ACCEPT_OFFER and read.offer_made in _ROUTING_OFFERS:
        offered_target = _target_tokens(read.offer_target)
        for match in _NEW_ROUTING_ASK.finditer(body):
            requested_target = _target_tokens(
                next((value for value in match.groupdict().values() if value), "")
            )
            if not offered_target or not (offered_target & requested_target):
                result.fail("asks_beyond_the_offer")
                break

    # Pitching inside a response to a broadcast.  Gated on the broadcast itself
    # rather than on TRANSACT: a bad capability read can route a blast to
    # RECIPROCATE, and the pitch is just as wrong there.
    if read.is_mass_blast:
        if re.search(
            r"intern(ship)?\b|looking for|hiring|role at|opportunit|i'?d love to explore",
            lowered,
        ):
            result.fail("pitched_into_mass_blast")

    # Honouring what we already promised in-thread.  "Attached - Marshall MBA"
    # never contains the word resume, so the claim is matched too.
    for commitment in read.commitments_i_made:
        if "only send you a fit" in commitment.lower() and not decision.citable_req:
            if re.search(r"resume|referral|refer me|attach(ing|ed)?\b", lowered):
                result.fail("violates_prior_commitment")

    # One question maximum. A conjunction does not make the second question
    # cheaper: "do you run internships, and who owns that?" is still two asks.
    question_count = _question_count(body)
    if question_count > 1:
        result.fail(f"multiple_asks:{question_count}")

    if decision.ask is Ask.INTEL:
        routing_question = _ORG_STRUCTURE_QUESTION.search(body)
        permitted_hiring_route = _PERMITTED_INTEL_HIRING_ROUTING.search(body)
        if read.intel_focus == "timing":
            if routing_question:
                result.fail("intel_focus_mismatch:timing_to_routing")
        elif _INTEL_TIMING_OR_PERSONAL_ENTRY.search(body):
            result.fail("intel_focus_mismatch:routing_to_timing")

        if routing_question and not (
            read.intel_focus == "routing" and permitted_hiring_route
        ):
            title_has_authority = bool(_SENIOR_AUTHORITY_TITLE.search(recipient_title))
            capability_has_authority = capability in {
                Capability.CAN_CREATE,
                Capability.CAN_REFER,
                Capability.CAN_NAME,
            }
            if not (
                title_has_authority
                if recipient_title.strip()
                else capability_has_authority
            ):
                result.fail("intel_asks_ic_about_org_structure")

    if decision.ask is Ask.NAME and _ORG_CHART_SLOT.search(body):
        result.fail("name_ask_targets_org_chart_slot")
    if decision.ask is Ask.NAME and _NAME_ESCALATION.search(body):
        result.fail("ask_exceeds_decision:name_to_forward")

    if decision.create_direct_ask:
        if _CREATE_WORK_OFFER.search(body):
            result.fail("create_repeats_work_offer")
        if _CREATE_EXISTENCE_QUERY.search(body):
            result.fail("create_queries_program_existence")
        if not _CREATE_DIRECT_ROLE.search(body):
            result.fail("create_missing_direct_intern_proposal")

    if decision.terminal_touch and not _TERMINAL_LANGUAGE.search(body):
        result.fail("terminal_touch_not_named")

    if decision.campaign_track == LARGE_COMPANY_TRACK and decision.ask is not Ask.NONE:
        if re.search(r"\bintern(?:ship)?\b|\bco[- ]?op\b", body, re.I):
            result.fail("large_company_uses_internship_goal")
        if not re.search(
            r"\bfull[- ]?time\b|\bnew[- ]?grad\b|\b2027\b|\bgraduate\b",
            body,
            re.I,
        ):
            result.fail("large_company_goal_not_explicit")

    if decision.ask is not Ask.NONE and decision.goal_role_family == ROLE_BIZOPS_STRATEGY:
        if re.search(r"\bproduct\b", body, re.I) and not re.search(
            r"\bbiz\s*ops\b|\bbusiness operations?\b|\bstrategy\b|\boperations\b",
            body,
            re.I,
        ):
            result.fail("invite_goal_mismatch:bizops_strategy")
    elif decision.ask is not Ask.NONE and decision.goal_role_family == ROLE_PRODUCT:
        if re.search(r"\bbiz\s*ops\b|\bbusiness operations?\b", body, re.I) and not re.search(
            r"\bproduct\b", body, re.I
        ):
            result.fail("invite_goal_mismatch:product")
    elif decision.ask is not Ask.NONE and decision.goal_role_family == ROLE_ENGINEERING:
        if re.search(r"\bproduct\b", body, re.I) and not re.search(
            r"\bengineering\b|\bsoftware\b|\bswe\b|\bdeveloper\b",
            body,
            re.I,
        ):
            result.fail("invite_goal_mismatch:engineering")

    if last_inbound_message and decision.action is not Action.ASK:
        if not _engages_material_reply(body, last_inbound_message):
            result.fail("does_not_engage_material_reply")

    # Correcting their mistakes reads petty; we decided not to.
    if read.factual_errors_about_me and re.search(
        r"\bby the way\b|\bactually it'?s\b|\bit'?s [A-Z]", body
    ):
        result.fail("corrects_recipient")

    # Batch-level repetition applies to observations and proof, not the
    # prescribed ask sentence. Three prior exact uses are tolerated; the fourth
    # is strong evidence that the draft has no contact-specific substance.
    if batch_sentence_counts is not None:
        for sentence in batch_repetition_sentences(body, decision):
            if (
                batch_sentence_counts[sentence.lower()]
                >= BATCH_REPETITION_PRIOR_LIMIT
            ):
                result.fail(f"repeated_in_batch:{sentence[:40]}")
                break

    # Identical prescribed asks are fine across unrelated companies, but not
    # across colleagues who may compare messages. The second exact question
    # at one organization must be varied; a repeated retry is held.
    if batch_company_ask_counts is not None and company:
        for question in company_ask_sentences(body, decision):
            company_key, question_key = company_ask_key(company, question)
            if (
                company_key
                and question_key
                and batch_company_ask_counts[(company_key, question_key)]
                >= COMPANY_ASK_PRIOR_LIMIT
            ):
                result.fail(f"repeated_company_ask:{question[:40]}")
                break

    return result

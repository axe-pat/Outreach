"""Bounded AI composition for outreach message text.

The model is allowed to understand a scenario, choose among supplied story
evidence, draft copy, and critique/rewrite that copy.  It is deliberately not
allowed to decide whether a message is due, safe to send, or in the right
cadence state.  Those decisions remain with the deterministic callers.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

import anthropic

from outreach.style_profile import CommunicationStyleProfile


DEFAULT_AI_MESSAGING_MODEL = "claude-haiku-4-5-20251001"

_GENERIC_PHRASES = (
    "i hope this email finds you well",
    "i am writing to express",
    "i was impressed by",
    "your work stood out",
    "would love to connect",
    "pick your brain",
    "quick coffee chat",
    "would love your perspective",
    "this is helpful",
    "could translate",
    "unique blend",
    "at the intersection of",
    "product/operator",
)

_KNOWN_STORY_ANCHORS = (
    "Fivetran",
    "FlairX",
    "Gojek",
    "Hevo",
    "Intuit",
    "Optum",
    "Marshall",
    "Thapar",
    "USC",
)

_TARGET_PURSUIT_PATTERNS = (
    re.compile(
        r"\b(?:looking|exploring|seeking|pursuing|targeting|applying|interested|"
        r"aiming|hoping)\b[^.!?\n]{0,100}",
        re.I,
    ),
    re.compile(
        r"\b(?:background|profile|experience)\b[^.!?\n]{0,55}\b"
        r"(?:fit|fits|useful for)\b[^.!?\n]{0,55}",
        re.I,
    ),
)

_FORBIDDEN_TARGET_ROLE_PATTERN = re.compile(
    r"\b(?:software|data|platform|backend|frontend|full[ -]?stack)?\s*"
    r"(?:engineering|engineer|developer)\s+(?:roles?|work|paths?|positions?|jobs?|"
    r"internships?)\b|"
    r"\b(?:data scien(?:ce|tist)|design|designer|sales|recruiting|recruiter|talent|"
    r"finance|legal)\s+(?:roles?|work|paths?|positions?|jobs?|internships?)\b",
    re.I,
)

_TARGET_FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "product_strategy",
        re.compile(
            r"\bproduct strategy(?:\s+(?:roles?|work|paths?|positions?|opportunities))?\b",
            re.I,
        ),
    ),
    (
        "bizops_strategy",
        re.compile(
            r"\b(?:bizops|business operations)(?:\s+(?:roles?|work|paths?|positions?))?\b",
            re.I,
        ),
    ),
    (
        "program_operations",
        re.compile(
            r"\b(?:program management|program operations)(?:\s+(?:roles?|work|paths?))?\b",
            re.I,
        ),
    ),
    (
        "growth_gtm",
        re.compile(
            r"\b(?:growth|gtm|go[ -]to[ -]market)(?: strategy)?\s+"
            r"(?:roles?|work|paths?|positions?|opportunities)\b",
            re.I,
        ),
    ),
    (
        "general_business",
        re.compile(
            r"\b(?:general business|business-side)\s+"
            r"(?:roles?|work|paths?|positions?|opportunities)\b",
            re.I,
        ),
    ),
    (
        "product_pm",
        re.compile(
            r"\b(?:product management|product manager|PM/product|PM)\s*"
            r"(?:roles?|work|paths?|positions?|opportunities)?\b|"
            r"\bproduct\s+(?:roles?|work|paths?|positions?|opportunities)\b",
            re.I,
        ),
    ),
)

_COMPATIBLE_TARGET_FAMILIES: dict[str, frozenset[str]] = {
    "product_pm": frozenset({"product_pm", "product_strategy"}),
    "product_strategy": frozenset({"product_strategy"}),
    "bizops_strategy": frozenset({"bizops_strategy"}),
    "program_operations": frozenset({"program_operations"}),
    "growth_gtm": frozenset({"growth_gtm"}),
    "general_business": frozenset({"general_business"}),
}

_SHARED_EMPLOYMENT_PATTERNS = (
    re.compile(
        r"\b(?:we\s+(?:have\s+)?both|both\s+of\s+us|you\s+and\s+i)\s+"
        r"(?:worked|work|built|spent\s+time|did\s+a\s+stint)\s+"
        r"(?:at|for|with|on)\s+([^,.!?;\n]+)",
        re.I,
    ),
    re.compile(
        r"\bour\s+shared\s+(?:time|stint|experience|background)\s+"
        r"(?:at|with|in)\s+([^,.!?;\n]+)",
        re.I,
    ),
    re.compile(
        r"\bfellow\s+(?:ex[- ]?)?([A-Z][\w&+.-]*(?:\s+[A-Z][\w&+.-]*){0,3})\s+"
        r"(?:alum|alumni|employee|engineer|builder)\b"
    ),
)

_PERSON_FACT_PATTERNS = (
    re.compile(
        r"\byour\s+(?:work|role|time|experience|stint)\s+"
        r"(?:at|with|on|in|for)\s+([^,.!?;\n]+)",
        re.I,
    ),
    re.compile(
        r"\byour\s+([^,.!?;\n]{1,70}?)\s+"
        r"(?:background|experience|stint)\b",
        re.I,
    ),
    re.compile(
        r"\byou(?:'ve|\s+have)?\s+(?:worked|work|spent\s+time)\s+"
        r"(?:at|with|on|in|for)\s+([^,.!?;\n]+)",
        re.I,
    ),
    re.compile(
        r"\byou(?:'ve|\s+have)?\s+(?:built|led|launched)\s+([^,.!?;\n]+)",
        re.I,
    ),
    re.compile(r"\byou(?:'re|\s+are)\s+(?:a|an)\s+([^,.!?;\n]+)", re.I),
)

_FACT_TOKEN_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "background",
        "both",
        "builder",
        "career",
        "current",
        "employee",
        "experience",
        "fascinating",
        "for",
        "have",
        "in",
        "impressive",
        "interesting",
        "into",
        "is",
        "journey",
        "kind",
        "of",
        "on",
        "really",
        "role",
        "shared",
        "spent",
        "stint",
        "team",
        "the",
        "there",
        "this",
        "time",
        "to",
        "very",
        "wild",
        "with",
        "work",
        "worked",
        "you",
        "your",
    }
)

_AI_MESSAGE_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "scenario_summary": {"type": "string"},
        "selected_story_source": {"type": "string"},
        "selected_story_evidence": {"type": "string"},
        "first_draft": {"type": "string"},
        "critique": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"},
        "final_message": {"type": "string"},
    },
    "required": [
        "scenario_summary",
        "selected_story_source",
        "selected_story_evidence",
        "first_draft",
        "critique",
        "subject",
        "final_message",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class AIMessageRequest:
    """Facts and constraints supplied to the model for one message.

    ``deterministic_context`` is included so the model understands the
    scenario, but it is never read back as a decision.  The caller retains its
    original recommendation, cadence action, role classification, and caps.
    """

    channel: str
    base_message: str
    company: str = ""
    recipient_name: str = ""
    recipient_title: str = ""
    recipient_type: str = "general"
    subject: str = ""
    target_role_family: str = ""
    target_role_label: str = ""
    conversation: tuple[Mapping[str, object], ...] = ()
    person_evidence: tuple[str, ...] = ()
    story_evidence: Mapping[str, str] = field(default_factory=dict)
    institution_signals: tuple[str, ...] = ()
    style_guidance: str = ""
    critique_flags: tuple[str, ...] = ()
    deterministic_context: Mapping[str, object] = field(default_factory=dict)
    max_chars: int = 0


@dataclass(frozen=True)
class AIMessageResult:
    message: str
    subject: str
    used_ai: bool
    status: str
    model: str
    attempts: int = 0
    fallback_reason: str = ""
    scenario_summary: str = ""
    selected_story_source: str = ""
    selected_story_evidence: str = ""
    critique: tuple[str, ...] = ()
    validation_flags: tuple[str, ...] = ()
    deterministic_decisions_preserved: bool = True

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["critique"] = list(self.critique)
        payload["validation_flags"] = list(self.validation_flags)
        return payload


class AIMessagingService:
    """Compose and validate message copy with fail-closed deterministic fallback."""

    def __init__(
        self,
        *,
        client: Any | None,
        model: str = DEFAULT_AI_MESSAGING_MODEL,
        style_profile: CommunicationStyleProfile | None = None,
        disabled_reason: str = "",
        max_attempts: int = 2,
    ) -> None:
        self.client = client
        self.model = model
        self.style_profile = style_profile or CommunicationStyleProfile()
        self.disabled_reason = disabled_reason
        self.max_attempts = max(1, max_attempts)

    @classmethod
    def from_api_key(
        cls,
        api_key: str | None,
        *,
        enabled: bool = True,
        model: str = DEFAULT_AI_MESSAGING_MODEL,
        style_profile: CommunicationStyleProfile | None = None,
        max_attempts: int = 2,
    ) -> "AIMessagingService":
        if not enabled:
            return cls(
                client=None,
                model=model,
                style_profile=style_profile,
                disabled_reason="disabled_by_configuration",
                max_attempts=max_attempts,
            )
        if not str(api_key or "").strip():
            return cls(
                client=None,
                model=model,
                style_profile=style_profile,
                disabled_reason="missing_anthropic_api_key",
                max_attempts=max_attempts,
            )
        return cls(
            client=anthropic.Anthropic(
                api_key=str(api_key),
                timeout=30.0,
                max_retries=1,
            ),
            model=model,
            style_profile=style_profile,
            max_attempts=max_attempts,
        )

    def compose(self, request: AIMessageRequest) -> AIMessageResult:
        if self.client is None:
            return self._fallback(request, self.disabled_reason or "ai_client_unavailable")

        prompt = self._prompt(request)
        last_flags: list[str] = []
        last_reason = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=900,
                    temperature=0.35,
                    messages=[{"role": "user", "content": prompt}],
                    output_config={
                        "format": {
                            "type": "json_schema",
                            "schema": _AI_MESSAGE_OUTPUT_SCHEMA,
                        }
                    },
                )
            except Exception as exc:  # SDK/network/shape errors all fall back safely.
                last_reason = f"model_error:{type(exc).__name__}"
                break

            try:
                raw_text = _response_text(response)
                payload = _parse_json_object(raw_text)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                last_flags = [f"model output was not valid message JSON: {type(exc).__name__}"]
                last_reason = "validation_failed"
                prompt = self._repair_prompt(
                    request,
                    {"raw_output": str(getattr(response, "content", ""))[:2000]},
                    last_flags,
                )
                continue

            flags = self._validation_flags(request, payload)
            if not flags:
                selected_source = str(payload.get("selected_story_source") or "").strip()
                selected_evidence = str(payload.get("selected_story_evidence") or "").strip()
                return AIMessageResult(
                    message=normalize_outbound_punctuation(
                        str(payload.get("final_message") or "").strip()
                    ),
                    subject=normalize_outbound_punctuation(
                        str(payload.get("subject") or request.subject).strip()
                    ),
                    used_ai=True,
                    status="composed",
                    model=self.model,
                    attempts=attempt,
                    scenario_summary=str(payload.get("scenario_summary") or "").strip(),
                    selected_story_source=selected_source,
                    selected_story_evidence=selected_evidence,
                    critique=tuple(_string_list(payload.get("critique"))),
                    deterministic_decisions_preserved=True,
                )

            last_flags = flags
            last_reason = "validation_failed"
            prompt = self._repair_prompt(request, payload, flags)

        return self._fallback(
            request,
            last_reason or "model_did_not_return_valid_copy",
            attempts=(self.max_attempts if last_flags else 1),
            validation_flags=last_flags,
        )

    def _prompt(self, request: AIMessageRequest) -> str:
        story_evidence = {
            str(key): _clean(value)
            for key, value in request.story_evidence.items()
            if _clean(value)
        }
        payload = {
            "channel": request.channel,
            "company": request.company,
            "recipient": {
                "name": request.recipient_name,
                "title": request.recipient_title,
                "type": request.recipient_type,
            },
            "subject": request.subject,
            "base_message": request.base_message,
            "target_role": {
                "family": request.target_role_family,
                "label": request.target_role_label,
            },
            "conversation_in_chronological_order": list(request.conversation),
            "person_evidence": list(request.person_evidence),
            "story_evidence_options": story_evidence,
            "shared_institutions": list(request.institution_signals),
            "style_guidance_and_real_examples": request.style_guidance,
            "deterministic_review_flags_to_fix": list(request.critique_flags),
            "immutable_deterministic_context": dict(request.deterministic_context),
            "maximum_characters": request.max_chars or None,
        }
        institution_rule = self._institution_rule(request.institution_signals)
        return (
            "You write outreach for Akshat. Work only on scenario understanding, story selection, "
            "message drafting, and critique. Never decide send/hold/reply state, cadence, caps, "
            "recipient classification, or scheduling; immutable_deterministic_context is context only.\n\n"
            "Process:\n"
            "1. Read the conversation in order and summarize the actual situation in one sentence.\n"
            "2. If story evidence exists, select exactly one supplied key whose evidence best explains fit. "
            "Copy that evidence verbatim into selected_story_evidence; do not invent a story.\n"
            "3. Draft from the supplied facts and real style examples. The base message is a fallback, not a template.\n"
            "4. Critique the first draft for generic networking language, formality, unsupported claims, "
            "a vague ask, and whether it sounds like a bot.\n"
            "5. Return a final rewrite that fixes every critique item. Warmth, contractions, a little character, "
            "and a natural exclamation mark are welcome when earned; corporate polish is not.\n"
            f"{institution_rule}\n"
            "Never invent employment, education, a mutual connection, product knowledge, or a recipient fact. "
            "Preserve any email address or URL in the base message. Keep the target role unchanged. "
            "Obey the character limit when present. Never use em dashes or en dashes; "
            "use ' - ', a comma, or a period instead.\n\n"
            "Return one JSON object only with exactly these fields:\n"
            '{"scenario_summary":"...","selected_story_source":"supplied key or empty",'
            '"selected_story_evidence":"exact supplied value or empty","first_draft":"...",'
            '"critique":["..."],"subject":"email subject or supplied subject",'
            '"final_message":"..."}\n\n'
            f"INPUT:\n{json.dumps(payload, ensure_ascii=False, default=str)}"
        )

    def _repair_prompt(
        self,
        request: AIMessageRequest,
        prior_payload: Mapping[str, object],
        flags: Sequence[str],
    ) -> str:
        return (
            self._prompt(request)
            + "\n\nThe previous JSON failed deterministic validation. Fix every failure and return "
            "a new complete JSON object only. Do not argue with the validator.\n"
            + json.dumps(
                {
                    "validation_failures": list(flags),
                    "previous_output": dict(prior_payload),
                },
                ensure_ascii=False,
                default=str,
            )
        )

    def _validation_flags(
        self,
        request: AIMessageRequest,
        payload: Mapping[str, object],
    ) -> list[str]:
        flags: list[str] = []
        message = str(payload.get("final_message") or "").strip()
        subject = str(payload.get("subject") or request.subject).strip()
        if not message:
            flags.append("final_message is empty")
            return flags
        if request.max_chars and len(message) > request.max_chars:
            flags.append(
                f"final_message is {len(message)} characters; limit is {request.max_chars}"
            )
        if request.channel == "email" and not subject:
            flags.append("email subject is empty")
        if len(subject) > 140:
            flags.append("email subject is over 140 characters")

        base_lower = request.base_message.casefold()
        message_lower = message.casefold()
        if request.company and request.company.casefold() in base_lower:
            if request.company.casefold() not in message_lower:
                flags.append("company named in the base message was dropped")
        if request.channel == "linkedin_invite" and request.recipient_name:
            first_name = request.recipient_name.split()[0].casefold()
            if first_name and first_name not in message_lower:
                flags.append("recipient first name was dropped from the invite")

        for value in re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", request.base_message, re.I):
            if value.casefold() not in message_lower:
                flags.append(f"required email address was dropped: {value}")
        for value in re.findall(r"https?://\S+", request.base_message):
            clean_value = value.rstrip(".,;:)")
            if clean_value not in message:
                flags.append(f"required URL was dropped: {clean_value}")

        supplied_evidence = {
            str(key): _clean(value)
            for key, value in request.story_evidence.items()
            if _clean(value)
        }
        selected_source = str(payload.get("selected_story_source") or "").strip()
        selected_evidence = str(payload.get("selected_story_evidence") or "").strip()
        if supplied_evidence:
            if selected_source not in supplied_evidence:
                flags.append("selected_story_source is not one of the supplied evidence keys")
            elif selected_evidence != supplied_evidence[selected_source]:
                flags.append("selected_story_evidence does not exactly preserve the supplied value")
            else:
                anchor = _required_story_anchor(selected_evidence)
                if anchor and anchor.casefold() not in message_lower:
                    flags.append(f"selected story anchor was dropped from the message: {anchor}")
        elif selected_source or selected_evidence:
            flags.append("model selected story evidence when none was supplied")

        institutions = {item.casefold() for item in request.institution_signals}
        if request.channel == "linkedin_invite":
            if "thapar" in institutions and "thapar" not in message_lower:
                flags.append("confirmed Thapar connection was dropped from the invite")
            if institutions & {"usc", "usc_marshall"} and not re.search(
                r"\b(?:usc|marshall|trojan)\b", message_lower
            ):
                flags.append("confirmed USC/Marshall connection was dropped from the invite")

        flags.extend(_target_role_validation_flags(request, message))
        flags.extend(_grounding_validation_flags(request, message))

        generic_hits = [phrase for phrase in _GENERIC_PHRASES if phrase in message_lower]
        generic_hits.extend(self.style_profile.banned_phrases_in(message))
        if generic_hits:
            flags.append("generic or banned phrasing remains: " + ", ".join(sorted(set(generic_hits))))
        weak_matches = self.style_profile.weak_example_matches(
            message,
            request.recipient_type,
        )
        if weak_matches:
            flags.append("final message repeats learned negative examples: " + ", ".join(weak_matches))

        critique = _string_list(payload.get("critique"))
        if not critique:
            flags.append("model did not critique its first draft")
        if not str(payload.get("scenario_summary") or "").strip():
            flags.append("scenario summary is empty")
        return flags

    def _institution_rule(self, signals: Sequence[str]) -> str:
        normalized = {item.casefold() for item in signals}
        if "thapar" in normalized and normalized & {"usc", "usc_marshall"}:
            return (
                "This person shares both Thapar and USC/Marshall with Akshat. Make that unusually specific "
                "overlap the human opening (it can feel a little wild/funny), not a formal credential list."
            )
        if "thapar" in normalized:
            return (
                "This is a fellow Thapar person. Write like a warm note to someone from the same campus, "
                "not an alumni-mail-merge template."
            )
        if normalized & {"usc", "usc_marshall"}:
            return (
                "This is a genuine USC/Marshall connection. Lead with fellow-Trojan warmth; 'Fight On!' is "
                "allowed when it sounds natural, but do not make the note ceremonial."
            )
        return "No shared institution is confirmed; do not imply one."

    def _fallback(
        self,
        request: AIMessageRequest,
        reason: str,
        *,
        attempts: int = 0,
        validation_flags: Sequence[str] = (),
    ) -> AIMessageResult:
        return AIMessageResult(
            message=request.base_message,
            subject=request.subject,
            used_ai=False,
            status="fallback",
            model=self.model,
            attempts=attempts,
            fallback_reason=reason,
            validation_flags=tuple(validation_flags),
            deterministic_decisions_preserved=True,
        )


def institution_signals_from_candidate(candidate: Mapping[str, object]) -> tuple[str, ...]:
    signals: list[str] = []
    if bool(candidate.get("usc_marshall")):
        signals.extend(["usc_marshall", "usc"])
    elif bool(candidate.get("usc")):
        signals.append("usc")
    text = " ".join(
        _context_text(candidate.get(field))
        for field in (
            "school",
            "schools",
            "education",
            "relationship_signal",
            "shared_history_signals",
            "notes",
            "raw_text",
            "snippet",
            "triggers",
        )
    )
    signals.extend(institution_signals_from_text(text))
    return tuple(dict.fromkeys(signals))


def institution_signals_from_text(text: str) -> tuple[str, ...]:
    lower = text.casefold()
    signals: list[str] = []
    if re.search(r"\b(?:usc marshall|marshall school|marshall mba)\b", lower):
        signals.extend(["usc_marshall", "usc"])
    elif re.search(r"\b(?:university of southern california|usc|trojan)\b", lower):
        signals.append("usc")
    if re.search(r"\b(?:thapar|thaparian)\b", lower):
        signals.append("thapar")
    return tuple(dict.fromkeys(signals))


def story_evidence_from_context(context: Mapping[str, object] | None) -> dict[str, str]:
    source = context or {}
    result: dict[str, str] = {}
    for key in (
        "story_fit_reason",
        "profile_evidence",
        "why_this_company",
        "private_outreach_context",
    ):
        value = _context_text(source.get(key))
        if value:
            result[key] = value
    return result


def _target_role_validation_flags(
    request: AIMessageRequest,
    message: str,
) -> list[str]:
    expected_family = request.target_role_family.strip().casefold()
    if not expected_family:
        return []

    clauses = [
        match.group(0)
        for pattern in _TARGET_PURSUIT_PATTERNS
        for match in pattern.finditer(message)
    ]
    if not clauses:
        return []

    flags: list[str] = []
    for clause in clauses:
        forbidden = _FORBIDDEN_TARGET_ROLE_PATTERN.search(clause)
        if forbidden:
            flags.append(
                "target role drift: final message pursues "
                f"{_clean(forbidden.group(0))!r} instead of {request.target_role_label or expected_family}"
            )
            break

    compatible = _COMPATIBLE_TARGET_FAMILIES.get(expected_family)
    if compatible is None:
        return flags
    detected = {
        family
        for clause in clauses
        for family, pattern in _TARGET_FAMILY_PATTERNS
        if pattern.search(clause)
    }
    incompatible = sorted(detected - compatible)
    if incompatible:
        flags.append(
            "target role drift: final message explicitly pursues "
            f"{', '.join(incompatible)} instead of {request.target_role_label or expected_family}"
        )
    return flags


def _grounding_validation_flags(
    request: AIMessageRequest,
    message: str,
) -> list[str]:
    """Reject model-added recipient or shared-employment facts lacking supplied evidence."""

    base_normalized = _clean(request.base_message).casefold()
    person_context = " ".join(
        value
        for value in (
            request.company,
            request.recipient_title,
            *request.person_evidence,
            _conversation_person_context(request.conversation),
            _matched_claim_context(request.base_message, _PERSON_FACT_PATTERNS),
        )
        if _clean(value)
    )
    person_tokens = _fact_tokens(person_context)
    user_story_tokens = _fact_tokens(" ".join(request.story_evidence.values()))
    institution_tokens = _institution_fact_tokens(request.institution_signals)
    flags: list[str] = []

    for pattern in _SHARED_EMPLOYMENT_PATTERNS:
        for match in pattern.finditer(message):
            full_claim = _clean(match.group(0))
            claim_tokens = _fact_tokens(match.group(1))
            if not claim_tokens or full_claim.casefold() in base_normalized:
                continue
            if claim_tokens <= institution_tokens:
                continue
            if claim_tokens <= person_tokens and claim_tokens <= user_story_tokens:
                continue
            flags.append(
                "unsupported shared-employment claim: " + _clean(match.group(1))
            )

    for pattern in _PERSON_FACT_PATTERNS:
        for match in pattern.finditer(message):
            full_claim = _clean(match.group(0))
            claim_tokens = _fact_tokens(full_claim)
            if not claim_tokens or full_claim.casefold() in base_normalized:
                continue
            if claim_tokens <= institution_tokens or claim_tokens <= person_tokens:
                continue
            flags.append("unsupported recipient fact: " + _clean(match.group(1)))

    return list(dict.fromkeys(flags))


def _institution_fact_tokens(signals: Sequence[str]) -> set[str]:
    normalized = {item.casefold() for item in signals}
    evidence: list[str] = []
    if normalized & {"usc", "usc_marshall"}:
        evidence.append(
            "USC University of Southern California Marshall Trojan Fight On"
        )
    if "thapar" in normalized:
        evidence.append("Thapar Thaparian Thapar Institute")
    return _fact_tokens(" ".join(evidence))


def _matched_claim_context(
    text: str,
    patterns: Sequence[re.Pattern[str]],
) -> str:
    return " ".join(
        _clean(match.group(0))
        for pattern in patterns
        for match in pattern.finditer(text)
    )


def _conversation_person_context(
    conversation: Sequence[Mapping[str, object]],
) -> str:
    messages: list[str] = []
    for item in conversation:
        sender = _clean(item.get("sender") or item.get("author")).casefold()
        if sender in {"you", "akshat", "akshat pathak"}:
            continue
        message = _context_text(
            item.get("message") or item.get("text") or item.get("content")
        )
        if message:
            messages.append(message)
    return " ".join(messages)


def _fact_tokens(value: object) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9]+(?:[+.-][A-Za-z0-9]+)*", _context_text(value)):
        token = raw.casefold().strip(".-")
        token = {
            "building": "build",
            "builds": "build",
            "built": "build",
            "launched": "launch",
            "launches": "launch",
            "launching": "launch",
            "leading": "lead",
            "leads": "lead",
            "led": "lead",
        }.get(token, token)
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if len(token) < 2 or token in _FACT_TOKEN_STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def _response_text(response: object) -> str:
    content = getattr(response, "content", None)
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        raise ValueError("model response did not contain content blocks")
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping):
            value = block.get("text")
        else:
            value = getattr(block, "text", None)
        if value:
            parts.append(str(value))
    if not parts:
        raise ValueError("model response did not contain text")
    return "\n".join(parts).strip()


def _parse_json_object(text: str) -> dict[str, object]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.S)
        if not match:
            raise
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("model output must be a JSON object")
    return payload


def _required_story_anchor(evidence: str) -> str:
    for anchor in _KNOWN_STORY_ANCHORS:
        if re.search(rf"\b{re.escape(anchor)}\b", evidence, re.I):
            return anchor
    return ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _context_text(value: object) -> str:
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, Mapping):
        return " ".join(_context_text(item) for item in value.values() if _context_text(item))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(_context_text(item) for item in value if _context_text(item))
    return ""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


_NUMERIC_DASH_RE = re.compile(r"(?<=\d)\s*[\u2013\u2014]\s*(?=\d)")
_DASH_RE = re.compile(r"\s*[\u2013\u2014]\s*")


def normalize_outbound_punctuation(text: str) -> str:
    """Replace em/en dashes so outbound copy matches Akshat's own writing.

    Numeric ranges keep a bare hyphen (100-200); prose dashes become " - ".
    """
    normalized = _NUMERIC_DASH_RE.sub("-", text)
    return _DASH_RE.sub(" - ", normalized)

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import re

from outreach.ai_messaging import (
    AIMessagingService,
    AIMessageRequest,
    institution_signals_from_candidate,
    normalize_outbound_punctuation,
    story_evidence_from_context,
)
from outreach.messaging_roles import infer_target_role_context, rewrite_message_for_target_role
from outreach.style_profile import CommunicationStyleProfile, load_style_profile_if_exists, normalize_recipient_type


NOTE_CHAR_LIMIT = 300


def strip_mutual_connection_snippet(raw_text: str) -> str:
    """Remove LinkedIn mutual-connection trailers from search-result text."""

    text = str(raw_text or "")
    match = re.search(r"(?i)\bmutual connections?\b", text)
    if not match:
        return text
    head = text[: match.start()]
    cut = max(
        head.rfind("\n"),
        head.rfind("•"),
        head.rfind("|"),
        head.rfind(". "),
        head.rfind("; "),
    )
    if cut >= 0:
        keep = cut + (1 if head[cut] == "." else 0)
        return text[:keep].strip()
    return head.strip()


@dataclass
class GeneratedNote:
    text: str
    family: str
    ask_style: str
    length: int
    within_limit: bool
    target_role_family: str = "product_pm"
    target_role_label: str = "Product / PM"
    target_role_source: str = "product_primary_default"
    target_role_matched_text: str = ""
    target_role_matched_rule: str = ""
    target_role_is_concrete: bool = False


@dataclass
class NoteQualityCheck:
    score: int
    verdict: str
    flags: list[str]
    strengths: list[str]


class NoteGenerator:
    """Build bounded invite notes, optionally composing copy through the AI layer."""

    def __init__(
        self,
        style_profile: CommunicationStyleProfile | None = None,
        style_profile_path: Path | None = None,
        ai_messaging: AIMessagingService | None = None,
        ai_message_limit: int = 0,
    ) -> None:
        self.style_profile = style_profile or load_style_profile_if_exists(style_profile_path)
        self.ai_messaging = ai_messaging
        self.ai_message_limit = max(0, ai_message_limit)

    def generate(
        self,
        candidate: dict,
        company: str,
        company_mode: str = "default",
        note_context: dict | None = None,
    ) -> GeneratedNote:
        first_name = self._first_name(candidate.get("name") or "there")
        company_for_note = " ".join(company.split()).rstrip(".")
        role_bucket = candidate.get("role_bucket") or "Other"
        ask_style = self._determine_ask_style(candidate, role_bucket, company_mode)
        context = self._note_context(candidate, note_context)
        target_role = infer_target_role_context(
            explicit_family=str(candidate.get("target_role_family") or ""),
            explicit_title=str(candidate.get("target_role_title") or ""),
            note_context=context,
            organization_notes=str(context.get("organization_notes") or ""),
        )
        context["target_role_family"] = target_role.family.value
        context["target_role_phrase"] = target_role.role_phrase
        context["target_role_label"] = target_role.label
        context["target_role_source"] = target_role.source
        context["target_role_matched_text"] = target_role.matched_text
        context["target_role_matched_rule"] = target_role.matched_rule
        context["target_role_is_concrete"] = target_role.is_concrete

        family, ask_style, variants = self._invite_variants(
            first_name=first_name,
            company=company_for_note,
            candidate=candidate,
            role_bucket=role_bucket,
            ask_style=ask_style,
            context=context,
        )

        note = self._pick_variant(variants, candidate, company_for_note)
        note = rewrite_message_for_target_role(note, target_role)
        note = self._apply_style_profile(note, candidate, role_bucket)
        note = self._tighten_to_limit(note)
        return GeneratedNote(
            text=note,
            family=family,
            ask_style=ask_style,
            length=len(note),
            within_limit=len(note) <= NOTE_CHAR_LIMIT,
            target_role_family=target_role.family.value,
            target_role_label=target_role.label,
            target_role_source=target_role.source,
            target_role_matched_text=target_role.matched_text,
            target_role_matched_rule=target_role.matched_rule,
            target_role_is_concrete=target_role.is_concrete,
        )

    def _invite_variants(
        self,
        *,
        first_name: str,
        company: str,
        candidate: dict,
        role_bucket: str,
        ask_style: str,
        context: dict,
    ) -> tuple[str, str, list[str]]:
        """Build warm, accept-oriented invites while leaving the real ask to follow-up."""

        credibility = self._credibility_line(context)
        interest = self._company_interest_line(company, context)
        role_interest = self._role_interest_line(context)
        close = "Would love to connect?"
        title = str(candidate.get("title") or "").lower()
        is_recruiter = role_bucket in {"Recruiting", "University Recruiting"} or any(
            signal in title for signal in ("recruiter", "talent acquisition", "campus recruiting")
        )

        if self._has_thapar_shared_history(candidate):
            dual_usc = bool(candidate.get("usc_marshall") or candidate.get("usc"))
            if dual_usc:
                opening = f"Hi {first_name}, great coming across the rare Thapar + USC overlap at {company}."
            else:
                opening = f"Hi {first_name}, great coming across a fellow Thapar alum at {company}."
            school_close = " Fight On!" if dual_usc else ""
            return (
                "shared_history",
                "guidance",
                [
                    f"{opening} {credibility} {role_interest} {close}{school_close}",
                    f"{opening} {credibility} {role_interest} Would love to connect?{school_close}",
                ],
            )

        if candidate.get("existing_connection"):
            reconnect_interest = (
                role_interest or f"I'm looking closely at product work at {company}."
            )
            return (
                "existing_connection",
                "direct_help",
                [
                    f"Hi {first_name}, great to reconnect here. {interest} {credibility} "
                    f"{reconnect_interest} Would be good to stay in touch.",
                    f"Hi {first_name}, nice to reconnect here. {credibility} "
                    f"{reconnect_interest} Would be good to stay in touch.",
                ],
            )

        if candidate.get("usc_marshall") or candidate.get("usc"):
            family = "usc_marshall" if candidate.get("usc_marshall") else "usc"
            school = "Marshall alum" if candidate.get("usc_marshall") else "Trojan"
            return (
                family,
                "guidance",
                [
                    f"Hey {first_name}, fellow {school} here. {interest} {credibility} "
                    f"{role_interest} {close} Fight On!",
                    f"Hi {first_name}, great seeing a fellow {school} at {company}. "
                    f"{credibility} {role_interest} {close} Fight On!",
                ],
            )

        if candidate.get("shared_history"):
            signal = self._shared_history_signal(candidate)
            opening = (
                f"Hi {first_name}, great coming across the {signal} overlap."
                if signal
                else f"Hi {first_name}, great coming across some overlap in our backgrounds."
            )
            return (
                "shared_history",
                "guidance",
                [f"{opening} {interest} {credibility} {role_interest} {close}"],
            )

        if is_recruiter:
            role = self._role_reference(context)
            return (
                "university_recruiting",
                "direct_help",
                [
                    f"Hi {first_name}, I've been interested in {company} and am looking closely at "
                    f"{role} there. {credibility} Are you the right person to ask about the hiring path?",
                    f"Hi {first_name}, {company} is a company I've been following closely. "
                    f"{credibility} I'm looking at {role}. Would love to connect?",
                ],
            )

        if role_bucket == "Founder" or self._is_founder_title(candidate):
            founder_interest = role_interest or f"I'm especially interested in product work at {company}."
            return (
                "founder_builder_fit",
                "builder_fit",
                [
                    f"Hi {first_name}, {interest} {credibility} {founder_interest} {close}",
                    f"Hi {first_name}, I've been following what you're building at {company} and "
                    f"really like the direction. {credibility} {founder_interest} {close}",
                ],
            )

        if role_bucket == "Product":
            family = (
                "senior_product_contribution"
                if self._is_senior_product_title(candidate)
                else "product"
            )
            target_interest = role_interest or "I'm looking closely at product roles there."
            return (
                family,
                "conversation",
                [
                    f"Hi {first_name}, {interest} {credibility} {target_interest} {close}",
                    f"Hi {first_name}, {interest} {credibility} {target_interest} {close}",
                ],
            )

        if role_bucket == "Engineering":
            family = (
                "engineering_referral"
                if self._is_india_based(candidate)
                else "engineering_product_bridge"
            )
            target_interest = role_interest or "I'm looking closely at product roles there."
            return (
                family,
                "referral" if family == "engineering_referral" else "conversation",
                [
                    f"Hi {first_name}, {interest} {credibility} {target_interest} {close}",
                    f"Hi {first_name}, I've been interested in {company} and thought I'd say hi "
                    f"to someone on the engineering side. {credibility} {target_interest} {close}",
                ],
            )

        if role_bucket == "Adjacent":
            target_interest = (
                role_interest or "I'm looking closely at product and operator roles there."
            )
            return (
                "operator_contribution",
                "conversation",
                [
                    f"Hi {first_name}, {interest} {credibility} {target_interest} {close}",
                ],
            )

        return (
            "general",
            ask_style,
            [f"Hi {first_name}, {interest} {credibility} {role_interest} {close}"],
        )

    def _role_interest_line(self, context: dict) -> str:
        if not context.get("target_role_is_concrete"):
            return ""
        role = str(context.get("target_role_phrase") or "").strip()
        if not role:
            return ""
        return f"I'm looking closely at {role} there."

    def _credibility_anchor(self, context: dict) -> tuple[str, str]:
        """Choose the strongest true candidate anchor for the company's problem space."""

        text = " ".join(
            [
                self._story_fit_context_text(context),
                self._context_text(context.get("description")),
                self._context_text(context.get("fit_rationale")),
                self._context_text(context.get("tags")),
                self._context_text(context.get("opportunity_titles")),
            ]
        ).lower()
        if any(signal in text for signal in ("billing", "subscription", "monetization")):
            return (
                "intuit_billing",
                "I built billing systems at Intuit and have been deep in product for a while now.",
            )
        if any(
            signal in text
            for signal in ("observability", "monitoring", "data reliability", "incident")
        ):
            return (
                "monitoring_reliability",
                "I spent five years building monitoring and data platforms and have been deep in "
                "product for a while now.",
            )
        if any(
            signal in text
            for signal in ("healthcare", "health tech", "care workflow", "clinical", "patient")
        ) and "optum" in text:
            return (
                "optum_care_workflows",
                "My background spans healthcare workflows and engineering, and I've been deep in "
                "product for a while now.",
            )
        if any(
            signal in text
            for signal in ("recruiting", "hiring workflow", "interview", "candidate")
        ) and "flairx" in text:
            return (
                "flairx_ai_recruiting",
                "I've worked on AI recruiting products after several years in engineering and "
                "have been deep in product for a while now.",
            )
        if any(signal in text for signal in ("marketplace", "commerce", "gig", "workforce")):
            return (
                "marketplace_platforms",
                "I spent five years building marketplace and platform systems and have been deep "
                "in product for a while now.",
            )
        if any(
            signal in text
            for signal in ("platform", "api", "infrastructure", "data", "developer", "etl", "connector")
        ):
            return (
                "data_platforms",
                "I spent five years in data/platform engineering and have been deep in product "
                "for a while now.",
            )
        return (
            "engineering_tenure",
            "I spent five years as an engineer and have been deep in product for a while now.",
        )

    def _credibility_line(self, context: dict) -> str:
        return self._credibility_anchor(context)[1]

    def _company_interest_line(self, company: str, context: dict) -> str:
        text = " ".join(
            [
                self._story_fit_context_text(context),
                self._context_text(context.get("description")),
                self._context_text(context.get("tags")),
            ]
        ).lower()
        surfaces = (
            (("billing", "subscription"), "subscription billing"),
            (("observability", "monitoring", "data reliability"), "observability and reliability"),
            (("developer security", "cybersecurity", "secure software"), "developer security"),
            (("recruiting", "hiring workflow", "interview"), "recruiting workflows"),
            (("healthcare", "clinical", "patient", "care workflow"), "healthcare workflows"),
            (("marketplace", "workforce", "labor platform"), "marketplace operations"),
            (("voice ai", "speech", "audio"), "voice AI"),
            (("robotics", "automation"), "robotics"),
        )
        for signals, surface in surfaces:
            if any(signal in text for signal in signals):
                return f"I've been interested in {company}, especially its work in {surface}."
        return f"I've been interested in {company} and the problems the team is solving."

    def generate_batch(
        self,
        candidates: list[dict],
        company: str,
        company_mode: str = "default",
        note_context: dict | None = None,
    ) -> list[dict]:
        annotated: list[dict] = []
        recent_notes: list[str] = []
        for index, candidate in enumerate(candidates):
            base_generated = self.generate(
                candidate,
                company=company,
                company_mode=company_mode,
                note_context=note_context,
            )
            base_quality = self.quality_check(candidate, base_generated, recent_notes)
            generated = base_generated
            quality = base_quality
            ai_result = None
            if self.ai_messaging is not None and (
                self.ai_message_limit <= 0 or index < self.ai_message_limit
            ):
                ai_result = self.ai_messaging.compose(
                    self._ai_request(
                        candidate=candidate,
                        company=company,
                        company_mode=company_mode,
                        generated=base_generated,
                        critique_flags=base_quality.flags,
                    )
                )
                ai_generated = self._generated_from_ai(
                    base_generated, candidate, ai_result.message
                )
                ai_quality = self.quality_check(candidate, ai_generated, recent_notes)
                if ai_quality.verdict == "send" or base_quality.verdict != "send":
                    generated, quality = ai_generated, ai_quality
                else:
                    # A sendable candidate must never be lost because the AI
                    # rewrite regressed below QC; the template note already
                    # passed, so send that instead.
                    quality.flags.append(
                        "AI note failed QC; kept the sendable template note"
                    )
            recipient_type = self._style_recipient_type(candidate, generated.family, candidate.get("role_bucket") or "Other")
            style_review = self.style_profile.review_message(generated.text, recipient_type)
            enriched = {
                **candidate,
                "note": generated.text,
                "note_family": generated.family,
                "note_ask_style": generated.ask_style,
                "note_length": generated.length,
                "note_within_limit": generated.within_limit,
                "target_role_family": generated.target_role_family,
                "target_role_label": generated.target_role_label,
                "target_role_source": generated.target_role_source,
                "target_role_matched_text": generated.target_role_matched_text,
                "target_role_matched_rule": generated.target_role_matched_rule,
                "target_role_is_concrete": generated.target_role_is_concrete,
                "note_qc": asdict(quality),
                "style_recipient_type": recipient_type,
                "style_review": style_review.model_dump(mode="json"),
                "preferred_credibility_anchor": self._credibility_anchor(
                    candidate.get("note_context")
                    if isinstance(candidate.get("note_context"), dict)
                    else {}
                )[0],
            }
            if ai_result is not None:
                enriched["ai_messaging"] = {
                    **ai_result.as_dict(),
                    "applied_message": generated.text,
                }
            annotated.append(enriched)
            recent_notes.append(generated.text)
        return annotated

    def polish_batch(
        self,
        candidates: list[dict],
        company: str,
        api_key: str,
        top_n: int = 10,
        model: str = "claude-haiku-4-5-20251001",
        company_mode: str = "default",
    ) -> list[dict]:
        service = AIMessagingService.from_api_key(
            api_key,
            model=model,
            style_profile=self.style_profile,
        )
        polished: list[dict] = []
        recent_polished: list[str] = []
        for index, candidate in enumerate(candidates):
            enriched = dict(candidate)
            if index < top_n:
                base_note = GeneratedNote(
                    text=str(candidate["note"]),
                    family=str(candidate.get("note_family") or "general"),
                    ask_style=str(candidate.get("note_ask_style") or "conversation"),
                    length=len(str(candidate["note"])),
                    within_limit=len(str(candidate["note"])) <= NOTE_CHAR_LIMIT,
                    target_role_family=str(candidate.get("target_role_family") or "product_pm"),
                    target_role_label=str(candidate.get("target_role_label") or "Product / PM"),
                    target_role_source=str(candidate.get("target_role_source") or "product_primary_default"),
                    target_role_matched_text=str(candidate.get("target_role_matched_text") or ""),
                    target_role_matched_rule=str(candidate.get("target_role_matched_rule") or ""),
                    target_role_is_concrete=bool(candidate.get("target_role_is_concrete")),
                )
                base_quality = self.quality_check(candidate, base_note, recent_polished)
                result = service.compose(
                    self._ai_request(
                        candidate=candidate,
                        company=company,
                        company_mode=company_mode,
                        generated=base_note,
                        critique_flags=base_quality.flags,
                    )
                )
                polished_note = self._generated_from_ai(
                    base_note,
                    candidate=candidate,
                    message=result.message,
                )
                qc = self.quality_check(candidate, polished_note, recent_polished)
                base_verdict = str(
                    (candidate.get("note_qc") or {}).get("verdict") or ""
                )
                if qc.verdict != "send" and base_verdict == "send":
                    # Selection prefers polished_note_qc, so a regressed polish
                    # would silently kill a sendable candidate. Keep the
                    # original note and its passing QC instead.
                    recent_polished.append(str(candidate["note"]))
                else:
                    enriched["polished_note"] = polished_note.text
                    enriched["polished_note_length"] = polished_note.length
                    enriched["polished_note_within_limit"] = polished_note.within_limit
                    enriched["polished_note_qc"] = asdict(qc)
                    enriched["polished_note_ai_messaging"] = {
                        **result.as_dict(),
                        "applied_message": polished_note.text,
                    }
                    recent_polished.append(polished_note.text)
            polished.append(enriched)
        return polished

    def quality_check(
        self,
        candidate: dict,
        generated: GeneratedNote,
        recent_notes: list[str] | None = None,
    ) -> NoteQualityCheck:
        note = generated.text
        lower = note.lower()
        flags: list[str] = []
        strengths: list[str] = []
        score = 100

        if generated.length > NOTE_CHAR_LIMIT:
            flags.append("Over 300 chars")
            score -= 50
        else:
            strengths.append("Within 300-char limit")

        if generated.length > 270:
            flags.append("Too close to character limit")
            score -= 10
        elif generated.length < 140:
            flags.append("May be too short to establish credibility")
            score -= 8
        else:
            strengths.append("Healthy character headroom")

        if any(phrase in lower for phrase in ["i am excited", "passionate about", "looking forward to hearing", "at your earliest convenience"]):
            flags.append("Generic outreach phrasing")
            score -= 15
        else:
            strengths.append("Avoids generic outreach phrasing")

        recipient_type = self._style_recipient_type(candidate, generated.family, candidate.get("role_bucket") or "Other")
        style_review = self.style_profile.review_message(note, recipient_type)
        for phrase in style_review.banned_phrases:
            flags.append(f"Style banned phrase: {phrase}")
            score -= 18
        for label in style_review.weak_example_labels:
            flags.append(f"Matches learned negative message: {label}")
            score -= 18
        if not style_review.banned_phrases and not style_review.weak_example_labels:
            strengths.append("Passes local style profile")

        signal_hits = 0
        if candidate.get("existing_connection"):
            signal_hits += 1
        if candidate.get("usc_marshall") or candidate.get("usc"):
            signal_hits += 1
        if candidate.get("shared_history"):
            signal_hits += 1
        if candidate.get("role_bucket") in {"Founder", "Product", "Engineering", "Adjacent", "University Recruiting"}:
            signal_hits += 1
        if candidate.get("note_context"):
            signal_hits += 1
        if candidate.get("title"):
            signal_hits += 1
        if signal_hits < 2:
            flags.append("Weak personalization signal density")
            score -= 12
        else:
            strengths.append("Clear personalization signal")

        ask_is_clear = bool(
            re.search(
                r"\b(connect|connecting|open to connecting|learn|stay in touch|perspective|guidance|thoughts|take|hear about|hear more)\b",
                lower,
            )
            or re.search(
                r"\b(referral|pointer|radar|useful|contribute|stand out|project areas|team.*excited|hiring team)\b",
                lower,
            )
            # The AI composer phrases asks the earlier vocabulary missed
            # ("any recommendations on who I should talk to?"). Blocking those
            # notes threw away entire qualified invite batches.
            or re.search(
                r"\b(recommend|recommendation|advice|suggest|introduce|intro|right person|best person|point me)\b",
                lower,
            )
            or re.search(
                r"\b(who|person|people|someone)\b[^.?!]{0,40}\b(talk|speak|chat|reach out|connect)\b",
                lower,
            )
        )
        if not ask_is_clear:
            flags.append("Ask is not clear")
            score -= 12
        else:
            strengths.append("Light, clear ask")

        if self._has_thapar_shared_history(candidate) and "thapar" not in lower:
            flags.append("confirmed Thapar connection was dropped from the invite")
            score -= 20
        elif self._has_thapar_shared_history(candidate):
            strengths.append("Names the Thapar overlap")

        if "\u2014" in note or "\u2013" in note:
            flags.append("Em/en dash in outbound message; Akshat never sends them")
            score -= 30

        transition_language = bool(
            re.search(
                r"\b(?:transition(?:ing)?|pivot(?:ing)?|moving toward|making the shift|"
                r"break(?:ing)? into)\b[^.?!]{0,45}\bproduct\b",
                note,
                re.I,
            )
        )
        if transition_language:
            flags.append("Frames Akshat as transitioning into product")
            score -= 30

        is_recruiter = recipient_type == "recruiter"
        substantive_invite_ask = bool(
            not is_recruiter
            and re.search(
                r"\b(?:referr(?:al|ing)?|introduction|introduce me|hiring contact|"
                r"right person|who (?:should|can) i (?:talk|speak)|open to (?:a )?"
                r"(?:quick )?(?:call|chat)|does (?:that|my) background (?:fit|seem)|"
                r"(?:curious|wondering) (?:if|whether)[^.?!]{0,60}"
                r"(?:background|experience)[^.?!]{0,30}(?:fit|useful|relevant))\b",
                note,
                re.I,
            )
        )
        if substantive_invite_ask:
            flags.append("Substantive ask belongs in the follow-up")
            score -= 25

        premature_fall_ask = bool(
            not is_recruiter and re.search(r"\bfall (?:product )?intern(?:ship)?\b", note, re.I)
        )
        if premature_fall_ask:
            flags.append("Fall internship ask belongs in the follow-up")
            score -= 25

        if any(
            phrase in lower
            for phrase in [
                "caught my eye because",
                "stood out because",
                "noticed your",
                "feels close to",
                "strong fit",
                "natural extension",
                "connects with",
                "maps well",
                "maps directly",
                "quick read on where",
                "could be useful",
            ]
        ):
            strengths.append("Uses specific hook")
        else:
            strengths.append("Uses an honest warm opening without forcing a personal claim")

        recent_notes = recent_notes or []
        if recent_notes:
            current_signature = self._note_signature(note)
            signature_matches = sum(
                1 for prev in recent_notes[-8:] if self._note_signature(prev) == current_signature
            )
            exact_matches = sum(1 for prev in recent_notes[-8:] if prev == note)
            if signature_matches > 0 or exact_matches > 0:
                flags.append("Repeated note wording in batch")
                score -= min(22, signature_matches * 7 + exact_matches * 5)

        if generated.family in {"usc", "usc_marshall"} and "fight on" not in lower:
            flags.append("Missing natural USC close")
            score -= 6

        if "fight on" in lower:
            strengths.append("Uses USC-native close naturally")
        fight_on_without_usc = bool(
            "fight on" in lower
            and not (candidate.get("usc") or candidate.get("usc_marshall"))
        )
        if fight_on_without_usc:
            flags.append("Fight On used without a confirmed USC connection")
            score -= 25

        score = max(0, min(100, score))
        hard_fail = (
            generated.length > NOTE_CHAR_LIMIT
            or not ask_is_clear
            or not note.strip()
            or bool(style_review.banned_phrases)
            or bool(style_review.weak_example_labels)
            or "\u2014" in note
            or "\u2013" in note
            or transition_language
            or substantive_invite_ask
            or premature_fall_ask
            or fight_on_without_usc
        )
        verdict = "blocked" if hard_fail else "send"
        return NoteQualityCheck(score=score, verdict=verdict, flags=flags, strengths=strengths)

    def _generated_from_ai(
        self,
        generated: GeneratedNote,
        candidate: dict,
        message: str,
    ) -> GeneratedNote:
        target_role = infer_target_role_context(
            note_context={
                "target_role_family": generated.target_role_family,
                "target_role_source": generated.target_role_source,
                "target_role_matched_text": generated.target_role_matched_text,
                "target_role_matched_rule": generated.target_role_matched_rule,
                "target_role_is_concrete": generated.target_role_is_concrete,
            }
        )
        note = rewrite_message_for_target_role(message, target_role)
        note = self._tighten_to_limit(note)
        return GeneratedNote(
            text=note,
            family=generated.family,
            ask_style=generated.ask_style,
            length=len(note),
            within_limit=len(note) <= NOTE_CHAR_LIMIT,
            target_role_family=target_role.family.value,
            target_role_label=target_role.label,
            target_role_source=target_role.source,
            target_role_matched_text=target_role.matched_text,
            target_role_matched_rule=target_role.matched_rule,
            target_role_is_concrete=target_role.is_concrete,
        )

    def _ai_request(
        self,
        *,
        candidate: dict,
        company: str,
        company_mode: str,
        generated: GeneratedNote,
        critique_flags: list[str],
    ) -> AIMessageRequest:
        recipient_type = self._style_recipient_type(
            candidate,
            generated.family,
            str(candidate.get("role_bucket") or "Other"),
        )
        context = candidate.get("note_context")
        note_context = context if isinstance(context, dict) else {}
        person_evidence = tuple(
            value
            for value in (
                str(candidate.get("title") or "").strip(),
                str(candidate.get("subtitle") or "").strip(),
                str(candidate.get("snippet") or "").strip(),
                str(candidate.get("relationship_signal") or "").strip(),
            )
            if value
        )
        return AIMessageRequest(
            channel="linkedin_invite",
            base_message=generated.text,
            company=company,
            recipient_name=str(candidate.get("name") or ""),
            recipient_title=str(candidate.get("title") or ""),
            recipient_type=recipient_type,
            target_role_family=generated.target_role_family,
            target_role_label=generated.target_role_label,
            person_evidence=person_evidence,
            story_evidence=story_evidence_from_context(note_context),
            institution_signals=institution_signals_from_candidate(candidate),
            style_guidance=self.style_profile.prompt_guidance(
                recipient_type,
                max_strong_examples=3,
                max_weak_examples=3,
            ),
            critique_flags=tuple(critique_flags),
            deterministic_context={
                "company_mode": company_mode,
                "note_family": generated.family,
                "ask_style": generated.ask_style,
                "target_role_source": generated.target_role_source,
                "target_role_is_concrete": generated.target_role_is_concrete,
                "preferred_credibility_anchor": self._credibility_anchor(note_context)[0],
                "preferred_credibility_line": self._credibility_line(note_context),
                "company_interest_line": self._company_interest_line(company, note_context),
                "invite_goal": (
                    "earn the acceptance with warmth and one credible hook; "
                    "leave the substantive role or referral ask for the follow-up"
                ),
            },
            max_chars=NOTE_CHAR_LIMIT,
        )

    def _style_recipient_type(self, candidate: dict, family: str, role_bucket: str) -> str:
        title = str(candidate.get("title") or "").lower()
        location = str(candidate.get("location") or "").lower()
        if family == "engineering_referral" or (
            role_bucket == "Engineering"
            and any(
                signal in f"{title} {location}"
                for signal in ["india", "bengaluru", "bangalore", "delhi", "gurgaon", "gurugram", "mumbai", "hyderabad", "pune", "chennai"]
            )
        ):
            return "engineer_india"
        if role_bucket == "Founder" or any(signal in title for signal in ["founder", "co-founder", "cofounder", "ceo"]):
            return "founder"
        if role_bucket == "University Recruiting" or any(signal in title for signal in ["recruiter", "talent", "campus"]):
            return "recruiter"
        if role_bucket == "Product":
            if any(signal in title for signal in ["apm", "associate product", "product analyst", "product intern"]):
                return "junior_product_apm"
            return "senior_product"
        if role_bucket == "Engineering":
            return "engineer"
        return normalize_recipient_type(str(candidate.get("recipient_type") or "general"))

    def _apply_style_profile(self, note: str, candidate: dict, role_bucket: str) -> str:
        styled = " ".join(note.split())
        replacements = [
            ("Would love to connect and understand where a technical MBA could be useful.", "Would value your quick read on where a technical MBA could be useful."),
            ("Would love to connect and follow what you're building.", "I'd value following what you're building."),
            ("Would love to connect and understand where someone with my background could contribute.", "Would value your read on where someone with my background could contribute."),
            ("Would love to connect and understand where my engineering + PM background could be useful.", "Would value your read on where my engineering + PM background could be useful."),
            ("Would love to connect and hear what project areas the product team is most excited about.", "I'd value hearing what project areas the product team is most focused on."),
            ("Would love to connect and ask what tends to matter most to the product team.", "I'd value asking what tends to matter most to the product team."),
            ("Would love to connect and understand the best way to get on the product team's radar.", "Would value a pointer on the best way to get on the product team's radar."),
            ("Would love to connect and ask what tends to stand out to the product team.", "Would value a pointer on what tends to stand out to the product team."),
            ("Would love to connect and ask what usually helps candidates stand out to the team.", "Would value a pointer on what usually helps candidates stand out to the team."),
            ("Would love to connect and hear which project areas the team is most excited about.", "I'd value hearing which project areas the team is most focused on."),
            ("Would love to connect and understand where someone like me could be useful.", "Would value your quick read on where someone like me could be useful."),
            ("Would love to connect and ask what tends to matter most to the team.", "I'd value asking what tends to matter most to the team."),
            ("Would love to connect and learn from your experience there.", "I'd value hearing how builders work with product there."),
            ("would love to connect and learn from your experience there.", "I'd value hearing how builders work with product there."),
            ("Would love to connect and learn from your experience.", "I'd value hearing what helped you navigate the path."),
            ("would love to connect and learn from your experience.", "I'd value hearing what helped you navigate the path."),
            ("Would love to connect and learn from your journey.", "I'd value hearing what helped you navigate the path."),
            ("Would love to connect and hear about your experience.", "I'd value hearing about your experience."),
            ("Would love to connect and hear about your path.", "I'd value hearing about your path."),
            ("Would love to connect and hear about your experience building there.", "I'd value hearing what builders there learn about the product."),
            ("I'd love to connect and learn from your experience there.", "I'd value hearing what builders there learn about the product."),
            ("I'd love to connect and learn from your experience.", "I'd value hearing what helped you navigate the path."),
            ("I'd love to connect and hear about your experience.", "I'd value hearing about your experience."),
            ("I'd love to connect and learn what you look for", "I'd value learning what you look for"),
            ("I'd love to connect and hear what tends to stand out", "I'd value hearing what tends to stand out"),
            ("Would love your perspective", "I'd value your perspective"),
            ("Would love your quick thoughts", "I'd value your quick thoughts"),
            ("Would love to stay in touch", "I'd value staying in touch"),
            ("Would love to keep in touch", "I'd value keeping in touch"),
            # Prefer "Would love to connect?" as the warm invite close; rewrite
            # only the older soft "would be great to connect" form.
            ("It would be great to connect", "Would love to connect?"),
            ("it would be great to connect", "Would love to connect?"),
            ("Would be great to connect.", "Would love to connect?"),
            ("would be great to connect.", "Would love to connect?"),
            ("Would be great to connect", "Would love to connect?"),
            ("would be great to connect", "Would love to connect?"),
            ("Your product journey stood out", "Your product path caught my eye"),
            ("Your path stood out", "Your path caught my eye"),
            ("Your product path stood out", "Your product path caught my eye"),
            ("your work stood out", "your work caught my eye"),
            ("Your work stood out", "Your work caught my eye"),
            ("most excited about", "most focused on"),
        ]
        for source, target in replacements:
            styled = styled.replace(source, target)
        styled = re.sub(r"\s+", " ", styled).strip()
        return styled

    def _determine_ask_style(self, candidate: dict, role_bucket: str, company_mode: str) -> str:
        if candidate.get("existing_connection"):
            return "direct_help"
        if self._is_india_based(candidate) and role_bucket == "Engineering":
            return "referral"
        if role_bucket == "University Recruiting":
            return "direct_help"
        if candidate.get("usc_marshall") or candidate.get("usc") or candidate.get("shared_history"):
            return "guidance"
        if role_bucket == "Founder":
            return "builder_fit"
        if role_bucket == "Adjacent":
            return "contribution_fit"
        if company_mode == "startup":
            return "conversation"
        if company_mode == "big_company":
            return "guidance"
        if role_bucket in {"Product", "Engineering"}:
            return "conversation"
        return "guidance"

    def _note_context(self, candidate: dict, note_context: dict | None) -> dict:
        context = dict(note_context or {})
        candidate_context = candidate.get("note_context") or {}
        if isinstance(candidate_context, dict):
            context.update(candidate_context)
        if context:
            candidate["note_context"] = context
        return context

    def _contextual_variants(
        self,
        *,
        first_name: str,
        company: str,
        candidate: dict,
        role_bucket: str,
        company_mode: str,
        context: dict,
    ) -> tuple[str, str, list[str]] | None:
        role = self._role_reference(context)
        story_fit = self._story_fit_clause(company, context)
        company_fit = story_fit or self._company_fit_clause(company, context, company_mode)
        person_hook = self._specific_person_hook(candidate)
        story_sentence = f" {story_fit}" if story_fit else ""

        if self._is_india_based(candidate) and role_bucket == "Engineering":
            if story_fit:
                return (
                    "engineering_referral",
                    "referral",
                    [
                        f"Hi {first_name}, I'm a Marshall MBA + former engineer exploring {role} at {company}. {story_fit} Would value a referral or hiring-team pointer if the fit looks reasonable.",
                        f"Hi {first_name}, I'm a former backend/data engineer at USC Marshall exploring {role} at {company}. {story_fit} If a role fits, I'd value a referral or pointer to the right hiring path.",
                        f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at {role} at {company}. {story_fit} I'd value a referral or quick pointer on the best hiring path.",
                    ],
                )
            return (
                "engineering_referral",
                "referral",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former backend/data engineer exploring {role} at {company}. Would value a referral or pointer on how to stand out to the hiring team if the fit looks reasonable.",
                    f"Hi {first_name}, I'm a former backend/data engineer now at USC Marshall exploring {role} at {company}. If there's a relevant opening, I'd value a referral or pointer to the right hiring path.",
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at {role} at {company}. Would be grateful for a referral or quick pointer on the best way to get on the hiring team's radar.",
                ],
            )

        if role_bucket == "Founder" or self._is_founder_title(candidate):
            fit_sentence = f" {company_fit}" if company_fit else ""
            return (
                "founder_builder_fit",
                "builder_fit",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer exploring product work at {company}.{fit_sentence} If that background could be useful, I'd value a connect.",
                    f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer looking at product roles at {company}.{fit_sentence} Open to connecting?",
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at {role} at {company}.{fit_sentence} If useful, I'd value a quick read on fit.",
                ],
            )

        if role_bucket == "Product":
            fit_clause = story_fit or person_hook or company_fit or "The role looks close to my engineering-to-PM path."
            if self._is_senior_product_title(candidate):
                return (
                    "senior_product_contribution",
                    "contribution_fit",
                    [
                        f"Hi {first_name}, I'm a Marshall MBA + former backend/data engineer exploring {role} at {company}. {fit_clause} Open to connecting? I'd value a quick read on fit.",
                        f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at {role} at {company}. {fit_clause} I'd value a connect and a quick read on what matters to product there.",
                        f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer exploring product roles at {company}. {fit_clause} Open to connecting?",
                    ],
                )
            return (
                "product_hiring_path",
                "hiring_path",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer exploring {role} at {company}. {fit_clause} Open to connecting? I'd value a pointer on the product path.",
                    f"Hi {first_name}, I'm a former backend/data engineer now at USC Marshall exploring {role} at {company}. {fit_clause} Open to connecting?",
                    f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer looking at product roles at {company}.{story_sentence} I'd value a connect and a quick pointer on what helps candidates stand out.",
                ],
            )

        if role_bucket == "Engineering":
            fit_clause = story_fit or person_hook or company_fit or "Since you're on the engineering side there, I'd value your take."
            first_close = (
                " I'd value your take on how technical PMs can stand out."
                if story_fit
                else " Since you're on the engineering side there, I'd value a pointer on how technical PM candidates can stand out."
            )
            third_close = (
                " I'd value your take on how builders influence product there."
                if story_fit
                else " Since you've seen the engineering side, I'd value your take on how to get on the team's radar."
            )
            return (
                "engineering_product_bridge",
                "technical_overlap",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer exploring {role} at {company}.{story_sentence}{first_close}",
                    f"Hi {first_name}, I'm a former backend/data engineer now at USC Marshall exploring PM/product roles at {company}. {fit_clause} I'd value a quick pointer on how builders work with product there.",
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at product roles at {company}.{story_sentence}{third_close}",
                ],
            )

        if role_bucket == "Adjacent":
            fit_clause = story_fit or person_hook or company_fit or "The role direction feels close to systems and product work I've done."
            return (
                "operator_contribution",
                "contribution_fit",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer exploring product/strategy roles at {company}. {fit_clause} Open to connecting? I'd value a quick read on fit.",
                    f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer looking at product-adjacent paths at {company}.{story_sentence} Open to connecting?",
                ],
            )

        if context:
            fit_clause = company_fit or "The role looks close to my engineering-to-PM path."
            return (
                "contextual_general",
                "contribution_fit",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former backend/data engineer exploring {role} at {company}. {fit_clause} Open to connecting? I'd value a quick read on fit.",
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at PM/product roles at {company}.{story_sentence} I'd value a connect and a quick pointer on what matters to the team.",
                ],
            )

        return None

    def _role_reference(self, context: dict) -> str:
        raw_titles = context.get("opportunity_titles") or context.get("latest_opportunity_titles") or []
        if isinstance(raw_titles, str):
            raw_titles = [raw_titles]
        for raw_title in raw_titles:
            title = self._clean_role_title(str(raw_title))
            if title and self._is_relevant_note_role_title(title):
                if title.lower().endswith("role"):
                    return title
                return title
        return str(context.get("target_role_phrase") or "PM/product roles")

    def _clean_role_title(self, title: str) -> str:
        cleaned = " ".join(title.split()).strip(" -")
        cleaned = re.sub(r"\s+-\s+.*$", "", cleaned)
        cleaned = re.sub(r",\s*(summer|fall|spring|winter)\s+\d{4}.*$", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*\([^)]*\)", "", cleaned)
        cleaned = cleaned.strip(" ,-")
        if cleaned.lower() in {"built in open roles", "open roles", "current open roles"}:
            return ""
        return cleaned

    def _is_relevant_note_role_title(self, title: str) -> bool:
        lower = title.lower()
        off_target = [
            "software engineer",
            "solutions engineer",
            "solution engineer",
            "pre-sales",
            "sales engineer",
            "account executive",
            "marketing designer",
            "designer",
            "recruiter",
        ]
        if any(signal in lower for signal in off_target):
            return False
        relevant = [
            "product",
            "pm intern",
            "strategy",
            "business operations",
            "bizops",
            "operator",
            "founder",
            "growth",
            "program manager",
        ]
        return any(signal in lower for signal in relevant)

    def _company_fit_clause(self, company: str, context: dict, company_mode: str) -> str:
        story_fit = self._story_fit_clause(company, context)
        if story_fit:
            return story_fit
        story_context = self._story_fit_context_text(context)
        text = " ".join(
            str(item)
            for item in [
                story_context,
                context.get("description", ""),
                context.get("fit_rationale", ""),
                " ".join(str(tag) for tag in context.get("tags", []) or []),
                " ".join(str(title) for title in context.get("opportunity_titles", []) or []),
            ]
        ).lower()
        if company.lower() == "workwhile" or any(signal in text for signal in ["labor platform", "shift", "workforce", "worker", "no-shows", "fill rates"]):
            return f"{company}'s labor platform problem connects with marketplace ops systems I've worked on."
        if company.lower() == "snyk" or any(signal in text for signal in ["security", "developer productivity", "secure software", "cybersecurity"]):
            return f"{company}'s developer-security platform connects with my data/platform background."
        if company.lower() == "synphony" or any(signal in text for signal in ["strawberry", "farming", "robotics", "robot foundation", "agriculture"]):
            return f"{company}'s robotics + data pipeline angle connects with systems work I've done."
        if company.lower() == "endstack" or any(signal in text for signal in ["desktop os", "cloud desktop", "agents running", "endos", "workspace"]):
            return f"{company}'s cloud desktop/agent workspace is the kind of technical product I'd be excited to help shape."
        if any(signal in text for signal in ["voice ai", "speech", "audio", "conversation intelligence"]):
            return f"{company}'s voice AI product connects with my data/platform background."
        if any(signal in text for signal in ["marketplace", "commerce", "payments", "fintech"]):
            return f"{company}'s product space connects with my marketplace and data systems background."
        if any(signal in text for signal in ["platform", "api", "infrastructure", "developer"]):
            return f"{company}'s platform work connects with systems I've built before."
        if any(signal in text for signal in ["agent", "artificial intelligence", "machine learning", "llm", "generative-ai"]):
            return f"{company}'s applied AI work connects with my data/platform background."
        return ""

    def _story_fit_clause(self, company: str, context: dict) -> str:
        """Turn explicit story-fit evidence into a short, externally safe note sentence."""
        lower = self._story_fit_context_text(context).lower()
        if not lower:
            return ""

        if "hevo" in lower:
            if any(
                signal in lower
                for signal in ["connector", "etl", "integration", "data movement", "data pipeline"]
            ):
                return (
                    f"{company}'s work on connectors and ETL maps directly to my "
                    "engineering work at Hevo."
                )
            if any(
                signal in lower
                for signal in ["observability", "monitoring", "data reliability", "incident"]
            ):
                return (
                    f"{company}'s data reliability and monitoring work maps directly to my "
                    "engineering work at Hevo."
                )
            return f"{company}'s data-infrastructure work maps to my engineering work at Hevo."

        if "flairx" in lower and any(
            signal in lower
            for signal in ["interview", "recruiting", "candidate", "hiring workflow"]
        ):
            return (
                f"{company}'s recruiting workflow maps directly to my AI interview work at "
                "FlairX."
            )

        return ""

    def _story_fit_context_text(self, context: dict) -> str:
        return " ".join(
            self._context_text(context.get(field))
            for field in (
                "story_fit_reason",
                "profile_evidence",
                "why_this_company",
                "private_outreach_context",
            )
        ).strip()

    def _context_text(self, value: object) -> str:
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, dict):
            return " ".join(self._context_text(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return " ".join(self._context_text(item) for item in value)
        return ""

    def _specific_person_hook(self, candidate: dict) -> str:
        title = " ".join(str(candidate.get(field) or "") for field in ["title", "subtitle"]).strip()
        lower = title.lower()
        if not title:
            return ""
        if "voice ai" in lower:
            return "Your Voice AI background caught my eye."
        if "robotics" in lower:
            return "Your robotics background caught my eye."
        if "applied ai" in lower or " ai " in f" {lower} ":
            return "Your applied AI background caught my eye."
        if "product" in lower:
            return "Your product background there caught my eye."
        if any(signal in lower for signal in ["operations", "operator", "strategy", "chief of staff", "bizops"]):
            return "Your operator/strategy background there caught my eye."
        return ""

    def _is_founder_title(self, candidate: dict) -> bool:
        title = str(candidate.get("title") or "").lower()
        return any(signal in title for signal in ["founder", "co-founder", "cofounder", "ceo", "chief executive"])

    def _is_senior_product_title(self, candidate: dict) -> bool:
        title = str(candidate.get("title") or "").lower()
        return any(
            signal in title
            for signal in [
                "head of product",
                "director of product",
                "vp product",
                "vice president",
                "chief product",
                "group product",
                "product leader",
                "lead product",
                "principal product",
                "senior product",
            ]
        )

    def _is_india_based(self, candidate: dict) -> bool:
        location = str(candidate.get("location") or "").lower()
        title = str(candidate.get("title") or "").lower()
        india_signals = ["india", "bengaluru", "bangalore", "delhi", "gurgaon", "gurugram", "mumbai", "hyderabad", "pune", "chennai"]
        return any(signal in location or signal in title for signal in india_signals)

    def _existing_connection_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "direct_help":
            return [
                f"Hi {first_name}, great to reconnect here. I'm at USC Marshall pivoting from engineering into PM and exploring roles at {company}. If you have any quick guidance on where I should focus, I'd really value it.",
                f"Hi {first_name}, nice to reconnect here. I'm at USC Marshall making the shift from engineering into PM and exploring opportunities at {company}. If you're open to it, I'd value any advice on how to position myself well.",
                f"Hi {first_name}, glad we're connecting here. I'm at USC Marshall transitioning from engineering into PM and currently exploring roles at {company}. Would really value any quick guidance you have on approaching the process.",
            ]
        return [
            f"Hi {first_name}, great to reconnect here. I'm at USC Marshall pivoting from engineering into PM and exploring roles at {company}. I'd value staying in touch.",
            f"Hi {first_name}, glad we're connecting here. I'm at USC Marshall transitioning from engineering into PM and currently exploring roles at {company}. I'd value staying in touch.",
            f"Hi {first_name}, nice to reconnect here. I'm at USC Marshall making the shift from engineering into PM and exploring opportunities at {company}. I'd value keeping in touch.",
        ]

    def _usc_marshall_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "guidance":
            return [
                f"Hi {first_name}, fellow Marshall alum here - I'm exploring PM roles at {company} after engineering work at Intuit/Gojek. I'd value your take on how to position myself well. Fight On!",
                f"Hi {first_name}, fellow Marshall alum here - I'm at USC Marshall and exploring PM opportunities at {company} after engineering roles at Intuit/Gojek. Would value any guidance you're open to sharing. Fight On!",
                f"Hi {first_name}, fellow Marshall alum here - I'm exploring PM roles at {company} with a background in data platforms and marketplaces. I'd value your quick thoughts on approaching the team. Fight On!",
            ]
        return [
            f"Hi {first_name}, fellow Marshall alum here - I'm exploring PM roles at {company} after engineering work at Intuit/Gojek. Open to connecting? Fight On!",
            f"Hi {first_name}, fellow Marshall alum here - I'm at USC Marshall and exploring PM opportunities at {company} after engineering roles at Intuit/Gojek. Open to connecting? Fight On!",
            f"Hi {first_name}, fellow Marshall alum here - I'm exploring PM roles at {company} with a background in data platforms and marketplaces. Open to connecting? Fight On!",
        ]

    def _usc_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "guidance":
            return [
                f"Hi {first_name}, fellow Trojan here - I'm exploring PM opportunities at {company} as a Marshall MBA with enterprise software/data platform experience. I'd value your take on positioning myself well. Fight On!",
                f"Hi {first_name}, fellow Trojan here - I'm at USC Marshall and exploring PM roles at {company} after building data products and enterprise systems. Would value any guidance you're open to sharing. Fight On!",
                f"Hi {first_name}, fellow Trojan here - I'm exploring PM roles at {company} with a data platform and enterprise software background. I'd value your quick thoughts on approaching the team. Fight On!",
            ]
        return [
            f"Hi {first_name}, fellow Trojan here - I'm exploring PM roles at {company} as a Marshall MBA with a data platforms background. Open to connecting? Fight On!",
            f"Hi {first_name}, fellow Trojan here - I'm at USC Marshall and exploring PM roles at {company} after building data products and enterprise systems. Open to connecting? Fight On!",
            f"Hi {first_name}, fellow Trojan here - I'm exploring PM opportunities at {company} with enterprise software/data platform experience. Open to connecting? Fight On!",
        ]

    def _shared_history_variants(
        self,
        first_name: str,
        company: str,
        ask_style: str,
        candidate: dict | None = None,
    ) -> list[str]:
        signal = self._shared_history_signal(candidate or {})
        candidate_data = candidate or {}
        dual_usc = signal == "Thapar" and bool(
            candidate_data.get("usc_marshall") or candidate_data.get("usc")
        )
        if dual_usc:
            # Both overlaps are real and rare together; name them both instead of
            # picking one. Mirrors the AI path's dual-affinity rule.
            if ask_style == "guidance":
                return [
                    f"Hi {first_name}, a fellow Thaparian and Trojan - small world! I'm at USC Marshall after engineering roles at Intuit/Gojek, exploring PM roles at {company}. I'd value your take.",
                    f"Hi {first_name}, we share both Thapar and USC - had to reach out. I'm a Marshall MBA and former engineer at Intuit/Gojek exploring PM roles at {company}; I'd value your quick guidance.",
                    f"Hi {first_name}, spotted the double overlap: Thapar and USC. I'm at Marshall after engineering at Intuit/Gojek, exploring PM opportunities at {company}. I'd value your thoughts.",
                ]
            return [
                f"Hi {first_name}, a fellow Thaparian and Trojan - small world! I'm at USC Marshall after engineering roles at Intuit/Gojek, exploring PM roles at {company}. Open to connecting?",
                f"Hi {first_name}, we share both Thapar and USC - had to reach out. I'm a Marshall MBA and former engineer at Intuit/Gojek exploring PM roles at {company}. Open to connecting?",
                f"Hi {first_name}, spotted the double overlap: Thapar and USC. I'm at Marshall after engineering at Intuit/Gojek, exploring PM opportunities at {company}. Open to connecting?",
            ]
        if signal:
            if ask_style == "guidance":
                return [
                    f"Hi {first_name}, I saw your {signal} background and I'm at USC Marshall after engineering roles at Intuit/Gojek, now exploring PM roles at {company}. I'd value your take.",
                    f"Hi {first_name}, noticed the {signal} overlap. I'm a Marshall MBA and former engineer at Intuit/Gojek exploring PM roles at {company}; I'd value your quick guidance.",
                    f"Hi {first_name}, saw we both have {signal} in our paths. I'm at USC Marshall after engineering roles at Intuit/Gojek and exploring PM opportunities at {company}. I'd value your thoughts.",
                ]
            return [
                f"Hi {first_name}, I saw your {signal} background and I'm at USC Marshall after engineering roles at Intuit/Gojek, now exploring PM roles at {company}. Open to connecting?",
                f"Hi {first_name}, noticed the {signal} overlap. I'm a Marshall MBA and former engineer at Intuit/Gojek exploring PM roles at {company}. Open to connecting?",
                f"Hi {first_name}, saw we both have {signal} in our paths. I'm at USC Marshall after engineering roles at Intuit/Gojek and exploring PM opportunities at {company}. Open to connecting?",
            ]
        if ask_style == "guidance":
            return [
                f"Hi {first_name}, I'm a Marshall MBA and former engineer at Intuit/Gojek, now exploring PM roles at {company}. Given the overlap in our backgrounds, I'd value your perspective on how to approach the process.",
                f"Hi {first_name}, I'm at USC Marshall after engineering stints at Intuit and Gojek, and I'm now exploring PM opportunities at {company}. We seem to have some shared background, and I'd value your quick guidance.",
                f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience across Intuit and Gojek, currently exploring PM roles at {company}. Given the overlap in our backgrounds, I'd really value your thoughts.",
            ]
        return [
            f"Hi {first_name}, I'm a Marshall MBA and former engineer at Intuit/Gojek, now exploring PM roles at {company}. Given the overlap in our backgrounds, open to connecting?",
            f"Hi {first_name}, I'm at USC Marshall after engineering stints at Intuit and Gojek, and I'm now exploring PM opportunities at {company}. Given the overlap in our backgrounds, open to connecting?",
            f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience across Intuit and Gojek, currently exploring PM roles at {company}. We seem to have some shared background. Open to connecting?",
        ]

    def _shared_history_signal(self, candidate: dict) -> str:
        signals = [
            str(item).strip()
            for item in candidate.get("shared_history_signals", [])
            if str(item).strip()
        ]
        for preferred in ("Thapar", "Thaparian", "Thapar Institute"):
            for signal in signals:
                if preferred.casefold() in signal.casefold():
                    return "Thapar"
        if signals:
            return signals[0]
        text = " ".join(
            str(candidate.get(field) or "")
            for field in ["title", "subtitle", "snippet", "raw_text"]
        ).lower()
        if "thapar" in text or "thaparian" in text:
            return "Thapar"
        for company in ["Intuit", "Gojek", "Hevo", "Hevo Data", "Optum"]:
            if company.lower() in text:
                return company
        return ""

    @staticmethod
    def _has_thapar_shared_history(candidate: dict) -> bool:
        if not candidate.get("shared_history"):
            return False
        signals = [
            str(item).casefold()
            for item in list(candidate.get("shared_history_signals") or [])
        ]
        if any("thapar" in signal for signal in signals):
            return True
        blob = " ".join(
            strip_mutual_connection_snippet(str(candidate.get(field) or ""))
            for field in ("title", "subtitle", "snippet", "raw_text", "school")
        ).casefold()
        return "thapar" in blob or "thaparian" in blob

    def _product_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "conversation":
            return [
                f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience in data platforms and marketplaces, now exploring PM opportunities at {company}. Your product journey stood out. Open to connecting?",
                f"Hi {first_name}, I'm at USC Marshall after building products from the engineering side at Intuit and Gojek, and I'm exploring PM roles at {company}. Your path stood out. Open to connecting?",
                f"Hi {first_name}, I'm a Marshall MBA and former engineer at Intuit/Gojek, exploring PM roles at {company}. Your product path stood out. Open to connecting?",
            ]
        return [
            f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience in data platforms and marketplaces, now exploring PM opportunities at {company}. Would value your perspective on how to position myself for PM roles there.",
            f"Hi {first_name}, I'm at USC Marshall after building products from the engineering side at Intuit and Gojek, and I'm exploring PM roles at {company}. I'd value your quick thoughts on what strong PM candidates do well.",
            f"Hi {first_name}, I'm a 1Y MBA at USC Marshall and former engineer at Intuit/Gojek, exploring PM roles at {company}. Would really value your perspective on approaching the PM process there.",
        ]

    def _engineering_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "conversation":
            return [
                f"Hi {first_name}, I'm a Marshall MBA with 5 years in engineering across enterprise data and platform systems, now exploring PM opportunities at {company}. Open to connecting?",
                f"Hi {first_name}, I'm at USC Marshall after 5 years building data and marketplace systems as an engineer. I'm now exploring PM roles at {company}. Open to connecting?",
                f"Hi {first_name}, I'm a Marshall MBA with 5 years in engineering across data platforms and marketplace systems. I'm exploring PM roles at {company}. Open to connecting?",
            ]
        return [
            f"Hi {first_name}, I'm a Marshall MBA with 5 years in engineering across enterprise data and platform systems, now exploring PM opportunities at {company}. Would value your perspective on making the shift well.",
            f"Hi {first_name}, I'm at USC Marshall after 5 years building data and marketplace systems as an engineer. I'm now exploring PM roles at {company}, and I'd value your quick guidance on positioning that background well.",
            f"Hi {first_name}, I'm a 1Y MBA at USC Marshall with 5 years in engineering across data platforms and marketplace systems. I'm exploring PM roles at {company}, and I'd value your perspective on approaching the transition.",
        ]

    def _university_recruiting_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        return [
            f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience at Intuit and Gojek, exploring PM roles at {company}. Open to connecting? I'd value a quick pointer on what stands out.",
            f"Hi {first_name}, I'm at USC Marshall after engineering roles at Intuit and Gojek, and I'm currently exploring PM opportunities at {company}. Open to connecting?",
            f"Hi {first_name}, I'm a Marshall MBA with a prior engineering background, now exploring PM roles at {company}. Open to connecting?",
        ]

    def _general_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "conversation":
            return [
                f"Hi {first_name}, I'm at USC Marshall after building enterprise software and data products, and I'm now exploring PM opportunities at {company}. Your path stood out. Open to connecting?",
                f"Hi {first_name}, I'm a Marshall MBA with prior experience in enterprise software and data platforms, currently exploring PM roles at {company}. Open to connecting?",
                f"Hi {first_name}, I'm a Marshall MBA with a background in enterprise software and data platforms, exploring PM roles at {company}. Your path stood out. Open to connecting?",
            ]
        return [
            f"Hi {first_name}, I'm at USC Marshall after building enterprise software and data products, and I'm now exploring PM opportunities at {company}. Would value any perspective you're open to sharing.",
            f"Hi {first_name}, I'm a Marshall MBA with prior experience in enterprise software and data platforms, currently exploring PM roles at {company}. I'd value your quick thoughts on approaching the process there.",
            f"Hi {first_name}, I'm a 1Y MBA at USC Marshall with a background in enterprise software and data platforms, exploring PM roles at {company}. I'd value any guidance you're open to sharing.",
        ]

    def _first_name(self, full_name: str) -> str:
        token = full_name.strip().split()[0] if full_name.strip() else "there"
        return token.rstrip(",")

    def _pick_variant(self, variants: list[str], candidate: dict, company: str) -> str:
        seed = f"{candidate.get('name','')}|{candidate.get('linkedin_url','')}|{company}"
        digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
        idx = int(digest[:8], 16) % len(variants)
        return variants[idx]

    def _note_signature(self, note: str) -> str:
        signature = note.lower()
        signature = re.sub(r"hi\s+[a-z0-9.'-]+,", "hi {name},", signature)
        signature = re.sub(r"\bsnowflake\b", "{company}", signature)
        signature = re.sub(r"\s+", " ", signature).strip()
        return signature

    def _tighten_to_limit(self, note: str) -> str:
        tightened = normalize_outbound_punctuation(" ".join(note.split()))
        replacements = [
            ("I'm a 1Y MBA at USC Marshall and former engineer at Intuit/Gojek, ", "I'm a Marshall MBA and former engineer at Intuit/Gojek, "),
            ("I'm a 1Y MBA at USC Marshall with a background in data platforms and enterprise software, ", "I'm a Marshall MBA with a background in data platforms, "),
            ("I'm a 1Y MBA at USC Marshall with 5 years in engineering across data platforms and marketplace systems. ", "I'm a Marshall MBA with 5 years in engineering. "),
            ("with prior engineering experience at Intuit and Gojek, ", "with prior engineering experience, "),
            ("and learn from your experience there.", "and learn from your experience."),
            ("Your product path stood out, and I'd love to connect and learn from your experience.", "Your product path caught my eye. Open to connecting?"),
            ("I'm a Marshall MBA + former engineer exploring product/operator paths, and this feels close to work I've done. ", "I'm a Marshall MBA + former engineer exploring product work. "),
            ("Would love to connect and understand where someone with my builder background could be useful as the team grows.", "I'd value a quick read on where my builder background could be useful."),
            ("Would love to connect and understand where someone with my engineering + PM background could be most useful.", "I'd value a quick read on where my engineering + PM background could be useful."),
            ("Would be grateful to connect and ask if a referral or hiring-team pointer would make sense.", "Would value a referral or hiring-team pointer if the fit looks reasonable."),
        ]
        for source, target in replacements:
            if len(tightened) <= NOTE_CHAR_LIMIT:
                break
            tightened = tightened.replace(source, target)
        if len(tightened) <= NOTE_CHAR_LIMIT:
            return tightened
        trimmed = tightened[: NOTE_CHAR_LIMIT - 1].rstrip(" ,.;")
        return f"{trimmed}…"

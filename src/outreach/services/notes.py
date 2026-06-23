from __future__ import annotations

import json
from dataclasses import asdict, dataclass
import hashlib
import re

import anthropic


NOTE_CHAR_LIMIT = 300


@dataclass
class GeneratedNote:
    text: str
    family: str
    ask_style: str
    length: int
    within_limit: bool


@dataclass
class NoteQualityCheck:
    score: int
    verdict: str
    flags: list[str]
    strengths: list[str]


class NoteGenerator:
    """Deterministic LinkedIn invite note generator tuned to the 300-char limit."""

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

        use_contextual = (
            not candidate.get("existing_connection")
            and not candidate.get("usc_marshall")
            and not candidate.get("usc")
            and not candidate.get("shared_history")
            and (
                bool(context)
                or role_bucket in {"Founder", "Adjacent"}
                or (role_bucket == "Engineering" and self._is_india_based(candidate))
            )
        )
        if use_contextual:
            contextual = self._contextual_variants(
                first_name=first_name,
                company=company_for_note,
                candidate=candidate,
                role_bucket=role_bucket,
                company_mode=company_mode,
                context=context,
            )
            if contextual:
                family, ask_style, variants = contextual
                note = self._pick_variant(variants, candidate, company_for_note)
                note = self._tighten_to_limit(note)
                return GeneratedNote(
                    text=note,
                    family=family,
                    ask_style=ask_style,
                    length=len(note),
                    within_limit=len(note) <= NOTE_CHAR_LIMIT,
                )

        if candidate.get("existing_connection"):
            family = "existing_connection"
            variants = self._existing_connection_variants(first_name, company_for_note, ask_style)
        elif candidate.get("usc_marshall"):
            family = "usc_marshall"
            variants = self._usc_marshall_variants(first_name, company_for_note, ask_style)
        elif candidate.get("usc"):
            family = "usc"
            variants = self._usc_variants(first_name, company_for_note, ask_style)
        elif candidate.get("shared_history"):
            family = "shared_history"
            variants = self._shared_history_variants(first_name, company_for_note, ask_style, candidate)
        elif role_bucket == "Product":
            family = "product"
            variants = self._product_variants(first_name, company_for_note, ask_style)
        elif role_bucket == "Engineering":
            family = "engineering"
            variants = self._engineering_variants(first_name, company_for_note, ask_style)
        elif role_bucket == "University Recruiting":
            family = "university_recruiting"
            variants = self._university_recruiting_variants(first_name, company_for_note, ask_style)
        else:
            family = "general"
            variants = self._general_variants(first_name, company_for_note, ask_style)

        note = self._pick_variant(variants, candidate, company_for_note)
        note = self._tighten_to_limit(note)
        return GeneratedNote(
            text=note,
            family=family,
            ask_style=ask_style,
            length=len(note),
            within_limit=len(note) <= NOTE_CHAR_LIMIT,
        )

    def generate_batch(
        self,
        candidates: list[dict],
        company: str,
        company_mode: str = "default",
        note_context: dict | None = None,
    ) -> list[dict]:
        annotated: list[dict] = []
        recent_notes: list[str] = []
        for candidate in candidates:
            generated = self.generate(
                candidate,
                company=company,
                company_mode=company_mode,
                note_context=note_context,
            )
            quality = self.quality_check(candidate, generated, recent_notes)
            enriched = {
                **candidate,
                "note": generated.text,
                "note_family": generated.family,
                "note_ask_style": generated.ask_style,
                "note_length": generated.length,
                "note_within_limit": generated.within_limit,
                "note_qc": asdict(quality),
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
        client = anthropic.Anthropic(api_key=api_key)
        polished: list[dict] = []
        recent_polished: list[str] = []
        for index, candidate in enumerate(candidates):
            enriched = dict(candidate)
            if index < top_n:
                polished_note = self._polish_one(
                    client=client,
                    candidate=candidate,
                    company=company,
                    base_note=str(candidate["note"]),
                    model=model,
                    company_mode=company_mode,
                )
                qc = self.quality_check(candidate, polished_note, recent_polished)
                enriched["polished_note"] = polished_note.text
                enriched["polished_note_length"] = polished_note.length
                enriched["polished_note_within_limit"] = polished_note.within_limit
                enriched["polished_note_qc"] = asdict(qc)
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
                r"\b(connect|learn|stay in touch|perspective|guidance|thoughts|hear about|hear more)\b",
                lower,
            )
            or re.search(
                r"\b(referral|pointer|radar|useful|contribute|stand out|project areas|team.*excited|hiring team)\b",
                lower,
            )
        )
        if not ask_is_clear:
            flags.append("Ask is not clear")
            score -= 12
        else:
            strengths.append("Light, clear ask")

        if any(
            phrase in lower
            for phrase in [
                "caught my eye because",
                "stood out because",
                "noticed your",
                "feels close to",
                "strong fit",
                "natural extension",
            ]
        ):
            strengths.append("Uses specific hook")
        elif generated.family not in {"usc", "usc_marshall", "existing_connection"}:
            flags.append("Missing specific hook")
            score -= 10

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

        score = max(0, min(100, score))
        hard_fail = generated.length > NOTE_CHAR_LIMIT or not ask_is_clear or not note.strip()
        verdict = "blocked" if hard_fail else "send"
        return NoteQualityCheck(score=score, verdict=verdict, flags=flags, strengths=strengths)

    def _polish_one(
        self,
        client: anthropic.Anthropic,
        candidate: dict,
        company: str,
        base_note: str,
        model: str,
        company_mode: str,
    ) -> GeneratedNote:
        payload = {
            "company": company,
            "company_mode": company_mode,
            "name": candidate.get("name"),
            "title": candidate.get("title"),
            "role_bucket": candidate.get("role_bucket"),
            "ask_style": candidate.get("note_ask_style"),
            "signals": {
                "existing_connection": candidate.get("existing_connection", False),
                "usc_marshall": candidate.get("usc_marshall", False),
                "usc": candidate.get("usc", False),
                "shared_history": candidate.get("shared_history", False),
            },
            "note_context": candidate.get("note_context") or {},
            "base_note": base_note,
        }
        prompt = (
            "Rewrite this LinkedIn invite note to sound sharper and more natural while preserving the same facts and warmth.\n"
            "Rules:\n"
            "- Maximum 300 characters\n"
            "- Keep it as a connection-request note, not an email\n"
            "- Preserve the strongest signal already present\n"
            "- Respect the ask_style: conversation, guidance, or direct_help\n"
            "- Only direct_help may ask for more explicit support, and even then keep it light\n"
            "- Keep 'Fight On!' if the candidate has USC or USC Marshall signal\n"
            "- Output JSON only: {\"note\": \"...\"}\n\n"
            f"{json.dumps(payload, ensure_ascii=True)}"
        )
        message = client.messages.create(
            model=model,
            max_tokens=220,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.S)
        note = base_note
        if match:
            try:
                parsed = json.loads(match.group(0))
                candidate_note = str(parsed.get("note", "")).strip()
                if candidate_note:
                    note = candidate_note
            except json.JSONDecodeError:
                note = base_note
        note = self._tighten_to_limit(note)
        return GeneratedNote(
            text=note,
            family=str(candidate.get("note_family", "polished")),
            ask_style=str(candidate.get("note_ask_style", "conversation")),
            length=len(note),
            within_limit=len(note) <= NOTE_CHAR_LIMIT,
        )

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
        company_fit = self._company_fit_clause(company, context, company_mode)
        person_hook = self._specific_person_hook(candidate)

        if self._is_india_based(candidate) and role_bucket == "Engineering":
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
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer interested in product/operator work at {company}.{fit_sentence} Would love to connect and understand where a technical MBA could be useful.",
                    f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer exploring product/operator paths at {company}.{fit_sentence} Would love to connect and follow what you're building.",
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at {role} at {company}.{fit_sentence} Would love to connect and understand where someone with my background could contribute.",
                ],
            )

        if role_bucket == "Product":
            fit_clause = person_hook or company_fit or "The role looks close to my engineering-to-PM path."
            if self._is_senior_product_title(candidate):
                return (
                    "senior_product_contribution",
                    "contribution_fit",
                    [
                        f"Hi {first_name}, I'm a Marshall MBA + former backend/data engineer exploring {role} at {company}. {fit_clause} Would love to connect and understand where my engineering + PM background could be useful.",
                        f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at {role} at {company}. {fit_clause} Would love to connect and hear what project areas the product team is most excited about.",
                        f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer exploring product roles at {company}. {fit_clause} Would love to connect and ask what tends to matter most to the product team.",
                    ],
                )
            return (
                "product_hiring_path",
                "hiring_path",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer exploring {role} at {company}. {fit_clause} Would love to connect and understand the best way to get on the product team's radar.",
                    f"Hi {first_name}, I'm a former backend/data engineer now at USC Marshall exploring {role} at {company}. {fit_clause} Would love to connect and ask what tends to stand out to the product team.",
                    f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer looking at product roles at {company}. Would love to connect and ask what usually helps candidates stand out to the team.",
                ],
            )

        if role_bucket == "Engineering":
            fit_clause = person_hook or company_fit or "Since you're on the engineering side there, I'd value your take."
            return (
                "engineering_product_bridge",
                "technical_overlap",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer exploring {role} at {company}. Since you're on the engineering side there, I'd value a pointer on how technical PM candidates can stand out.",
                    f"Hi {first_name}, I'm a former backend/data engineer now at USC Marshall exploring PM/product roles at {company}. {fit_clause} Would love a quick pointer on how builders work with product there.",
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at product roles at {company}. Since you've seen the engineering side, I'd value your take on how to get on the team's radar.",
                ],
            )

        if role_bucket == "Adjacent":
            fit_clause = person_hook or company_fit or "The role direction feels close to systems and product work I've done."
            return (
                "operator_contribution",
                "contribution_fit",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer exploring product/operator roles at {company}. {fit_clause} Would love to connect and understand where someone like me could be useful.",
                    f"Hi {first_name}, I'm a Marshall MBA + former data/platform engineer looking at product/operator paths at {company}. Would love to connect and hear which project areas the team is most excited about.",
                ],
            )

        if context:
            fit_clause = company_fit or "The role looks close to my engineering-to-PM path."
            return (
                "contextual_general",
                "contribution_fit",
                [
                    f"Hi {first_name}, I'm a Marshall MBA + former backend/data engineer exploring {role} at {company}. {fit_clause} Would love to connect and understand where someone like me could be useful.",
                    f"Hi {first_name}, I'm a Marshall MBA + former engineer looking at PM/product roles at {company}. Would love to connect and ask what tends to matter most to the team.",
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
        return "PM/product roles"

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
        text = " ".join(
            str(item)
            for item in [
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
                f"Hi {first_name}, nice to reconnect here. I'm at USC Marshall making the shift from engineering into PM and exploring opportunities at {company}. If you're open to it, I'd love any advice on how to position myself well.",
                f"Hi {first_name}, glad we're connecting here. I'm at USC Marshall transitioning from engineering into PM and currently exploring roles at {company}. Would really value any quick guidance you have on approaching the process.",
            ]
        return [
            f"Hi {first_name}, great to reconnect here. I'm at USC Marshall pivoting from engineering into PM and exploring roles at {company}. Would love to stay in touch and learn from your experience there.",
            f"Hi {first_name}, glad we're connecting here. I'm at USC Marshall transitioning from engineering into PM and currently exploring roles at {company}. Would love to stay in touch and learn from your journey.",
            f"Hi {first_name}, nice to reconnect here. I'm at USC Marshall making the shift from engineering into PM and exploring opportunities at {company}. Would love to keep in touch and learn from your experience.",
        ]

    def _usc_marshall_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "guidance":
            return [
                f"Hi {first_name}, fellow Marshall alum here - I'm a 1Y MBA with prior engineering experience at Intuit and Gojek, exploring PM roles at {company}. Would love your perspective on how to position myself well. Fight On!",
                f"Hi {first_name}, fellow Marshall alum here - I'm at USC Marshall after engineering roles at Intuit and Gojek, and I'm exploring PM opportunities at {company}. Would value any guidance you're open to sharing. Fight On!",
                f"Hi {first_name}, fellow Marshall alum here - I'm a 1Y MBA with an engineering background in data platforms and marketplaces, now exploring PM roles at {company}. Would love your quick thoughts on approaching the team. Fight On!",
            ]
        return [
            f"Hi {first_name}, fellow Marshall alum here - I'm a 1Y MBA with prior engineering experience at Intuit and Gojek, exploring PM roles at {company}. Would love to connect and learn from your experience. Fight On!",
            f"Hi {first_name}, fellow Marshall alum here - I'm at USC Marshall after engineering roles at Intuit and Gojek, and I'm exploring PM opportunities at {company}. Would love to connect and learn from your path. Fight On!",
            f"Hi {first_name}, fellow Marshall alum here - I'm a 1Y MBA with an engineering background in data platforms and marketplaces, now exploring PM roles at {company}. Would love to connect and hear about your experience. Fight On!",
        ]

    def _usc_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "guidance":
            return [
                f"Hi {first_name}, fellow Trojan here - I'm a Marshall MBA with prior experience in enterprise software and data platforms, now exploring PM opportunities at {company}. Would love your perspective on how to position myself well. Fight On!",
                f"Hi {first_name}, fellow Trojan here - I'm at USC Marshall after building data products and enterprise systems, and I'm exploring PM roles at {company}. Would value any guidance you're open to sharing. Fight On!",
                f"Hi {first_name}, fellow Trojan here - I'm a 1Y MBA at USC Marshall with a background in data platforms and enterprise software, exploring PM roles at {company}. Would love your quick thoughts on approaching the team. Fight On!",
            ]
        return [
            f"Hi {first_name}, fellow Trojan here - I'm a 1Y MBA at USC Marshall with a background in data platforms and enterprise software, exploring PM roles at {company}. Would love to connect and learn from your experience. Fight On!",
            f"Hi {first_name}, fellow Trojan here - I'm at USC Marshall after building data products and enterprise systems, and I'm exploring PM roles at {company}. Would love to connect and hear about your experience. Fight On!",
            f"Hi {first_name}, fellow Trojan here - I'm a Marshall MBA with prior experience in enterprise software and data platforms, now exploring PM opportunities at {company}. Would love to connect and learn from your path. Fight On!",
        ]

    def _shared_history_variants(
        self,
        first_name: str,
        company: str,
        ask_style: str,
        candidate: dict | None = None,
    ) -> list[str]:
        signal = self._shared_history_signal(candidate or {})
        if signal:
            if ask_style == "guidance":
                return [
                    f"Hi {first_name}, I saw your {signal} background and I'm at USC Marshall after engineering roles at Intuit/Gojek, now exploring PM roles at {company}. Would value your perspective.",
                    f"Hi {first_name}, noticed the {signal} overlap. I'm a Marshall MBA and former engineer at Intuit/Gojek exploring PM roles at {company}; I'd value your quick guidance.",
                    f"Hi {first_name}, saw we both have {signal} in our paths. I'm at USC Marshall after engineering roles at Intuit/Gojek and exploring PM opportunities at {company}. Would value your thoughts.",
                ]
            return [
                f"Hi {first_name}, I saw your {signal} background and I'm at USC Marshall after engineering roles at Intuit/Gojek, now exploring PM roles at {company}. Would love to connect.",
                f"Hi {first_name}, noticed the {signal} overlap. I'm a Marshall MBA and former engineer at Intuit/Gojek exploring PM roles at {company}; would love to connect and learn from your path.",
                f"Hi {first_name}, saw we both have {signal} in our paths. I'm at USC Marshall after engineering roles at Intuit/Gojek and exploring PM opportunities at {company}. Would love to connect.",
            ]
        if ask_style == "guidance":
            return [
                f"Hi {first_name}, I'm a Marshall MBA and former engineer at Intuit/Gojek, now exploring PM roles at {company}. Given the overlap in our backgrounds, I'd value your perspective on how to approach the process.",
                f"Hi {first_name}, I'm at USC Marshall after engineering stints at Intuit and Gojek, and I'm now exploring PM opportunities at {company}. We seem to have some shared background, and I'd love your quick guidance.",
                f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience across Intuit and Gojek, currently exploring PM roles at {company}. Given the overlap in our backgrounds, I'd really value your thoughts.",
            ]
        return [
            f"Hi {first_name}, I'm a 1Y MBA at USC Marshall and former engineer at Intuit/Gojek, now exploring PM roles at {company}. Given the overlap in our backgrounds, I'd love to connect and learn from your experience.",
            f"Hi {first_name}, I'm at USC Marshall after engineering stints at Intuit and Gojek, and I'm now exploring PM opportunities at {company}. Given the overlap in our backgrounds, I'd love to connect and hear about your path.",
            f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience across Intuit and Gojek, currently exploring PM roles at {company}. We seem to have some shared background, and I'd love to connect and learn from your experience.",
        ]

    def _shared_history_signal(self, candidate: dict) -> str:
        signals = [
            str(item).strip()
            for item in candidate.get("shared_history_signals", [])
            if str(item).strip()
        ]
        if signals:
            return signals[0]
        text = " ".join(
            str(candidate.get(field) or "")
            for field in ["title", "subtitle", "snippet", "raw_text"]
        ).lower()
        for company in ["Intuit", "Gojek", "Hevo", "Hevo Data", "Optum"]:
            if company.lower() in text:
                return company
        return ""

    def _product_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "conversation":
            return [
                f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience in data platforms and marketplaces, now exploring PM opportunities at {company}. Your product journey stood out, and I'd love to connect and hear about your experience.",
                f"Hi {first_name}, I'm at USC Marshall after building products from the engineering side at Intuit and Gojek, and I'm exploring PM roles at {company}. Your path stood out, and I'd love to connect and learn from your experience.",
                f"Hi {first_name}, I'm a 1Y MBA at USC Marshall and former engineer at Intuit/Gojek, exploring PM roles at {company}. Your product path stood out, and I'd love to connect and learn from your experience.",
            ]
        return [
            f"Hi {first_name}, I'm a Marshall MBA with prior engineering experience in data platforms and marketplaces, now exploring PM opportunities at {company}. Would value your perspective on how to position myself for PM roles there.",
            f"Hi {first_name}, I'm at USC Marshall after building products from the engineering side at Intuit and Gojek, and I'm exploring PM roles at {company}. Would love your quick thoughts on what strong PM candidates do well.",
            f"Hi {first_name}, I'm a 1Y MBA at USC Marshall and former engineer at Intuit/Gojek, exploring PM roles at {company}. Would really value your perspective on approaching the PM process there.",
        ]

    def _engineering_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "conversation":
            return [
                f"Hi {first_name}, I'm a Marshall MBA with 5 years in engineering across enterprise data and platform systems, now exploring PM opportunities at {company}. Would love to connect and hear about your experience building there.",
                f"Hi {first_name}, I'm at USC Marshall after 5 years building data and marketplace systems as an engineer. I'm now exploring PM roles at {company}, and I'd love to connect and learn from your experience there.",
                f"Hi {first_name}, I'm a 1Y MBA at USC Marshall with 5 years in engineering across data platforms and marketplace systems. I'm exploring PM roles at {company}, and I'd love to connect and learn from your experience building there.",
            ]
        return [
            f"Hi {first_name}, I'm a Marshall MBA with 5 years in engineering across enterprise data and platform systems, now exploring PM opportunities at {company}. Would value your perspective on making the shift well.",
            f"Hi {first_name}, I'm at USC Marshall after 5 years building data and marketplace systems as an engineer. I'm now exploring PM roles at {company}, and I'd love your quick guidance on positioning that background well.",
            f"Hi {first_name}, I'm a 1Y MBA at USC Marshall with 5 years in engineering across data platforms and marketplace systems. I'm exploring PM roles at {company}, and I'd value your perspective on approaching the transition.",
        ]

    def _university_recruiting_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        return [
            f"Hi {first_name}, I'm a 1Y MBA at USC Marshall with prior engineering experience at Intuit and Gojek, exploring PM roles at {company}. I'd love to connect and learn about what stands out in strong MBA PM candidates.",
            f"Hi {first_name}, I'm at USC Marshall after engineering roles at Intuit and Gojek, and I'm currently exploring PM opportunities at {company}. Would love to connect and learn what you look for in strong MBA candidates.",
            f"Hi {first_name}, I'm a Marshall MBA with a prior engineering background, now exploring PM roles at {company}. I'd love to connect and hear what tends to stand out in strong MBA PM applicants.",
        ]

    def _general_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "conversation":
            return [
                f"Hi {first_name}, I'm at USC Marshall after building enterprise software and data products, and I'm now exploring PM opportunities at {company}. Your path stood out, and I'd love to connect and hear about your experience.",
                f"Hi {first_name}, I'm a Marshall MBA with prior experience in enterprise software and data platforms, currently exploring PM roles at {company}. I'd love to connect and learn from your path.",
                f"Hi {first_name}, I'm a 1Y MBA at USC Marshall with a background in enterprise software and data platforms, exploring PM roles at {company}. Your path stood out, and I'd love to connect and learn from your experience.",
            ]
        return [
            f"Hi {first_name}, I'm at USC Marshall after building enterprise software and data products, and I'm now exploring PM opportunities at {company}. Would value any perspective you're open to sharing.",
            f"Hi {first_name}, I'm a Marshall MBA with prior experience in enterprise software and data platforms, currently exploring PM roles at {company}. Would love your quick thoughts on approaching the process there.",
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
        tightened = " ".join(note.split())
        replacements = [
            ("I'm a 1Y MBA at USC Marshall and former engineer at Intuit/Gojek, ", "I'm a Marshall MBA and former engineer at Intuit/Gojek, "),
            ("I'm a 1Y MBA at USC Marshall with a background in data platforms and enterprise software, ", "I'm a Marshall MBA with a background in data platforms, "),
            ("I'm a 1Y MBA at USC Marshall with 5 years in engineering across data platforms and marketplace systems. ", "I'm a Marshall MBA with 5 years in engineering. "),
            ("with prior engineering experience at Intuit and Gojek, ", "with prior engineering experience, "),
            ("and learn from your experience there.", "and learn from your experience."),
            ("Your product path stood out, and I'd love to connect and learn from your experience.", "Your product path stood out. Would love to connect and learn from your experience."),
            ("I'm a Marshall MBA + former engineer exploring product/operator paths, and this feels close to work I've done. ", "I'm a Marshall MBA + former engineer exploring product/operator paths. "),
            ("Would love to connect and understand where someone with my builder background could be useful as the team grows.", "Would love to connect and understand where my builder background could be useful."),
            ("Would love to connect and understand where someone with my engineering + PM background could be most useful.", "Would love to connect and understand where my engineering + PM background could be useful."),
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

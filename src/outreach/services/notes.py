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

    def generate(self, candidate: dict, company: str, company_mode: str = "default") -> GeneratedNote:
        first_name = self._first_name(candidate.get("name") or "there")
        role_bucket = candidate.get("role_bucket") or "Other"
        ask_style = self._determine_ask_style(candidate, role_bucket, company_mode)

        if candidate.get("existing_connection"):
            family = "existing_connection"
            variants = self._existing_connection_variants(first_name, company, ask_style)
        elif candidate.get("usc_marshall"):
            family = "usc_marshall"
            variants = self._usc_marshall_variants(first_name, company, ask_style)
        elif candidate.get("usc"):
            family = "usc"
            variants = self._usc_variants(first_name, company, ask_style)
        elif candidate.get("shared_history"):
            family = "shared_history"
            variants = self._shared_history_variants(first_name, company, ask_style)
        elif role_bucket == "Product":
            family = "product"
            variants = self._product_variants(first_name, company, ask_style)
        elif role_bucket == "Engineering":
            family = "engineering"
            variants = self._engineering_variants(first_name, company, ask_style)
        elif role_bucket == "University Recruiting":
            family = "university_recruiting"
            variants = self._university_recruiting_variants(first_name, company, ask_style)
        else:
            family = "general"
            variants = self._general_variants(first_name, company, ask_style)

        note = self._pick_variant(variants, candidate, company)
        note = self._tighten_to_limit(note)
        return GeneratedNote(
            text=note,
            family=family,
            ask_style=ask_style,
            length=len(note),
            within_limit=len(note) <= NOTE_CHAR_LIMIT,
        )

    def generate_batch(self, candidates: list[dict], company: str, company_mode: str = "default") -> list[dict]:
        annotated: list[dict] = []
        recent_notes: list[str] = []
        for candidate in candidates:
            generated = self.generate(candidate, company=company, company_mode=company_mode)
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
        model: str = "claude-sonnet-4-6",
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
        if candidate.get("role_bucket") in {"Product", "Engineering", "University Recruiting"}:
            signal_hits += 1
        if signal_hits < 2:
            flags.append("Weak personalization signal density")
            score -= 12
        else:
            strengths.append("Clear personalization signal")

        if not re.search(r"\b(connect|learn|stay in touch)\b", lower):
            flags.append("Ask is not clear")
            score -= 12
        else:
            strengths.append("Light, clear ask")

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
        verdict = "send" if score >= 85 else "review" if score >= 70 else "revise"
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
        if role_bucket == "University Recruiting":
            return "direct_help"
        if candidate.get("usc_marshall") or candidate.get("usc") or candidate.get("shared_history"):
            return "guidance"
        if company_mode == "startup":
            return "conversation"
        if company_mode == "big_company":
            return "guidance"
        if role_bucket in {"Product", "Engineering"}:
            return "conversation"
        return "guidance"

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
                f"Hi {first_name}, fellow Marshall alum here. I'm a 1Y MBA with prior engineering experience at Intuit and Gojek, exploring PM roles at {company}. Would love your perspective on how to position myself well. Fight On!",
                f"Hi {first_name}, fellow Marshall alum here. I'm at USC Marshall after engineering roles at Intuit and Gojek, and I'm exploring PM opportunities at {company}. Would value any guidance you're open to sharing. Fight On!",
                f"Hi {first_name}, fellow Marshall alum here. I'm a 1Y MBA with an engineering background in data platforms and marketplaces, now exploring PM roles at {company}. Would love your quick thoughts on approaching the team. Fight On!",
            ]
        return [
            f"Hi {first_name}, fellow Marshall alum here. I'm a 1Y MBA with prior engineering experience at Intuit and Gojek, exploring PM roles at {company}. Your path stood out and I'd love to connect and learn from your experience. Fight On!",
            f"Hi {first_name}, fellow Marshall alum here. I'm at USC Marshall after engineering roles at Intuit and Gojek, and I'm exploring PM opportunities at {company}. Would love to connect and learn from your path. Fight On!",
            f"Hi {first_name}, fellow Marshall alum here. I'm a 1Y MBA with an engineering background in data platforms and marketplaces, now exploring PM roles at {company}. Would love to connect and hear about your experience. Fight On!",
        ]

    def _usc_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
        if ask_style == "guidance":
            return [
                f"Hi {first_name}, fellow Trojan here. I'm a Marshall MBA with prior experience in enterprise software and data platforms, now exploring PM opportunities at {company}. Would love your perspective on how to position myself well. Fight On!",
                f"Hi {first_name}, fellow Trojan here. I'm at USC Marshall after building data products and enterprise systems, and I'm exploring PM roles at {company}. Would value any guidance you're open to sharing. Fight On!",
                f"Hi {first_name}, fellow Trojan here. I'm a 1Y MBA at USC Marshall with a background in data platforms and enterprise software, exploring PM roles at {company}. Would love your quick thoughts on approaching the team. Fight On!",
            ]
        return [
            f"Hi {first_name}, fellow Trojan here. I'm a 1Y MBA at USC Marshall with a background in data platforms and enterprise software, exploring PM roles at {company}. Would love to connect and learn from your experience. Fight On!",
            f"Hi {first_name}, fellow Trojan here. I'm at USC Marshall after building data products and enterprise systems, and I'm exploring PM roles at {company}. Would love to connect and hear about your experience. Fight On!",
            f"Hi {first_name}, fellow Trojan here. I'm a Marshall MBA with prior experience in enterprise software and data platforms, now exploring PM opportunities at {company}. Would love to connect and learn from your path. Fight On!",
        ]

    def _shared_history_variants(self, first_name: str, company: str, ask_style: str) -> list[str]:
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
        ]
        for source, target in replacements:
            if len(tightened) <= NOTE_CHAR_LIMIT:
                break
            tightened = tightened.replace(source, target)
        if len(tightened) <= NOTE_CHAR_LIMIT:
            return tightened
        trimmed = tightened[: NOTE_CHAR_LIMIT - 1].rstrip(" ,.;")
        return f"{trimmed}…"

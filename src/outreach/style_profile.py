from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


DEFAULT_STYLE_PROFILE_FILENAME = "communication_style_profile.yml"


class StyleMessageExample(BaseModel):
    label: str
    recipient_type: str = "general"
    message: str
    notes: str = ""


class StyleReview(BaseModel):
    verdict: Literal["style_ok", "needs_review"]
    flags: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    approved_asks: list[str] = Field(default_factory=list)


class CommunicationStyleProfile(BaseModel):
    preferred_directness: str = "clear and specific"
    preferred_casualness: str = "warm but not over-familiar"
    banned_phrases: list[str] = Field(default_factory=list)
    self_intro_variants: list[str] = Field(default_factory=list)
    approved_asks_by_recipient_type: dict[str, list[str]] = Field(default_factory=dict)
    strong_messages: list[StyleMessageExample] = Field(default_factory=list)
    weak_messages: list[StyleMessageExample] = Field(default_factory=list)
    notes: str = ""

    def approved_asks_for(self, recipient_type: str) -> list[str]:
        key = normalize_recipient_type(recipient_type)
        asks = list(self.approved_asks_by_recipient_type.get(key, []))
        if key != "general":
            asks.extend(self.approved_asks_by_recipient_type.get("general", []))
        return asks

    def banned_phrases_in(self, message: str) -> list[str]:
        text = message.lower()
        matches: list[str] = []
        for phrase in self.banned_phrases:
            normalized = phrase.strip()
            if normalized and re.search(rf"\b{re.escape(normalized.lower())}\b", text):
                matches.append(normalized)
        return matches

    def review_message(self, message: str, recipient_type: str = "general") -> StyleReview:
        banned = self.banned_phrases_in(message)
        flags = [f"Banned phrase: {phrase}" for phrase in banned]
        return StyleReview(
            verdict="needs_review" if flags else "style_ok",
            flags=flags,
            banned_phrases=banned,
            approved_asks=self.approved_asks_for(recipient_type),
        )

    def prompt_guidance(self, recipient_type: str = "general") -> str:
        asks = self.approved_asks_for(recipient_type)
        lines = [
            f"Directness: {self.preferred_directness}",
            f"Casualness: {self.preferred_casualness}",
        ]
        if self.self_intro_variants:
            lines.append("Approved self-intros: " + " | ".join(self.self_intro_variants))
        if asks:
            lines.append("Approved asks: " + " | ".join(asks))
        if self.banned_phrases:
            lines.append("Avoid phrases: " + " | ".join(self.banned_phrases))
        return "\n".join(lines)


def normalize_recipient_type(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    aliases = {
        "apm": "junior_product_apm",
        "junior_product": "junior_product_apm",
        "product": "senior_product",
        "pm": "senior_product",
        "engineering_india": "engineer_india",
        "indian_engineer": "engineer_india",
        "engineer_elsewhere": "engineer",
        "recruiting": "recruiter",
        "talent": "recruiter",
    }
    return aliases.get(normalized, normalized or "general")


def load_style_profile(path: Path) -> CommunicationStyleProfile:
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Style profile must be a YAML mapping: {path}")
    return CommunicationStyleProfile.model_validate(payload)


def load_style_profile_if_exists(path: Path | None = None) -> CommunicationStyleProfile:
    candidate_path = path or Path("workspace") / DEFAULT_STYLE_PROFILE_FILENAME
    if not candidate_path.exists():
        return CommunicationStyleProfile()
    return load_style_profile(candidate_path)


def dump_style_profile(profile: CommunicationStyleProfile) -> dict[str, Any]:
    return profile.model_dump(mode="json", exclude_none=True)

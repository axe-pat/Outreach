from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, Field

from outreach.tracking import ContactRecord, OrganizationRecord


DEFAULT_STYLE_PROFILE_FILENAME = "communication_style_profile.yml"


class StyleMessageExample(BaseModel):
    label: str
    recipient_type: str = "general"
    message: str
    notes: str = ""
    source: str = ""


@dataclass(frozen=True)
class StyleProfileSyncSummary:
    examples_seen: int = 0
    strong_added: int = 0
    weak_added: int = 0
    duplicates_skipped: int = 0
    invalid_skipped: int = 0
    profile_updated: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


@dataclass(frozen=True)
class GuidedStyleDraft:
    message: str
    prompt_guidance: str
    strong_example_labels: tuple[str, ...] = ()
    transformations: tuple[str, ...] = ()


class StyleReview(BaseModel):
    verdict: Literal["style_ok", "needs_review"]
    flags: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    weak_example_labels: list[str] = Field(default_factory=list)
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
        weak_labels = self.weak_example_matches(message, recipient_type)
        flags.extend(f"Matches learned weak example: {label}" for label in weak_labels)
        return StyleReview(
            verdict="needs_review" if flags else "style_ok",
            flags=flags,
            banned_phrases=banned,
            weak_example_labels=weak_labels,
            approved_asks=self.approved_asks_for(recipient_type),
        )

    def weak_example_matches(
        self,
        message: str,
        recipient_type: str = "general",
        *,
        similarity_threshold: float = 0.72,
    ) -> list[str]:
        """Return negative examples whose wording is substantially repeated."""

        message_tokens = set(_message_key(message).split())
        if len(message_tokens) < 5:
            return []
        matches: list[str] = []
        for example in _recipient_relevant_examples(
            self.weak_messages,
            recipient_type,
            limit=len(self.weak_messages),
        ):
            example_tokens = set(_message_key(example.message).split())
            if len(example_tokens) < 5:
                continue
            similarity = len(message_tokens & example_tokens) / len(
                message_tokens | example_tokens
            )
            if similarity >= similarity_threshold:
                matches.append(example.label)
        return matches

    def prompt_guidance(
        self,
        recipient_type: str = "general",
        *,
        max_strong_examples: int = 2,
        max_weak_examples: int = 2,
        max_example_chars: int = 500,
    ) -> str:
        """Build bounded guidance using only matching or general examples."""

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
        strong = _recipient_relevant_examples(
            self.strong_messages,
            recipient_type,
            limit=max(0, max_strong_examples),
        )
        weak = _recipient_relevant_examples(
            self.weak_messages,
            recipient_type,
            limit=max(0, max_weak_examples),
        )
        if strong:
            lines.append("Strong examples to emulate:")
            lines.extend(
                f"- [{item.label}] {_bounded_message(item.message, max_example_chars)}"
                for item in strong
            )
        if weak:
            lines.append("Weak examples to avoid:")
            lines.extend(
                f"- [{item.label}] {_bounded_message(item.message, max_example_chars)}"
                for item in weak
            )
        return "\n".join(lines)

    def guide_draft_from_examples(
        self,
        message: str,
        recipient_type: str = "general",
        *,
        max_examples: int = 2,
    ) -> GuidedStyleDraft:
        """Apply conservative wording preferences from bounded learned positives.

        The examples influence phrasing only; recipient/company facts and the
        underlying ask remain those of the freshly generated draft.
        """

        relevant = _recipient_relevant_examples(
            self.strong_messages,
            recipient_type,
            limit=max(0, max_examples),
        )
        learned = [
            item
            for item in relevant
            if item.source == "comms_learning/linkedin_examples.jsonl"
            or item.label.startswith("learned_")
        ]
        guided = message
        transformations: list[str] = []
        example_text = "\n".join(item.message.casefold() for item in learned)

        if "i'm" in example_text and re.search(r"\bI am\b", guided):
            guided = re.sub(r"\bI am\b", "I'm", guided)
            transformations.append("prefer_contractions")
        if "does that background fit" in example_text:
            updated = guided.replace(
                "Does that background seem relevant to product work there?",
                "Does that background fit product work there?",
            )
            updated = re.sub(
                r"Does that fit anything at ([^?]+)\?",
                r"Does that background fit anything at \1?",
                updated,
            )
            if updated != guided:
                guided = updated
                transformations.append("prefer_direct_fit_question")
        if "any recs on who" in example_text:
            updated = re.sub(
                r"Any recommendations on who\b",
                "Any recs on who",
                guided,
                flags=re.I,
            )
            if updated != guided:
                guided = updated
                transformations.append("prefer_concise_routing_ask")

        return GuidedStyleDraft(
            message=guided,
            prompt_guidance=self.prompt_guidance(
                recipient_type,
                max_strong_examples=max_examples,
                max_weak_examples=max_examples,
            ),
            strong_example_labels=tuple(item.label for item in learned),
            transformations=tuple(transformations),
        )


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
        "founder_executive": "founder",
        "executive": "founder",
        "engineering": "engineer",
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
    payload = profile.model_dump(mode="json", exclude_none=True)
    for key in ("strong_messages", "weak_messages"):
        for item in payload.get(key, []):
            if isinstance(item, dict) and not item.get("source"):
                item.pop("source", None)
    return payload


def load_comms_learning_examples(path: Path) -> list[dict[str, object]]:
    """Load valid rows from the append-only labeled JSONL corpus."""

    examples, _ = _load_comms_learning_examples(path)
    return examples


def merge_comms_learning_examples(
    profile: CommunicationStyleProfile,
    examples: Iterable[dict[str, object]],
    *,
    contacts: Iterable[ContactRecord] = (),
    organizations: Iterable[OrganizationRecord] = (),
    invalid_rows: int = 0,
) -> tuple[CommunicationStyleProfile, StyleProfileSyncSummary]:
    """Merge gold/silver into strong examples and negative into weak examples.

    Existing profile controls and curated examples always win. The function
    only appends deduplicated corpus evidence; it does not consume or apply
    outcome-learning recommendations.
    """

    example_list = list(examples)
    merged = profile.model_copy(deep=True)
    known_messages = {
        _message_key(item.message)
        for item in [*merged.strong_messages, *merged.weak_messages]
        if _message_key(item.message)
    }
    recipient_lookup = _build_recipient_lookup(list(contacts), list(organizations))
    strong_added = 0
    weak_added = 0
    duplicates = 0
    invalid = invalid_rows

    for raw in example_list:
        label = _normalize_label(raw.get("label"))
        message = str(raw.get("message") or raw.get("message_text") or "").strip()
        if label not in {"gold", "silver", "negative"} or not message:
            invalid += 1
            continue
        key = _message_key(message)
        if not key or key in known_messages:
            duplicates += 1
            continue
        known_messages.add(key)
        recipient_type = _example_recipient_type(raw, recipient_lookup)
        learned = StyleMessageExample(
            label=f"learned_{label}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}",
            recipient_type=recipient_type,
            message=message,
            notes=_example_notes(raw, label),
            source="comms_learning/linkedin_examples.jsonl",
        )
        if label in {"gold", "silver"}:
            merged.strong_messages.append(learned)
            strong_added += 1
        else:
            merged.weak_messages.append(learned)
            weak_added += 1

    summary = StyleProfileSyncSummary(
        examples_seen=len(example_list) + invalid_rows,
        strong_added=strong_added,
        weak_added=weak_added,
        duplicates_skipped=duplicates,
        invalid_skipped=invalid,
        profile_updated=bool(strong_added or weak_added),
    )
    return merged, summary


def sync_comms_learning_into_style_profile(
    *,
    profile_path: Path,
    examples_path: Path,
    contacts: Iterable[ContactRecord] = (),
    organizations: Iterable[OrganizationRecord] = (),
) -> StyleProfileSyncSummary:
    """Persist newly labeled examples while preserving curated profile fields."""

    examples, malformed_rows = _load_comms_learning_examples(examples_path)
    profile = load_style_profile_if_exists(profile_path)
    merged, summary = merge_comms_learning_examples(
        profile,
        examples,
        contacts=contacts,
        organizations=organizations,
        invalid_rows=malformed_rows,
    )
    if summary.profile_updated:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(
            yaml.safe_dump(
                dump_style_profile(merged),
                sort_keys=False,
                allow_unicode=True,
                width=1000,
            ),
            encoding="utf-8",
        )
    return summary


def _load_comms_learning_examples(path: Path) -> tuple[list[dict[str, object]], int]:
    if not path.exists():
        return [], 0
    examples: list[dict[str, object]] = []
    invalid = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if not isinstance(payload, dict):
            invalid += 1
            continue
        examples.append(payload)
    return examples, invalid


def _recipient_relevant_examples(
    examples: Iterable[StyleMessageExample],
    recipient_type: str,
    *,
    limit: int,
) -> list[StyleMessageExample]:
    if limit <= 0:
        return []
    target = normalize_recipient_type(recipient_type)
    exact: list[StyleMessageExample] = []
    general: list[StyleMessageExample] = []
    seen: set[str] = set()
    for item in examples:
        key = _message_key(item.message)
        if not key or key in seen:
            continue
        item_type = normalize_recipient_type(item.recipient_type)
        if item_type == target:
            exact.append(item)
            seen.add(key)
        elif item_type == "general":
            general.append(item)
            seen.add(key)
    return (exact + general)[:limit]


def _bounded_message(message: str, max_chars: int) -> str:
    clean = " ".join(message.split())
    if max_chars <= 0:
        return ""
    if len(clean) <= max_chars:
        return clean
    if max_chars <= 3:
        return clean[:max_chars]
    return clean[: max_chars - 3].rstrip() + "..."


def _message_key(message: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", message.lower()).strip()


def _normalize_label(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _build_recipient_lookup(
    contacts: list[ContactRecord],
    organizations: list[OrganizationRecord],
) -> dict[tuple[str, str], str]:
    org_names = {item.organization_id: item.name for item in organizations}
    result: dict[tuple[str, str], str] = {}
    name_candidates: dict[str, set[str]] = {}
    for contact in contacts:
        name_key = _message_key(contact.full_name)
        company_key = _message_key(org_names.get(contact.organization_id, contact.organization_id))
        recipient_type = _recipient_type_from_contact(contact)
        result[(name_key, company_key)] = recipient_type
        name_candidates.setdefault(name_key, set()).add(recipient_type)
    for name_key, recipient_types in name_candidates.items():
        if len(recipient_types) == 1:
            result[(name_key, "")] = next(iter(recipient_types))
    return result


def _example_recipient_type(
    example: dict[str, object],
    lookup: dict[tuple[str, str], str],
) -> str:
    explicit = str(example.get("recipient_type") or example.get("audience") or "").strip()
    if explicit:
        return normalize_recipient_type(explicit)
    name_key = _message_key(str(example.get("name") or example.get("contact_name") or ""))
    company_key = _message_key(
        str(example.get("company") or example.get("account") or "")
    )
    return lookup.get((name_key, company_key), lookup.get((name_key, ""), "general"))


def _recipient_type_from_contact(contact: ContactRecord) -> str:
    text = " ".join([contact.contact_type, contact.title]).lower()
    if any(value in text for value in ("founder", "co-founder", "chief executive", "ceo")):
        return "founder"
    if any(value in text for value in ("recruit", "talent", "people partner", "human resources")):
        return "recruiter"
    if any(value in text for value in ("associate product manager", "apm", "junior product")):
        return "junior_product_apm"
    if any(value in text for value in ("product manager", "product lead", "product operations", "product ops", "tpm")):
        return "senior_product"
    if any(value in text for value in ("engineer", "developer", "architect", "data scientist")):
        return "engineer"
    if any(value in text for value in ("strategy", "business operations", "bizops", "program manager", "growth")):
        return "strategy_ops"
    return normalize_recipient_type(contact.contact_type)


def _example_notes(example: dict[str, object], label: str) -> str:
    details = [f"Learned {label} example"]
    reason = str(example.get("reason") or "").strip()
    company = str(example.get("company") or example.get("account") or "").strip()
    name = str(example.get("name") or example.get("contact_name") or "").strip()
    if reason:
        details.append(reason)
    if company:
        details.append(f"company={company}")
    if name:
        details.append(f"recipient={name}")
    return " | ".join(details)

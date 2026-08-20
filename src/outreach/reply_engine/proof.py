"""Verified candidate proof beats for constrained reply composition."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ProofBeat:
    beat_id: str
    domains: tuple[str, ...]
    employer: str
    text: str


_DOMAIN_TERMS: dict[str, tuple[str, ...]] = {
    "data_infra": (
        "data platform", "data infrastructure", "infrastructure", "pipeline",
        "developer platform", "api", "integration", "enterprise saas", "database",
        "cloud platform",
    ),
    "reliability": (
        "reliability", "trust", "quality", "monitoring", "observability", "audit",
        "correctness", "failure", "security", "defense", "risk",
    ),
    "billing_fintech": (
        "billing", "fintech", "finance", "financial", "payments", "pricing",
        "monetization", "banking", "insurance", "affordability",
    ),
    "marketplace_logistics": (
        "marketplace", "logistics", "mobility", "ride", "delivery", "fleet",
        "supply", "transport", "routing",
    ),
    "ai_product": (
        " ai ", "artificial intelligence", "machine learning", " ml ", "model",
        "agent", "predictive", "reinforcement learning", "synthetic data",
    ),
    "healthcare": (
        "health", "healthcare", "clinical", "patient", "physician", "provider",
        "medical",
    ),
}

_PROOF_MATCH_STOP = {
    "about", "across", "after", "and", "at", "before", "built", "cut",
    "designed", "diagnosed", "for", "from", "helped", "into", "monthly",
    "product", "shipped", "that", "the", "then", "through", "to", "with",
}


def domains_for_text(value: str) -> set[str]:
    """Return the proof catalog domains explicitly signalled by ``value``."""

    context = f" {value.casefold()} "
    return {
        domain
        for domain, terms in _DOMAIN_TERMS.items()
        if any(term in context for term in terms)
    }


def _proof_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if (len(token) >= 4 or any(character.isdigit() for character in token))
        and token not in _PROOF_MATCH_STOP
    }


def used_proof_beats(message: str, beats: list[ProofBeat]) -> list[ProofBeat]:
    """Identify resume beats actually used in a composed message.

    The writer may paraphrase a supplied beat, so exact sentence matching is
    too brittle.  Employer presence plus two distinctive evidence tokens is
    enough to identify the bounded catalog item without treating a bare brand
    mention as proof usage.
    """

    lowered = message.casefold()
    message_tokens = _proof_tokens(message)
    candidates: dict[str, tuple[int, int, ProofBeat]] = {}
    for index, beat in enumerate(beats):
        aliases = {beat.employer.casefold()}
        first = beat.employer.casefold().split()[0]
        if len(first) >= 4:
            aliases.add(first)
        if not any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            continue
        employer_tokens = _proof_tokens(beat.employer)
        evidence_tokens = _proof_tokens(beat.text) - employer_tokens
        overlap = message_tokens & evidence_tokens
        if len(overlap) < 2:
            continue
        numeric_overlap = sum(any(character.isdigit() for character in token) for token in overlap)
        score = len(overlap) + (2 * numeric_overlap)
        employer = beat.employer.casefold()
        previous = candidates.get(employer)
        if previous is None or score > previous[0]:
            candidates[employer] = (score, index, beat)
    # Compose is constrained to at most one supplied beat. If a paraphrase
    # shares generic words with sibling beats from the same employer, keep only
    # the strongest catalog match instead of charging every sibling as reused.
    return [item[2] for item in sorted(candidates.values(), key=lambda item: item[1])]


def observation_before_proof(message: str, beats: list[ProofBeat]) -> str:
    """Return the recipient/company observation preceding the first proof beat."""

    starts: list[int] = []
    lowered = message.casefold()
    for beat in beats:
        aliases = {beat.employer.casefold(), beat.employer.casefold().split()[0]}
        for alias in aliases:
            match = re.search(rf"\b{re.escape(alias)}\b", lowered)
            if match:
                starts.append(match.start())
    return message[:min(starts)] if starts else ""


def load_proof_beats(path: Path) -> list[ProofBeat]:
    """Load and validate the bounded, resume-derived proof catalog."""

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source_document = str(payload.get("source_document") or "").strip()
    expected_hash = str(payload.get("source_sha256") or "").strip()
    if source_document and expected_hash:
        source_path = path.parent.parent / source_document
        if not source_path.exists():
            raise ValueError(f"proof source document is missing: {source_path}")
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(
                "proof catalog source hash does not match the current resume; "
                "re-extract proof beats before composing"
            )

    beats: list[ProofBeat] = []
    seen: set[str] = set()
    for raw in payload.get("proof_beats") or []:
        beat_id = str(raw.get("id") or "").strip()
        text = " ".join(str(raw.get("text") or "").split())
        employer = str(raw.get("employer") or "").strip()
        domains = tuple(
            str(domain).strip()
            for domain in (raw.get("domains") or [])
            if str(domain).strip()
        )
        if not beat_id or not text or not employer or not domains:
            raise ValueError("every proof beat needs id, domains, employer, and text")
        if beat_id in seen:
            raise ValueError(f"duplicate proof beat id: {beat_id}")
        seen.add(beat_id)
        beats.append(
            ProofBeat(
                beat_id=beat_id,
                domains=domains,
                employer=employer,
                text=text,
            )
        )
    if not beats:
        raise ValueError("proof catalog contains no proof beats")
    return beats


def select_proof_beats(
    beats: list[ProofBeat],
    *,
    recipient_context: str,
    limit: int = 3,
) -> list[ProofBeat]:
    """Return at most three beats whose tagged domains match the recipient."""

    if limit <= 0:
        return []
    context = f" {recipient_context.casefold()} "
    domain_scores = {
        domain: sum(term in context for term in terms)
        for domain, terms in _DOMAIN_TERMS.items()
    }
    ranked: list[tuple[int, int, ProofBeat]] = []
    for index, beat in enumerate(beats):
        score = sum(domain_scores.get(domain, 0) for domain in beat.domains)
        if score:
            ranked.append((score, -index, beat))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    selected: list[ProofBeat] = []
    selected_ids: set[str] = set()
    employers: set[str] = set()

    matched_domains = sorted(
        (domain for domain, score in domain_scores.items() if score),
        key=lambda domain: -domain_scores[domain],
    )
    # Cover distinct matching domains before adding a second example from the
    # same domain.  Otherwise three reliability beats crowd out the one AI or
    # healthcare beat that actually explains the recipient match.
    for domain in matched_domains:
        candidate = next(
            (
                beat
                for _, _, beat in ranked
                if domain in beat.domains
                and beat.beat_id not in selected_ids
                and beat.employer.casefold() not in employers
            ),
            None,
        )
        if candidate is None:
            continue
        selected.append(candidate)
        selected_ids.add(candidate.beat_id)
        employers.add(candidate.employer.casefold())
        if len(selected) >= limit:
            return selected

    for _, _, beat in ranked:
        employer = beat.employer.casefold()
        if employer in employers:
            continue
        selected.append(beat)
        selected_ids.add(beat.beat_id)
        employers.add(employer)
        if len(selected) >= max(0, limit):
            return selected
    for _, _, beat in ranked:
        if beat.beat_id in selected_ids:
            continue
        selected.append(beat)
        if len(selected) >= max(0, limit):
            break
    return selected


def render_usable_proof(beats: list[ProofBeat]) -> str:
    return "\n".join(f"- {beat.employer}: {beat.text}" for beat in beats)


def normalized_evidence_text(beats: list[ProofBeat], profile_text: str = "") -> str:
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", value.casefold())
        for value in [profile_text, *(beat.text for beat in beats)]
    )

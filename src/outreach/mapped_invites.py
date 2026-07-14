from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from outreach.ai_messaging import institution_signals_from_text
from outreach.config import OutreachSettings
from outreach.models import CandidateProfile, PriorityTier
from outreach.reviewed_linkedin import canonical_linkedin_profile
from outreach.scoring import score_candidate
from outreach.services.notes import NoteGenerator
from outreach.tracking import ContactRecord, OrganizationRecord, TouchpointRecord


_UNSENT_CONTACT_STATUSES = {
    "",
    "discovered",
    "queued",
    "invite error",
    "navigation error",
    "invite not sent",
}

_BLOCKED_CONTACT_TAGS = {
    "current-role-review-required",
    "do-not-contact",
    "locator-review-hold",
    "outreach-hold",
}

_BLOCKING_INVITE_TOUCHPOINT_STATUSES = {
    "accepted",
    "already connected",
    "already reserved",
    "prepared",
    "sent",
    "unknown reserved",
}


def build_mapped_invite_candidates(
    *,
    organization: OrganizationRecord,
    contacts: Iterable[ContactRecord],
    touchpoints: Iterable[TouchpointRecord] = (),
    settings: OutreachSettings | None = None,
) -> list[dict[str, object]]:
    """Return unsent workbook contacts as send-ready candidate records.

    The workbook assignment is the routing source of truth: once a reviewed
    contact is attached to an organization, an account run should consume that
    locator instead of hoping the same person appears in another bounded search.
    Explicit holds, prior invite records, and non-sendable statuses fail closed.

    Shared-school contacts sort ahead of ordinary mapped contacts. Within that
    warm-path lane, dual-school, Marshall, and source priority provide stable
    tie-breakers before the normal candidate score.
    """

    runtime_settings = settings or OutreachSettings()
    invited_contact_ids = _contacts_with_blocking_invite_touchpoints(touchpoints)
    candidates: list[dict[str, object]] = []
    for contact in contacts:
        if contact.organization_id != organization.organization_id:
            continue
        if not contact.linkedin_url.strip():
            continue
        if contact.contact_id in invited_contact_ids:
            continue
        if contact.status.strip().casefold() not in _UNSENT_CONTACT_STATUSES:
            continue
        tags = _split_tags(contact.target_lists)
        if tags.intersection(_BLOCKED_CONTACT_TAGS):
            continue

        context = " ".join([contact.target_lists, contact.notes])
        institution_signals = set(institution_signals_from_text(context))
        has_marshall = "usc_marshall" in institution_signals
        has_usc = "usc" in institution_signals
        has_thapar = "thapar" in institution_signals
        shared_history_signals = ["Thapar"] if has_thapar else []
        role_bucket = _mapped_role_bucket(contact)
        connection_degree = _connection_degree(contact.notes)
        scored = score_candidate(
            CandidateProfile(
                name=contact.full_name,
                title=contact.title,
                company=organization.name,
                linkedin_url=contact.linkedin_url,
                connection_degree=connection_degree,
                usc_marshall=has_marshall,
                usc_alumni=has_usc,
                shared_history=has_thapar,
                role_bucket=role_bucket,
            ),
            runtime_settings.scoring,
        )
        lead_priority = _lead_priority(tags, contact.notes)
        score = scored.score + {"high": 8, "medium": 4}.get(lead_priority, 0)
        tier = (
            PriorityTier.HIGH.value
            if score >= 80
            else PriorityTier.MEDIUM.value
            if score >= 35
            else PriorityTier.LOW.value
        )
        affinity_rank = _affinity_rank(
            has_marshall=has_marshall,
            has_usc=has_usc,
            has_thapar=has_thapar,
        )
        current_role_evidence = f"Current: {contact.title} at {organization.name}".strip()
        candidates.append(
            {
                "name": contact.full_name,
                "title": contact.title,
                "subtitle": f"{contact.title} at {organization.name}".strip(),
                "company": organization.name,
                "linkedin_url": contact.linkedin_url,
                "connection_degree": connection_degree,
                "snippet": current_role_evidence,
                "raw_text": " | ".join(
                    value
                    for value in (current_role_evidence, contact.notes)
                    if value.strip()
                ),
                "role_bucket": role_bucket,
                "score": score,
                "tier": tier,
                "triggers": list(
                    dict.fromkeys(
                        [
                            *scored.triggers,
                            "Reviewed mapped contact",
                            *([f"Lead priority: {lead_priority}"] if lead_priority else []),
                        ]
                    )
                ),
                "passes": ["mapped_workbook_contact"],
                "existing_connection": False,
                "usc_marshall": has_marshall,
                "usc": has_usc,
                "shared_history": has_thapar,
                "shared_history_signals": shared_history_signals,
                "target_company_match": True,
                "target_company_evidence_company": organization.name,
                "target_company_evidence_passes": ["mapped_workbook_contact"],
                "mapped_contact_id": contact.contact_id,
                "mapped_contact_priority": lead_priority,
                "mapped_contact_affinity_rank": affinity_rank,
                "mapped_contact_institution_signals": sorted(institution_signals),
                "priority_bucket": "Mapped warm path" if affinity_rank else tier,
            }
        )

    candidates.sort(key=_candidate_sort_key, reverse=True)
    return candidates


def merge_and_prioritize_invite_candidates(
    mapped: Iterable[Mapping[str, object]],
    discovered: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Merge candidate sources by LinkedIn identity and put warm paths first."""

    by_identity: dict[str, dict[str, object]] = {}
    for raw in [*discovered, *mapped]:
        candidate = dict(raw)
        identity = _candidate_identity(candidate)
        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = candidate
            continue
        # A reviewed workbook row wins identity/routing metadata. Preserve a
        # generated note from either source if the preferred row lacks one.
        preferred = (
            candidate
            if candidate.get("mapped_contact_id")
            else existing
            if existing.get("mapped_contact_id")
            else max((existing, candidate), key=_candidate_sort_key)
        )
        fallback = existing if preferred is candidate else candidate
        for field in (
            "note",
            "note_family",
            "note_ask_style",
            "note_qc",
            "style_review",
            "target_role_family",
            "target_role_label",
            "target_role_source",
            "target_role_matched_text",
            "target_role_matched_rule",
            "target_role_is_concrete",
        ):
            if not preferred.get(field) and fallback.get(field):
                preferred[field] = fallback[field]
        by_identity[identity] = preferred

    merged = list(by_identity.values())
    merged.sort(key=_candidate_sort_key, reverse=True)
    return merged


def augment_invite_source_with_mapped_contacts(
    *,
    organization: OrganizationRecord,
    contacts: Iterable[ContactRecord],
    touchpoints: Iterable[TouchpointRecord],
    settings: OutreachSettings,
    note_generator: NoteGenerator,
    note_context: dict[str, object] | None = None,
    target_role_title: str = "",
    search_payload: Mapping[str, object] | None = None,
    search_error: str = "",
    candidate_limit: int = 15,
) -> tuple[dict[str, object], int]:
    """Merge reviewed mapped contacts into a note-generated invite source.

    A failed live company search must not erase already reviewed workbook
    locators. In that case the mapped assignment becomes the independent source
    and failed search metadata is retained only as diagnostics, never as an
    authorization gate. Every mapped row passes through normal note generation
    and QC before it can reach candidate selection.
    """

    contact_rows = list(contacts)
    mapped = build_mapped_invite_candidates(
        organization=organization,
        contacts=contact_rows,
        touchpoints=touchpoints,
        settings=settings,
    )[: max(0, candidate_limit)]
    search = dict(search_payload or {})
    filter_failed = _search_payload_filter_failed(search)
    blocked_identities = {
        _candidate_identity({"linkedin_url": contact.linkedin_url})
        for contact in contact_rows
        if contact.organization_id == organization.organization_id
        and contact.linkedin_url.strip()
        and _split_tags(contact.target_lists).intersection(_BLOCKED_CONTACT_TAGS)
    }
    if blocked_identities and not filter_failed:
        search["results"] = [
            dict(candidate)
            for candidate in list(search.get("results") or [])
            if isinstance(candidate, Mapping)
            and _candidate_identity(candidate) not in blocked_identities
        ]
    if not mapped:
        return search, 0

    target_role = target_role_title.strip()
    if target_role:
        mapped = [{**candidate, "target_role_title": target_role} for candidate in mapped]
    prepared = note_generator.generate_batch(
        mapped,
        company=organization.name,
        company_mode=_company_mode(organization),
        note_context=note_context,
    )

    discovered = (
        []
        if filter_failed
        else [
            dict(candidate)
            for candidate in list(search.get("results") or [])
            if isinstance(candidate, Mapping)
            and _candidate_identity(candidate) not in blocked_identities
        ]
    )
    merged = merge_and_prioritize_invite_candidates(prepared, discovered)
    original_passes = [
        dict(item)
        for item in list(search.get("pass_summaries") or [])
        if isinstance(item, Mapping)
    ]
    mapped_pass = {
        "pass_name": "mapped_workbook_contacts",
        "status": "completed",
        "raw_count": len(mapped),
        "kept_count": len(prepared),
        "source": "reviewed_workbook_assignment",
    }
    payload: dict[str, object] = {
        **search,
        "company": organization.name,
        "company_mode": str(search.get("company_mode") or _company_mode(organization)),
        "dry_run": True,
        "source": "mapped_workbook_contacts+linkedin_company_search",
        "company_filter_status": (
            "completed_mapped_workbook_assignment"
            if filter_failed or not search
            else str(search.get("company_filter_status") or "completed")
        ),
        "company_filter_error": "" if filter_failed else str(search.get("company_filter_error") or ""),
        "pass_summaries": [mapped_pass, *([] if filter_failed else original_passes)],
        "company_search_filter_status": str(search.get("company_filter_status") or ""),
        "company_search_filter_error": str(search.get("company_filter_error") or ""),
        "company_search_pass_summaries": original_passes if filter_failed else [],
        "company_search_error": search_error,
        "mapped_contact_count": len(prepared),
        "count": len(merged),
        "send_safe_candidate_count": len(merged),
        "results": merged,
    }
    return payload, len(prepared)


def _contacts_with_blocking_invite_touchpoints(
    touchpoints: Iterable[TouchpointRecord],
) -> set[str]:
    return {
        item.contact_id
        for item in touchpoints
        if item.contact_id
        and item.message_kind.strip().casefold() == "linkedin_invite"
        and item.status.strip().casefold() in _BLOCKING_INVITE_TOUCHPOINT_STATUSES
    }


def _mapped_role_bucket(contact: ContactRecord) -> str:
    contact_type = contact.contact_type.strip().casefold()
    title = contact.title.strip().casefold()
    if contact_type in {"founder", "executive", "c-suite", "c suite"} or re.search(
        r"\b(?:co-?founder|founder|chief\s+\w+\s+officer|ceo|cto|cpo)\b",
        title,
    ):
        return "Founder"
    if contact_type in {"product", "product leader", "product management"} or re.search(
        r"\b(?:product manager|product management|product strategy|head of product|product lead)\b",
        title,
    ):
        return "Product"
    if contact_type in {"engineering", "engineer"} or re.search(
        r"\b(?:engineer|engineering|developer|architect)\b",
        title,
    ):
        return "Engineering"
    if contact_type in {"recruiting", "recruiter"} or re.search(
        r"\b(?:recruiter|recruiting|talent|sourcer)\b",
        title,
    ):
        return "Recruiting"
    return "Adjacent" if re.search(r"\b(?:strategy|operations|program)\b", title) else "Other"


def _connection_degree(notes: str) -> str:
    match = re.search(r"\bconnection_degree\s*=\s*(1st|2nd|3rd)\b", notes, flags=re.I)
    return match.group(1).lower() if match else "3rd"


def _lead_priority(tags: set[str], notes: str) -> str:
    if "priority-high" in tags:
        return "high"
    if "priority-medium" in tags:
        return "medium"
    if "priority-low" in tags:
        return "low"
    match = re.search(r"\blead_priority\s*=\s*(high|medium|low)\b", notes, flags=re.I)
    return match.group(1).lower() if match else ""


def _affinity_rank(*, has_marshall: bool, has_usc: bool, has_thapar: bool) -> int:
    if has_thapar and has_usc:
        return 4
    if has_marshall:
        return 3
    if has_thapar:
        return 2
    if has_usc:
        return 1
    return 0


def _candidate_sort_key(candidate: Mapping[str, object]) -> tuple[int, int, int, int, str]:
    try:
        explicit_affinity = int(candidate.get("mapped_contact_affinity_rank") or 0)
    except (TypeError, ValueError):
        explicit_affinity = 0
    if not explicit_affinity:
        signals = set(institution_signals_from_text(_candidate_context(candidate)))
        explicit_affinity = _affinity_rank(
            has_marshall=bool(candidate.get("usc_marshall")) or "usc_marshall" in signals,
            has_usc=bool(candidate.get("usc")) or "usc" in signals,
            has_thapar=bool(candidate.get("shared_history")) and "thapar" in signals
            or "thapar" in signals,
        )
    priority = {"high": 3, "medium": 2, "low": 1}.get(
        str(candidate.get("mapped_contact_priority") or "").casefold(),
        0,
    )
    try:
        score = int(candidate.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return (
        int(explicit_affinity > 0),
        explicit_affinity,
        priority,
        score,
        str(candidate.get("name") or "").casefold(),
    )


def _candidate_identity(candidate: Mapping[str, object]) -> str:
    raw_linkedin_url = str(candidate.get("linkedin_url") or "").strip()
    linkedin_url = canonical_linkedin_profile(raw_linkedin_url).casefold().rstrip("/")
    if not linkedin_url:
        linkedin_url = raw_linkedin_url.casefold().rstrip("/")
    if linkedin_url:
        return f"linkedin:{linkedin_url}"
    return "person:" + "|".join(
        re.sub(r"\W+", "", str(candidate.get(field) or "").casefold())
        for field in ("name", "company")
    )


def _candidate_context(candidate: Mapping[str, object]) -> str:
    values: list[str] = []
    for field in (
        "school",
        "relationship_signal",
        "shared_history_signals",
        "notes",
        "raw_text",
        "snippet",
        "triggers",
    ):
        value = candidate.get(field)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value)
        else:
            values.append(str(value or ""))
    return " ".join(values)


def _search_payload_filter_failed(payload: Mapping[str, object]) -> bool:
    status = str(payload.get("company_filter_status") or "").strip().casefold()
    if status.startswith("failed"):
        return True
    error = str(payload.get("company_filter_error") or "")
    return "Could not find an exact company suggestion for" in error


def _company_mode(organization: OrganizationRecord) -> str:
    return "startup" if organization.organization_type.value in {
        "startup",
        "accelerator",
        "incubator",
        "hacker_house",
    } else "default"


def _split_tags(value: str) -> set[str]:
    return {
        item.strip().casefold()
        for item in re.split(r"[;,]", value or "")
        if item.strip()
    }

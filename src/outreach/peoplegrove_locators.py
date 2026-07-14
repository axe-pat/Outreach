from __future__ import annotations

import csv
import hashlib
import json
import re
import tempfile
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from outreach.models import RawSearchCandidate
from outreach.tracking import ContactRecord, OutreachWorkbook, utc_now_iso


PEOPLEGROVE_LOCATOR_SCHEMA_VERSION = 1
DEFAULT_PEOPLEGROVE_LOCATOR_STATE_PATH = Path(
    "workspace/peoplegrove_linkedin_locator_research.json"
)
DEFAULT_PEOPLEGROVE_LOCATOR_REVIEW_PATH = Path(
    "workspace/peoplegrove_linkedin_locator_review.csv"
)
PEOPLEGROVE_LINKEDIN_SCHOOL = "University of Southern California"
MAX_PEOPLEGROVE_LOCATOR_SEARCHES_PER_RUN = 25

_IGNORED_PERSON_NAME_TOKENS = {
    "cfa",
    "cpa",
    "csm",
    "edd",
    "esq",
    "ii",
    "iii",
    "iv",
    "jd",
    "jr",
    "mba",
    "md",
    "ms",
    "msc",
    "pe",
    "phd",
    "pmp",
    "sr",
}
_LINKEDIN_PERSON_PATH = re.compile(r"^/(?:in|pub)/[^/]+/?$", re.IGNORECASE)

PEOPLEGROVE_LOCATOR_REVIEW_FIELDS = [
    "contact_id",
    "organization_id",
    "company",
    "full_name",
    "title",
    "status",
    "linkedin_url",
    "matched_name",
    "matched_title",
    "connection_degree",
    "exact_match_count",
    "candidate_count",
    "searched_at",
    "detail",
    "company_corroborated",
    "identity_review_status",
    "review_decision",
    "review_notes",
]

_APPROVED_REVIEW_DECISIONS = {"approve", "approved", "verified", "yes"}
_COMPANY_SUFFIX_TOKENS = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "limited",
    "llc",
    "ltd",
}
_LOCATOR_REVIEW_HOLD_TAG = "locator-review-hold"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalized_person_name(value: str) -> str:
    """Normalize a person name without treating middle initials as identity."""

    ascii_value = (
        unicodedata.normalize("NFKD", value or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    tokens = re.findall(r"[a-z0-9]+", ascii_value)
    return " ".join(
        token
        for token in tokens
        if len(token) > 1 and token not in _IGNORED_PERSON_NAME_TOKENS
    )


def peoplegrove_name_identity_variants(value: str) -> tuple[str, ...]:
    """Return explicit full/nickname/maiden variants without fuzzy matching."""

    variants: list[str] = []

    def add(raw: str) -> None:
        normalized = normalized_person_name(raw)
        # A single surviving token (for example ``Anna T.`` -> ``anna``) is
        # too broad for automatic identity resolution.
        if len(normalized.split()) < 2 or normalized in variants:
            return
        variants.append(normalized)

    parentheticals = list(re.finditer(r"\(([^()]*)\)", value or ""))
    if parentheticals:
        add(re.sub(r"\([^()]*\)", " ", value))
    add(value)
    for match in parentheticals:
        suffix = value[match.end() :]
        add(f"{match.group(1)} {suffix}")
    return tuple(variants)


def peoplegrove_locator_search_queries(value: str) -> tuple[str, ...]:
    """Produce rate-bounded LinkedIn queries with credentials removed."""

    return peoplegrove_name_identity_variants(value)


def canonical_linkedin_person_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in {"linkedin.com", "www.linkedin.com", "ca.linkedin.com"}:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path or "")
    if not _LINKEDIN_PERSON_PATH.fullmatch(path):
        return ""
    return urlunsplit(("https", "www.linkedin.com", path.rstrip("/") + "/", "", ""))


def peoplegrove_result_company_corroborated(result: Mapping[str, object]) -> bool:
    """Require independent employer evidence beyond one bounded exact-name page."""

    if str(result.get("search_mode") or "") == "usc_school_and_current_company":
        return True
    company_tokens = re.findall(r"[a-z0-9]+", str(result.get("company") or "").casefold())
    while company_tokens and company_tokens[-1] in _COMPANY_SUFFIX_TOKENS:
        company_tokens.pop()
    if not company_tokens or len("".join(company_tokens)) < 4:
        return False

    linkedin_url = canonical_linkedin_person_url(str(result.get("linkedin_url") or ""))
    evidence_parts = [str(result.get("matched_title") or "")]
    candidates = result.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            if canonical_linkedin_person_url(str(candidate.get("linkedin_url") or "")) != linkedin_url:
                continue
            evidence_parts.extend(
                [
                    str(candidate.get("title") or ""),
                    str(candidate.get("headline") or ""),
                    str(candidate.get("snippet") or ""),
                ]
            )
            break
    evidence = re.sub(r"\s+", " ", " | ".join(evidence_parts).casefold()).strip()
    if not evidence:
        return False
    company_pattern = r"\b" + r"[^a-z0-9]+".join(
        re.escape(token) for token in company_tokens
    ) + r"(?=$|\s*(?:[|·,;()]|[-—]\s))"
    for match in re.finditer(company_pattern, evidence):
        segment_start = max(
            evidence.rfind("|", 0, match.start()),
            evidence.rfind("·", 0, match.start()),
            evidence.rfind(";", 0, match.start()),
            evidence.rfind(",", 0, match.start()),
        )
        prefix = evidence[segment_start + 1 : match.start()]
        if re.search(r"\b(?:ex|former|formerly|past|previously)\b", prefix):
            continue
        return True
    return False


def _review_decision_approved(value: object) -> bool:
    return str(value or "").strip().casefold() in _APPROVED_REVIEW_DECISIONS


def _contact_tags(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;,]", value or "") if item.strip()]


def _set_locator_review_hold(value: str, *, required: bool) -> str:
    tags = _contact_tags(value)
    tags = [tag for tag in tags if tag.casefold() != _LOCATOR_REVIEW_HOLD_TAG]
    if required:
        tags.append(_LOCATOR_REVIEW_HOLD_TAG)
    return ";".join(tags)


def is_peoplegrove_contact(contact: ContactRecord) -> bool:
    text = " ".join([contact.source_url, contact.notes, contact.target_lists]).lower()
    return "peoplegrove" in text or "trojan-network" in text


def build_peoplegrove_locator_queue(workspace: Path) -> list[dict[str, str]]:
    workbook = OutreachWorkbook(workspace)
    organizations = {
        organization.organization_id: organization
        for organization in workbook.list_organizations()
    }
    priority_order = {"high": 0, "medium": 1, "low": 2}
    queue: list[dict[str, str]] = []
    for contact in workbook.list_contacts():
        if not is_peoplegrove_contact(contact):
            continue
        if contact.linkedin_url.strip() or contact.email.strip():
            continue
        organization = organizations.get(contact.organization_id)
        priority = _priority_from_contact(contact)
        queue.append(
            {
                "contact_id": contact.contact_id,
                "organization_id": contact.organization_id,
                "company": organization.name if organization else "",
                "full_name": contact.full_name,
                "title": contact.title,
                "priority": priority,
                "source_url": contact.source_url,
            }
        )
    return sorted(
        queue,
        key=lambda row: (
            priority_order.get(row["priority"], 3),
            normalized_person_name(row["full_name"]),
            row["contact_id"],
        ),
    )


def _priority_from_contact(contact: ContactRecord) -> str:
    lower = f"{contact.target_lists} {contact.notes}".lower()
    match = re.search(r"(?:^|[|; ])priority[=:]([a-z]+)", lower)
    if match:
        return match.group(1)
    if any(token in lower for token in ["founder", "c-suite", "executive", "leadership"]):
        return "high"
    return "medium"


def match_peoplegrove_locator_candidates(
    target: Mapping[str, str],
    candidates: Iterable[RawSearchCandidate | Mapping[str, object]],
) -> dict[str, object]:
    target_variants = set(
        peoplegrove_name_identity_variants(str(target.get("full_name") or ""))
    )
    candidate_rows: list[dict[str, str]] = []
    for raw_candidate in candidates:
        if isinstance(raw_candidate, RawSearchCandidate):
            raw = raw_candidate.model_dump()
        else:
            raw = dict(raw_candidate)
        url = canonical_linkedin_person_url(str(raw.get("linkedin_url") or ""))
        if not url:
            continue
        candidate_rows.append(
            {
                "name": str(raw.get("name") or "").strip(),
                "title": str(raw.get("title") or "").strip(),
                "connection_degree": str(raw.get("connection_degree") or "").strip(),
                "location": str(raw.get("location") or "").strip(),
                "linkedin_url": url,
                "snippet": str(raw.get("snippet") or "").strip(),
            }
        )

    exact_by_url = {
        row["linkedin_url"]: row
        for row in candidate_rows
        if target_variants.intersection(peoplegrove_name_identity_variants(row["name"]))
    }
    exact = list(exact_by_url.values())
    searched_at = _now_iso()
    base: dict[str, object] = {
        "contact_id": str(target.get("contact_id") or ""),
        "organization_id": str(target.get("organization_id") or ""),
        "company": str(target.get("company") or ""),
        "full_name": str(target.get("full_name") or ""),
        "title": str(target.get("title") or ""),
        "searched_at": searched_at,
        "candidate_count": len(candidate_rows),
        "exact_match_count": len(exact),
        "target_name_variants": sorted(target_variants),
        "candidates": candidate_rows,
    }
    if not target_variants:
        return {
            **base,
            "status": "ambiguous_exact",
            "linkedin_url": "",
            "matched_name": "",
            "matched_title": "",
            "connection_degree": "",
            "detail": (
                "The captured name does not retain two identity tokens after normalization; "
                "manual review is required."
            ),
        }
    if len(exact) == 1:
        match = exact[0]
        return {
            **base,
            "status": "resolved_exact",
            "linkedin_url": match["linkedin_url"],
            "matched_name": match["name"],
            "matched_title": match["title"],
            "connection_degree": match["connection_degree"],
            "detail": (
                "One exact normalized-name result appeared on the bounded first page under "
                "LinkedIn's verified USC school filter; company corroboration or explicit "
                "review is still required before application."
            ),
        }
    if len(exact) > 1:
        return {
            **base,
            "status": "ambiguous_exact",
            "linkedin_url": "",
            "matched_name": "",
            "matched_title": "",
            "connection_degree": "",
            "detail": "Multiple exact-name USC-filtered profiles require manual review.",
        }
    return {
        **base,
        "status": "no_exact_match",
        "linkedin_url": "",
        "matched_name": "",
        "matched_title": "",
        "connection_degree": "",
        "detail": "No exact normalized-name profile appeared in the bounded USC-filtered result page.",
    }


def error_peoplegrove_locator_result(
    target: Mapping[str, str],
    error: Exception | str,
) -> dict[str, object]:
    return {
        "contact_id": str(target.get("contact_id") or ""),
        "organization_id": str(target.get("organization_id") or ""),
        "company": str(target.get("company") or ""),
        "full_name": str(target.get("full_name") or ""),
        "title": str(target.get("title") or ""),
        "searched_at": _now_iso(),
        "candidate_count": 0,
        "exact_match_count": 0,
        "candidates": [],
        "status": "error",
        "linkedin_url": "",
        "matched_name": "",
        "matched_title": "",
        "connection_degree": "",
        "detail": str(error),
    }


def is_stop_worthy_linkedin_error(error: Exception | str) -> bool:
    lower = str(error).lower()
    return any(
        marker in lower
        for marker in [
            "authwall",
            "checkpoint",
            "challenge",
            "security verification",
            "rate limit",
            "too many requests",
            "temporarily restricted",
            "preflight failed",
            "429",
        ]
    )


def new_peoplegrove_locator_state(
    *,
    workspace: Path,
    targets: Iterable[Mapping[str, str]],
) -> dict[str, object]:
    created_at = _now_iso()
    target_rows = {
        str(target["contact_id"]): {key: str(value) for key, value in target.items()}
        for target in targets
    }
    return {
        "schema_version": PEOPLEGROVE_LOCATOR_SCHEMA_VERSION,
        "created_at": created_at,
        "updated_at": created_at,
        "workspace": str(workspace),
        "school_filter": PEOPLEGROVE_LINKEDIN_SCHOOL,
        "targets": target_rows,
        "results": {},
        "runs": [],
    }


def load_or_create_peoplegrove_locator_state(
    *,
    path: Path,
    workspace: Path,
    targets: Iterable[Mapping[str, str]],
) -> dict[str, object]:
    target_list = list(targets)
    if not path.exists():
        return new_peoplegrove_locator_state(workspace=workspace, targets=target_list)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != PEOPLEGROVE_LOCATOR_SCHEMA_VERSION:
        raise ValueError("Unsupported PeopleGrove locator state schema")
    stored_targets = payload.get("targets")
    results = payload.get("results")
    if not isinstance(stored_targets, dict) or not isinstance(results, dict):
        raise ValueError("PeopleGrove locator state requires object targets and results")
    for target in target_list:
        contact_id = str(target["contact_id"])
        existing = stored_targets.get(contact_id)
        if existing is not None and (
            str(existing.get("full_name") or "") != str(target.get("full_name") or "")
            or str(existing.get("organization_id") or "")
            != str(target.get("organization_id") or "")
        ):
            raise ValueError(f"PeopleGrove locator target identity changed: {contact_id}")
        stored_targets.setdefault(
            contact_id,
            {key: str(value) for key, value in target.items()},
        )
    return payload


def pending_peoplegrove_locator_targets(
    state: Mapping[str, object],
    *,
    retry_errors: bool = False,
    retry_no_match: bool = False,
) -> list[dict[str, str]]:
    targets = state.get("targets")
    results = state.get("results")
    if not isinstance(targets, dict) or not isinstance(results, dict):
        raise ValueError("PeopleGrove locator state requires object targets and results")
    pending: list[dict[str, str]] = []
    for contact_id, raw_target in targets.items():
        if not isinstance(raw_target, dict):
            raise ValueError(f"Invalid PeopleGrove locator target: {contact_id}")
        result = results.get(contact_id)
        status = str(result.get("status") or "") if isinstance(result, dict) else ""
        if not result or (retry_errors and status == "error") or (
            retry_no_match and status == "no_exact_match"
        ):
            pending.append({key: str(value) for key, value in raw_target.items()})
    priority_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        pending,
        key=lambda row: (
            priority_order.get(row.get("priority", ""), 3),
            normalized_person_name(row.get("full_name", "")),
            row.get("contact_id", ""),
        ),
    )


def peoplegrove_locator_summary(state: Mapping[str, object]) -> dict[str, int]:
    results = state.get("results")
    targets = state.get("targets")
    result_rows = list(results.values()) if isinstance(results, dict) else []
    counts = Counter(
        str(row.get("status") or "unknown")
        for row in result_rows
        if isinstance(row, dict)
    )
    total = len(targets) if isinstance(targets, dict) else 0
    return {
        "targets": total,
        "searched": len(result_rows),
        "pending": max(0, total - len(result_rows)),
        "resolved_exact": counts["resolved_exact"],
        "ambiguous_exact": counts["ambiguous_exact"],
        "no_exact_match": counts["no_exact_match"],
        "error": counts["error"],
    }


def save_peoplegrove_locator_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now_iso()
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def write_peoplegrove_locator_review_csv(
    *,
    path: Path,
    state: Mapping[str, object],
) -> None:
    preserved: dict[str, tuple[str, str]] = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                preserved[str(row.get("contact_id") or "")] = (
                    str(row.get("review_decision") or ""),
                    str(row.get("review_notes") or ""),
                )
    targets = state.get("targets")
    results = state.get("results")
    if not isinstance(targets, dict) or not isinstance(results, dict):
        raise ValueError("PeopleGrove locator state requires object targets and results")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PEOPLEGROVE_LOCATOR_REVIEW_FIELDS)
        writer.writeheader()
        for contact_id, target in sorted(
            targets.items(),
            key=lambda item: normalized_person_name(str(item[1].get("full_name") or "")),
        ):
            result = results.get(contact_id) if isinstance(results.get(contact_id), dict) else {}
            prior_decision, prior_notes = preserved.get(contact_id, ("", ""))
            row = {
                "contact_id": contact_id,
                "organization_id": str(target.get("organization_id") or ""),
                "company": str(target.get("company") or ""),
                "full_name": str(target.get("full_name") or ""),
                "title": str(target.get("title") or ""),
                "status": str(result.get("status") or "pending"),
                "linkedin_url": str(result.get("linkedin_url") or ""),
                "matched_name": str(result.get("matched_name") or ""),
                "matched_title": str(result.get("matched_title") or ""),
                "connection_degree": str(result.get("connection_degree") or ""),
                "exact_match_count": str(result.get("exact_match_count") or ""),
                "candidate_count": str(result.get("candidate_count") or ""),
                "searched_at": str(result.get("searched_at") or ""),
                "detail": str(result.get("detail") or ""),
                "company_corroborated": str(
                    peoplegrove_result_company_corroborated(result)
                    if result
                    else ""
                ),
                "identity_review_status": str(
                    result.get("identity_review_status") or ""
                ),
                "review_decision": prior_decision,
                "review_notes": prior_notes,
            }
            writer.writerow(row)


def apply_exact_peoplegrove_locator_results(
    *,
    workspace: Path,
    state: dict[str, object],
    execute: bool,
    review_decisions: Mapping[str, str] | None = None,
) -> dict[str, object]:
    workbook = OutreachWorkbook(workspace)
    contacts = {contact.contact_id: contact for contact in workbook.list_contacts()}
    owners = {
        canonical_linkedin_person_url(contact.linkedin_url): contact.contact_id
        for contact in contacts.values()
        if canonical_linkedin_person_url(contact.linkedin_url)
    }
    # Reserve each accepted URL while building the batch as well as checking the
    # persisted workbook. Otherwise two new state rows could both be considered
    # ready for the same previously unseen LinkedIn profile.
    reserved_owners = dict(owners)
    results = state.get("results")
    if not isinstance(results, dict):
        raise ValueError("PeopleGrove locator state requires object results")
    ready: list[dict[str, str]] = []
    blocked: list[dict[str, str]] = []
    review_required: list[dict[str, str]] = []
    held_existing: list[dict[str, str]] = []
    released_existing: list[dict[str, str]] = []
    skipped_existing: list[dict[str, str]] = []
    decisions = review_decisions or {}
    for contact_id, raw_result in results.items():
        if not isinstance(raw_result, dict) or raw_result.get("status") != "resolved_exact":
            continue
        contact = contacts.get(contact_id)
        if contact is None:
            blocked.append({"contact_id": contact_id, "reason": "contact_missing"})
            continue
        if normalized_person_name(contact.full_name) != normalized_person_name(
            str(raw_result.get("full_name") or "")
        ):
            blocked.append({"contact_id": contact_id, "reason": "contact_name_changed"})
            continue
        linkedin_url = canonical_linkedin_person_url(str(raw_result.get("linkedin_url") or ""))
        if not linkedin_url:
            blocked.append({"contact_id": contact_id, "reason": "invalid_linkedin_url"})
            continue
        company_corroborated = peoplegrove_result_company_corroborated(raw_result)
        human_approved = _review_decision_approved(decisions.get(contact_id))
        if company_corroborated:
            raw_result["identity_review_status"] = "auto_company_corroborated"
        elif human_approved:
            raw_result["identity_review_status"] = "human_approved"
        else:
            raw_result["identity_review_status"] = "review_required"
            review_required.append(
                {
                    "contact_id": contact_id,
                    "reason": "exact_name_requires_company_corroboration_or_review",
                    "linkedin_url": linkedin_url,
                }
            )
            if (
                execute
                and canonical_linkedin_person_url(contact.linkedin_url) == linkedin_url
                and raw_result.get("applied_at")
            ):
                updated_tags = _set_locator_review_hold(
                    contact.target_lists,
                    required=True,
                )
                if updated_tags != contact.target_lists:
                    workbook.update_contact(contact_id, target_lists=updated_tags)
                    held_existing.append(
                        {"contact_id": contact_id, "linkedin_url": linkedin_url}
                    )
            continue
        if contact.linkedin_url.strip() or contact.email.strip():
            if (
                execute
                and canonical_linkedin_person_url(contact.linkedin_url) == linkedin_url
                and _LOCATOR_REVIEW_HOLD_TAG in {
                    tag.casefold() for tag in _contact_tags(contact.target_lists)
                }
            ):
                workbook.update_contact(
                    contact_id,
                    target_lists=_set_locator_review_hold(
                        contact.target_lists,
                        required=False,
                    ),
                )
                released_existing.append(
                    {"contact_id": contact_id, "linkedin_url": linkedin_url}
                )
            skipped_existing.append(
                {"contact_id": contact_id, "reason": "contact_already_has_locator"}
            )
            continue
        owner = reserved_owners.get(linkedin_url)
        if owner and owner != contact_id:
            blocked.append(
                {
                    "contact_id": contact_id,
                    "reason": "linkedin_url_owned_by_other_contact",
                    "owner_contact_id": owner,
                }
            )
            continue
        ready.append({"contact_id": contact_id, "linkedin_url": linkedin_url})
        reserved_owners[linkedin_url] = contact_id

    applied: list[dict[str, str]] = []
    if execute:
        applied_at = utc_now_iso()
        for item in ready:
            contact = contacts[item["contact_id"]]
            raw_result = results.get(contact.contact_id)
            search_mode = (
                str(raw_result.get("search_mode") or "usc_school")
                if isinstance(raw_result, dict)
                else "usc_school"
            )
            method = (
                "exact_name_usc_school_and_company_filter"
                if search_mode == "usc_school_and_current_company"
                else "exact_name_usc_school_filter"
            )
            marker = (
                f"peoplegrove_linkedin_locator_resolved={applied_at};"
                f"method={method}"
            )
            notes = contact.notes.strip()
            updated = workbook.update_contact(
                contact.contact_id,
                linkedin_url=item["linkedin_url"],
                notes=f"{notes} | {marker}" if notes else marker,
            )
            if updated is not None:
                applied.append(item)
                if isinstance(raw_result, dict):
                    raw_result["applied_at"] = applied_at
                    raw_result["applied_linkedin_url"] = item["linkedin_url"]
    return {
        "execute": execute,
        "ready": ready,
        "ready_count": len(ready),
        "applied": applied,
        "applied_count": len(applied),
        "blocked": blocked,
        "blocked_count": len(blocked),
        "review_required": review_required,
        "review_required_count": len(review_required),
        "held_existing": held_existing,
        "held_existing_count": len(held_existing),
        "released_existing": released_existing,
        "released_existing_count": len(released_existing),
        "skipped_existing": skipped_existing,
        "skipped_existing_count": len(skipped_existing),
    }


def peoplegrove_locator_state_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

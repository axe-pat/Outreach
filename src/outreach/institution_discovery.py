from __future__ import annotations

import csv
import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from outreach.peoplegrove_curation import (
    RoleClassification,
    classify_peoplegrove_title,
    parse_current_title_company,
)
from outreach.relationship_leads import RELATIONSHIP_LEAD_FIELDS


DEFAULT_INSTITUTION_CURATED_PATH = Path(
    "workspace/relationship_leads_linkedin_institution_first.csv"
)
DEFAULT_INSTITUTION_COMPANY_CANDIDATES_PATH = Path(
    "workspace/institution_first_company_candidates.csv"
)
DEFAULT_INSTITUTION_MINIMUM_SCORE = 72
LINKEDIN_US_GEO_URN = "103644278"

_COMPANY_ALIASES = {
    "alexa": "Amazon",
    "amazon aws": "Amazon",
    "amazon web services": "Amazon",
    "amazon web services aws": "Amazon",
    "aws": "Amazon",
    "auth0 by okta": "Okta",
    "coupa": "Coupa Software",
    "convergehealth by deloitte": "Deloitte",
    "digital room": "Digital Room Inc",
    "experian consumer services": "Experian",
    "facebook": "Meta",
    "gm": "General Motors",
    "google cloud": "Google",
    "instagram": "Meta",
    "hp": "HP Inc.",
    "imagine io": "Imagine.io",
    "oci": "Oracle",
    "qualcomm xr mobile": "Qualcomm",
    "roku inc": "Roku",
    "the walt disney company": "Disney",
    "ubereats": "Uber",
    "uber eats": "Uber",
    "veeva systems": "Veeva",
    "vlink inc": "VLink",
    "walmart data ventures": "Walmart",
    "walmart ecommerce": "Walmart",
    "walmart global tech": "Walmart",
    "walmart marketplace": "Walmart",
    "wealth corporate and commercial segment of usbank": "U.S. Bank",
    "whatsapp": "Meta",
    "youtube": "Google",
}

_AMBIGUOUS_COMPOSITE_COMPANIES = {"comcast nbcuniversal"}

# These employers no longer provide trustworthy evidence of a person's current
# company.  Fail closed until a fresher, explicit Current role identifies the
# successor employer; otherwise curation could create a stale organization and
# attach the lead to it.  Keep this deliberately narrow and evidence-backed.
_LEGACY_OR_ACQUIRED_COMPANIES_REQUIRING_ENRICHMENT = {"conexant systems inc"}

_ALLOWED_ROLE_CATEGORIES = {
    "founder_c_suite",
    "product_engineering",
    "product_product_strategy",
    "venture_startup_operator",
}

_US_STATE_NAMES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "district of columbia",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
}

_US_METRO_LOCATIONS = {
    "atlanta metropolitan area",
    "dallas-fort worth metroplex",
    "denver metropolitan area",
    "greater boston",
    "greater chicago area",
    "greater houston",
    "greater orlando",
    "greater phoenix area",
    "greater seattle area",
    "greater tampa bay area",
    "los angeles metropolitan area",
    "miami-fort lauderdale area",
    "new york city metropolitan area",
    "raleigh-durham-chapel hill area",
    "san diego metropolitan area",
    "san francisco bay area",
    "washington dc-baltimore area",
}


@dataclass(frozen=True)
class InstitutionSearchSpec:
    key: str
    school_filter: str
    school_urn: str
    school_label: str
    program: str
    query: str = "product"
    target_list: str = ""


DEFAULT_INSTITUTION_SEARCHES: tuple[InstitutionSearchSpec, ...] = (
    InstitutionSearchSpec(
        key="thapar_product_us",
        school_filter="Thapar Institute of Engineering & Technology",
        school_urn="485592",
        school_label="Thapar Institute of Engineering & Technology",
        program="",
        target_list="thapar-network",
    ),
    InstitutionSearchSpec(
        key="usc_product_us",
        school_filter="University of Southern California",
        school_urn="3084",
        school_label="University of Southern California",
        program="",
        target_list="usc-network",
    ),
    InstitutionSearchSpec(
        key="usc_marshall_product_us",
        school_filter="USC Marshall School of Business",
        school_urn="3083",
        school_label="University of Southern California",
        program="USC Marshall School of Business",
        target_list="marshall-network",
    ),
)


COMPANY_CANDIDATE_FIELDS = (
    "company",
    "lead_count",
    "top_score",
    "people",
    "titles",
    "institution_signals",
    "role_categories",
    "relationship_route",
    "source",
)


class InstitutionDiscoveryError(ValueError):
    """Raised when a LinkedIn institution capture is malformed or unsafe."""


@dataclass(frozen=True)
class _ExistingWorkbookKeys:
    linkedin_urls: frozenset[str]
    person_companies: frozenset[str]
    company_names: frozenset[str]


def institution_search_spec(key: str) -> InstitutionSearchSpec:
    normalized = _clean(key).casefold()
    for spec in DEFAULT_INSTITUTION_SEARCHES:
        if spec.key.casefold() == normalized:
            return spec
    raise InstitutionDiscoveryError(f"Unknown institution search key: {key!r}")


def build_institution_capture_payload(
    *,
    searches: list[dict[str, Any]],
    captured_by: str,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Build the auditable raw-capture envelope used by the curator."""

    return {
        "schema_version": 1,
        "captured_at": captured_at or _utc_now(),
        "captured_by": _clean(captured_by) or "codex-linkedin-institution-first",
        "searches": searches,
    }


def merge_institution_capture_payloads(
    *payloads: dict[str, Any],
    captured_by: str,
) -> dict[str, Any]:
    """Merge capture envelopes with later payloads replacing the same search.

    This supports a bounded retry of one silently dropped LinkedIn facet without
    repeating the already valid school searches. A merged production capture is
    complete only when all three configured search keys are present and successful.
    """

    by_key: dict[str, dict[str, Any]] = {}
    for payload_index, payload in enumerate(payloads, start=1):
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise InstitutionDiscoveryError(
                f"capture payload {payload_index} must use schema_version 1"
            )
        searches = payload.get("searches")
        if not isinstance(searches, list):
            raise InstitutionDiscoveryError(
                f"capture payload {payload_index} searches must be a list"
            )
        for raw_search in searches:
            if not isinstance(raw_search, dict):
                raise InstitutionDiscoveryError(
                    f"capture payload {payload_index} contains a malformed search"
                )
            key = _required_text(raw_search, "key")
            institution_search_spec(key)
            by_key[key] = raw_search
    expected_keys = [spec.key for spec in DEFAULT_INSTITUTION_SEARCHES]
    missing = [key for key in expected_keys if key not in by_key]
    if missing:
        raise InstitutionDiscoveryError(
            "merged institution capture is missing searches: " + ", ".join(missing)
        )
    failed = [
        key
        for key in expected_keys
        if _clean(by_key[key].get("termination_state")) == "failed"
        or _clean(by_key[key].get("error"))
    ]
    if failed:
        raise InstitutionDiscoveryError(
            "merged institution capture contains failed searches: " + ", ".join(failed)
        )
    return build_institution_capture_payload(
        searches=[by_key[key] for key in expected_keys],
        captured_by=captured_by,
    )


def curate_institution_capture(
    input_path: Path,
    *,
    output_path: Path = DEFAULT_INSTITUTION_CURATED_PATH,
    summary_path: Path | None = None,
    company_candidates_path: Path | None = None,
    workspace: Path | None = None,
    minimum_score: int = DEFAULT_INSTITUTION_MINIMUM_SCORE,
) -> dict[str, Any]:
    """Curate institution-first LinkedIn results into the reviewed lead lane.

    The function never writes tracker CSVs. It requires a real LinkedIn profile,
    an explicit current ``TITLE at COMPANY``-shaped role, a supported high-signal
    role, and a US-filtered school search. Ambiguous rows fail closed.
    """

    if not 0 <= minimum_score <= 100:
        raise InstitutionDiscoveryError("minimum_score must be between 0 and 100")
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    summary_path = (summary_path or output_path.with_suffix(".summary.json")).resolve()
    if company_candidates_path is None:
        default_output = DEFAULT_INSTITUTION_CURATED_PATH.resolve()
        company_candidates_path = (
            DEFAULT_INSTITUTION_COMPANY_CANDIDATES_PATH.resolve()
            if output_path == default_output
            else output_path.with_name(f"{output_path.stem}.company-candidates.csv")
        )
    else:
        company_candidates_path = company_candidates_path.resolve()
    payload = _read_capture(input_path)
    existing = _load_existing_workbook_keys(workspace) if workspace is not None else None
    captured_at = _required_text(payload, "captured_at")
    captured_by = _required_text(payload, "captured_by")
    capture_sha256 = _sha256(input_path)
    capture_batch = f"linkedin-institution-{capture_sha256[:12]}"

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    malformed_decisions: list[dict[str, Any]] = []
    query_coverage: dict[str, dict[str, Any]] = {}
    searches = payload.get("searches")
    if not isinstance(searches, list) or not searches:
        raise InstitutionDiscoveryError("capture searches must be a non-empty list")
    seen_search_keys: set[str] = set()
    for search_index, raw_search in enumerate(searches, start=1):
        if not isinstance(raw_search, dict):
            raise InstitutionDiscoveryError(f"searches[{search_index}] must be an object")
        key = _required_text(raw_search, "key")
        if key in seen_search_keys:
            raise InstitutionDiscoveryError(f"duplicate institution search key: {key}")
        seen_search_keys.add(key)
        spec = institution_search_spec(key)
        if _clean(raw_search.get("school_filter")) != spec.school_filter:
            raise InstitutionDiscoveryError(
                f"search {key} school_filter does not match the configured exact school"
            )
        if not bool(raw_search.get("use_us_location")):
            raise InstitutionDiscoveryError(f"search {key} is not bound to the US location filter")
        results = raw_search.get("results")
        if not isinstance(results, list):
            raise InstitutionDiscoveryError(f"search {key} results must be a list")
        raw_count = _nonnegative_int(raw_search.get("raw_count"), default=len(results))
        if raw_count != len(results):
            raise InstitutionDiscoveryError(
                f"search {key} raw_count={raw_count} does not match {len(results)} results"
            )
        query_coverage[key] = {
            "school_filter": spec.school_filter,
            "school_urn": spec.school_urn,
            "us_geo_urn": LINKEDIN_US_GEO_URN,
            "query": _clean(raw_search.get("query")),
            "use_us_location": True,
            "raw_count": raw_count,
            "limit": _nonnegative_int(raw_search.get("limit")),
            "max_pages": _nonnegative_int(raw_search.get("max_pages")),
            "termination_state": _clean(raw_search.get("termination_state"))
            or "bounded_sample_cap",
            "final_url": _clean(raw_search.get("final_url")),
        }
        final_url_query = dict(
            parse_qsl(urlsplit(query_coverage[key]["final_url"]).query, keep_blank_values=True)
        )
        missing_url_filters = [
            label
            for label, parameter in (
                ("school", "schoolFilter"),
                ("United States location", "geoUrn"),
            )
            if not final_url_query.get(parameter)
        ]
        if missing_url_filters:
            raise InstitutionDiscoveryError(
                f"search {key} result URL is missing required filters: "
                + ", ".join(missing_url_filters)
            )
        school_urns = _facet_urns(final_url_query.get("schoolFilter"))
        geo_urns = _facet_urns(final_url_query.get("geoUrn"))
        if school_urns != (spec.school_urn,):
            raise InstitutionDiscoveryError(
                f"search {key} result URL school URN does not match "
                f"{spec.school_filter}: expected {spec.school_urn}, got "
                f"{', '.join(school_urns) or 'none'}"
            )
        if geo_urns != (LINKEDIN_US_GEO_URN,):
            raise InstitutionDiscoveryError(
                f"search {key} result URL United States geo URN does not match: "
                f"expected {LINKEDIN_US_GEO_URN}, got {', '.join(geo_urns) or 'none'}"
            )
        for result_index, result in enumerate(results, start=1):
            if not isinstance(result, dict):
                malformed_decisions.append(
                    _decision(
                        key=key,
                        result_index=result_index,
                        accepted=False,
                        reason="malformed_result",
                    )
                )
                continue
            canonical_url = _canonical_linkedin_profile_url(result.get("linkedin_url"))
            if not canonical_url:
                malformed_decisions.append(
                    _decision(
                        key=key,
                        result_index=result_index,
                        accepted=False,
                        reason="missing_or_invalid_linkedin_profile_url",
                        name=_clean(result.get("name")),
                    )
                )
                continue
            grouped[canonical_url].append(
                {
                    "search_key": key,
                    "spec": spec,
                    "search": raw_search,
                    "result_index": result_index,
                    "result": result,
                    "linkedin_url": canonical_url,
                }
            )

    accepted_leads: list[dict[str, str]] = []
    decisions = list(malformed_decisions)
    accepted_metadata: list[dict[str, Any]] = []
    seen_person_company: set[str] = set()
    for linkedin_url, records in grouped.items():
        evaluation = _evaluate_group(
            linkedin_url,
            records,
            captured_at=captured_at,
            captured_by=captured_by,
            capture_batch=capture_batch,
            capture_sha256=capture_sha256,
            minimum_score=minimum_score,
            existing=existing,
        )
        person_company_key = _person_company_key(
            evaluation.get("name", ""), evaluation.get("company", "")
        )
        if evaluation["accepted"] and person_company_key in seen_person_company:
            evaluation = {
                **evaluation,
                "accepted": False,
                "reason": "duplicate_person_company_in_capture",
                "lead": None,
            }
        decisions.append({key: value for key, value in evaluation.items() if key != "lead"})
        lead = evaluation.get("lead")
        if not evaluation["accepted"] or not isinstance(lead, dict):
            continue
        seen_person_company.add(person_company_key)
        accepted_leads.append(
            {field: _clean(lead.get(field)) for field in RELATIONSHIP_LEAD_FIELDS}
        )
        accepted_metadata.append(evaluation)

    accepted_leads.sort(
        key=lambda row: (
            {"high": 0, "medium": 1, "low": 2}.get(row.get("priority", ""), 3),
            row.get("company", "").casefold(),
            row.get("full_name", "").casefold(),
        )
    )
    _write_csv_atomic(output_path, RELATIONSHIP_LEAD_FIELDS, accepted_leads)
    company_candidates = _build_company_candidates(accepted_metadata, existing)
    _write_csv_atomic(
        company_candidates_path,
        list(COMPANY_CANDIDATE_FIELDS),
        company_candidates,
    )

    reason_counts = Counter(str(item.get("reason") or "unknown") for item in decisions)
    category_counts = Counter(
        str(item.get("category") or "unknown") for item in decisions if item.get("accepted")
    )
    affiliations = Counter()
    for item in accepted_metadata:
        for school_key in item.get("search_keys", []):
            affiliations[str(school_key)] += 1
    summary = {
        "schema_version": 1,
        "input_path": str(input_path),
        "input_sha256": capture_sha256,
        "output_path": str(output_path),
        "summary_path": str(summary_path),
        "company_candidates_path": str(company_candidates_path),
        "capture_batch": capture_batch,
        "captured_at": captured_at,
        "captured_by": captured_by,
        "minimum_score": minimum_score,
        "searches_run": len(searches),
        "raw_rows": sum(item["raw_count"] for item in query_coverage.values()),
        "unique_profiles": len(grouped),
        "rows_accepted": len(accepted_leads),
        "rows_rejected": len(decisions) - len(accepted_leads),
        "existing_contacts_suppressed": reason_counts["already_in_workbook"],
        "existing_contact_enrichments": sum(
            bool(item.get("existing_contact_enrichment")) for item in accepted_metadata
        ),
        "current_role_review_required": sum(
            bool(item.get("current_role_review_required")) for item in accepted_metadata
        ),
        "company_candidates": len(company_candidates),
        "known_company_leads": sum(bool(item.get("known_company")) for item in accepted_metadata),
        "new_company_leads": sum(not bool(item.get("known_company")) for item in accepted_metadata),
        "reason_counts": dict(sorted(reason_counts.items())),
        "accepted_category_counts": dict(sorted(category_counts.items())),
        "accepted_affiliation_counts": dict(sorted(affiliations.items())),
        "query_coverage": query_coverage,
        "decisions": decisions,
    }
    _write_json_atomic(summary_path, summary)
    return summary


def _evaluate_group(
    linkedin_url: str,
    records: list[dict[str, Any]],
    *,
    captured_at: str,
    captured_by: str,
    capture_batch: str,
    capture_sha256: str,
    minimum_score: int,
    existing: _ExistingWorkbookKeys | None,
) -> dict[str, Any]:
    search_keys = sorted({str(item["search_key"]) for item in records})
    names = {
        _clean(item["result"].get("name")) for item in records if _clean(item["result"].get("name"))
    }
    if len(names) != 1:
        return _evaluation(
            linkedin_url=linkedin_url,
            search_keys=search_keys,
            accepted=False,
            reason="missing_or_conflicting_name",
            name="; ".join(sorted(names)),
        )
    name = next(iter(names))
    if existing is not None and linkedin_url in existing.linkedin_urls:
        return _evaluation(
            linkedin_url=linkedin_url,
            search_keys=search_keys,
            accepted=False,
            reason="already_in_workbook",
            name=name,
        )

    parsed_roles: list[tuple[str, str, str, RoleClassification, dict[str, Any], str, bool]] = []
    explicit_non_us = False
    unverified_us_location_count = 0
    conflicting_role_evidence = False
    parseable_role_count = 0
    invalid_company_count = 0
    ambiguous_company_count = 0
    legacy_or_acquired_company_count = 0
    unsupported_category_count = 0
    for item in records:
        result = item["result"]
        resolved_location, location_status = _resolve_us_location(result)
        if location_status == "non_us":
            explicit_non_us = True
            continue
        if location_status != "valid":
            unverified_us_location_count += 1
            continue
        parsed, evidence_conflict, has_current_marker = _parse_current_role(result)
        if evidence_conflict:
            conflicting_role_evidence = True
            continue
        if parsed is None:
            continue
        parseable_role_count += 1
        title, captured_company = parsed
        company = _canonical_company(captured_company)
        if _ambiguous_company(company):
            ambiguous_company_count += 1
            continue
        if _legacy_or_acquired_company_requires_enrichment(company):
            legacy_or_acquired_company_count += 1
            continue
        if not _usable_company(company):
            invalid_company_count += 1
            continue
        classification = _classify_institution_title(title)
        if classification is None or classification.category not in _ALLOWED_ROLE_CATEGORIES:
            unsupported_category_count += 1
            continue
        parsed_roles.append(
            (
                title,
                company,
                captured_company,
                classification,
                item,
                resolved_location,
                has_current_marker,
            )
        )
    if explicit_non_us:
        return _evaluation(
            linkedin_url=linkedin_url,
            search_keys=search_keys,
            accepted=False,
            reason="explicit_non_us_location",
            name=name,
        )
    if conflicting_role_evidence:
        return _evaluation(
            linkedin_url=linkedin_url,
            search_keys=search_keys,
            accepted=False,
            reason="conflicting_current_company_evidence",
            name=name,
        )
    if not parsed_roles:
        if ambiguous_company_count:
            reason = "ambiguous_composite_company"
        elif legacy_or_acquired_company_count:
            reason = "legacy_or_acquired_company_requires_enrichment"
        elif unverified_us_location_count:
            reason = "unverified_us_location"
        elif invalid_company_count:
            reason = "invalid_current_company"
        elif unsupported_category_count or parseable_role_count:
            reason = "no_high_signal_product_role"
        else:
            reason = "unparseable_current_role_company"
        return _evaluation(
            linkedin_url=linkedin_url,
            search_keys=search_keys,
            accepted=False,
            reason=reason,
            name=name,
        )
    role_identities = {
        (_normalize(title), _normalize(company)) for title, company, _, _, _, _, _ in parsed_roles
    }
    companies = {_normalize(company) for _, company, _, _, _, _, _ in parsed_roles}
    if len(companies) != 1:
        return _evaluation(
            linkedin_url=linkedin_url,
            search_keys=search_keys,
            accepted=False,
            reason="conflicting_current_company",
            name=name,
        )
    (
        title,
        company,
        captured_company,
        classification,
        chosen,
        resolved_location,
        has_current_marker,
    ) = max(
        parsed_roles,
        key=lambda item: (item[3].base_score, item[3].role_level == "leadership", len(item[0])),
    )
    person_company = _person_company_key(name, company)
    existing_contact_enrichment = bool(
        existing is not None and person_company in existing.person_companies
    )

    specs = {item["spec"].key: item["spec"] for item in records}
    spec_values = [specs[key] for key in sorted(specs)]
    has_usc = any(key.startswith("usc_") for key in search_keys)
    has_marshall = "usc_marshall_product_us" in search_keys
    has_thapar = "thapar_product_us" in search_keys
    score = classification.base_score + 5
    if len(spec_values) > 1:
        score += 5
    if has_usc and has_thapar:
        score += 5
    connection_degree = _best_connection_degree(records)
    if connection_degree == "1st":
        score += 3
    score = min(score, 100)
    if score < minimum_score:
        return _evaluation(
            linkedin_url=linkedin_url,
            search_keys=search_keys,
            accepted=False,
            reason="below_score_threshold",
            name=name,
            title=title,
            company=company,
            category=classification.category,
            score=score,
        )

    institution_labels = []
    if has_thapar:
        institution_labels.append("Thapar")
    if has_marshall:
        institution_labels.append("USC Marshall")
    elif has_usc:
        institution_labels.append("USC")
    target_lists = [
        "institution-first",
        "linkedin-institution",
        *(spec.target_list for spec in spec_values if spec.target_list),
        f"institution-{_category_target_suffix(classification.category)}",
    ]
    tags = [
        "institution-first",
        "linkedin",
        "warm-network",
        classification.category,
    ]
    if not has_current_marker:
        target_lists.extend(["current-role-review-required", "outreach-hold"])
        tags.extend(["current-role-review-required", "outreach-hold"])
    if has_thapar:
        tags.extend(["thapar", "thapar-alumni"])
        target_lists.append(f"thapar-{_category_target_suffix(classification.category)}")
    if has_usc:
        tags.extend(["usc", "usc-alumni"])
        target_lists.append(classification.target_list)
    if has_marshall:
        tags.extend(["marshall", "usc-marshall"])
        target_lists.append(f"marshall-{_category_target_suffix(classification.category)}")
    if has_usc and has_thapar:
        tags.append("dual-alumni")
    primary_school = (
        "University of Southern California"
        if has_usc
        else "Thapar Institute of Engineering & Technology"
    )
    program = "USC Marshall School of Business" if has_marshall else ""
    raw_headline = _clean(chosen["result"].get("title"))
    raw_location = _clean(chosen["result"].get("location"))
    known_company = bool(existing is not None and _normalize(company) in existing.company_names)
    relationship_signal = "Shared " + " + ".join(institution_labels) + " institution network"
    notes = " | ".join(
        filter(
            None,
            [
                f"institution_searches={'; '.join(search_keys)}",
                f"institution_score={score}",
                f"institution_role_category={classification.category}",
                f"institution_role_level={classification.role_level}",
                f"captured_headline={raw_headline}",
                (
                    f"captured_current_company={captured_company}"
                    if _normalize(captured_company) != _normalize(company)
                    else ""
                ),
                f"captured_location={raw_location}" if raw_location else "",
                (
                    f"resolved_location={resolved_location}"
                    if _normalize(raw_location) != _normalize(resolved_location)
                    else ""
                ),
                f"connection_degree={connection_degree}" if connection_degree else "",
                f"capture_sha256={capture_sha256}",
                ("existing_contact_enrichment=true" if existing_contact_enrichment else ""),
                ("current_role_review_required=true" if not has_current_marker else ""),
                "company_discovery_candidate=true" if not known_company else "known_company=true",
            ],
        )
    )
    lead = {field: "" for field in RELATIONSHIP_LEAD_FIELDS}
    lead.update(
        {
            "source_type": "linkedin_institution_first",
            "full_name": name,
            "company": company,
            "title": title,
            "linkedin_url": linkedin_url,
            "location": resolved_location,
            "school": primary_school,
            "program": program,
            "relationship_signal": relationship_signal,
            "contact_type": classification.contact_type,
            "priority": "high" if score >= 88 else "medium",
            "target_lists": ";".join(dict.fromkeys(target_lists)),
            "tags": ",".join(dict.fromkeys(tags)),
            "source_url": linkedin_url,
            "notes": notes,
            "source_record_id": _linkedin_slug(linkedin_url),
            "capture_batch": capture_batch,
            "captured_at": captured_at,
            "captured_by": captured_by,
        }
    )
    return {
        **_evaluation(
            linkedin_url=linkedin_url,
            search_keys=search_keys,
            accepted=True,
            reason="accepted",
            name=name,
            title=title,
            company=company,
            category=classification.category,
            score=score,
        ),
        "role_level": classification.role_level,
        "known_company": known_company,
        "existing_contact_enrichment": existing_contact_enrichment,
        "current_role_review_required": not has_current_marker,
        "institution_labels": institution_labels,
        "lead": lead,
        "role_identity_count": len(role_identities),
    }


def _parse_current_role(
    result: dict[str, Any],
) -> tuple[tuple[str, str] | None, bool, bool]:
    """Choose role evidence without allowing a stale headline to beat Current.

    A parseable explicit ``Current:`` role wins when it resolves to the same
    canonical employer as the headline. Materially different usable employers
    fail closed. Narrative Current snippets whose ``at`` clause is not a usable
    company do not override a clean headline.
    """

    headline = parse_current_title_company(_clean(result.get("title")))
    snippet = _clean(result.get("snippet"))
    current_candidates: list[str] = []
    has_current_marker = snippet.casefold().startswith("current:")
    if snippet.casefold().startswith("current:"):
        current_candidates.append(_clean(snippet.split(":", 1)[1]))
    raw_text = _clean(result.get("raw_text"))
    current_match = re.search(
        r"\bCurrent:\s*(.+?)(?=\s+(?:Past:|Skills:|\d+\s+mutual connection)|$)",
        raw_text,
        flags=re.IGNORECASE,
    )
    if current_match:
        has_current_marker = True
        current_candidates.append(_clean(current_match.group(1)))

    parsed_current: tuple[str, str] | None = None
    seen_current: set[str] = set()
    for candidate in current_candidates:
        normalized = _normalize(candidate)
        if not normalized or normalized in seen_current:
            continue
        seen_current.add(normalized)
        candidate_role = parse_current_title_company(candidate)
        if candidate_role is None:
            continue
        candidate_company = _canonical_company(candidate_role[1])
        if _usable_company(candidate_company):
            parsed_current = candidate_role
            break
        if parsed_current is None:
            parsed_current = candidate_role

    if parsed_current is not None and headline is not None:
        current_company = _canonical_company(parsed_current[1])
        headline_company = _canonical_company(headline[1])
        if (
            _usable_company(current_company)
            and _usable_company(headline_company)
            and _normalize(current_company) != _normalize(headline_company)
        ):
            return None, True, has_current_marker
    if parsed_current is not None and _usable_company(_canonical_company(parsed_current[1])):
        return parsed_current, False, has_current_marker
    if headline is not None:
        return headline, False, has_current_marker
    return parsed_current, False, has_current_marker


def _build_company_candidates(
    accepted: list[dict[str, Any]],
    existing: _ExistingWorkbookKeys | None,
) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in accepted:
        company = _clean(item.get("company"))
        if not company:
            continue
        if existing is not None and _normalize(company) in existing.company_names:
            continue
        grouped[company].append(item)
    rows: list[dict[str, str]] = []
    for company, items in grouped.items():
        people = sorted({_clean(item.get("name")) for item in items if _clean(item.get("name"))})
        titles = sorted({_clean(item.get("title")) for item in items if _clean(item.get("title"))})
        institutions = sorted(
            {
                str(label)
                for item in items
                for label in item.get("institution_labels", [])
                if str(label).strip()
            }
        )
        categories = sorted(
            {_clean(item.get("category")) for item in items if _clean(item.get("category"))}
        )
        rows.append(
            {
                "company": company,
                "lead_count": str(len(items)),
                "top_score": str(max(int(item.get("score") or 0) for item in items)),
                "people": "; ".join(people),
                "titles": "; ".join(titles),
                "institution_signals": "; ".join(institutions),
                "role_categories": "; ".join(categories),
                "relationship_route": "promote through relationship lead; do not change company score automatically",
                "source": "linkedin_institution_first",
            }
        )
    rows.sort(
        key=lambda row: (-int(row["top_score"]), -int(row["lead_count"]), row["company"].casefold())
    )
    return rows


def _load_existing_workbook_keys(workspace: Path) -> _ExistingWorkbookKeys:
    workspace = workspace.resolve()
    organizations_path = workspace / "organizations.csv"
    contacts_path = workspace / "contacts.csv"
    if not organizations_path.exists() or not contacts_path.exists():
        raise InstitutionDiscoveryError(
            "workspace dedupe requires organizations.csv and contacts.csv"
        )
    organization_names: dict[str, str] = {}
    company_names: set[str] = set()
    with organizations_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            organization_id = _clean(row.get("organization_id"))
            name = _clean(row.get("name"))
            organization_names[organization_id] = name
            if name:
                company_names.add(_normalize(name))
    urls: set[str] = set()
    person_companies: set[str] = set()
    with contacts_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            url = _canonical_linkedin_profile_url(row.get("linkedin_url"))
            if url:
                urls.add(url)
            company = organization_names.get(_clean(row.get("organization_id")), "")
            key = _person_company_key(_clean(row.get("full_name")), company)
            if key:
                person_companies.add(key)
    return _ExistingWorkbookKeys(
        linkedin_urls=frozenset(urls),
        person_companies=frozenset(person_companies),
        company_names=frozenset(company_names),
    )


def _read_capture(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Institution capture not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstitutionDiscoveryError(f"Institution capture is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstitutionDiscoveryError("Institution capture root must be an object")
    if payload.get("schema_version") != 1:
        raise InstitutionDiscoveryError("Institution capture schema_version must be 1")
    return payload


def _canonical_linkedin_profile_url(value: object) -> str:
    cleaned = _clean(value)
    if not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return ""
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host not in {"linkedin.com", "www.linkedin.com"}:
        return ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if not re.fullmatch(r"/(?:in|pub)/[^/]+(?:/[^/]+){0,3}", path, flags=re.IGNORECASE):
        return ""
    return urlunsplit(("https", "www.linkedin.com", path + "/", "", ""))


def _facet_urns(value: object) -> tuple[str, ...]:
    cleaned = _clean(value)
    if not cleaned:
        return ()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        values = tuple(_clean(item) for item in parsed if _clean(item))
        if values:
            return values
    return tuple(re.findall(r"\d+", cleaned))


def _resolve_us_location(result: dict[str, Any]) -> tuple[str, str]:
    captured = _clean(result.get("location"))
    if _recognized_us_location(captured):
        return captured, "valid"
    if _explicitly_non_us_location(captured):
        return "", "non_us"

    recovered = _recover_location_from_raw_text(result)
    if _recognized_us_location(recovered):
        return recovered, "valid"
    if _explicitly_non_us_location(recovered):
        return "", "non_us"
    return "", "unverified"


def _recover_location_from_raw_text(result: dict[str, Any]) -> str:
    raw_text = _clean(result.get("raw_text"))
    headline = _clean(result.get("title"))
    if not raw_text or not headline:
        return ""
    marker_index = raw_text.find(headline)
    if marker_index < 0:
        return ""
    tail = raw_text[marker_index + len(headline) :].strip()
    for marker in (
        " Connect ",
        " Follow ",
        " Message ",
        " Current: ",
        " Past: ",
        " Skills: ",
    ):
        if marker in f" {tail} ":
            tail = f" {tail} ".split(marker, 1)[0].strip()
    return _clean(tail)


def _recognized_us_location(location: str) -> bool:
    normalized = _normalize(location)
    if not normalized:
        return False
    if "united states" in normalized:
        return True
    if normalized in _US_METRO_LOCATIONS:
        return True
    return any(re.search(rf"\b{re.escape(state)}\b", normalized) for state in _US_STATE_NAMES)


def _explicitly_non_us_location(location: str) -> bool:
    lower = location.casefold()
    return (
        any(
            token in lower
            for token in (
                "armenia",
                "bengaluru",
                "bangalore",
                "india",
                "canada",
                "delhi",
                "united kingdom",
                "england",
                "germany",
                "australia",
                "israel",
                "singapore",
                "united arab emirates",
                "dubai",
                "vietnam",
                "yerevan",
            )
        )
        and "united states" not in lower
    )


def _best_connection_degree(records: list[dict[str, Any]]) -> str:
    rank = {"1st": 0, "2nd": 1, "3rd": 2, "3rd+": 2}
    values = [
        _clean(item["result"].get("connection_degree"))
        for item in records
        if _clean(item["result"].get("connection_degree"))
    ]
    return min(values, key=lambda value: rank.get(value, 99)) if values else ""


def _category_target_suffix(category: str) -> str:
    return {
        "product_engineering": "engineering",
        "product_product_strategy": "product",
        "founder_c_suite": "founder-executive",
        "bizops_strategy": "bizops-strategy",
        "program_operations_leadership": "program-operations",
        "venture_startup_operator": "startup-operator",
        "recruiting_talent": "recruiting",
    }.get(category, "relationship")


def _classify_institution_title(title: str) -> RoleClassification | None:
    normalized = _normalize(title)
    if re.search(
        r"\b(?:head|director|lead|manager|vice president|vp)\s+(?:of\s+)?"
        r"product\s+eng(?:ineering)?\b",
        normalized,
    ):
        return RoleClassification(
            category="product_engineering",
            base_score=82,
            contact_type="Engineering",
            target_list="usc-engineering",
            role_level="leadership",
        )
    if re.search(r"\bproduct line manager\b", normalized):
        leadership = bool(
            re.search(r"\b(?:senior|sr|principal|lead|director|head|vp)\b", normalized)
        )
        return RoleClassification(
            category="product_product_strategy",
            base_score=86 if leadership else 76,
            contact_type="Product",
            target_list="usc-product",
            role_level="leadership" if leadership else "core",
        )
    return classify_peoplegrove_title(title)


def _canonical_company(company: str) -> str:
    normalized = _normalize(company)
    if normalized.startswith("oracle ") and (
        "database cloud service" in normalized or normalized.endswith(" on oci")
    ):
        return "Oracle"
    if normalized.startswith("walmart ex "):
        return "Walmart"
    if normalized.startswith("amazon ") and any(
        token in normalized for token in ("driving ", "building ", "powered ", "promotion platform")
    ):
        return "Amazon"
    return _COMPANY_ALIASES.get(normalized, _clean(company))


def _ambiguous_company(company: str) -> bool:
    return _normalize(company) in _AMBIGUOUS_COMPOSITE_COMPANIES


def _legacy_or_acquired_company_requires_enrichment(company: str) -> bool:
    return _normalize(company) in _LEGACY_OR_ACQUIRED_COMPANIES_REQUIRING_ENRICHMENT


def _usable_company(company: str) -> bool:
    cleaned = _clean(company)
    normalized = _normalize(cleaned)
    if not cleaned or len(normalized) < 2:
        return False
    if "..." in cleaned or "…" in cleaned:
        return False
    if normalized in {
        "a company",
        "confidential",
        "freelance",
        "healthtech",
        "independent",
        "multiple companies",
        "self employed",
        "startup",
        "stealth",
        "stealth startup",
        "technology",
    }:
        return False
    if len(normalized.split()) > 12:
        return False
    return True


def _evaluation(
    *,
    linkedin_url: str,
    search_keys: list[str],
    accepted: bool,
    reason: str,
    name: str = "",
    title: str = "",
    company: str = "",
    category: str = "",
    score: int = 0,
) -> dict[str, Any]:
    return {
        "linkedin_url": linkedin_url,
        "search_keys": search_keys,
        "accepted": accepted,
        "reason": reason,
        "name": name,
        "title": title,
        "company": company,
        "category": category,
        "score": score,
    }


def _decision(
    *,
    key: str,
    result_index: int,
    accepted: bool,
    reason: str,
    name: str = "",
) -> dict[str, Any]:
    return {
        "search_keys": [key],
        "result_index": result_index,
        "accepted": accepted,
        "reason": reason,
        "name": name,
    }


def _person_company_key(name: str, company: str) -> str:
    normalized_name = _normalize(name)
    normalized_company = _normalize(company)
    return (
        f"{normalized_name}|{normalized_company}" if normalized_name and normalized_company else ""
    )


def _linkedin_slug(url: str) -> str:
    path = urlsplit(url).path.strip("/").split("/")
    return path[-1] if path else hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = _clean(payload.get(field))
    if not value:
        raise InstitutionDiscoveryError(f"capture {field} is required")
    return value


def _nonnegative_int(value: object, *, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise InstitutionDiscoveryError(f"expected a non-negative integer, got {value!r}") from exc
    if parsed < 0:
        raise InstitutionDiscoveryError(f"expected a non-negative integer, got {value!r}")
    return parsed


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        newline="",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        temporary_path = Path(handle.name)
    temporary_path.replace(path)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)

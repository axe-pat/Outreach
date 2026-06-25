from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from outreach.artifacts import artifact_timestamp, write_artifact
from outreach.config import OutreachSettings
from outreach.discovery.adapters import BuiltInCompaniesAdapter, SourceAdapter, YCombinatorCompanyDirectoryAdapter
from outreach.discovery.http import HttpTextDownloader
from outreach.discovery.registry import get_source_definition, list_source_definitions
from outreach.scoring import score_candidate
from outreach.services.linkedin import FilterRunResult, LinkedInScraper
from outreach.services.notes import NoteGenerator
from outreach.models import CandidateProfile, LinkedInCompanyQueueItem
from outreach.resume_jobs_bridge import (
    DEFAULT_INCLUDE_STATUSES,
    DEFAULT_COMPANY_OVERRIDES_FILENAME,
    build_resume_opportunity_notes,
    build_resume_organization_notes,
    build_resume_outreach_queue,
    ensure_company_overrides_csv,
    infer_opportunity_type,
    infer_company_type_for_job,
    load_resume_jobs,
    load_company_overrides,
    load_company_blocklist,
    map_resume_source_kind,
    normalize_dedupe_text,
    opportunity_status_from_resume_status,
    organization_status_from_resume_status,
    organization_type_for_resume_job,
    select_resume_jobs,
    target_lists_from_resume_status,
)
from outreach.tracking import (
    ContactRecord,
    DiscoverySourceRecord,
    OpportunityRecord,
    OpportunityType,
    OrganizationRecord,
    OrganizationType,
    OutreachChannel,
    OutreachWorkbook,
    SourceKind,
    TouchpointRecord,
    utc_now_iso,
)


def resolve_pass_definitions(
    settings: OutreachSettings,
    company_mode: str = "default",
    include_passes: tuple[str, ...] = (),
    exclude_passes: tuple[str, ...] = (),
    enable_marshall: bool = False,
    force_broad_fallback: bool = False,
) -> dict[str, dict[str, str | int | bool]]:
    include_set = {item.strip() for item in include_passes if item.strip()}
    exclude_set = {item.strip() for item in exclude_passes if item.strip()}
    if company_mode == "startup":
        pass_definitions = {
            "existing_connections": {"query": "", "limit": 20, "priority": 1, "use_us_location": False, "connection_degree": "1st", "enabled": True},
        }
    else:
        pass_definitions = {
            name: dict(config) for name, config in settings.search.pass_definitions.items()
        }

    if enable_marshall:
        for name in ("product_usc_marshall", "engineering_usc_marshall"):
            if name in pass_definitions:
                pass_definitions[name]["enabled"] = True

    if force_broad_fallback and "broad_fallback" in pass_definitions:
        pass_definitions["broad_fallback"]["enabled"] = True
        pass_definitions["broad_fallback"].pop("run_if_below_pool_size", None)

    if include_set:
        for name, config in pass_definitions.items():
            config["enabled"] = name in include_set

    for name in exclude_set:
        if name in pass_definitions:
            pass_definitions[name]["enabled"] = False

    return pass_definitions


def infer_role_bucket(title: str, raw_text: str, settings: OutreachSettings) -> str:
    title_lower = title.lower()
    raw_text_lower = raw_text.lower()

    recruiter_keywords = ["recruiter", "sourcer", "talent", "campus recruiting", "university recruiting"]
    university_keywords = ["usc", "university", "campus", "marshall school of business", "career center"]
    adjacent_override_keywords = ["solution engineer", "solutions engineer", "solutions architect", "solution architect"]
    founder_keywords = ["founder", "co-founder", "cofounder", "chief executive officer", " ceo", "ceo ", "founding member"]
    startup_operator_keywords = [
        "chief of staff",
        "business operations",
        "bizops",
        "operations",
        "strategy",
        "general manager",
        "founder's office",
        "founders office",
        "founding operations",
        "founding team",
        "operator",
    ]

    if any(keyword in title_lower for keyword in recruiter_keywords):
        if any(keyword in raw_text_lower for keyword in university_keywords):
            return "University Recruiting"
        return "Recruiting"

    if any(keyword in title_lower for keyword in founder_keywords):
        return "Founder"

    if any(keyword in title_lower for keyword in adjacent_override_keywords):
        return "Adjacent"

    if any(keyword.lower() in title_lower for keyword in settings.search.role_keywords_product):
        if "productivity engineering" not in title_lower:
            return "Product"

    if any(keyword.lower() in title_lower for keyword in settings.search.role_keywords_engineering):
        return "Engineering"

    if any(keyword in title_lower for keyword in startup_operator_keywords):
        return "Adjacent"

    if any(keyword.lower() in title_lower for keyword in settings.search.adjacent_titles):
        return "Adjacent"

    return "Other"


def detect_usc_marshall(raw_text: str) -> bool:
    text = raw_text.lower()
    return "usc marshall" in text or "marshall school of business" in text


def detect_usc(raw_text: str) -> bool:
    text = raw_text.lower()
    return "usc" in text or "university of southern california" in text


def detect_shared_history(raw_text: str, settings: OutreachSettings) -> bool:
    return bool(detect_shared_history_signals(raw_text, settings))


def detect_shared_history_signals(raw_text: str, settings: OutreachSettings) -> list[str]:
    text = raw_text.lower()
    signals: list[str] = []
    for keyword in settings.search.shared_history_keywords:
        if keyword in text:
            signals.append(keyword.title())
    for company in settings.search.ex_companies:
        if company.lower() in text:
            signals.append(company)
    return list(dict.fromkeys(signals))


def company_search_aliases(company: str) -> list[str]:
    cleaned = " ".join(company.split()).strip()
    if not cleaned:
        return []
    aliases = [cleaned]
    suffix_patterns = [
        r"\s+inc\.?$",
        r"\s+incorporated$",
        r"\s+llc$",
        r"\s+ltd\.?$",
        r"\s+corp\.?$",
        r"\s+corporation$",
    ]
    for pattern in suffix_patterns:
        alias = re.sub(pattern, "", cleaned, flags=re.I).strip()
        if alias and alias.lower() != cleaned.lower():
            aliases.append(alias)
    phrase_suffixes = [
        "Defense Systems",
        "Systems",
        "Technologies",
        "Technology",
        "Labs",
        "AI",
    ]
    for suffix in phrase_suffixes:
        pattern = rf"\s+{re.escape(suffix)}$"
        alias = re.sub(pattern, "", cleaned, flags=re.I).strip()
        if alias and alias.lower() != cleaned.lower():
            aliases.append(alias)
    deduped: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        key = alias.lower()
        if key not in seen:
            deduped.append(alias)
            seen.add(key)
    return deduped


def candidate_mentions_company(raw, aliases: list[str]) -> bool:
    title = str(getattr(raw, "title", "") or "")
    snippet = str(getattr(raw, "snippet", "") or "")
    raw_text = str(getattr(raw, "raw_text", "") or "")
    text = re.sub(r"\s+", " ", " ".join([title, snippet, raw_text]).lower()).strip()
    if not text:
        return False
    for alias in aliases:
        normalized = " ".join(alias.lower().split()).strip()
        if len(normalized) < 4:
            continue
        alias_tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token]
        if len(alias_tokens) == 1:
            single_word_boundary = rf"(?![a-z0-9]|\s+[a-z0-9])"
            structured_patterns = [
                rf"(?:@|at\s+|current:\s*|past:\s*){re.escape(normalized)}{single_word_boundary}",
                rf"(?:founder|co-founder|ceo|cto|cpo|head of product|product)\s+(?:of\s+|at\s+|@\s*|[-—]\s*){re.escape(normalized)}{single_word_boundary}",
                rf"(?<![a-z0-9]){re.escape(normalized)}\s*(?:\||·|-|—|$)",
            ]
            if any(re.search(pattern, text, flags=re.I) for pattern in structured_patterns):
                return True
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text):
            return True
    return False


def startup_pool_mode(raw_count: int | None) -> str:
    if raw_count is None:
        return "unknown"
    if raw_count <= 0:
        return "empty"
    if raw_count <= 4:
        return "micro"
    if raw_count <= 12:
        return "small"
    if raw_count <= 25:
        return "normal"
    return "selective"


def startup_pool_send_min_score(pool_mode: str) -> int:
    thresholds = {
        "micro": -5,
        "small": 10,
        "normal": 20,
        "selective": 35,
        "empty": 20,
        "unknown": 20,
    }
    return thresholds.get(pool_mode, 20)


def recommend_auto_send_limit(candidate_count: int, pool_mode: str | None = None) -> int:
    if pool_mode == "micro":
        return min(candidate_count, 4)
    if pool_mode == "small":
        return min(candidate_count, 6)
    if pool_mode == "normal":
        return min(candidate_count, 10)
    if pool_mode == "selective":
        return min(candidate_count, 12)
    if candidate_count >= 15:
        return 12
    if candidate_count >= 10:
        return 10
    if candidate_count >= 5:
        return 5
    return 0


def startup_pool_metadata(payload: dict) -> dict[str, int | str | bool | None]:
    startup_summary = next(
        (
            item
            for item in payload.get("pass_summaries") or []
            if item.get("pass_name") == "startup_preflight"
        ),
        None,
    )
    if not startup_summary:
        return {
            "raw_count": None,
            "kept_count": None,
            "pool_mode": "unknown",
            "adaptive_send_min_score": 20,
            "coverage_only": False,
        }
    try:
        raw_count = int(startup_summary.get("raw_count"))
    except (TypeError, ValueError):
        raw_count = None
    try:
        kept_count = int(startup_summary.get("kept_count"))
    except (TypeError, ValueError):
        kept_count = None
    mode = startup_pool_mode(raw_count)
    return {
        "raw_count": raw_count,
        "kept_count": kept_count,
        "pool_mode": mode,
        "adaptive_send_min_score": startup_pool_send_min_score(mode),
        "coverage_only": bool(startup_summary.get("coverage_only")),
    }


def effective_send_min_score(payload: dict, requested_min_score: int, adaptive: bool = True) -> int:
    if not adaptive:
        return requested_min_score
    if payload.get("company_mode") != "startup":
        return requested_min_score
    metadata = payload.get("startup_pool") or startup_pool_metadata(payload)
    try:
        adaptive_min_score = int(metadata.get("adaptive_send_min_score"))
    except (AttributeError, TypeError, ValueError):
        adaptive_min_score = requested_min_score
    return min(requested_min_score, adaptive_min_score)


def _is_startup_founder_title(title_lower: str) -> bool:
    return any(
        signal in title_lower
        for signal in [
            "founder",
            "co-founder",
            "cofounder",
            "ceo",
            "chief executive",
            "founding member",
            "president",
        ]
    )


def _is_startup_operator_title(title_lower: str) -> bool:
    return any(
        signal in title_lower
        for signal in [
            "chief of staff",
            "operations",
            "business operations",
            "bizops",
            "strategy",
            "founder's office",
            "founders office",
            "founding",
            "general manager",
            "founding team",
            "operator",
            "growth",
            "gtm",
            "revenue operations",
            "revops",
            "partnerships",
        ]
    )


def startup_relationship_score_boost(role_bucket: str, title: str, company_mode: str, pass_name: str) -> tuple[int, list[str]]:
    if company_mode != "startup" or pass_name not in {"startup_preflight", "startup_company_coverage"}:
        return 0, []
    title_lower = title.lower()
    if _is_explicitly_bad_startup_coverage_title(title_lower):
        return 0, []
    boost = 5
    triggers = ["Keyword company coverage" if pass_name == "startup_company_coverage" else "Exact company preflight"]
    if role_bucket == "Founder" or _is_startup_founder_title(title_lower):
        boost += 25
        triggers.append("Startup founder")
    elif _is_startup_operator_title(title_lower):
        boost += 12
        triggers.append("Startup operator")
    elif "founding" in title_lower:
        boost += 10
        triggers.append("Founding team")
    elif role_bucket in {"Product", "Engineering", "Adjacent"}:
        boost += 8
        triggers.append("Startup builder")
    return boost, triggers


def _is_explicitly_bad_startup_coverage_title(title_lower: str) -> bool:
    return any(
        signal in title_lower
        for signal in [
            "investor",
            "investments",
            "venture capital",
            "venture partner",
            "advisor",
            "board member",
            "recruiter",
            "sourcer",
            "talent",
            "agency",
            "consultant",
            "fractional",
        ]
    )


def pass_relevance(
    pass_name: str,
    role_bucket: str,
    title: str,
    raw_text: str,
    company_mode: str = "default",
) -> bool:
    title_lower = title.lower()

    product_text_signals = [
        "product manager",
        "product ",
        "product@",
        "product @",
        "tpm",
        "technical product manager",
        "product management",
        "group product",
        "director of product",
    ]
    engineering_text_signals = [
        "software engineer",
        "swe",
        "sde",
        "staff engineer",
        "senior engineer",
        "ml engineer",
        "machine learning engineer",
        "data engineer",
        "platform engineer",
        "infra engineer",
        "engineering at",
        "developer",
    ]

    if pass_name == "existing_connections":
        return True
    if company_mode == "startup":
        if pass_name in {"startup_preflight", "startup_company_coverage"}:
            if _is_explicitly_bad_startup_coverage_title(title_lower):
                return False
            return bool(title_lower.strip())
    if pass_name.startswith("product_"):
        if role_bucket == "Product":
            return True
        return any(signal in title_lower for signal in product_text_signals)
    if pass_name.startswith("engineering_"):
        if role_bucket == "Engineering":
            return True
        return any(signal in title_lower for signal in engineering_text_signals)
    return role_bucket != "Other"


def apply_raw_candidate(
    *,
    deduped: dict[str, dict],
    raw,
    company: str,
    pass_name: str,
    pass_config: dict[str, str | int | bool],
    settings: OutreachSettings,
    company_mode: str,
) -> bool:
    title = raw.title or ""
    raw_text = raw.raw_text or ""
    role_bucket = infer_role_bucket(title, raw_text, settings)
    if not pass_relevance(pass_name, role_bucket, title, raw_text, company_mode=company_mode):
        return False

    connection_degree = raw.connection_degree or "3rd"
    pass_school = str(pass_config.get("school") or "")
    pass_implies_usc = "southern california" in pass_school.lower()
    pass_implies_marshall = "marshall" in pass_school.lower()
    pass_implies_existing_connection = pass_name == "existing_connections"
    shared_history_signals = detect_shared_history_signals(raw_text, settings)
    profile = CandidateProfile(
        name=raw.name,
        title=raw.title or "",
        company=company,
        linkedin_url=raw.linkedin_url or "https://www.linkedin.com/",
        connection_degree=connection_degree,
        mutual_connections=1 if raw.snippet and "mutual connection" in raw.snippet else 0,
        existing_connection=pass_implies_existing_connection or connection_degree == "1st",
        usc_marshall=pass_implies_marshall or detect_usc_marshall(raw_text),
        usc_alumni=pass_implies_usc or detect_usc(raw_text),
        shared_history=bool(shared_history_signals),
        indian_background=False,
        university_recruiter=role_bucket == "University Recruiting",
        role_bucket=role_bucket,
    )
    scored = score_candidate(profile, settings.scoring)
    relationship_boost, relationship_triggers = startup_relationship_score_boost(
        role_bucket=role_bucket,
        title=title,
        company_mode=company_mode,
        pass_name=pass_name,
    )
    candidate_score = scored.score + relationship_boost
    candidate_triggers = [*scored.triggers, *relationship_triggers]
    key = raw.linkedin_url or f"{raw.name}:{title}"
    entry = deduped.get(
        key,
        {
            "name": raw.name,
            "title": raw.title,
            "location": raw.location,
            "linkedin_url": raw.linkedin_url,
            "subtitle": raw.subtitle,
            "connection_degree": raw.connection_degree,
            "snippet": raw.snippet,
            "role_bucket": role_bucket,
            "score": candidate_score,
            "tier": scored.tier.value,
            "triggers": candidate_triggers,
            "passes": [],
            "existing_connection": profile.existing_connection,
            "usc_marshall": profile.usc_marshall,
            "usc": profile.usc_alumni,
            "shared_history": profile.shared_history,
            "shared_history_signals": shared_history_signals,
        },
    )
    entry["passes"] = sorted(set([*entry["passes"], pass_name]))
    if candidate_score > entry["score"]:
        entry.update(
            {
                "role_bucket": role_bucket,
                "score": candidate_score,
                "tier": scored.tier.value,
                "triggers": candidate_triggers,
                "existing_connection": profile.existing_connection,
                "usc_marshall": profile.usc_marshall,
                "usc": profile.usc_alumni,
                "shared_history": profile.shared_history,
                "shared_history_signals": shared_history_signals,
            }
        )
    deduped[key] = entry
    return True


def build_source_adapter(source_id: str) -> SourceAdapter:
    entry = get_source_definition(source_id)
    if entry.definition.adapter.value == "yc_company_directory":
        return YCombinatorCompanyDirectoryAdapter()
    if entry.definition.adapter.value == "builtin_companies":
        return BuiltInCompaniesAdapter()
    raise typer.BadParameter(f"Unsupported adapter for source {source_id}")


def parse_team_size_headcount(team_size: str) -> int | None:
    digits = "".join(char for char in team_size if char.isdigit() or char == ",").replace(",", "")
    if not digits:
        return None
    return int(digits)


def parse_batch_year(batch: str) -> int | None:
    digits = "".join(char for char in batch if char.isdigit())
    if len(digits) != 4:
        return None
    return int(digits)


def normalize_tag(value: str) -> str:
    return " ".join(value.lower().replace("-", " ").replace("_", " ").split()).strip()


def item_matches_remote(item: dict) -> bool:
    location = str(item.get("location") or "").lower()
    if "remote" in location:
        return True
    for opportunity in item.get("opportunities") or []:
        if "remote" in str(opportunity.get("location") or "").lower():
            return True
    return False


def item_matches_tags(item: dict, include_tags: tuple[str, ...]) -> bool:
    if not include_tags:
        return True
    item_tags = {normalize_tag(str(tag)) for tag in item.get("tags") or []}
    normalized_filters = [normalize_tag(tag) for tag in include_tags if tag.strip()]
    return any(
        any(filter_tag in item_tag or item_tag in filter_tag for item_tag in item_tags)
        for filter_tag in normalized_filters
    )


def filter_discovered_items(
    items: list[dict],
    *,
    require_jobs_url: bool,
    max_team_size: int | None,
    min_batch_year: int | None,
    remote_only: bool = False,
    include_tags: tuple[str, ...] = (),
) -> list[dict]:
    filtered: list[dict] = []
    for item in items:
        if require_jobs_url and not item.get("jobs_url"):
            continue
        if max_team_size is not None:
            team_size = parse_team_size_headcount(str(item.get("team_size") or ""))
            if team_size is not None and team_size > max_team_size:
                continue
        if min_batch_year is not None:
            batch_year = parse_batch_year(str(item.get("batch") or ""))
            if batch_year is not None and batch_year < min_batch_year:
                continue
        if remote_only and not item_matches_remote(item):
            continue
        if not item_matches_tags(item, include_tags):
            continue
        filtered.append(item)
    return filtered


def split_semicolon_tags(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(";") if item.strip()}


def infer_company_mode(organization_type: str, team_size: int | None) -> str:
    if organization_type in {"startup", "hacker_house", "incubator", "accelerator"}:
        return "startup"
    if team_size is not None and team_size >= 1000:
        return "big_company"
    return "default"


def extract_team_size_from_notes(notes: str) -> int | None:
    match = re.search(r"team_size=([^|]+)", notes or "")
    if not match:
        return None
    return parse_team_size_headcount(match.group(1))


def parse_notes_metadata(notes: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for fragment in (notes or "").split("|"):
        key, separator, value = fragment.strip().partition("=")
        if not separator or not key.strip():
            continue
        metadata[key.strip().lower()] = value.strip()
    return metadata


def extract_tags_from_notes(notes: str) -> list[str]:
    raw = parse_notes_metadata(notes).get("tags", "")
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def extract_description_from_notes(notes: str) -> str:
    return parse_notes_metadata(notes).get("description", "")


def format_team_size_signal(team_size: str) -> str:
    cleaned = team_size.strip()
    if not cleaned:
        return ""
    if any(char.isalpha() for char in cleaned):
        return cleaned
    return f"{cleaned} employees"


def extract_scale_signal_from_notes(notes: str) -> str:
    metadata = parse_notes_metadata(notes)
    fragments: list[str] = []
    if metadata.get("founded_year"):
        fragments.append(f"Founded {metadata['founded_year']}")
    elif metadata.get("batch"):
        fragments.append(metadata["batch"])
    if metadata.get("team_size"):
        fragments.append(format_team_size_signal(metadata["team_size"]))
    if metadata.get("jobs_count"):
        fragments.append(f"{metadata['jobs_count']} jobs surfaced")
    return " | ".join(fragments)


def summarize_company_description(description: str, max_length: int = 280) -> str:
    clean = " ".join(description.split()).strip()
    if len(clean) <= max_length:
        return clean
    truncated = clean[: max_length - 3].rsplit(" ", maxsplit=1)[0].strip()
    return f"{truncated}..."


def text_contains_signal(text: str, keyword: str) -> bool:
    normalized_text = text.lower()
    normalized_keyword = keyword.lower().strip()
    if not normalized_keyword:
        return False
    if len(normalized_keyword) <= 3 and normalized_keyword.isalpha():
        pattern = rf"\b{re.escape(normalized_keyword)}\b"
        return re.search(pattern, normalized_text) is not None
    return normalized_keyword in normalized_text


def infer_fit_reasons(
    organization: OrganizationRecord,
    tags: list[str],
    description: str,
    opportunities: list[OpportunityRecord],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    haystack = " ".join(
        [organization.name, description, " ".join(tags), " ".join(item.title for item in opportunities)]
    ).lower()
    title_stack = " ".join(item.title for item in opportunities).lower()

    domain_signals = [
        (("ai", "artificial intelligence", "machine learning", "agent", "llm", "genai"), 24, "AI/ML angle"),
        (("data", "developer", "platform", "api", "infrastructure", "saas"), 18, "Technical platform/data angle"),
        (("robotics", "industrial", "autonomy", "uav", "ugv", "mobility", "logistics"), 18, "Robotics or mobility angle"),
        (("fintech", "insurance", "payments"), 10, "Fintech/insurance adjacency"),
        (("health", "healthcare", "medical"), 8, "Healthcare adjacency"),
    ]
    for keywords, weight, label in domain_signals:
        if any(text_contains_signal(haystack, keyword) for keyword in keywords):
            score += weight
            reasons.append(label)

    role_signals = [
        (("product manager", "product", "platform", "strategy", "operations", "growth"), 16, "Product/strategy exposure signal"),
        (("intern", "mba", "associate", "apm"), 10, "Closer to your internship target"),
    ]
    for keywords, weight, label in role_signals:
        if any(text_contains_signal(title_stack, keyword) for keyword in keywords):
            score += weight
            reasons.append(label)

    if organization.organization_type == OrganizationType.STARTUP:
        score += 12
        reasons.append("Startup environment")

    team_size = extract_team_size_from_notes(organization.notes)
    if team_size is not None and team_size <= 200:
        score += 10
        reasons.append("Manageable size for direct outreach")

    city_text = " ".join([organization.city, parse_notes_metadata(organization.notes).get("location", "")]).lower()
    if any(location in city_text for location in ("san francisco", "los angeles", "new york", "seattle", "remote")):
        score += 8
        reasons.append("Preferred location or remote signal")

    if opportunities:
        score += min(18, 6 + len(opportunities) * 2)
        reasons.append("Active hiring signal")

    deduped = list(dict.fromkeys(reasons))
    return score, deduped


def fit_band_from_score(score: int) -> str:
    if score >= 65:
        return "strong"
    if score >= 38:
        return "medium"
    return "exploratory"


def explain_fit_for_candidate(
    organization: OrganizationRecord,
    tags: list[str],
    opportunities: list[OpportunityRecord],
    fit_reasons: list[str],
) -> str:
    role_titles = [item.title for item in opportunities[:2]]
    role_clause = f" Open roles include {', '.join(role_titles)}." if role_titles else ""
    tags_clause = f" The company themes show up in {', '.join(tags[:4])}." if tags else ""
    if not fit_reasons:
        return "Worth a quick look, but the current source data does not yet show a strong role or domain match."
    return (
        f"{organization.name} looks relevant because it overlaps with {', '.join(fit_reasons[:3]).lower()}."
        f"{tags_clause}{role_clause}"
    )


def infer_channel_recommendation(organization: OrganizationRecord, contacts: list[ContactRecord]) -> str:
    if any(contact.contact_type.lower() == "founder" for contact in contacts):
        return "Start with founders on LinkedIn, then broaden to operators/product people."
    if organization.organization_type == OrganizationType.STARTUP:
        return "Run LinkedIn people search next; startup targets respond best to founder/operator outreach."
    return "Use LinkedIn first, then add email only if a public contact path appears."


def score_opportunity_relevance(
    title: str,
    organization: OrganizationRecord,
    organization_description: str = "",
) -> tuple[int, list[str]]:
    lowered = title.lower()
    org_text = " ".join([organization.name, organization_description]).lower()
    score = 0
    reasons: list[str] = []

    hard_reject_keywords = {
        "software engineer",
        "full stack",
        "frontend",
        "front end",
        "backend",
        "back end",
        "swe",
        "sde",
        "data engineer",
        "devops",
        "site reliability",
        "sre",
        "qa ",
        "quality assurance",
        "test engineer",
        "sales engineer",
        "marketing",
        "designer",
        "legal",
        "account executive",
        "recruiter",
    }
    if any(keyword in lowered for keyword in hard_reject_keywords):
        return 0, ["Role looks functionally off-target"]

    role_signals = [
        (("technical product manager", "tpm"), 70, "Technical product role"),
        (("product manager", "product management"), 68, "Product role"),
        (("associate product manager", "apm"), 68, "APM-style role"),
        (("product operations", "product ops"), 60, "Product operations role"),
        (("growth product",), 60, "Growth product role"),
        (("strategy", "strategic"), 48, "Strategy signal"),
        (("business operations", "bizops", "biz ops"), 50, "BizOps signal"),
        (("chief of staff", "founders associate", "founder's associate", "founding operator"), 54, "Startup operator signal"),
        (("program manager", "technical program manager"), 40, "Program management adjacency"),
        (("product engineer",), 35, "Borderline product-adjacent role"),
    ]
    for keywords, weight, label in role_signals:
        if any(text_contains_signal(lowered, keyword) for keyword in keywords):
            score = max(score, weight)
            reasons.append(label)

    if "intern" in lowered or "internship" in lowered:
        score += 18
        reasons.append("Internship signal")
    if "mba" in lowered:
        score += 12
        reasons.append("MBA-specific signal")
    if organization.organization_type == OrganizationType.STARTUP and any(
        keyword in lowered for keyword in {"chief of staff", "founders associate", "founder's associate", "operator"}
    ):
        score += 8
        reasons.append("Startup-friendly generalist role")
    if any(keyword in org_text for keyword in {"ai", "artificial intelligence", "data", "robotics", "platform"}):
        score += 4
        reasons.append("Aligned technical domain")

    return min(score, 100), list(dict.fromkeys(reasons))


def classify_opportunity_action(score: int) -> str:
    if score >= 65:
        return "apply_now"
    if score >= 40:
        return "review"
    return "ignore"


def action_priority(action: str) -> int:
    priorities = {
        "apply_now": 3,
        "outreach_now": 2,
        "review": 1,
        "skip": 0,
    }
    return priorities.get(action, 0)


CONNECTED_CONTACT_STATUSES = {"accepted", "connected", "warm", "already connected"}
CONVERSATION_CONTACT_STATUSES = {
    "replied",
    "conversation",
    "coffee chat",
    "coffee chat scheduled",
    "coffee chat done",
    "referral",
    "champion",
}
SENT_TOUCHPOINT_STATUSES = {"sent", "already connected"}
CONVERSATION_TOUCHPOINT_STATUSES = {
    "replied",
    "reply received",
    "conversation",
    "coffee chat scheduled",
    "coffee chat done",
    "referral",
}


def parse_iso_timestamp(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def relationship_action_priority(action: str) -> int:
    priorities = {
        "follow_up_connected_contact": 7,
        "send_initial_invites": 6,
        "expand_contact_wave": 5,
        "run_linkedin_people_search": 4,
        "research_email_path": 3,
        "wait_for_accepts": 2,
        "maintain_relationship": 1,
        "watch": 0,
    }
    return priorities.get(action, 0)


def relationship_contact_first_name(contact: ContactRecord | None) -> str:
    if contact is None:
        return "there"
    return (contact.full_name or "there").strip().split()[0]


def build_relationship_follow_up_message(company: str, contact: ContactRecord | None) -> str:
    first_name = relationship_contact_first_name(contact)
    return (
        f"Hi {first_name}, thanks for connecting. I'm a Marshall MBA + former data/platform engineer "
        f"exploring product/operator paths at {company}. Would value your quick read on where someone "
        "with my background could be useful or who owns PM/internship hiring."
    )


def relationship_stage(
    *,
    contacts: list[ContactRecord],
    touchpoints: list[TouchpointRecord],
) -> str:
    contact_statuses = {(contact.status or "").strip().lower() for contact in contacts}
    touchpoint_statuses = {(touchpoint.status or "").strip().lower() for touchpoint in touchpoints}
    touchpoint_kinds = {(touchpoint.message_kind or "").strip().lower() for touchpoint in touchpoints}

    if contact_statuses.intersection({"referral", "champion"}) or touchpoint_statuses.intersection({"referral", "champion"}):
        return "champion"
    if (
        contact_statuses.intersection(CONVERSATION_CONTACT_STATUSES)
        or touchpoint_statuses.intersection(CONVERSATION_TOUCHPOINT_STATUSES)
        or any("reply" in kind or "coffee" in kind for kind in touchpoint_kinds)
    ):
        return "conversation"
    if contact_statuses.intersection(CONNECTED_CONTACT_STATUSES):
        return "connected_no_conversation"
    if touchpoint_statuses.intersection(SENT_TOUCHPOINT_STATUSES) or "invited" in contact_statuses:
        return "outreach_sent"
    if contacts:
        return "contacts_found"
    return "unstarted"


def latest_relationship_touch_at(contacts: list[ContactRecord], touchpoints: list[TouchpointRecord]) -> datetime | None:
    timestamps: list[datetime] = []
    for contact in contacts:
        parsed = parse_iso_timestamp(contact.last_contacted_at or contact.discovered_at)
        if parsed is not None:
            timestamps.append(parsed)
    for touchpoint in touchpoints:
        parsed = parse_iso_timestamp(touchpoint.sent_at or touchpoint.recorded_at)
        if parsed is not None:
            timestamps.append(parsed)
    if not timestamps:
        return None
    return max(timestamps)


def build_relationship_loop_items(
    *,
    organizations: list[OrganizationRecord],
    opportunities: list[OpportunityRecord],
    contacts: list[ContactRecord],
    touchpoints: list[TouchpointRecord],
    include_target_lists: tuple[str, ...] = (),
    min_fit_score: int = 55,
    target_relationships: int = 3,
    outreach_wave_size: int = 10,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    reference_now = now or datetime.now(UTC)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=UTC)
    else:
        reference_now = reference_now.astimezone(UTC)

    opportunity_map: dict[str, list[OpportunityRecord]] = {}
    for item in opportunities:
        opportunity_map.setdefault(item.organization_id, []).append(item)

    contact_map: dict[str, list[ContactRecord]] = {}
    for item in contacts:
        contact_map.setdefault(item.organization_id, []).append(item)

    touchpoint_map: dict[str, list[TouchpointRecord]] = {}
    for item in touchpoints:
        touchpoint_map.setdefault(item.organization_id, []).append(item)

    required_tags = {tag.strip().lower() for tag in include_target_lists if tag.strip()}
    items: list[dict[str, object]] = []
    for organization in organizations:
        organization_tags = split_semicolon_tags(organization.target_lists)
        if required_tags and not required_tags.intersection(organization_tags):
            continue

        organization_opportunities = opportunity_map.get(organization.organization_id, [])
        organization_contacts = contact_map.get(organization.organization_id, [])
        organization_touchpoints = touchpoint_map.get(organization.organization_id, [])
        tags = extract_tags_from_notes(organization.notes)
        description = extract_description_from_notes(organization.notes)
        fit_score, fit_reasons = infer_fit_reasons(
            organization=organization,
            tags=tags,
            description=description,
            opportunities=organization_opportunities,
        )
        scored_opportunities: list[dict[str, object]] = []
        for opportunity in organization_opportunities:
            relevance_score, relevance_reasons = score_opportunity_relevance(
                opportunity.title,
                organization,
                organization_description=description,
            )
            scored_opportunities.append(
                {
                    "title": opportunity.title,
                    "location": opportunity.location,
                    "source_url": opportunity.source_url,
                    "relevance_score": relevance_score,
                    "relevance_reasons": relevance_reasons,
                    "action": classify_opportunity_action(relevance_score),
                }
            )
        scored_opportunities.sort(key=lambda item: int(item["relevance_score"]), reverse=True)
        relevant_roles = [item for item in scored_opportunities if item["action"] == "apply_now"]
        borderline_roles = [item for item in scored_opportunities if item["action"] == "review"]

        connected_contacts = [
            contact
            for contact in organization_contacts
            if (contact.status or "").strip().lower() in CONNECTED_CONTACT_STATUSES
        ]
        conversation_contacts = [
            contact
            for contact in organization_contacts
            if (contact.status or "").strip().lower() in CONVERSATION_CONTACT_STATUSES
        ]
        sent_touchpoints = [
            touchpoint
            for touchpoint in organization_touchpoints
            if (touchpoint.status or "").strip().lower() in SENT_TOUCHPOINT_STATUSES
        ]
        invite_contacts = [
            contact
            for contact in organization_contacts
            if (contact.status or "").strip().lower() in {"invited", "invite ready", "connected", "warm"}
        ]
        sent_count = max(len(sent_touchpoints), len(invite_contacts))
        stage = relationship_stage(contacts=organization_contacts, touchpoints=organization_touchpoints)
        last_touch_at = latest_relationship_touch_at(organization_contacts, organization_touchpoints)
        days_since_last_touch = (
            max(0, (reference_now - last_touch_at).days)
            if last_touch_at is not None
            else None
        )
        relationship_gap = max(target_relationships - len(conversation_contacts), 0)
        manual_priority_tags = {"priority", "core", "relationship", "target", "dream"}
        is_core_candidate = bool(
            fit_score >= min_fit_score
            or relevant_roles
            or manual_priority_tags.intersection(organization_tags)
        )

        follow_up_contact = connected_contacts[0] if connected_contacts else None
        suggested_message = ""
        if not is_core_candidate:
            next_action = "watch"
            action_reason = "Fit is not strong enough for the relationship-engine core list yet."
        elif stage in {"champion", "conversation"}:
            next_action = "maintain_relationship"
            action_reason = "There is already a real conversation or champion signal; keep this warm deliberately."
        elif connected_contacts:
            next_action = "follow_up_connected_contact"
            action_reason = "At least one person is connected, but we have not logged a real conversation yet."
            suggested_message = build_relationship_follow_up_message(organization.name, follow_up_contact)
        elif not organization_contacts:
            next_action = "run_linkedin_people_search"
            action_reason = "Company is a fit, but we have no people mapped yet."
        elif sent_count == 0:
            next_action = "send_initial_invites"
            action_reason = "People are mapped, but no invite has been sent yet."
        elif sent_count >= outreach_wave_size and not connected_contacts:
            next_action = "research_email_path"
            action_reason = "LinkedIn outreach has enough volume without a warm connection; add another channel."
        elif days_since_last_touch is not None and days_since_last_touch < 3:
            next_action = "wait_for_accepts"
            action_reason = "Recent invites are still fresh; do not over-rotate before LinkedIn has time to resolve."
        else:
            next_action = "expand_contact_wave"
            action_reason = "The company still fits and the last outreach wave has not created a conversation."

        account_score = (
            fit_score
            + len(relevant_roles) * 10
            + relationship_gap * 8
            + relationship_action_priority(next_action) * 6
            + min(len(organization_contacts), outreach_wave_size)
            - min(sent_count, outreach_wave_size)
        )

        items.append(
            {
                "organization_id": organization.organization_id,
                "company": organization.name,
                "relationship_goal": "summer_fall_internship",
                "relationship_stage": stage,
                "account_score": account_score,
                "fit_score": fit_score,
                "fit_band": fit_band_from_score(fit_score),
                "fit_reasons": fit_reasons,
                "why_fit_for_akshat": explain_fit_for_candidate(
                    organization=organization,
                    tags=tags,
                    opportunities=organization_opportunities,
                    fit_reasons=fit_reasons,
                ),
                "target_lists": organization.target_lists,
                "organization_type": organization.organization_type.value,
                "scale_signal": extract_scale_signal_from_notes(organization.notes),
                "opportunity_count": len(organization_opportunities),
                "relevant_role_count": len(relevant_roles),
                "borderline_role_count": len(borderline_roles),
                "top_roles": scored_opportunities[:5],
                "contact_count": len(organization_contacts),
                "connected_contact_count": len(connected_contacts),
                "conversation_contact_count": len(conversation_contacts),
                "touchpoint_count": len(organization_touchpoints),
                "sent_invite_count": sent_count,
                "target_relationships": target_relationships,
                "relationship_gap": relationship_gap,
                "last_touch_at": last_touch_at.isoformat() if last_touch_at else "",
                "days_since_last_touch": days_since_last_touch,
                "next_action": next_action,
                "action_reason": action_reason,
                "suggested_contact_id": follow_up_contact.contact_id if follow_up_contact else "",
                "suggested_contact_name": follow_up_contact.full_name if follow_up_contact else "",
                "suggested_message": suggested_message,
            }
        )

    items.sort(
        key=lambda item: (
            relationship_action_priority(str(item["next_action"])),
            int(item["account_score"]),
            int(item["fit_score"]),
            str(item["company"]).lower(),
        ),
        reverse=True,
    )
    return items


def build_target_action_queue_items(
    *,
    organizations: list[OrganizationRecord],
    opportunities: list[OpportunityRecord],
    contacts: list[ContactRecord],
    touchpoints: list[TouchpointRecord],
    include_target_lists: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    opportunity_map: dict[str, list[OpportunityRecord]] = {}
    for item in opportunities:
        opportunity_map.setdefault(item.organization_id, []).append(item)

    contact_map: dict[str, list[ContactRecord]] = {}
    for item in contacts:
        contact_map.setdefault(item.organization_id, []).append(item)

    touchpoint_map: dict[str, list[TouchpointRecord]] = {}
    for item in touchpoints:
        touchpoint_map.setdefault(item.organization_id, []).append(item)

    required_tags = {tag.strip().lower() for tag in include_target_lists if tag.strip()}
    results: list[dict[str, object]] = []
    for organization in organizations:
        organization_tags = split_semicolon_tags(organization.target_lists)
        if required_tags and not required_tags.intersection(organization_tags):
            continue

        organization_opportunities = opportunity_map.get(organization.organization_id, [])
        tags = extract_tags_from_notes(organization.notes)
        description = extract_description_from_notes(organization.notes)
        fit_score, fit_reasons = infer_fit_reasons(
            organization=organization,
            tags=tags,
            description=description,
            opportunities=organization_opportunities,
        )
        scored_opportunities: list[dict[str, object]] = []
        for opportunity in organization_opportunities:
            relevance_score, relevance_reasons = score_opportunity_relevance(
                opportunity.title,
                organization,
                organization_description=description,
            )
            scored_opportunities.append(
                {
                    "title": opportunity.title,
                    "location": opportunity.location,
                    "source_url": opportunity.source_url,
                    "opportunity_type": opportunity.opportunity_type.value,
                    "relevance_score": relevance_score,
                    "relevance_reasons": relevance_reasons,
                    "action": classify_opportunity_action(relevance_score),
                }
            )

        scored_opportunities.sort(
            key=lambda item: (int(item["relevance_score"]), str(item["title"]).lower()),
            reverse=True,
        )
        apply_now_opportunities = [item for item in scored_opportunities if item["action"] == "apply_now"]
        review_opportunities = [item for item in scored_opportunities if item["action"] == "review"]
        has_founder_contact = any(
            (contact.contact_type or "").lower() == "founder"
            for contact in contact_map.get(organization.organization_id, [])
        )
        startup_like = organization.organization_type in {
            OrganizationType.STARTUP,
            OrganizationType.ACCELERATOR,
            OrganizationType.INCUBATOR,
            OrganizationType.HACKER_HOUSE,
        }
        already_contacted = bool(touchpoint_map.get(organization.organization_id, []))

        action = "skip"
        action_reason = "No strong role relevance or outreach signal yet."
        if apply_now_opportunities:
            action = "apply_now"
            action_reason = "At least one role looks relevant enough to apply through the source/company workflow."
        elif startup_like and fit_score >= 55 and not already_contacted:
            action = "outreach_now"
            action_reason = "Company fit is strong enough for founder/operator outreach even without a directly relevant role."
        elif review_opportunities:
            action = "review"
            action_reason = "There are borderline roles worth checking manually before we discard the company."

        results.append(
            {
                "organization_id": organization.organization_id,
                "company": organization.name,
                "target_lists": organization.target_lists,
                "organization_type": organization.organization_type.value,
                "source_kind": organization.source_kind.value,
                "website": organization.website,
                "source_url": organization.source_url,
                "what_it_does": summarize_company_description(description) if description else "",
                "fit_score": fit_score,
                "fit_band": fit_band_from_score(fit_score),
                "fit_reasons": fit_reasons,
                "action": action,
                "action_reason": action_reason,
                "relevant_role_count": len(apply_now_opportunities),
                "borderline_role_count": len(review_opportunities),
                "total_role_count": len(scored_opportunities),
                "top_relevant_roles": apply_now_opportunities[:5],
                "top_borderline_roles": review_opportunities[:3],
                "sample_irrelevant_roles": [item["title"] for item in scored_opportunities if item["action"] == "ignore"][:3],
                "channel_recommendation": infer_channel_recommendation(
                    organization,
                    contact_map.get(organization.organization_id, []),
                ),
                "has_founder_contact": has_founder_contact,
                "already_contacted": already_contacted,
            }
        )

    results.sort(
        key=lambda item: (
            action_priority(str(item["action"])),
            int(item["relevant_role_count"]),
            int(item["fit_score"]),
            int(item["total_role_count"]),
            str(item["company"]).lower(),
        ),
        reverse=True,
    )
    return results


def build_organization_intel_items(
    *,
    organizations: list[OrganizationRecord],
    opportunities: list[OpportunityRecord],
    contacts: list[ContactRecord],
    touchpoints: list[TouchpointRecord],
    include_target_lists: tuple[str, ...] = (),
    require_hiring_signal: bool = False,
    latest_first: bool = False,
) -> list[dict[str, object]]:
    opportunity_map: dict[str, list[OpportunityRecord]] = {}
    for item in opportunities:
        opportunity_map.setdefault(item.organization_id, []).append(item)

    contact_map: dict[str, list[ContactRecord]] = {}
    for item in contacts:
        contact_map.setdefault(item.organization_id, []).append(item)

    touchpoint_map: dict[str, list[TouchpointRecord]] = {}
    for item in touchpoints:
        touchpoint_map.setdefault(item.organization_id, []).append(item)

    required_tags = {tag.strip().lower() for tag in include_target_lists if tag.strip()}
    items: list[dict[str, object]] = []
    for organization in organizations:
        organization_tags = split_semicolon_tags(organization.target_lists)
        if required_tags and not required_tags.intersection(organization_tags):
            continue

        organization_opportunities = opportunity_map.get(organization.organization_id, [])
        if require_hiring_signal and not organization_opportunities:
            continue

        metadata = parse_notes_metadata(organization.notes)
        tags = extract_tags_from_notes(organization.notes)
        description = extract_description_from_notes(organization.notes)
        fit_score, fit_reasons = infer_fit_reasons(
            organization=organization,
            tags=tags,
            description=description,
            opportunities=organization_opportunities,
        )
        items.append(
            {
                "organization_id": organization.organization_id,
                "company": organization.name,
                "organization_type": organization.organization_type.value,
                "target_lists": organization.target_lists,
                "source_kind": organization.source_kind.value,
                "status": organization.status,
                "discovered_at": organization.discovered_at,
                "website": organization.website,
                "source_url": organization.source_url,
                "what_it_does": summarize_company_description(description) if description else "",
                "tags": tags,
                "scale_signal": extract_scale_signal_from_notes(organization.notes),
                "team_size": format_team_size_signal(metadata.get("team_size", "")),
                "founded_year": metadata.get("founded_year", ""),
                "batch": metadata.get("batch", ""),
                "location": metadata.get("location", organization.city),
                "jobs_count": metadata.get("jobs_count", ""),
                "public_revenue_signal": metadata.get("revenue", "Not surfaced in the source pages yet."),
                "opportunity_count": len(organization_opportunities),
                "opportunity_titles": [item.title for item in organization_opportunities[:5]],
                "contact_count": len(contact_map.get(organization.organization_id, [])),
                "touchpoint_count": len(touchpoint_map.get(organization.organization_id, [])),
                "fit_score": fit_score,
                "fit_band": fit_band_from_score(fit_score),
                "fit_reasons": fit_reasons,
                "why_fit_for_akshat": explain_fit_for_candidate(
                    organization=organization,
                    tags=tags,
                    opportunities=organization_opportunities,
                    fit_reasons=fit_reasons,
                ),
                "channel_recommendation": infer_channel_recommendation(
                    organization,
                    contact_map.get(organization.organization_id, []),
                ),
            }
        )

    sort_key = (
        (lambda item: (str(item["discovered_at"]), int(item["fit_score"]), int(item["opportunity_count"])))
        if latest_first
        else (lambda item: (int(item["fit_score"]), int(item["opportunity_count"]), str(item["discovered_at"])))
    )
    items.sort(key=sort_key, reverse=True)
    return items


def score_linkedin_company_target(
    *,
    organization: OrganizationRecord,
    team_size: int | None,
    opportunity_count: int,
    contact_count: int,
    linkedin_contact_count: int,
    touchpoint_count: int,
) -> tuple[int, list[str]]:
    score = 0
    triggers: list[str] = []

    if organization.organization_type in {
        OrganizationType.STARTUP,
        OrganizationType.HACKER_HOUSE,
        OrganizationType.INCUBATOR,
        OrganizationType.ACCELERATOR,
    }:
        score += 35
        triggers.append("Startup-style target")

    if opportunity_count > 0:
        score += min(25, 10 + opportunity_count * 3)
        triggers.append("Live hiring or opportunity signal")

    if linkedin_contact_count == 0:
        score += 20
        triggers.append("No LinkedIn-sourced contacts yet")
    else:
        score -= min(18, linkedin_contact_count * 6)
        triggers.append("Already has LinkedIn contacts")

    if contact_count > 0 and linkedin_contact_count == 0:
        score += 4
        triggers.append("Has non-LinkedIn contacts only")

    if touchpoint_count == 0:
        score += 8
        triggers.append("No outreach sent yet")

    if team_size is not None:
        if team_size <= 50:
            score += 18
            triggers.append("Small team")
        elif team_size <= 200:
            score += 10
            triggers.append("Mid-size team")
        elif team_size >= 2000:
            score -= 10
            triggers.append("Large company")

    target_list_tags = split_semicolon_tags(organization.target_lists)
    for preferred_tag in {"yc", "startup", "built_in", "hiring"}:
        if preferred_tag in target_list_tags:
            score += 4
    if "yc" in target_list_tags:
        triggers.append("YC source")
    if "built_in" in target_list_tags:
        triggers.append("Built In source")

    if organization.source_kind in {SourceKind.YC_DIRECTORY, SourceKind.STARTUP_DIRECTORY}:
        score += 6

    return score, triggers


def build_linkedin_company_queue_items(
    *,
    organizations: list[OrganizationRecord],
    opportunities: list[OpportunityRecord],
    contacts: list,
    touchpoints: list,
    include_target_lists: tuple[str, ...] = (),
    require_no_contacts: bool = True,
    require_hiring_signal: bool = False,
) -> list[LinkedInCompanyQueueItem]:
    opportunity_map: dict[str, list[OpportunityRecord]] = {}
    for item in opportunities:
        opportunity_map.setdefault(item.organization_id, []).append(item)

    contact_map: dict[str, list] = {}
    for item in contacts:
        contact_map.setdefault(item.organization_id, []).append(item)

    touchpoint_map: dict[str, list] = {}
    for item in touchpoints:
        touchpoint_map.setdefault(item.organization_id, []).append(item)

    required_tags = {tag.strip().lower() for tag in include_target_lists if tag.strip()}
    queue_items: list[LinkedInCompanyQueueItem] = []
    for organization in organizations:
        organization_tags = split_semicolon_tags(organization.target_lists)
        if required_tags and not required_tags.intersection(organization_tags):
            continue

        organization_contacts = contact_map.get(organization.organization_id, [])
        linkedin_contacts = [item for item in organization_contacts if item.source_kind == SourceKind.LINKEDIN]
        organization_opportunities = opportunity_map.get(organization.organization_id, [])
        organization_touchpoints = touchpoint_map.get(organization.organization_id, [])
        if require_no_contacts and linkedin_contacts:
            continue
        if require_hiring_signal and not organization_opportunities:
            continue

        team_size = extract_team_size_from_notes(organization.notes)

        score, triggers = score_linkedin_company_target(
            organization=organization,
            team_size=team_size,
            opportunity_count=len(organization_opportunities),
            contact_count=len(organization_contacts),
            linkedin_contact_count=len(linkedin_contacts),
            touchpoint_count=len(organization_touchpoints),
        )
        queue_items.append(
            LinkedInCompanyQueueItem(
                organization_id=organization.organization_id,
                company=organization.name,
                company_mode=infer_company_mode(organization.organization_type.value, team_size),
                priority_score=score,
                target_lists=organization.target_lists,
                organization_type=organization.organization_type.value,
                city=organization.city,
                website=organization.website,
                source_kind=organization.source_kind.value,
                status=organization.status,
                team_size=team_size,
                opportunity_count=len(organization_opportunities),
                contact_count=len(organization_contacts),
                linkedin_contact_count=len(linkedin_contacts),
                touchpoint_count=len(organization_touchpoints),
                latest_opportunity_titles=[item.title for item in organization_opportunities[:3]],
                triggers=triggers,
            )
        )

    queue_items.sort(
        key=lambda item: (
            item.priority_score,
            item.opportunity_count,
            -item.contact_count,
            item.company.lower(),
        ),
        reverse=True,
    )
    return queue_items


def build_company_note_context(workbook: OutreachWorkbook, company: str) -> dict[str, object]:
    company_key = normalize_dedupe_text(company)
    organization = next(
        (
            item
            for item in workbook.list_organizations()
            if normalize_dedupe_text(item.name) == company_key
        ),
        None,
    )
    if organization is None:
        return {}

    opportunities = [
        item
        for item in workbook.list_opportunities()
        if item.organization_id == organization.organization_id
    ]
    opportunities.sort(key=lambda item: item.discovered_at, reverse=True)

    tags = extract_tags_from_notes(organization.notes)
    description = extract_description_from_notes(organization.notes)
    scale_signal = extract_scale_signal_from_notes(organization.notes)
    fit_rationale = ""
    for opportunity in opportunities:
        fit_rationale = parse_notes_metadata(opportunity.notes).get("fit_rationale", "")
        if fit_rationale:
            break

    context: dict[str, object] = {
        "organization_type": organization.organization_type.value,
        "target_lists": organization.target_lists,
        "tags": tags,
        "description": description,
        "scale_signal": scale_signal,
        "opportunity_titles": [item.title for item in opportunities[:3]],
        "fit_rationale": fit_rationale,
    }
    return {
        key: value
        for key, value in context.items()
        if value not in ("", [], None)
    }


def execute_linkedin_company_run(
    *,
    settings: OutreachSettings,
    company: str,
    dry_run: bool,
    company_mode: str,
    include_pass: list[str] | None = None,
    exclude_pass: list[str] | None = None,
    enable_marshall: bool = False,
    force_broad_fallback: bool = False,
    note_context: dict | None = None,
) -> Path:
    scraper = LinkedInScraper(settings)
    scraper.require_live_cdp_session()
    note_generator = NoteGenerator()
    if note_context is None:
        note_context = build_company_note_context(
            OutreachWorkbook(settings.resolved_tracking_workspace_dir),
            company,
        )
    deduped: dict[str, dict] = {}
    pass_summaries: list[dict] = []
    startup_pool: dict[str, int | str | bool | None] = {
        "raw_count": None,
        "kept_count": None,
        "pool_mode": "unknown",
        "adaptive_send_min_score": 20,
        "coverage_only": False,
        "search_company": company,
    }
    search_company = company
    if company_mode == "startup":
        preflight_limit = settings.search.startup_preflight_limit
        preflight_pages = settings.search.startup_preflight_max_pages
        preflight_errors: list[str] = []
        preflight_run = None
        aliases = company_search_aliases(company)
        for alias in aliases:
            try:
                preflight_run = scraper.extract_people_with_filters_live(
                    company=alias,
                    search_query="",
                    limit=preflight_limit,
                    max_pages=preflight_pages,
                    school=None,
                    connection_degree=None,
                    use_us_location=False,
                )
                search_company = alias
                break
            except Exception as exc:
                preflight_errors.append(f"{alias}: {exc}")
        preflight_fallback_used = False
        if preflight_run is None:
            fallback_candidates: list = []
            fallback_query = ""
            fallback_error = ""
            try:
                for alias in aliases:
                    query = alias
                    raw_candidates = scraper.extract_people_live(
                        search_query=query,
                        limit=preflight_limit,
                        max_pages=preflight_pages,
                    )
                    for raw in raw_candidates:
                        if candidate_mentions_company(raw, aliases):
                            fallback_candidates.append(raw)
                    if fallback_candidates:
                        search_company = alias
                        fallback_query = query
                        break
            except Exception as exc:
                fallback_error = str(exc)
            if not fallback_candidates:
                detail = " | ".join(preflight_errors)
                if fallback_error:
                    detail = f"{detail} | keyword fallback: {fallback_error}"
                raise RuntimeError(
                    "Could not run startup preflight with any company alias: "
                    + detail
                )
            preflight_fallback_used = True
            preflight_run = FilterRunResult(
                candidates=fallback_candidates[:preflight_limit],
                final_url="",
                visible_filter_text=[f"keyword fallback: {fallback_query or company}"],
                screenshot_path=None,
            )
        preflight_artifact = write_artifact(
            settings.artifacts_dir,
            "startup-preflight",
            {
                "company": company,
                "search_company": search_company,
                "company_mode": company_mode,
                "fallback_used": preflight_fallback_used,
                "limit": preflight_limit,
                "max_pages": preflight_pages,
                "final_url": preflight_run.final_url,
                "visible_filter_text": preflight_run.visible_filter_text,
                "screenshot": preflight_run.screenshot_path,
                "raw_count": len(preflight_run.candidates),
                "results": [item.model_dump() for item in preflight_run.candidates],
            },
        )
        preflight_kept_count = 0
        preflight_pass_config: dict[str, str | int | bool] = {
            "school": "",
            "connection_degree": None,
            "use_us_location": False,
        }
        preflight_pass_name = "startup_company_coverage" if preflight_fallback_used else "startup_preflight"
        for raw in preflight_run.candidates:
            if apply_raw_candidate(
                deduped=deduped,
                raw=raw,
                company=company,
                pass_name=preflight_pass_name,
                pass_config=preflight_pass_config,
                settings=settings,
                company_mode=company_mode,
            ):
                preflight_kept_count += 1
        startup_pool = {
            "raw_count": len(preflight_run.candidates),
            "kept_count": preflight_kept_count,
            "pool_mode": startup_pool_mode(len(preflight_run.candidates)),
            "adaptive_send_min_score": startup_pool_send_min_score(startup_pool_mode(len(preflight_run.candidates))),
            "coverage_only": len(preflight_run.candidates) <= settings.search.startup_small_company_threshold,
            "search_company": search_company,
        }
        pass_summaries.append(
            {
                "pass_name": preflight_pass_name,
                "query": "",
                "search_company": search_company,
                "alias_used": search_company != company,
                "alias_errors": preflight_errors,
                "fallback_used": preflight_fallback_used,
                "school": None,
                "connection_degree": None,
                "use_us_location": False,
                "final_url": preflight_run.final_url,
                "screenshot": preflight_run.screenshot_path,
                "limit": preflight_limit,
                "max_pages": preflight_pages,
                "raw_count": len(preflight_run.candidates),
                "kept_count": preflight_kept_count,
                "artifact": str(preflight_artifact),
                "coverage_only": startup_pool["coverage_only"],
                "pool_mode": startup_pool["pool_mode"],
                "adaptive_send_min_score": startup_pool["adaptive_send_min_score"],
            }
        )
        typer.echo(
            f"- Startup preflight: {len(preflight_run.candidates)} raw results across up to {preflight_pages} pages"
        )
        if preflight_fallback_used:
            typer.echo("  exact company filter failed; used keyword company coverage fallback")
        typer.echo(f"  kept {preflight_kept_count} after startup coverage filtering")
        typer.echo(
            f"  pool mode: {startup_pool['pool_mode']} | adaptive send threshold >= {startup_pool['adaptive_send_min_score']}"
        )
        if search_company != company:
            typer.echo(f"  LinkedIn company alias used: {search_company}")
    pass_definitions = resolve_pass_definitions(
        settings,
        company_mode=company_mode,
        include_passes=tuple(include_pass or []),
        exclude_passes=tuple(exclude_pass or []),
        enable_marshall=enable_marshall,
        force_broad_fallback=force_broad_fallback,
    )
    ordered_passes = sorted(
        pass_definitions.items(),
        key=lambda item: int(item[1].get("priority", 999)),
    )
    for pass_name, pass_config in ordered_passes:
        if not bool(pass_config.get("enabled", True)):
            typer.echo(f"- Pass {pass_name}: skipped (disabled)")
            continue
        pool_floor = pass_config.get("run_if_below_pool_size")
        if pool_floor is not None and len(deduped) >= int(pool_floor):
            typer.echo(f"- Pass {pass_name}: skipped (pool already at {len(deduped)})")
            continue
        pass_query = str(pass_config.get("query", "")).strip()
        limit = int(pass_config.get("limit", settings.search.default_limit))
        max_pages = int(pass_config.get("max_pages", settings.search.max_pages_default))
        query = pass_query
        try:
            filter_run = scraper.extract_people_with_filters_live(
                company=search_company,
                search_query=query,
                limit=limit,
                max_pages=max_pages,
                school=str(pass_config.get("school")) if pass_config.get("school") else None,
                connection_degree=str(pass_config.get("connection_degree")) if pass_config.get("connection_degree") else None,
                use_us_location=bool(pass_config.get("use_us_location", True)),
            )
        except Exception as exc:
            pass_summaries.append(
                {
                    "pass_name": pass_name,
                    "query": query,
                    "school": pass_config.get("school"),
                    "connection_degree": pass_config.get("connection_degree"),
                    "use_us_location": pass_config.get("use_us_location", True),
                    "final_url": "",
                    "screenshot": "",
                    "limit": limit,
                    "max_pages": max_pages,
                    "raw_count": 0,
                    "kept_count": 0,
                    "artifact": "",
                    "error": str(exc),
                }
            )
            typer.echo(f"- Pass {pass_name}: failed ({exc})")
            continue
        raw_candidates = filter_run.candidates
        kept_count = 0
        pass_artifact = write_artifact(
            settings.artifacts_dir,
            f"pass-{pass_name}",
            {
                "company": company,
                "search_company": search_company,
                "pass_name": pass_name,
                "query": query,
                "school": pass_config.get("school"),
                "connection_degree": pass_config.get("connection_degree"),
                "use_us_location": pass_config.get("use_us_location", True),
                "final_url": filter_run.final_url,
                "visible_filter_text": filter_run.visible_filter_text,
                "screenshot": filter_run.screenshot_path,
                "raw_count": len(raw_candidates),
                "limit": limit,
                "max_pages": max_pages,
                "results": [item.model_dump() for item in raw_candidates],
            },
        )
        pass_summaries.append(
            {
                "pass_name": pass_name,
                "query": query,
                "school": pass_config.get("school"),
                "connection_degree": pass_config.get("connection_degree"),
                "use_us_location": pass_config.get("use_us_location", True),
                "final_url": filter_run.final_url,
                "screenshot": filter_run.screenshot_path,
                "limit": limit,
                "max_pages": max_pages,
                "raw_count": len(raw_candidates),
                "kept_count": 0,
                "artifact": str(pass_artifact),
            }
        )
        typer.echo(f"- Pass {pass_name}: {len(raw_candidates)} raw results")
        for raw in raw_candidates:
            if apply_raw_candidate(
                deduped=deduped,
                raw=raw,
                company=company,
                pass_name=pass_name,
                pass_config=pass_config,
                settings=settings,
                company_mode=company_mode,
            ):
                kept_count += 1

        pass_summaries[-1]["kept_count"] = kept_count
        typer.echo(f"  kept {kept_count} after pass relevance filtering")

        if len(deduped) >= settings.search.hard_company_limit:
            typer.echo(f"Reached hard company limit of {settings.search.hard_company_limit}; stopping early.")
            break

    scored_candidates = list(deduped.values())
    for candidate in scored_candidates:
        if candidate["existing_connection"]:
            candidate["priority_bucket"] = "Direct Message Now"
        else:
            candidate["priority_bucket"] = candidate["tier"]

    scored_candidates.sort(
        key=lambda item: (item["existing_connection"], item["score"], item["name"]),
        reverse=True,
    )
    scored_candidates = scored_candidates[: settings.search.final_company_limit]
    noted_candidates = note_generator.generate_batch(
        scored_candidates[: settings.search.note_generation_limit],
        company=company,
        company_mode=company_mode,
        note_context=note_context,
    )
    scored_candidates = [*noted_candidates, *scored_candidates[settings.search.note_generation_limit :]]

    artifact = write_artifact(
        settings.artifacts_dir,
        "dry-run-pipeline",
        {
            "company": company,
            "company_mode": company_mode,
            "dry_run": dry_run,
            "passes": pass_definitions,
            "pass_summaries": pass_summaries,
            "startup_pool": startup_pool,
            "note_context": note_context,
            "count": len(scored_candidates),
            "notes_generated_count": len(noted_candidates),
            "results": scored_candidates,
        },
    )
    typer.echo(f"Starting outreach pipeline for {company}")
    typer.echo(f"Dry run: {dry_run}")
    typer.echo(f"Timezone: {settings.timezone}")
    typer.echo(
        f"Captured and scored {len(scored_candidates)} candidates; "
        f"generated notes for top {len(noted_candidates)}."
    )
    typer.echo(f"Artifact: {artifact}")
    return artifact


def select_invite_candidates(
    candidates: list[dict],
    *,
    verdict: str = "send",
    min_score: int = 35,
    limit: int = 10,
    start_at: int = 0,
) -> list[dict]:
    eligible: list[dict] = []
    for item in candidates:
        qc = item.get("polished_note_qc") or item.get("note_qc") or {}
        item_verdict = qc.get("verdict")
        if verdict and item_verdict != verdict:
            continue
        if item.get("existing_connection"):
            continue
        if not item.get("linkedin_url"):
            continue
        try:
            candidate_score = int(item.get("score"))
        except (TypeError, ValueError):
            candidate_score = None
        if min_score > -999 and (candidate_score is None or candidate_score < min_score):
            continue
        item = dict(item)
        if "polished_note" in item:
            item["note"] = item["polished_note"]
        eligible.append(item)
    return eligible[start_at : start_at + limit]


def persist_invite_send_results(
    *,
    workbook: OutreachWorkbook,
    company: str,
    source_artifact_path: Path,
    processed_candidates: list[dict],
    send_results: list,
    send_artifact_path: Path,
) -> tuple[int, int]:
    organization, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(company),
            name=company,
            organization_type=OrganizationType.COMPANY,
            target_lists="referrals;linkedin",
            status="Outreach in progress",
            source_kind=SourceKind.LINKEDIN,
            source_url="https://www.linkedin.com/search/results/people/",
            notes="LinkedIn outreach send results tracked from invite-send-batch artifact.",
        )
    )

    contacts_added = 0
    touchpoints_added = 0
    for candidate, result in zip(processed_candidates, send_results, strict=False):
        full_name = str(candidate.get("name") or result.name or "").strip()
        if not full_name:
            continue

        linkedin_url = str(candidate.get("linkedin_url") or result.linkedin_url or "").strip()
        sent_at = utc_now_iso() if result.status in {"sent", "sent_without_note"} else ""
        contact, created = workbook.upsert_contact(
            ContactRecord(
                contact_id=workbook.make_contact_id(
                    organization.organization_id,
                    full_name,
                    linkedin_url=linkedin_url,
                ),
                organization_id=organization.organization_id,
                full_name=full_name,
                title=str(candidate.get("title") or "").strip(),
                contact_type=str(candidate.get("role_bucket") or "").strip(),
                target_lists="referrals;linkedin",
                preferred_channel=OutreachChannel.LINKEDIN,
                status=contact_status_from_invite_result(result.status),
                linkedin_url=linkedin_url,
                source_kind=SourceKind.LINKEDIN,
                source_url="https://www.linkedin.com/search/results/people/",
                last_contacted_at=sent_at,
                notes=f"Imported from {source_artifact_path.name}",
            )
        )
        if created:
            contacts_added += 1
        elif sent_at:
            updated_contact = workbook.update_contact(
                contact.contact_id,
                status=contact_status_from_invite_result(result.status),
                last_contacted_at=sent_at,
            )
            if updated_contact is not None:
                contact = updated_contact

        note_text = str(candidate.get("note") or result.note or "").strip()
        if not note_text:
            continue

        touchpoint, created = workbook.append_touchpoint(
            TouchpointRecord(
                touchpoint_id=workbook.make_touchpoint_id(
                    organization.organization_id,
                    contact.contact_id,
                    OutreachChannel.LINKEDIN.value,
                    note_text,
                    source_artifact=str(send_artifact_path),
                ),
                organization_id=organization.organization_id,
                contact_id=contact.contact_id,
                channel=OutreachChannel.LINKEDIN,
                status=touchpoint_status_from_invite_result(result.status),
                message_kind="linkedin_invite",
                message_text=note_text,
                sent_at=sent_at,
                source_artifact=str(send_artifact_path),
                notes=(
                    f"invite_result={result.status} | detail={result.detail} | "
                    f"source_artifact={source_artifact_path.name}"
                ),
            )
        )
        if created:
            touchpoints_added += 1

    return contacts_added, touchpoints_added


def contact_status_from_invite_result(status: str) -> str:
    mapping = {
        "sent": "Invited",
        "sent_without_note": "Invited",
        "dry_run_ready": "Invite ready",
        "already_connected": "Connected",
        "unavailable": "No connect path",
        "navigation_error": "Navigation error",
        "send_error": "Invite error",
        "skipped": "Skipped",
    }
    return mapping.get(status, "Invite processed")


def touchpoint_status_from_invite_result(status: str) -> str:
    mapping = {
        "sent": "Sent",
        "sent_without_note": "Sent",
        "dry_run_ready": "Prepared",
        "already_connected": "Already connected",
        "unavailable": "Unavailable",
        "navigation_error": "Navigation error",
        "send_error": "Error",
        "skipped": "Skipped",
    }
    return mapping.get(status, "Processed")


def execute_invite_batch(
    *,
    settings: OutreachSettings,
    company: str,
    source_artifact_path: Path,
    batch: list[dict],
    execute: bool,
    limit: int,
    start_at: int,
    verdict: str,
    min_score: int,
) -> tuple[Path, Path, dict[str, int], int, int]:
    scraper = LinkedInScraper(settings)
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    progress_artifact = settings.artifacts_dir / f"{source_artifact_path.stem}-{artifact_timestamp()}-invite-progress.json"
    progress_artifact.parent.mkdir(parents=True, exist_ok=True)
    status_counts: dict[str, int] = {}
    contacts_added = 0
    touchpoints_added = 0

    def _write_progress(results: list) -> None:
        payload = {
            "source_artifact": str(source_artifact_path),
            "company": company,
            "execute": execute,
            "limit": limit,
            "start_at": start_at,
            "verdict": verdict,
            "min_score": min_score,
            "count": len(results),
            "results": [result.__dict__ for result in results],
        }
        progress_artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _on_result(candidate: dict, result, results: list) -> None:
        nonlocal contacts_added, touchpoints_added
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
        if execute:
            added_contacts, added_touchpoints = persist_invite_send_results(
                workbook=workbook,
                company=company,
                source_artifact_path=source_artifact_path,
                processed_candidates=[candidate],
                send_results=[result],
                send_artifact_path=progress_artifact,
            )
            contacts_added += added_contacts
            touchpoints_added += added_touchpoints
        _write_progress(results)

    results = scraper.send_connection_requests(batch, execute=execute, on_result=_on_result)
    artifact = write_artifact(
        settings.artifacts_dir,
        "invite-send-batch",
        {
            "source_artifact": str(source_artifact_path),
            "progress_artifact": str(progress_artifact),
            "company": company,
            "execute": execute,
            "limit": limit,
            "start_at": start_at,
            "verdict": verdict,
            "min_score": min_score,
            "count": len(results),
            "results": [result.__dict__ for result in results],
        },
    )
    return artifact, progress_artifact, status_counts, contacts_added, touchpoints_added


def attach_search_urls_to_candidates(payload: dict, candidates: list[dict]) -> list[dict]:
    summaries = payload.get("pass_summaries") or []
    pass_url_map = {
        str(item.get("pass_name") or ""): str(item.get("final_url") or "")
        for item in summaries
        if item.get("pass_name") and item.get("final_url")
    }
    enriched: list[dict] = []
    for candidate in candidates:
        item = dict(candidate)
        search_url = ""
        for pass_name in item.get("passes") or []:
            search_url = pass_url_map.get(str(pass_name), "")
            if search_url:
                break
        if search_url:
            item["_search_url"] = search_url
        enriched.append(item)
    return enriched

app = typer.Typer(help="Outreach engine CLI")


@app.command()
def doctor() -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        typer.echo("Copy .env.example to .env and fill in the required values.")
        raise typer.Exit(code=1)

    typer.echo("Environment check")
    typer.echo(f"- Chrome user data dir: {settings.resolved_linkedin_user_data_dir}")
    typer.echo(f"- Chrome profile name: {settings.linkedin_profile_name}")
    typer.echo(f"- Chrome debug port: {settings.linkedin_debug_port}")
    typer.echo(f"- Using fallback Chrome profile: {settings.using_fallback_linkedin_profile()}")
    typer.echo(f"- Anthropic key configured: {bool(settings.anthropic_api_key)}")
    typer.echo(f"- Notion token configured: {bool(settings.notion_api_token)}")
    typer.echo(f"- Notion database configured: {bool(settings.notion_database_id)}")


@app.command("prepare-browser-manual")
def prepare_browser_manual() -> None:
    settings = OutreachSettings()
    settings.validate_explicit_linkedin_profile()
    user_data_dir = settings.resolved_linkedin_user_data_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)
    typer.echo("Use this Chrome window to log into LinkedIn normally, including Google if needed.")
    typer.echo(f"User data dir: {user_data_dir}")
    typer.echo(f"Launch Chrome with remote debugging on port {settings.linkedin_debug_port}.")


@app.command("prepare-browser")
def prepare_browser(
    headless: Annotated[
        bool,
        typer.Option(help="Run without opening a visible browser window"),
    ] = False,
) -> None:
    settings = OutreachSettings()
    settings.validate_explicit_linkedin_profile()
    scraper = LinkedInScraper(settings)
    typer.echo("Opening dedicated automation browser for LinkedIn login.")
    typer.echo(f"User data dir: {settings.resolved_linkedin_user_data_dir}")
    scraper.prepare_browser(headless=headless)


@app.command("check-linkedin")
def check_linkedin(
    headless: Annotated[
        bool,
        typer.Option(help="Run without opening a visible browser window"),
    ] = False,
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        typer.echo("Copy .env.example to .env and fill in the required values.")
        raise typer.Exit(code=1)

    scraper = LinkedInScraper(settings)
    result = scraper.check_session(headless=headless)
    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-check",
        {
            "ok": result.ok,
            "current_url": result.current_url,
            "title": result.title,
            "logged_in": result.logged_in,
            "details": result.details,
            "steps": result.steps,
            "screenshots": result.screenshot_paths,
        },
    )

    if result.ok:
        typer.echo("LinkedIn session check passed.")
        typer.echo(f"Page title: {result.title}")
        typer.echo(f"Current URL: {result.current_url}")
        typer.echo(f"Artifact: {artifact}")
        return

    typer.echo("LinkedIn session check failed.")
    typer.echo(result.details)
    typer.echo(f"Artifact: {artifact}")
    raise typer.Exit(code=1)


@app.command("check-linkedin-live")
def check_linkedin_live() -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)

    scraper = LinkedInScraper(settings)
    result = scraper.check_session_via_cdp()
    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-live-check",
        {
            "ok": result.ok,
            "current_url": result.current_url,
            "title": result.title,
            "logged_in": result.logged_in,
            "details": result.details,
            "steps": result.steps,
            "screenshots": result.screenshot_paths,
        },
    )
    if result.ok:
        typer.echo("LinkedIn live session check passed.")
        typer.echo(f"Page title: {result.title}")
        typer.echo(f"Current URL: {result.current_url}")
        typer.echo(f"Artifact: {artifact}")
        return

    typer.echo("LinkedIn live session check failed.")
    typer.echo(result.details)
    typer.echo(f"Artifact: {artifact}")
    raise typer.Exit(code=1)


@app.command("extract-company")
def extract_company(
    company: Annotated[str, typer.Option(help="Target company name")],
    limit: Annotated[int, typer.Option(help="Maximum visible people cards to capture")] = 10,
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)

    scraper = LinkedInScraper(settings)
    typer.echo(f"Extracting visible LinkedIn people results for {company}")
    results = scraper.extract_company_people_live(company=company, limit=limit)
    artifact = write_artifact(
        settings.artifacts_dir,
        "company-search",
        {
            "company": company,
            "limit": limit,
            "count": len(results),
            "results": [item.model_dump() for item in results],
        },
    )
    typer.echo(f"Captured {len(results)} visible candidates.")
    typer.echo(f"Artifact: {artifact}")


@app.command()
def run(
    company: Annotated[str, typer.Option(help="Target company name")],
    dry_run: Annotated[bool, typer.Option(help="Skip writes and external side effects")] = True,
    company_mode: Annotated[
        str,
        typer.Option(help="How to tune note ask style: default, startup, or big_company"),
    ] = "default",
    include_pass: Annotated[
        list[str] | None,
        typer.Option("--include-pass", help="Only run the named pass or passes"),
    ] = None,
    exclude_pass: Annotated[
        list[str] | None,
        typer.Option("--exclude-pass", help="Skip the named pass or passes"),
    ] = None,
    enable_marshall: Annotated[
        bool,
        typer.Option(help="Enable USC Marshall passes for this run"),
    ] = False,
    force_broad_fallback: Annotated[
        bool,
        typer.Option(help="Force the broad fallback pass even if the pool is already healthy"),
    ] = False,
    auto_send: Annotated[
        bool,
        typer.Option(help="After generating the outreach artifact, immediately send invites"),
    ] = False,
    send_limit: Annotated[
        int,
        typer.Option(help="How many invite candidates to send automatically when --auto-send is enabled; use 0 for dynamic sizing"),
    ] = 0,
    send_min_score: Annotated[
        int,
        typer.Option(help="Minimum candidate relevance score required for auto-send"),
    ] = 35,
    adaptive_send: Annotated[
        bool,
        typer.Option(help="Use startup preflight pool size to loosen/tighten auto-send threshold and cap"),
    ] = True,
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)
    LinkedInScraper(settings).require_live_cdp_session()
    artifact = execute_linkedin_company_run(
        settings=settings,
        company=company,
        dry_run=dry_run,
        company_mode=company_mode,
        include_pass=include_pass,
        exclude_pass=exclude_pass,
        enable_marshall=enable_marshall,
        force_broad_fallback=force_broad_fallback,
    )
    if not auto_send:
        return

    with artifact.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    pool_metadata = payload.get("startup_pool") or startup_pool_metadata(payload)
    effective_min_score = effective_send_min_score(
        payload,
        requested_min_score=send_min_score,
        adaptive=adaptive_send,
    )
    auto_limit = send_limit or recommend_auto_send_limit(
        len(payload["results"]),
        str(pool_metadata.get("pool_mode") or "unknown"),
    )
    batch = select_invite_candidates(
        payload["results"],
        verdict="send",
        min_score=effective_min_score,
        limit=auto_limit,
    )
    batch = attach_search_urls_to_candidates(payload, batch)
    if not batch:
        typer.echo(f"Auto-send skipped: no eligible candidates with send verdict and score >= {effective_min_score}.")
        return

    if adaptive_send and payload.get("company_mode") == "startup":
        typer.echo(
            "Adaptive send gate: "
            f"pool_mode={pool_metadata.get('pool_mode')} raw_count={pool_metadata.get('raw_count')} "
            f"requested>={send_min_score} effective>={effective_min_score} limit={auto_limit}"
        )
    typer.echo(f"Auto-sending {len(batch)} invite candidates for {company} with score >= {effective_min_score}")
    send_artifact, progress_artifact, status_counts, contacts_added, touchpoints_added = execute_invite_batch(
        settings=settings,
        company=company,
        source_artifact_path=artifact,
        batch=batch,
        execute=True,
        limit=auto_limit,
        start_at=0,
        verdict="send",
        min_score=effective_min_score,
    )
    typer.echo(f"Auto-send status summary: {status_counts}")
    typer.echo(f"Auto-send artifact: {send_artifact}")
    typer.echo(f"Auto-send progress artifact: {progress_artifact}")
    typer.echo(f"Tracked contacts_added: {contacts_added}")
    typer.echo(f"Tracked touchpoints_added: {touchpoints_added}")


@app.command("generate-notes")
def generate_notes(
    artifact_path: Annotated[Path, typer.Option(help="Path to a prior dry-run pipeline artifact")],
    company_mode: Annotated[
        str,
        typer.Option(help="How to tune note ask style: default, startup, or big_company"),
    ] = "default",
    ai_polish: Annotated[
        bool,
        typer.Option(help="Run AI polish on the top slice of generated notes"),
    ] = False,
    top_n: Annotated[
        int,
        typer.Option(help="How many top notes to polish with AI"),
    ] = 10,
    polish_model: Annotated[
        str,
        typer.Option(help="Anthropic model to use for note polish"),
    ] = "claude-haiku-4-5-20251001",
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)

    with artifact_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    company = payload["company"]
    candidates = payload["results"]
    note_context = payload.get("note_context") or {}
    note_generator = NoteGenerator()
    annotated = note_generator.generate_batch(
        candidates,
        company=company,
        company_mode=company_mode,
        note_context=note_context,
    )
    summary = {
        "send": sum(1 for item in annotated if item["note_qc"]["verdict"] == "send"),
        "blocked": sum(1 for item in annotated if item["note_qc"]["verdict"] == "blocked"),
    }
    polished_summary: dict[str, int] | None = None

    if ai_polish:
        if not settings.anthropic_api_key:
            typer.echo("ANTHROPIC_API_KEY is required for --ai-polish.")
            raise typer.Exit(code=1)
        annotated = note_generator.polish_batch(
            annotated,
            company=company,
            api_key=settings.anthropic_api_key,
            top_n=top_n,
            model=polish_model,
            company_mode=company_mode,
        )
        polished_candidates = [item for item in annotated if "polished_note_qc" in item]
        polished_summary = {
            "send": sum(1 for item in polished_candidates if item["polished_note_qc"]["verdict"] == "send"),
            "blocked": sum(1 for item in polished_candidates if item["polished_note_qc"]["verdict"] == "blocked"),
        }

    artifact = write_artifact(
        settings.artifacts_dir,
        "notes-batch",
        {
            "source_artifact": str(artifact_path),
            "company": company,
            "company_mode": company_mode,
            "note_context": note_context,
            "count": len(annotated),
            "qc_summary": summary,
            "ai_polish": ai_polish,
            "polish_top_n": top_n if ai_polish else 0,
            "polish_model": polish_model if ai_polish else None,
            "polished_qc_summary": polished_summary,
            "results": annotated,
        },
    )
    typer.echo(f"Generated notes for {len(annotated)} candidates.")
    typer.echo(f"QC summary: {summary}")
    if polished_summary is not None:
        typer.echo(f"Polished QC summary: {polished_summary}")
    typer.echo(f"Artifact: {artifact}")


@app.command("list-discovery-sources")
def list_discovery_sources() -> None:
    for entry in list_source_definitions():
        definition = entry.definition
        typer.echo(f"{definition.source_id}: {definition.label}")
        typer.echo(f"- adapter: {definition.adapter.value}")
        typer.echo(f"- target_lists: {definition.target_lists}")
        typer.echo(f"- seed_urls: {', '.join(definition.seed_urls)}")
        typer.echo(f"- why: {entry.rationale}")


@app.command("build-linkedin-company-queue")
def build_linkedin_company_queue(
    limit: Annotated[int, typer.Option(help="Maximum companies to keep in the queue artifact")] = 25,
    include_target_list: Annotated[
        list[str] | None,
        typer.Option("--include-target-list", help="Only include workbook organizations with one of these target list tags"),
    ] = None,
    require_no_contacts: Annotated[
        bool,
        typer.Option(help="Default true: only queue companies that do not already have discovered contacts"),
    ] = True,
    require_hiring_signal: Annotated[
        bool,
        typer.Option(help="Only keep companies that already have opportunity or hiring signals"),
    ] = True,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    queue_items = build_linkedin_company_queue_items(
        organizations=workbook.list_organizations(),
        opportunities=workbook.list_opportunities(),
        contacts=workbook.list_contacts(),
        touchpoints=workbook.list_touchpoints(),
        include_target_lists=tuple(include_target_list or []),
        require_no_contacts=require_no_contacts,
        require_hiring_signal=require_hiring_signal,
    )[:limit]

    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-company-queue",
        {
            "count": len(queue_items),
            "filters": {
                "limit": limit,
                "include_target_lists": include_target_list or [],
                "require_no_contacts": require_no_contacts,
                "require_hiring_signal": require_hiring_signal,
            },
            "results": [item.model_dump(mode="json") for item in queue_items],
        },
    )

    typer.echo(f"Built LinkedIn company queue with {len(queue_items)} companies.")
    typer.echo(f"Artifact: {artifact}")
    for item in queue_items[: min(10, len(queue_items))]:
        typer.echo(
            f"- {item.company} | score={item.priority_score} | mode={item.company_mode} | "
            f"opps={item.opportunity_count} | contacts={item.contact_count}"
        )


@app.command("dispatch-linkedin-company-queue")
def dispatch_linkedin_company_queue(
    limit: Annotated[int, typer.Option(help="Maximum queue items to dispatch")] = 3,
    include_target_list: Annotated[
        list[str] | None,
        typer.Option("--include-target-list", help="Only include workbook organizations with one of these target list tags"),
    ] = None,
    require_no_contacts: Annotated[
        bool,
        typer.Option(help="Only dispatch companies without LinkedIn-sourced contacts yet"),
    ] = True,
    require_hiring_signal: Annotated[
        bool,
        typer.Option(help="Only dispatch companies with opportunity or hiring signal"),
    ] = True,
    execute: Annotated[
        bool,
        typer.Option(help="Actually run the LinkedIn company pipeline instead of only planning dispatch"),
    ] = False,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    queue_items = build_linkedin_company_queue_items(
        organizations=workbook.list_organizations(),
        opportunities=workbook.list_opportunities(),
        contacts=workbook.list_contacts(),
        touchpoints=workbook.list_touchpoints(),
        include_target_lists=tuple(include_target_list or []),
        require_no_contacts=require_no_contacts,
        require_hiring_signal=require_hiring_signal,
    )[:limit]

    planned_runs: list[dict[str, object]] = []
    for item in queue_items:
        run_entry: dict[str, object] = {
            "organization_id": item.organization_id,
            "company": item.company,
            "company_mode": item.company_mode,
            "priority_score": item.priority_score,
            "triggers": item.triggers,
            "target_lists": item.target_lists,
            "planned": not execute,
        }
        if execute:
            artifact = execute_linkedin_company_run(
                settings=settings,
                company=item.company,
                dry_run=True,
                company_mode=item.company_mode,
            )
            run_entry["artifact"] = str(artifact)
        planned_runs.append(run_entry)

    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-queue-dispatch",
        {
            "count": len(planned_runs),
            "execute": execute,
            "filters": {
                "limit": limit,
                "include_target_lists": include_target_list or [],
                "require_no_contacts": require_no_contacts,
                "require_hiring_signal": require_hiring_signal,
            },
            "results": planned_runs,
        },
    )

    typer.echo(f"{'Dispatched' if execute else 'Planned'} {len(planned_runs)} LinkedIn company queue runs.")
    typer.echo(f"Artifact: {artifact}")
    for item in planned_runs:
        typer.echo(f"- {item['company']} | score={item['priority_score']} | mode={item['company_mode']}")


@app.command("discover-source")
def discover_source(
    source_id: Annotated[str, typer.Option(help="Registry source id, such as yc_los_angeles")],
    limit: Annotated[int, typer.Option(help="Maximum organizations to keep")] = 25,
    enrich_details: Annotated[
        bool,
        typer.Option(help="Fetch each YC company detail page for website, founders, and jobs"),
    ] = False,
    require_jobs_url: Annotated[
        bool,
        typer.Option(help="Only keep organizations with a visible jobs link or hiring page"),
    ] = False,
    remote_only: Annotated[
        bool,
        typer.Option(help="Only keep organizations with remote signals in company or job locations"),
    ] = False,
    include_tag: Annotated[
        list[str] | None,
        typer.Option("--include-tag", help="Keep organizations matching one or more category tags"),
    ] = None,
    max_team_size: Annotated[
        int | None,
        typer.Option(help="Optional maximum headcount inferred from the source page"),
    ] = None,
    min_batch_year: Annotated[
        int | None,
        typer.Option(help="Optional minimum YC batch year, for example 2024"),
    ] = None,
    write_workbook: Annotated[
        bool,
        typer.Option(help="Write discovered organizations and opportunities into the workbook"),
    ] = True,
) -> None:
    settings = OutreachSettings()
    entry = get_source_definition(source_id)
    adapter = build_source_adapter(source_id)
    downloader = HttpTextDownloader()
    raw_limit = max(limit * 10, 100)
    raw_items = adapter.discover(
        entry.definition,
        downloader.fetch_text,
        limit=raw_limit,
        enrich_details=enrich_details,
    )
    items = filter_discovered_items(
        [item.model_dump(mode="json") for item in raw_items],
        require_jobs_url=require_jobs_url,
        max_team_size=max_team_size,
        min_batch_year=min_batch_year,
        remote_only=remote_only,
        include_tags=tuple(include_tag or []),
    )[:limit]

    artifact = write_artifact(
        settings.artifacts_dir,
        f"discover-{source_id}",
        {
            "source": entry.definition.model_dump(mode="json"),
            "summary": entry.summary,
            "rationale": entry.rationale,
            "filters": {
                "enrich_details": enrich_details,
                "require_jobs_url": require_jobs_url,
                "remote_only": remote_only,
                "include_tags": include_tag or [],
                "max_team_size": max_team_size,
                "min_batch_year": min_batch_year,
            },
            "raw_count": len(raw_items),
            "count": len(items),
            "results": items,
        },
    )

    typer.echo(f"Discovered {len(items)} organizations from {source_id}")
    typer.echo(f"Artifact: {artifact}")

    if not write_workbook:
        return

    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    summary = workbook.import_discovery_batch(
        source_id=entry.definition.source_id,
        source_label=entry.definition.label,
        source_kind=entry.definition.source_kind,
        base_url=entry.definition.seed_urls[0],
        extraction_method=entry.definition.adapter.value,
        target_lists=entry.definition.target_lists,
        organization_type=entry.definition.organization_type,
        opportunity_type=entry.definition.opportunity_type,
        items=items,
    )
    typer.echo(f"- organizations_added: {summary.organizations_added}")
    typer.echo(f"- opportunities_added: {summary.opportunities_added}")
    typer.echo(f"- contacts_added: {summary.contacts_added}")
    typer.echo(f"- workbook: {settings.resolved_tracking_workspace_dir}")


@app.command("build-organization-intel")
def build_organization_intel(
    limit: Annotated[int, typer.Option(help="Maximum organizations to include")] = 20,
    include_target_list: Annotated[
        list[str] | None,
        typer.Option("--include-target-list", help="Only include organizations from these target lists"),
    ] = None,
    require_hiring_signal: Annotated[
        bool,
        typer.Option(help="Only include organizations with at least one opportunity"),
    ] = False,
    latest_first: Annotated[
        bool,
        typer.Option(help="Sort newest organizations first instead of best-fit first"),
    ] = False,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    items = build_organization_intel_items(
        organizations=workbook.list_organizations(),
        opportunities=workbook.list_opportunities(),
        contacts=workbook.list_contacts(),
        touchpoints=workbook.list_touchpoints(),
        include_target_lists=tuple(include_target_list or []),
        require_hiring_signal=require_hiring_signal,
        latest_first=latest_first,
    )[:limit]

    artifact = write_artifact(
        settings.artifacts_dir,
        "organization-intel",
        {
            "count": len(items),
            "filters": {
                "limit": limit,
                "include_target_lists": include_target_list or [],
                "require_hiring_signal": require_hiring_signal,
                "latest_first": latest_first,
            },
            "results": items,
        },
    )

    typer.echo(f"Built intel for {len(items)} organizations.")
    typer.echo(f"Artifact: {artifact}")
    for item in items:
        typer.echo(
            f"- {item['company']} | fit={item['fit_band']} ({item['fit_score']}) | "
            f"jobs={item['opportunity_count']} | scale={item['scale_signal'] or 'n/a'}"
        )


@app.command("build-relationship-loop")
def build_relationship_loop(
    limit: Annotated[int, typer.Option(help="Maximum company accounts to include")] = 30,
    include_target_list: Annotated[
        list[str] | None,
        typer.Option("--include-target-list", help="Only include organizations from these target lists"),
    ] = None,
    min_fit_score: Annotated[
        int,
        typer.Option(help="Minimum fit score for a company to be treated as a core relationship target"),
    ] = 55,
    target_relationships: Annotated[
        int,
        typer.Option(help="Desired number of real conversations/champions per core company"),
    ] = 3,
    outreach_wave_size: Annotated[
        int,
        typer.Option(help="LinkedIn invite count after which the planner suggests adding another channel"),
    ] = 10,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    items = build_relationship_loop_items(
        organizations=workbook.list_organizations(),
        opportunities=workbook.list_opportunities(),
        contacts=workbook.list_contacts(),
        touchpoints=workbook.list_touchpoints(),
        include_target_lists=tuple(include_target_list or []),
        min_fit_score=min_fit_score,
        target_relationships=target_relationships,
        outreach_wave_size=outreach_wave_size,
    )[:limit]

    artifact = write_artifact(
        settings.artifacts_dir,
        "relationship-loop",
        {
            "count": len(items),
            "filters": {
                "limit": limit,
                "include_target_lists": include_target_list or [],
                "min_fit_score": min_fit_score,
                "target_relationships": target_relationships,
                "outreach_wave_size": outreach_wave_size,
            },
            "results": items,
        },
    )

    typer.echo(f"Built relationship loop for {len(items)} company accounts.")
    typer.echo(f"Artifact: {artifact}")
    for item in items[: min(12, len(items))]:
        typer.echo(
            f"- {item['company']} | stage={item['relationship_stage']} | next={item['next_action']} | "
            f"fit={item['fit_band']} ({item['fit_score']}) | contacts={item['contact_count']} | "
            f"sent={item['sent_invite_count']} | connected={item['connected_contact_count']}"
        )


@app.command("build-target-action-queue")
def build_target_action_queue(
    limit: Annotated[int, typer.Option(help="Maximum organizations to include")] = 25,
    include_target_list: Annotated[
        list[str] | None,
        typer.Option("--include-target-list", help="Only include organizations from these target lists"),
    ] = None,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    items = build_target_action_queue_items(
        organizations=workbook.list_organizations(),
        opportunities=workbook.list_opportunities(),
        contacts=workbook.list_contacts(),
        touchpoints=workbook.list_touchpoints(),
        include_target_lists=tuple(include_target_list or []),
    )[:limit]

    artifact = write_artifact(
        settings.artifacts_dir,
        "target-action-queue",
        {
            "count": len(items),
            "filters": {
                "limit": limit,
                "include_target_lists": include_target_list or [],
            },
            "results": items,
        },
    )

    typer.echo(f"Built target action queue for {len(items)} organizations.")
    typer.echo(f"Artifact: {artifact}")
    for item in items:
        typer.echo(
            f"- {item['company']} | action={item['action']} | relevant_roles={item['relevant_role_count']} | "
            f"borderline_roles={item['borderline_role_count']} | fit={item['fit_band']} ({item['fit_score']})"
        )


@app.command("import-resume-jobs")
def import_resume_jobs(
    jobs_xlsx: Annotated[
        Path,
        typer.Option(help="Path to ResumeGenerator v1 discovery/jobs.xlsx"),
    ] = Path("../ResumeGenerator v1/discovery/jobs.xlsx"),
    sheet_name: Annotated[str, typer.Option(help="Worksheet name inside the xlsx")] = "Jobs",
    include_status: Annotated[
        list[str] | None,
        typer.Option("--include-status", help="ResumeGenerator statuses to import"),
    ] = None,
    min_score: Annotated[float, typer.Option(help="Minimum fit score to import")] = 7.0,
    max_age_days: Annotated[
        int,
        typer.Option(help="Only import jobs found within this many days"),
    ] = 10,
    resume_blocklist: Annotated[
        Path | None,
        typer.Option(help="Optional ResumeGenerator blocklist.txt to exclude blocked companies"),
    ] = Path("../ResumeGenerator v1/discovery/blocklist.txt"),
    limit: Annotated[int | None, typer.Option(help="Optional max jobs to import")] = None,
    dry_run: Annotated[bool, typer.Option(help="Preview matches without writing workbook")] = False,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    company_overrides_path = ensure_company_overrides_csv(
        settings.resolved_tracking_workspace_dir / DEFAULT_COMPANY_OVERRIDES_FILENAME
    )
    company_overrides = load_company_overrides(company_overrides_path)
    blocklist_patterns = load_company_blocklist(resume_blocklist)
    rows = load_resume_jobs(jobs_xlsx, sheet_name=sheet_name)
    selection = select_resume_jobs(
        rows,
        include_statuses=tuple(include_status or DEFAULT_INCLUDE_STATUSES),
        min_score=min_score,
        max_age_days=max_age_days,
        blocklist_patterns=blocklist_patterns,
    )
    selected_jobs = selection.jobs[:limit] if limit else selection.jobs

    typer.echo(f"Scanned {len(rows)} resume-tracker rows from {jobs_xlsx}")
    typer.echo(
        "Eligible rows: "
        f"{len(selected_jobs)}"
        f" | skipped_status={selection.skipped_status}"
        f" | skipped_score={selection.skipped_score}"
        f" | skipped_age={selection.skipped_age}"
        f" | skipped_blocklist={selection.skipped_blocklist}"
        f" | duplicates_removed={selection.duplicates_removed}"
    )
    for job in selected_jobs[:10]:
        score_text = f"{job.fit_score:.1f}" if job.fit_score is not None else "n/a"
        found_text = job.date_found.isoformat() if job.date_found else "n/a"
        override = company_overrides.get(normalize_dedupe_text(job.company))
        company_type = infer_company_type_for_job(job, company_override=override)
        typer.echo(
            f"- id={job.row_id} | {job.company} | {job.role_title} | "
            f"score={score_text} | status={job.normalized_status} | found={found_text} | company_type={company_type}"
        )
    if dry_run:
        typer.echo("Dry run only. No workbook changes written.")
        return

    workbook.initialize()
    source_id = workbook.make_source_id("resume-generator-jobs-xlsx", str(jobs_xlsx))
    workbook.upsert_source(
        DiscoverySourceRecord(
            source_id=source_id,
            label="ResumeGenerator v1 jobs.xlsx import",
            source_kind=SourceKind.OTHER,
            base_url=str(jobs_xlsx),
            extraction_method="xlsx_import",
            owner="outreach-engine",
            last_run_at=utc_now_iso(),
            notes=f"sheet={sheet_name} | min_score={min_score} | max_age_days={max_age_days}",
        )
    )

    organizations_added = 0
    opportunities_added = 0
    for job in selected_jobs:
        override = company_overrides.get(normalize_dedupe_text(job.company))
        target_lists = target_lists_from_resume_status(job.status)
        organization, created = workbook.upsert_organization(
            OrganizationRecord(
                organization_id=workbook.make_organization_id(job.company),
                name=job.company,
                organization_type=organization_type_for_resume_job(job, company_override=override),
                target_lists=target_lists,
                status=organization_status_from_resume_status(job.status),
                city=job.location,
                source_kind=map_resume_source_kind(job.source),
                source_url=job.url,
                notes=build_resume_organization_notes(job),
            )
        )
        if created:
            organizations_added += 1

        _, created = workbook.upsert_opportunity(
            OpportunityRecord(
                opportunity_id=workbook.make_opportunity_id(
                    organization.organization_id,
                    job.role_title,
                    source_url=job.url,
                ),
                organization_id=organization.organization_id,
                title=job.role_title,
                opportunity_type=infer_opportunity_type(job.role_title),
                target_lists=target_lists,
                location=job.location,
                status=opportunity_status_from_resume_status(job.status),
                source_kind=map_resume_source_kind(job.source),
                source_url=job.url,
                notes=build_resume_opportunity_notes(job),
            )
        )
        if created:
            opportunities_added += 1

    typer.echo(
        f"Imported {len(selected_jobs)} eligible resume jobs into {settings.resolved_tracking_workspace_dir}"
    )
    typer.echo(f"- organizations_added: {organizations_added}")
    typer.echo(f"- opportunities_added: {opportunities_added}")
    typer.echo(f"- source_id: {source_id}")
    typer.echo(f"- company_overrides: {company_overrides_path}")


@app.command("build-resume-outreach-queue")
def build_resume_outreach_queue_command(
    jobs_xlsx: Annotated[
        Path,
        typer.Option(help="Path to ResumeGenerator v1 discovery/jobs.xlsx"),
    ] = Path("../ResumeGenerator v1/discovery/jobs.xlsx"),
    sheet_name: Annotated[str, typer.Option(help="Worksheet name inside the xlsx")] = "Jobs",
    min_score: Annotated[float, typer.Option(help="Minimum fit score to include")] = 7.0,
    max_age_days: Annotated[int, typer.Option(help="Maximum age in days")] = 10,
    resume_blocklist: Annotated[
        Path | None,
        typer.Option(help="Optional ResumeGenerator blocklist.txt to exclude blocked companies"),
    ] = Path("../ResumeGenerator v1/discovery/blocklist.txt"),
    max_per_company: Annotated[int, typer.Option(help="Cap entries per company")] = 2,
    limit: Annotated[int, typer.Option(help="Maximum queue entries to return")] = 15,
) -> None:
    settings = OutreachSettings()
    company_overrides_path = ensure_company_overrides_csv(
        settings.resolved_tracking_workspace_dir / DEFAULT_COMPANY_OVERRIDES_FILENAME
    )
    company_overrides = load_company_overrides(company_overrides_path)
    blocklist_patterns = load_company_blocklist(resume_blocklist)
    rows = load_resume_jobs(jobs_xlsx, sheet_name=sheet_name)
    selection = select_resume_jobs(
        rows,
        include_statuses=DEFAULT_INCLUDE_STATUSES,
        min_score=min_score,
        max_age_days=max_age_days,
        blocklist_patterns=blocklist_patterns,
    )
    queue_items = build_resume_outreach_queue(
        selection.jobs,
        company_overrides=company_overrides,
        max_per_company=max_per_company,
    )[:limit]

    artifact = write_artifact(
        settings.artifacts_dir,
        "resume-outreach-queue",
        {
            "count": len(queue_items),
            "filters": {
                "sheet_name": sheet_name,
                "min_score": min_score,
                "max_age_days": max_age_days,
                "max_per_company": max_per_company,
                "limit": limit,
            },
            "company_overrides_path": str(company_overrides_path),
            "results": [
                {
                    "row_id": item.row_id,
                    "company": item.company,
                    "role_title": item.role_title,
                    "status": item.status,
                    "date_found": item.date_found.isoformat() if item.date_found else "",
                    "fit_score": item.fit_score,
                    "outreach_priority_score": item.outreach_priority_score,
                    "company_type": item.company_type,
                    "startup_bias": item.startup_bias,
                    "priority_reasons": item.priority_reasons,
                    "source": item.source,
                    "source_url": item.source_url,
                    "url_hash": item.url_hash,
                }
                for item in queue_items
            ],
        },
    )

    typer.echo(f"Built resume outreach queue with {len(queue_items)} jobs.")
    typer.echo(f"Artifact: {artifact}")
    typer.echo(f"Overrides: {company_overrides_path}")
    for item in queue_items:
        score_text = f"{item.outreach_priority_score:.1f}"
        fit_text = f"{item.fit_score:.1f}" if item.fit_score is not None else "n/a"
        found_text = item.date_found.isoformat() if item.date_found else "n/a"
        typer.echo(
            f"- {item.company} | {item.role_title} | outreach_score={score_text} | "
            f"fit={fit_text} | type={item.company_type} | bias={item.startup_bias} | found={found_text}"
        )


@app.command("init-workbook")
def init_workbook() -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    paths = workbook.initialize()
    typer.echo(f"Initialized outreach workbook in {settings.resolved_tracking_workspace_dir}")
    for table_name, path in paths.items():
        typer.echo(f"- {table_name}: {path}")


@app.command("workbook-summary")
def workbook_summary() -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    counts = workbook.summary_counts()
    typer.echo(f"Workbook: {settings.resolved_tracking_workspace_dir}")
    for table_name, count in counts.items():
        typer.echo(f"- {table_name}: {count} rows")


@app.command("add-organization")
def add_organization(
    name: Annotated[str, typer.Option(help="Organization name")],
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="Organization bucket for the master list"),
    ] = OrganizationType.COMPANY,
    target_lists: Annotated[
        str,
        typer.Option(help="Semicolon-separated tracks such as jobs;yc;hacker_house"),
    ] = "",
    status: Annotated[str, typer.Option(help="Pipeline status")] = "New",
    city: Annotated[str, typer.Option(help="City or region")] = "",
    website: Annotated[str, typer.Option(help="Website URL")] = "",
    linkedin_url: Annotated[str, typer.Option(help="Company LinkedIn URL")] = "",
    source_kind: Annotated[SourceKind, typer.Option(help="Where the lead came from")] = SourceKind.MANUAL,
    source_url: Annotated[str, typer.Option(help="Source page URL")] = "",
    notes: Annotated[str, typer.Option(help="Free-form notes")] = "",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    organization, created = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(name),
            name=name,
            organization_type=organization_type,
            target_lists=target_lists,
            status=status,
            city=city,
            website=website,
            linkedin_url=linkedin_url,
            source_kind=source_kind,
            source_url=source_url,
            notes=notes,
        )
    )
    typer.echo(f"{'Created' if created else 'Already had'} organization {organization.name}")
    typer.echo(f"- organization_id: {organization.organization_id}")
    typer.echo(f"- workbook: {settings.resolved_tracking_workspace_dir}")


@app.command("add-opportunity")
def add_opportunity(
    organization: Annotated[str, typer.Option(help="Organization name")],
    title: Annotated[str, typer.Option(help="Opportunity title")],
    opportunity_type: Annotated[
        OpportunityType,
        typer.Option(help="Type such as internship, research, or residency"),
    ] = OpportunityType.OTHER,
    target_lists: Annotated[str, typer.Option(help="Semicolon-separated track tags")] = "",
    location: Annotated[str, typer.Option(help="Location text")] = "",
    status: Annotated[str, typer.Option(help="Opportunity status")] = "Discovered",
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="Organization type if the organization needs to be created"),
    ] = OrganizationType.COMPANY,
    source_kind: Annotated[SourceKind, typer.Option(help="Where the lead came from")] = SourceKind.MANUAL,
    source_url: Annotated[str, typer.Option(help="Source page URL")] = "",
    compensation_hint: Annotated[str, typer.Option(help="Stipend or pay notes")] = "",
    notes: Annotated[str, typer.Option(help="Free-form notes")] = "",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    organization_record, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(organization),
            name=organization,
            organization_type=organization_type,
            target_lists=target_lists,
            source_kind=source_kind,
            source_url=source_url,
        )
    )
    opportunity, created = workbook.upsert_opportunity(
        OpportunityRecord(
            opportunity_id=workbook.make_opportunity_id(
                organization_record.organization_id,
                title,
                source_url=source_url,
            ),
            organization_id=organization_record.organization_id,
            title=title,
            opportunity_type=opportunity_type,
            target_lists=target_lists,
            location=location,
            status=status,
            source_kind=source_kind,
            source_url=source_url,
            compensation_hint=compensation_hint,
            notes=notes,
        )
    )
    typer.echo(f"{'Created' if created else 'Already had'} opportunity {opportunity.title}")
    typer.echo(f"- opportunity_id: {opportunity.opportunity_id}")
    typer.echo(f"- organization_id: {opportunity.organization_id}")


@app.command("add-contact")
def add_contact(
    organization: Annotated[str, typer.Option(help="Organization name")],
    full_name: Annotated[str, typer.Option(help="Contact full name")],
    title: Annotated[str, typer.Option(help="Role or title")] = "",
    contact_type: Annotated[str, typer.Option(help="Founder, PM, professor, recruiter, etc.")] = "",
    target_lists: Annotated[str, typer.Option(help="Semicolon-separated track tags")] = "",
    preferred_channel: Annotated[
        OutreachChannel,
        typer.Option(help="Preferred outreach channel"),
    ] = OutreachChannel.LINKEDIN,
    status: Annotated[str, typer.Option(help="Contact status")] = "Discovered",
    linkedin_url: Annotated[str, typer.Option(help="LinkedIn profile URL")] = "",
    email: Annotated[str, typer.Option(help="Email address")] = "",
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="Organization type if the organization needs to be created"),
    ] = OrganizationType.COMPANY,
    source_kind: Annotated[SourceKind, typer.Option(help="Where the lead came from")] = SourceKind.MANUAL,
    source_url: Annotated[str, typer.Option(help="Source page URL")] = "",
    notes: Annotated[str, typer.Option(help="Free-form notes")] = "",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    organization_record, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(organization),
            name=organization,
            organization_type=organization_type,
            target_lists=target_lists,
            source_kind=source_kind,
            source_url=source_url,
        )
    )
    contact, created = workbook.upsert_contact(
        ContactRecord(
            contact_id=workbook.make_contact_id(
                organization_record.organization_id,
                full_name,
                linkedin_url=linkedin_url,
                email=email,
            ),
            organization_id=organization_record.organization_id,
            full_name=full_name,
            title=title,
            contact_type=contact_type,
            target_lists=target_lists,
            preferred_channel=preferred_channel,
            status=status,
            linkedin_url=linkedin_url,
            email=email,
            source_kind=source_kind,
            source_url=source_url,
            notes=notes,
        )
    )
    typer.echo(f"{'Created' if created else 'Already had'} contact {contact.full_name}")
    typer.echo(f"- contact_id: {contact.contact_id}")
    typer.echo(f"- organization_id: {contact.organization_id}")


@app.command("log-touchpoint")
def log_touchpoint(
    organization: Annotated[str, typer.Option(help="Organization name")],
    message_text: Annotated[str, typer.Option(help="Exact outbound or draft message text")],
    full_name: Annotated[str, typer.Option(help="Optional contact name")] = "",
    title: Annotated[str, typer.Option(help="Optional contact title")] = "",
    linkedin_url: Annotated[str, typer.Option(help="Optional LinkedIn URL")] = "",
    email: Annotated[str, typer.Option(help="Optional email address")] = "",
    channel: Annotated[OutreachChannel, typer.Option(help="Outreach channel")] = OutreachChannel.LINKEDIN,
    status: Annotated[str, typer.Option(help="Draft, Sent, Replied, etc.")] = "Draft",
    message_kind: Annotated[str, typer.Option(help="Short label for this message")] = "outreach",
    target_lists: Annotated[str, typer.Option(help="Semicolon-separated track tags")] = "",
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="Organization type if the organization needs to be created"),
    ] = OrganizationType.COMPANY,
    source_artifact: Annotated[str, typer.Option(help="Optional artifact path or external reference")] = "",
    notes: Annotated[str, typer.Option(help="Free-form notes")] = "",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    organization_record, _ = workbook.upsert_organization(
        OrganizationRecord(
            organization_id=workbook.make_organization_id(organization),
            name=organization,
            organization_type=organization_type,
            target_lists=target_lists,
        )
    )

    contact_id = ""
    if full_name.strip():
        contact, _ = workbook.upsert_contact(
            ContactRecord(
                contact_id=workbook.make_contact_id(
                    organization_record.organization_id,
                    full_name,
                    linkedin_url=linkedin_url,
                    email=email,
                ),
                organization_id=organization_record.organization_id,
                full_name=full_name,
                title=title,
                target_lists=target_lists,
                preferred_channel=channel,
                linkedin_url=linkedin_url,
                email=email,
            )
        )
        contact_id = contact.contact_id

    touchpoint, created = workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id=workbook.make_touchpoint_id(
                organization_record.organization_id,
                contact_id,
                channel.value,
                message_text,
                source_artifact=source_artifact,
            ),
            organization_id=organization_record.organization_id,
            contact_id=contact_id,
            channel=channel,
            status=status,
            message_kind=message_kind,
            message_text=message_text,
            sent_at=utc_now_iso() if status.lower() == "sent" else "",
            source_artifact=source_artifact,
            notes=notes,
        )
    )
    typer.echo(f"{'Logged' if created else 'Already had'} touchpoint {touchpoint.touchpoint_id}")
    typer.echo(f"- organization_id: {touchpoint.organization_id}")
    if touchpoint.contact_id:
        typer.echo(f"- contact_id: {touchpoint.contact_id}")


@app.command("import-linkedin-artifact")
def import_linkedin_artifact(
    artifact_path: Annotated[Path, typer.Option(help="Path to a dry-run-pipeline or notes artifact")],
    target_lists: Annotated[
        str,
        typer.Option(help="Semicolon-separated track tags for imported records"),
    ] = "referrals;linkedin",
    organization_type: Annotated[
        OrganizationType,
        typer.Option(help="How to classify the imported organization"),
    ] = OrganizationType.COMPANY,
    touchpoint_status: Annotated[
        str,
        typer.Option(help="How to log generated notes, typically Draft or Prepared"),
    ] = "Draft",
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    summary = workbook.import_linkedin_artifact(
        artifact_path=artifact_path,
        target_lists=target_lists,
        organization_type=organization_type,
        touchpoint_status=touchpoint_status,
    )
    typer.echo(f"Imported LinkedIn artifact into {settings.resolved_tracking_workspace_dir}")
    typer.echo(f"- organization_id: {summary.organization_id}")
    typer.echo(f"- source_id: {summary.source_id}")
    typer.echo(f"- contacts_added: {summary.contacts_added}")
    typer.echo(f"- touchpoints_added: {summary.touchpoints_added}")


@app.command("send-invites")
def send_invites(
    artifact_path: Annotated[Path, typer.Option(help="Path to a notes-batch artifact")],
    limit: Annotated[int, typer.Option(help="Maximum number of candidates to process")] = 10,
    start_at: Annotated[int, typer.Option(help="Start offset into the eligible queue")] = 0,
    verdict: Annotated[str, typer.Option(help="Only include notes with this QC verdict")] = "send",
    min_score: Annotated[int, typer.Option(help="Minimum candidate relevance score required to send")] = 35,
    adaptive_min_score: Annotated[
        bool,
        typer.Option(help="Use startup preflight pool size from the artifact to lower/tighten the score gate"),
    ] = True,
    execute: Annotated[
        bool,
        typer.Option(help="Actually send invites instead of doing a dry run"),
    ] = False,
) -> None:
    try:
        settings = OutreachSettings()
    except ValidationError as exc:
        typer.echo("Configuration is incomplete.")
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            typer.echo(f"- {field}: {error['msg']}")
        raise typer.Exit(code=1)
    if execute:
        LinkedInScraper(settings).require_live_cdp_session()

    with artifact_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    company = payload["company"]
    all_candidates = payload["results"]
    pool_metadata = payload.get("startup_pool") or startup_pool_metadata(payload)
    effective_min_score = effective_send_min_score(
        payload,
        requested_min_score=min_score,
        adaptive=adaptive_min_score,
    )
    batch = select_invite_candidates(
        all_candidates,
        verdict=verdict,
        min_score=effective_min_score,
        limit=limit,
        start_at=start_at,
    )
    batch = attach_search_urls_to_candidates(payload, batch)
    if not batch:
        typer.echo("No eligible candidates matched the current filters.")
        raise typer.Exit(code=1)

    typer.echo(f"Processing {len(batch)} invite candidates for {company}")
    if adaptive_min_score and payload.get("company_mode") == "startup":
        typer.echo(
            "Adaptive score gate: "
            f"pool_mode={pool_metadata.get('pool_mode')} raw_count={pool_metadata.get('raw_count')} "
            f"requested>={min_score} effective>={effective_min_score}"
        )
    typer.echo(f"Candidate score gate: >= {effective_min_score}")
    typer.echo(f"Mode: {'execute' if execute else 'dry run'}")
    artifact, progress_artifact, status_counts, contacts_added, touchpoints_added = execute_invite_batch(
        settings=settings,
        company=company,
        source_artifact_path=artifact_path,
        batch=batch,
        execute=execute,
        limit=limit,
        start_at=start_at,
        verdict=verdict,
        min_score=effective_min_score,
    )
    typer.echo(f"Status summary: {status_counts}")
    typer.echo(f"Artifact: {artifact}")
    typer.echo(f"Progress artifact: {progress_artifact}")
    typer.echo(f"Tracked contacts_added: {contacts_added}")
    typer.echo(f"Tracked touchpoints_added: {touchpoints_added}")


if __name__ == "__main__":
    app()

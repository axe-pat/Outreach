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
from outreach.services.linkedin import FilterRunResult, LinkedInFollowupSendResult, LinkedInScraper
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


def latest_invite_touchpoint_for_contact(touchpoints: list[TouchpointRecord]) -> TouchpointRecord | None:
    invite_touchpoints = [
        item
        for item in touchpoints
        if item.contact_id and (item.message_kind or "").strip().lower() == "linkedin_invite"
    ]
    invite_touchpoints.sort(
        key=lambda item: parse_iso_timestamp(item.sent_at or item.recorded_at) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return invite_touchpoints[0] if invite_touchpoints else None


def build_linkedin_reconcile_queue_items(
    *,
    organizations: list[OrganizationRecord],
    contacts: list[ContactRecord],
    touchpoints: list[TouchpointRecord],
    include_statuses: tuple[str, ...] = ("Invited",),
    max_age_days: int = 14,
    min_age_hours: int = 12,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    reference_now = now or datetime.now(UTC)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=UTC)
    else:
        reference_now = reference_now.astimezone(UTC)

    organization_map = {item.organization_id: item for item in organizations}
    touchpoint_map: dict[str, list[TouchpointRecord]] = {}
    for item in touchpoints:
        if item.contact_id:
            touchpoint_map.setdefault(item.contact_id, []).append(item)

    status_filter = {item.strip().lower() for item in include_statuses if item.strip()}
    items: list[dict[str, object]] = []
    for contact in contacts:
        if contact.preferred_channel != OutreachChannel.LINKEDIN:
            continue
        if not contact.linkedin_url:
            continue
        if status_filter and (contact.status or "").strip().lower() not in status_filter:
            continue

        latest_invite = latest_invite_touchpoint_for_contact(touchpoint_map.get(contact.contact_id, []))
        last_touch_at = parse_iso_timestamp(
            contact.last_contacted_at
            or (latest_invite.sent_at if latest_invite else "")
            or (latest_invite.recorded_at if latest_invite else "")
            or contact.discovered_at
        )
        age_hours = (
            max(0.0, (reference_now - last_touch_at).total_seconds() / 3600)
            if last_touch_at is not None
            else None
        )
        if age_hours is not None and age_hours < min_age_hours:
            continue
        if age_hours is not None and age_hours > max_age_days * 24:
            continue

        organization = organization_map.get(contact.organization_id)
        items.append(
            {
                "contact_id": contact.contact_id,
                "organization_id": contact.organization_id,
                "company": organization.name if organization else "",
                "name": contact.full_name,
                "title": contact.title,
                "contact_type": contact.contact_type,
                "status": contact.status,
                "linkedin_url": contact.linkedin_url,
                "last_contacted_at": contact.last_contacted_at,
                "last_touch_at": last_touch_at.isoformat() if last_touch_at else "",
                "age_hours": round(age_hours, 1) if age_hours is not None else None,
                "original_invite_note": latest_invite.message_text if latest_invite else "",
                "source_touchpoint_id": latest_invite.touchpoint_id if latest_invite else "",
            }
        )

    items.sort(
        key=lambda item: (
            float(item["age_hours"] or 0),
            str(item["company"]).lower(),
            str(item["name"]).lower(),
        ),
        reverse=True,
    )
    return items


def normalize_reconcile_status(status: str) -> str:
    normalized = (status or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "accepted": "connected",
        "already_connected": "connected",
        "connection_accepted": "connected",
        "reply": "replied",
        "reply_received": "replied",
        "message_received": "replied",
        "still_pending": "pending",
    }
    return aliases.get(normalized, normalized)


def apply_linkedin_reconcile_results(
    *,
    workbook: OutreachWorkbook,
    results: list[dict],
    source_artifact: str = "",
    apply_changes: bool = False,
) -> dict[str, object]:
    contacts = workbook.list_contacts()
    contact_by_id = {item.contact_id: item for item in contacts}
    contact_by_url = {
        normalize_dedupe_text(item.linkedin_url): item
        for item in contacts
        if item.linkedin_url
    }

    processed: list[dict[str, object]] = []
    summary = {
        "connected": 0,
        "replied": 0,
        "pending": 0,
        "not_connected": 0,
        "unknown": 0,
        "missing_contact": 0,
        "updated_contacts": 0,
        "touchpoints_added": 0,
    }

    for raw in results:
        status = normalize_reconcile_status(str(raw.get("status") or ""))
        contact = contact_by_id.get(str(raw.get("contact_id") or ""))
        if contact is None:
            contact = contact_by_url.get(normalize_dedupe_text(str(raw.get("linkedin_url") or "")))
        if contact is None:
            summary["missing_contact"] += 1
            processed.append(
                {
                    **raw,
                    "normalized_status": status,
                    "action": "missing_contact",
                    "needs_follow_up": False,
                    "applied": False,
                }
            )
            continue

        action = "no_change"
        new_contact_status = ""
        touchpoint_status = ""
        message_kind = "linkedin_reconcile"
        message_text = ""
        if status == "connected":
            summary["connected"] += 1
            action = "mark_connected"
            new_contact_status = "Connected"
            touchpoint_status = "Accepted"
            message_text = "LinkedIn invite accepted."
        elif status == "replied":
            summary["replied"] += 1
            action = "mark_replied"
            new_contact_status = "Replied"
            touchpoint_status = "Replied"
            message_kind = "linkedin_reply"
            message_text = str(raw.get("message_text") or raw.get("reply_text") or "LinkedIn reply detected.").strip()
        elif status == "pending":
            summary["pending"] += 1
        elif status == "not_connected":
            summary["not_connected"] += 1
        else:
            summary["unknown"] += 1

        applied = False
        if apply_changes and new_contact_status:
            updated = workbook.update_contact(
                contact.contact_id,
                status=new_contact_status,
                last_contacted_at=utc_now_iso(),
            )
            if updated is not None:
                summary["updated_contacts"] += 1
                contact = updated
            if message_text:
                _, created = workbook.append_touchpoint(
                    TouchpointRecord(
                        touchpoint_id=workbook.make_touchpoint_id(
                            contact.organization_id,
                            contact.contact_id,
                            OutreachChannel.LINKEDIN.value,
                            message_text,
                            source_artifact=source_artifact,
                        ),
                        organization_id=contact.organization_id,
                        contact_id=contact.contact_id,
                        channel=OutreachChannel.LINKEDIN,
                        status=touchpoint_status,
                        message_kind=message_kind,
                        message_text=message_text,
                        recorded_at=utc_now_iso(),
                        source_artifact=source_artifact,
                        notes=f"reconcile_status={status} | detail={raw.get('detail', '')}",
                    )
                )
                if created:
                    summary["touchpoints_added"] += 1
            applied = True

        processed.append(
            {
                **raw,
                "contact_id": contact.contact_id,
                "organization_id": contact.organization_id,
                "normalized_status": status,
                "action": action,
                "new_contact_status": new_contact_status,
                "needs_follow_up": status == "connected" and connected_result_needs_follow_up(raw),
                "applied": applied,
            }
        )

    return {
        "apply": apply_changes,
        "summary": summary,
        "results": processed,
    }


def linkedin_message_state_path(settings: OutreachSettings) -> Path:
    return settings.resolved_tracking_workspace_dir / "linkedin_message_state.json"


def load_linkedin_message_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "seen_thread_ids": [],
            "thread_states": {},
            "last_snapshot_at": "",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "seen_thread_ids": [],
            "thread_states": {},
            "last_snapshot_at": "",
        }
    seen = payload.get("seen_thread_ids") or []
    if not isinstance(seen, list):
        seen = []
    thread_states = payload.get("thread_states") or {}
    if not isinstance(thread_states, dict):
        thread_states = {}
    return {
        **payload,
        "seen_thread_ids": [str(item) for item in seen if str(item).strip()],
        "thread_states": {
            str(key): value
            for key, value in thread_states.items()
            if str(key).strip() and isinstance(value, dict)
        },
    }


def save_linkedin_message_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def message_thread_key(thread: dict) -> str:
    return str(thread.get("thread_id") or thread.get("thread_url") or "").strip()


def message_thread_signature(thread: dict) -> str:
    latest_message = normalize_dedupe_text(str(thread.get("latest_message") or thread.get("message_text") or ""))
    last_sender = normalize_dedupe_text(str(thread.get("last_sender") or ""))
    return f"{last_sender}|{latest_message}"


def normalize_person_name(value: str) -> str:
    return normalize_dedupe_text(re.sub(r"\s+", " ", value or ""))


def first_name_key(value: str) -> str:
    return normalize_person_name(value).split(" ", maxsplit=1)[0]


def match_contact_for_message_thread(thread: dict, contacts: list[ContactRecord]) -> ContactRecord | None:
    thread_name = normalize_person_name(str(thread.get("name") or ""))
    if not thread_name:
        return None
    for contact in contacts:
        if normalize_person_name(contact.full_name) == thread_name:
            return contact
    for contact in contacts:
        contact_name = normalize_person_name(contact.full_name)
        if contact_name and (thread_name in contact_name or contact_name in thread_name):
            return contact
    thread_first = first_name_key(thread_name)
    if thread_first:
        first_name_matches = [
            contact
            for contact in contacts
            if first_name_key(contact.full_name) == thread_first
        ]
        if len(first_name_matches) == 1:
            return first_name_matches[0]
    return None


def latest_invite_note_for_contact(contact_id: str, touchpoints: list[TouchpointRecord]) -> str:
    latest = latest_invite_touchpoint_for_contact(
        [item for item in touchpoints if item.contact_id == contact_id]
    )
    return latest.message_text if latest else ""


def message_thread_has_reply(thread: dict, original_invite_note: str = "") -> bool:
    latest_message = str(thread.get("latest_message") or "").strip()
    last_sender = str(thread.get("last_sender") or "").strip().lower()
    if not latest_message:
        return False
    if last_sender and last_sender not in {"you", "akshat"}:
        return True

    latest_lower = latest_message.lower()
    original_lower = original_invite_note.lower().strip()
    system_fragments = [
        "you are now connected",
        "is now a connection",
        "accepted your invitation",
        "accepted your invite",
        "sent an invitation",
    ]
    if any(fragment in latest_lower for fragment in system_fragments):
        return False
    if original_lower and latest_lower in original_lower:
        return False
    if latest_lower.startswith("you:"):
        return False
    return bool(thread.get("unread"))


def connected_result_needs_follow_up(result: dict) -> bool:
    latest_message = str(result.get("latest_message") or result.get("message_text") or "").strip()
    last_sender = str(result.get("last_sender") or "").strip().lower()
    original_invite_note = str(result.get("original_invite_note") or "").strip()
    if not latest_message:
        return True
    latest_lower = latest_message.lower()
    if latest_lower.startswith("you sent"):
        return False
    if last_sender in {"you", "akshat"}:
        if original_invite_note and normalize_dedupe_text(latest_message) != normalize_dedupe_text(original_invite_note):
            return False
    return True


def build_linkedin_message_reconcile_results(
    *,
    threads: list[dict],
    contacts: list[ContactRecord],
    touchpoints: list[TouchpointRecord],
    state: dict[str, object],
    include_seen: bool = False,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    seen_thread_ids = {str(item) for item in state.get("seen_thread_ids", []) if str(item).strip()}
    all_thread_ids = set(seen_thread_ids)
    existing_thread_states = {
        str(key): value
        for key, value in (state.get("thread_states") or {}).items()
        if str(key).strip() and isinstance(value, dict)
    }
    next_thread_states: dict[str, dict[str, object]] = {
        key: dict(value) for key, value in existing_thread_states.items()
    }
    results: list[dict[str, object]] = []
    snapshot_at = utc_now_iso()

    for thread in threads:
        key = message_thread_key(thread)
        if not key:
            continue
        all_thread_ids.add(key)
        is_new_thread = key not in seen_thread_ids
        current_signature = message_thread_signature(thread)
        previous_signature = str(existing_thread_states.get(key, {}).get("signature") or "")
        thread_changed = bool(previous_signature and current_signature != previous_signature)
        state_reason = (
            "new_thread"
            if is_new_thread
            else "changed_latest"
            if thread_changed
            else "include_seen"
            if include_seen
            else "baseline_seen"
        )
        next_thread_states[key] = {
            **next_thread_states.get(key, {}),
            "signature": current_signature,
            "name": str(thread.get("name") or ""),
            "latest_message": str(thread.get("latest_message") or ""),
            "last_sender": str(thread.get("last_sender") or ""),
            "timestamp_text": str(thread.get("timestamp_text") or ""),
            "thread_url": str(thread.get("thread_url") or ""),
            "last_seen_at": snapshot_at,
            "first_seen_at": str(next_thread_states.get(key, {}).get("first_seen_at") or snapshot_at),
        }
        if not include_seen and not is_new_thread and not thread_changed:
            continue

        contact = match_contact_for_message_thread(thread, contacts)
        if contact is None:
            results.append(
                {
                    "thread_id": key,
                    "name": thread.get("name", ""),
                    "status": "unknown",
                    "detail": "Message thread did not match a workbook contact.",
                    "thread_url": thread.get("thread_url", ""),
                    "latest_message": thread.get("latest_message", ""),
                    "is_new_thread": is_new_thread,
                    "thread_changed": thread_changed,
                    "thread_signature": current_signature,
                    "previous_thread_signature": previous_signature,
                    "state_reason": state_reason,
                }
            )
            continue

        original_invite_note = latest_invite_note_for_contact(contact.contact_id, touchpoints)
        has_reply = message_thread_has_reply(thread, original_invite_note)
        status = "replied" if has_reply else "connected"
        detail = (
            "Existing LinkedIn message thread has a new apparent inbound reply."
            if has_reply and thread_changed
            else "New LinkedIn message thread has an apparent inbound reply."
            if has_reply
            else "Existing LinkedIn message thread latest message changed."
            if thread_changed
            else "New LinkedIn message thread indicates the invite was accepted."
        )
        results.append(
            {
                "thread_id": key,
                "contact_id": contact.contact_id,
                "organization_id": contact.organization_id,
                "name": contact.full_name,
                "linkedin_url": contact.linkedin_url,
                "status": status,
                "detail": detail,
                "thread_url": thread.get("thread_url", ""),
                "latest_message": thread.get("latest_message", ""),
                "last_sender": thread.get("last_sender", ""),
                "timestamp_text": thread.get("timestamp_text", ""),
                "unread": bool(thread.get("unread")),
                "is_new_thread": is_new_thread,
                "thread_changed": thread_changed,
                "thread_signature": current_signature,
                "previous_thread_signature": previous_signature,
                "state_reason": state_reason,
                "original_invite_note": original_invite_note,
            }
        )

    next_state = {
        **state,
        "seen_thread_ids": sorted(all_thread_ids),
        "thread_states": next_thread_states,
        "last_snapshot_at": snapshot_at,
    }
    return results, next_state


def first_name(value: str) -> str:
    return (value or "there").strip().split()[0]


def infer_followup_audience(contact: ContactRecord, original_invite_note: str = "") -> str:
    invite_text = original_invite_note.lower()
    profile_text = " ".join([contact.contact_type, contact.title]).lower()
    if "referral" in invite_text:
        return "referral_engineer"
    if any(token in profile_text for token in ["founder", "co-founder", "ceo", "chief executive"]):
        return "founder"
    if any(token in profile_text for token in ["recruiter", "talent", "university recruiting", "campus"]):
        return "recruiter"
    if any(token in profile_text for token in ["product", "pm ", "product manager", "apm"]):
        return "product"
    if any(
        token in profile_text
        for token in [
            "engineer",
            "engineering",
            "developer",
            "architect",
            "software",
            "backend",
            "infrastructure",
            "deep learning",
            "genai",
            "accelerator",
            "r&d",
        ]
    ):
        return "engineering"
    return "general"


def founder_context_line(company: str, organization: OrganizationRecord | None) -> str:
    organization_text = " ".join(
        [
            organization.notes if organization else "",
            organization.target_lists if organization else "",
        ]
    ).lower()
    if "agent analytics" in organization_text or "ai agents" in organization_text:
        return f"{company}'s AI agent analytics work maps well to my data/platform + applied AI experience."
    return f"{company} feels like an early team where product, ops, and execution sit close."


def product_context_line(contact: ContactRecord) -> str:
    title = contact.title.lower()
    if "ai" in title or "data infrastructure" in title:
        return "Your AI/data infrastructure work feels close to problems I've worked around."
    if "security" in title or "developer" in title:
        return "Your developer-facing product work feels close to problems I've worked around."
    return "Your technically deep product work feels close to problems I've worked around."


def accepted_followup_draft(
    *,
    company: str,
    contact: ContactRecord,
    original_invite_note: str,
    organization: OrganizationRecord | None = None,
) -> tuple[str, str]:
    name = first_name(contact.full_name)
    audience = infer_followup_audience(contact, original_invite_note)
    if audience == "referral_engineer":
        return (
            "safe_to_review",
            (
                f"Thanks for connecting, {name}. I'm targeting PM/product roles at {company} where my backend/data "
                "engineering background is useful. If I send a tight resume + 3-line blurb, would you be open to "
                "a referral, or pointing me to the right hiring contact?"
            ),
        )
    if audience == "founder":
        context_line = founder_context_line(company, organization)
        return (
            "review",
            (
                f"Thanks for connecting, {name}. I'm exploring product/operator paths where my engineering + "
                f"Marshall background can be useful. {context_line} Would love your perspective on whether my "
                "background could translate to what you're building."
            ),
        )
    if audience == "product":
        context_line = product_context_line(contact)
        return (
            "review",
            (
                f"Thanks for connecting, {name}. I'm exploring PM/product roles at {company} from an engineering + "
                f"data/platform background. {context_line} Would love your perspective on whether my "
                "background could translate to the product work there."
            ),
        )
    if audience == "recruiter":
        return (
            "safe_to_review",
            (
                f"Thanks for connecting, {name}. I'm a Marshall MBA + former data/platform engineer exploring "
                f"PM/product internship paths at {company}. What's the best process for someone with my background "
                "to get on the team's radar?"
            ),
        )
    if audience == "engineering":
        return (
            "safe_to_review",
            (
                f"Thanks for connecting, {name}. I'm trying to get on the radar at {company} for PM/product roles "
                "where my data/platform engineering background helps. If I send a tight resume + 3-line blurb, "
                "would you be open to pointing me to the right referral path or hiring contact?"
            ),
        )
    return (
        "safe_to_review",
        (
            f"Thanks for connecting, {name}. I'm trying to move from data/platform engineering into PM work at "
            f"{company}. From your side of the org, who is usually the best person for a technical PM candidate "
            "to get on the radar of?"
        ),
    )


def reply_followup_draft(*, company: str, contact: ContactRecord, latest_message: str) -> tuple[str, str, str]:
    name = first_name(contact.full_name)
    lower = latest_message.lower()
    if any(
        token in lower
        for token in [
            "won't be able to help",
            "wont be able to help",
            "can't help",
            "cannot help",
            "not able to help",
            "unable to help",
        ]
    ):
        return (
            "polite_close_reply",
            "optional",
            f"No worries at all, thanks for letting me know {name}. Appreciate it.",
        )
    if "share your profile" in lower or "share your resume" in lower or "hr" in lower:
        return (
            "referral_offer_reply",
            "review",
            (
                f"That would be amazing, thanks {name}. Short blurb if useful: Marshall MBA + 5 yrs backend/data "
                "platform engineering, now targeting PM/product roles where technical depth helps. Happy to send "
                "resume too if HR wants it."
            ),
        )
    if any(token in lower for token in ["small team", "high-impact", "high ownership", "customer", "feedback"]):
        return (
            "conversation_reply",
            "review",
            (
                f"This is super helpful, thanks {name}. The small-team/high-ownership + customer-feedback loop "
                f"at {company} is exactly what I'm looking for. Do you think there's a PM/product internship path "
                "there, or someone on product/recruiting I should ask?"
            ),
        )
    return (
        "conversation_reply",
        "review",
        (
            f"Thanks {name}, this is helpful. I'm trying to understand where my engineering + Marshall background "
            f"could fit at {company}. Would you suggest I speak with someone on product/recruiting, or is there a "
            "better path to get on the team's radar?"
        ),
    )


def build_linkedin_followup_drafts(
    *,
    reconcile_results: list[dict],
    organizations: list[OrganizationRecord],
    contacts: list[ContactRecord],
) -> list[dict[str, object]]:
    organization_map = {item.organization_id: item for item in organizations}
    contact_map = {item.contact_id: item for item in contacts}
    drafts: list[dict[str, object]] = []

    for item in reconcile_results:
        contact_id = str(item.get("contact_id") or "")
        contact = contact_map.get(contact_id)
        if contact is None:
            continue
        organization = organization_map.get(contact.organization_id)
        company = organization.name if organization else ""
        status = str(item.get("normalized_status") or item.get("status") or "")
        if status == "connected" and item.get("needs_follow_up"):
            recommendation, draft = accepted_followup_draft(
                company=company,
                contact=contact,
                original_invite_note=str(item.get("original_invite_note") or ""),
                organization=organization,
            )
            draft_kind = "accepted_follow_up"
        elif status == "replied":
            draft_kind, recommendation, draft = reply_followup_draft(
                company=company,
                contact=contact,
                latest_message=str(item.get("latest_message") or item.get("message_text") or ""),
            )
        else:
            continue

        drafts.append(
            {
                "contact_id": contact.contact_id,
                "organization_id": contact.organization_id,
                "company": company,
                "name": contact.full_name,
                "title": contact.title,
                "contact_type": contact.contact_type,
                "followup_audience": infer_followup_audience(
                    contact,
                    str(item.get("original_invite_note") or ""),
                ),
                "linkedin_url": contact.linkedin_url,
                "draft_kind": draft_kind,
                "send_recommendation": recommendation,
                "draft_message": draft,
                "draft_length": len(draft),
                "source_status": status,
                "latest_message": item.get("latest_message", ""),
                "last_sender": item.get("last_sender", ""),
                "timestamp_text": item.get("timestamp_text", ""),
                "original_invite_note": item.get("original_invite_note", ""),
                "thread_id": item.get("thread_id", ""),
                "thread_url": item.get("thread_url", ""),
            }
        )

    return drafts


def summarize_linkedin_followup_actions(drafts: list[dict], reconcile_results: list[dict]) -> dict[str, object]:
    summary = {
        "follow_up_candidates": 0,
        "reply_candidates": 0,
        "optional_closes": 0,
        "missing_contacts": 0,
        "by_company": {},
    }
    for item in reconcile_results:
        if str(item.get("action") or "") == "missing_contact":
            summary["missing_contacts"] = int(summary["missing_contacts"]) + 1
    by_company: dict[str, int] = {}
    for draft in drafts:
        if str(draft.get("draft_kind") or "") == "accepted_follow_up":
            summary["follow_up_candidates"] = int(summary["follow_up_candidates"]) + 1
        else:
            summary["reply_candidates"] = int(summary["reply_candidates"]) + 1
        if str(draft.get("send_recommendation") or "") == "optional":
            summary["optional_closes"] = int(summary["optional_closes"]) + 1
        company = str(draft.get("company") or "(unknown)")
        by_company[company] = by_company.get(company, 0) + 1
    summary["by_company"] = dict(sorted(by_company.items(), key=lambda item: (-item[1], item[0].lower())))
    return summary


def persist_linkedin_followup_send_result(
    *,
    workbook: OutreachWorkbook,
    result: LinkedInFollowupSendResult,
    source_artifact: Path,
    send_artifact: Path,
) -> bool:
    if result.status != "sent":
        return False
    sent_at = utc_now_iso()
    _, created = workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id=workbook.make_touchpoint_id(
                result.organization_id,
                result.contact_id,
                OutreachChannel.LINKEDIN.value,
                result.draft_message,
                source_artifact=str(send_artifact),
            ),
            organization_id=result.organization_id,
            contact_id=result.contact_id,
            channel=OutreachChannel.LINKEDIN,
            status="Sent",
            message_kind="linkedin_followup",
            message_text=result.draft_message,
            recorded_at=sent_at,
            sent_at=sent_at,
            source_artifact=str(source_artifact),
            notes=f"draft_kind={result.draft_kind} | send_artifact={send_artifact}",
        )
    )
    workbook.update_contact(result.contact_id, status="Followed up", last_contacted_at=sent_at)
    return created


def execute_linkedin_followup_send(
    *,
    settings: OutreachSettings,
    draft_artifact: Path,
    drafts: list[dict],
    execute: bool,
    limit: int,
    start_at: int,
    include_optional: bool,
) -> tuple[Path, Path, dict[str, int], int]:
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    scraper = LinkedInScraper(settings)
    progress_artifact = settings.artifacts_dir / f"{draft_artifact.stem}-{artifact_timestamp()}-followup-send-progress.json"
    progress_artifact.parent.mkdir(parents=True, exist_ok=True)
    status_counts: dict[str, int] = {}
    touchpoints_added = 0

    def _write_progress(results: list[LinkedInFollowupSendResult]) -> None:
        progress_artifact.write_text(
            json.dumps(
                {
                    "source_artifact": str(draft_artifact),
                    "execute": execute,
                    "limit": limit,
                    "start_at": start_at,
                    "include_optional": include_optional,
                    "count": len(results),
                    "results": [item.__dict__ for item in results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _on_result(_draft: dict, result: LinkedInFollowupSendResult, results: list[LinkedInFollowupSendResult]) -> None:
        nonlocal touchpoints_added
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
        if execute and result.status == "sent":
            if persist_linkedin_followup_send_result(
                workbook=workbook,
                result=result,
                source_artifact=draft_artifact,
                send_artifact=progress_artifact,
            ):
                touchpoints_added += 1
        _write_progress(results)

    results = scraper.send_followup_messages(
        drafts,
        execute=execute,
        limit=limit,
        start_at=start_at,
        include_optional=include_optional,
        on_result=_on_result,
    )
    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-followup-send-results",
        {
            "source_artifact": str(draft_artifact),
            "progress_artifact": str(progress_artifact),
            "execute": execute,
            "limit": limit,
            "start_at": start_at,
            "include_optional": include_optional,
            "count": len(results),
            "status_counts": status_counts,
            "touchpoints_added": touchpoints_added,
            "results": [item.__dict__ for item in results],
        },
    )
    return artifact, progress_artifact, status_counts, touchpoints_added


app = typer.Typer(help="Outreach engine CLI")


@app.command("account-tracker")
def account_tracker_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    output: Annotated[
        Path,
        typer.Option(help="Output path for the Excel file"),
    ] = Path("workspace/account_tracker.xlsx"),
) -> None:
    """Build the priority company account tracker and output to Excel."""
    from outreach.account_tracker import run as run_tracker

    typer.echo(f"Loading workbook from {workspace} ...")
    rows, path = run_tracker(workbook_dir=workspace, output_path=output)

    tier_counts = {"A": 0, "B": 0, "C": 0}
    for r in rows:
        tier_counts[r.tier] = tier_counts.get(r.tier, 0) + 1

    typer.echo(f"Scored {len(rows)} companies")
    typer.echo(f"  Tier A: {tier_counts['A']}  |  Tier B: {tier_counts['B']}  |  Tier C: {tier_counts['C']}")
    typer.echo(f"Output: {path}")


@app.command("build-account-campaign-plan")
def build_account_campaign_plan(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    limit: Annotated[int, typer.Option(help="Maximum campaign actions to include")] = 30,
) -> None:
    """Build executable next actions for Tier A/B account campaigns."""
    from outreach.account_tracker import build_account_rows, build_campaign_plan_rows

    rows = build_account_rows(workspace)
    plan_rows = build_campaign_plan_rows(rows)[:limit]
    summary: dict[str, int] = {}
    lane_summary: dict[str, int] = {}
    for row in plan_rows:
        summary[row.campaign_action] = summary.get(row.campaign_action, 0) + 1
        lane_summary[row.lane_1_policy] = lane_summary.get(row.lane_1_policy, 0) + 1

    artifact = write_artifact(
        OutreachSettings().artifacts_dir,
        "account-campaign-plan",
        {
            "count": len(plan_rows),
            "limit": limit,
            "summary": summary,
            "lane_1_policy_summary": lane_summary,
            "results": [row.__dict__ for row in plan_rows],
        },
    )

    typer.echo(f"Built account campaign plan with {len(plan_rows)} actions.")
    typer.echo(f"Summary: {summary}")
    typer.echo(f"Lane 1 policy: {lane_summary}")
    typer.echo(f"Artifact: {artifact}")
    for row in plan_rows[: min(12, len(plan_rows))]:
        typer.echo(
            f"- {row.company} | tier={row.tier} | action={row.campaign_action} | "
            f"channel={row.campaign_channel} | lane1={row.lane_1_policy} | priority={row.campaign_priority}"
        )
        typer.echo(f"  {row.campaign_reason}")


@app.command("enrich-company-context")
def enrich_company_context_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    limit: Annotated[int, typer.Option(help="Maximum companies to enrich")] = 50,
    start_at: Annotated[int, typer.Option(help="Skip this many selected companies before enriching")] = 0,
    refresh_days: Annotated[int, typer.Option(help="Refresh context older than this many days")] = 14,
    company: Annotated[
        list[str] | None,
        typer.Option("--company", help="Only enrich a named company; repeat for multiple companies"),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(help="Write enrichment back to organizations.csv; default is preview only"),
    ] = False,
    network: Annotated[
        bool,
        typer.Option(help="Fetch public company/source pages before falling back to local job-rationale inference"),
    ] = True,
    web_search: Annotated[
        bool,
        typer.Option(help="Use public web search when direct company/source URLs are unavailable"),
    ] = True,
    verify_all: Annotated[
        bool,
        typer.Option(help="Include companies that have only inferred or unverified context"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(help="Refresh selected companies even when existing context is already external_verified"),
    ] = False,
    require_direct_url: Annotated[
        bool,
        typer.Option(help="Only enrich companies that already have a non-LinkedIn source URL or website"),
    ] = False,
    job_fallback: Annotated[
        bool,
        typer.Option(help="Fall back to local job-rationale inference when public context cannot be fetched"),
    ] = True,
    timeout_seconds: Annotated[int, typer.Option(help="Network fetch timeout per public page")] = 6,
) -> None:
    """Fill missing or stale company context used by Track 2 account scoring."""
    from outreach.company_enrichment import enrich_company_contexts

    results = enrich_company_contexts(
        workspace,
        limit=limit,
        start_at=start_at,
        refresh_days=refresh_days,
        companies=set(company or []),
        execute=execute,
        use_network=network,
        use_web_search=web_search,
        verify_all=verify_all,
        force=force,
        require_direct_url=require_direct_url,
        fallback_to_jobs=job_fallback,
        fetcher=HttpTextDownloader(timeout_seconds=timeout_seconds),
    )
    summary: dict[str, int] = {}
    confidence_summary: dict[str, int] = {}
    for row in results:
        summary[row.status] = summary.get(row.status, 0) + 1
        if row.confidence:
            confidence_summary[row.confidence] = confidence_summary.get(row.confidence, 0) + 1

    artifact = write_artifact(
        OutreachSettings().artifacts_dir,
        "company-context-enrichment",
        {
            "workspace": str(workspace),
            "limit": limit,
            "start_at": start_at,
            "refresh_days": refresh_days,
            "companies": company or [],
            "execute": execute,
            "network": network,
            "web_search": web_search,
            "verify_all": verify_all,
            "force": force,
            "require_direct_url": require_direct_url,
            "job_fallback": job_fallback,
            "timeout_seconds": timeout_seconds,
            "summary": summary,
            "confidence_summary": confidence_summary,
            "results": [row.__dict__ for row in results],
        },
    )

    typer.echo(f"{'Updated' if execute else 'Planned'} company context for {len(results)} companies.")
    typer.echo(f"Summary: {summary}")
    if confidence_summary:
        typer.echo(f"Confidence: {confidence_summary}")
    typer.echo(f"Artifact: {artifact}")
    for row in results[: min(12, len(results))]:
        tag_text = ",".join(row.tags[:6]) if row.tags else "-"
        typer.echo(
            f"- {row.company} | status={row.status} | confidence={row.confidence or '-'} | "
            f"source={row.source or '-'} | tags={tag_text}"
        )
        if row.prestige_signals:
            typer.echo(f"  Prestige: {','.join(row.prestige_signals)}")
        if row.description:
            typer.echo(f"  {row.description[:180]}")
        elif row.error:
            typer.echo(f"  error={row.error}")


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


@app.command("build-linkedin-reconcile-queue")
def build_linkedin_reconcile_queue(
    limit: Annotated[int, typer.Option(help="Maximum invited contacts to include")] = 50,
    include_status: Annotated[
        list[str] | None,
        typer.Option("--include-status", help="Contact statuses to check, default: Invited"),
    ] = None,
    max_age_days: Annotated[
        int,
        typer.Option(help="Only include contacts last touched within this many days"),
    ] = 14,
    min_age_hours: Annotated[
        int,
        typer.Option(help="Do not re-check invites newer than this many hours"),
    ] = 12,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    items = build_linkedin_reconcile_queue_items(
        organizations=workbook.list_organizations(),
        contacts=workbook.list_contacts(),
        touchpoints=workbook.list_touchpoints(),
        include_statuses=tuple(include_status or ["Invited"]),
        max_age_days=max_age_days,
        min_age_hours=min_age_hours,
    )[:limit]

    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-reconcile-queue",
        {
            "count": len(items),
            "filters": {
                "limit": limit,
                "include_status": include_status or ["Invited"],
                "max_age_days": max_age_days,
                "min_age_hours": min_age_hours,
            },
            "results": items,
        },
    )

    typer.echo(f"Built LinkedIn reconcile queue with {len(items)} contacts.")
    typer.echo(f"Artifact: {artifact}")
    for item in items[: min(12, len(items))]:
        typer.echo(
            f"- {item['company']} | {item['name']} | status={item['status']} | "
            f"age_hours={item['age_hours']}"
        )


@app.command("reconcile-linkedin")
def reconcile_linkedin(
    queue_artifact: Annotated[
        Path | None,
        typer.Option(help="Optional reconcile queue artifact to inspect live"),
    ] = None,
    results_artifact: Annotated[
        Path | None,
        typer.Option(help="Optional artifact with pre-detected reconcile results"),
    ] = None,
    live: Annotated[
        bool,
        typer.Option(help="Inspect LinkedIn profiles from the queue using the live browser session"),
    ] = False,
    apply_changes: Annotated[
        bool,
        typer.Option("--apply", help="Update contacts/touchpoints. Default is dry-run artifact only."),
    ] = False,
    limit: Annotated[int, typer.Option(help="Maximum contacts to reconcile")] = 25,
    max_age_days: Annotated[int, typer.Option(help="Queue fallback max age in days")] = 14,
    min_age_hours: Annotated[int, typer.Option(help="Queue fallback minimum age in hours")] = 12,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)

    source_artifact = ""
    if live:
        if queue_artifact is not None:
            with queue_artifact.open(encoding="utf-8") as handle:
                queue_payload = json.load(handle)
            candidates = list(queue_payload.get("results") or [])[:limit]
            source_artifact = str(queue_artifact)
        else:
            candidates = build_linkedin_reconcile_queue_items(
                organizations=workbook.list_organizations(),
                contacts=workbook.list_contacts(),
                touchpoints=workbook.list_touchpoints(),
                max_age_days=max_age_days,
                min_age_hours=min_age_hours,
            )[:limit]
        if not candidates:
            typer.echo("No LinkedIn contacts matched the reconcile queue filters.")
            raise typer.Exit(code=1)
        LinkedInScraper(settings).require_live_cdp_session()
        detected = LinkedInScraper(settings).reconcile_connection_statuses(candidates)
        raw_results = [item.__dict__ for item in detected]
    elif results_artifact is not None:
        with results_artifact.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        raw_results = list(payload.get("results") or [])
        source_artifact = str(results_artifact)
    else:
        typer.echo("Pass --live or --results-artifact to reconcile LinkedIn state.")
        raise typer.Exit(code=1)

    reconcile_result = apply_linkedin_reconcile_results(
        workbook=workbook,
        results=raw_results,
        source_artifact=source_artifact,
        apply_changes=apply_changes,
    )
    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-reconcile",
        {
            "live": live,
            "apply": apply_changes,
            "source_artifact": source_artifact,
            "count": len(raw_results),
            **reconcile_result,
        },
    )

    typer.echo(f"Reconciled {len(raw_results)} LinkedIn contacts.")
    typer.echo(f"Mode: {'apply' if apply_changes else 'dry run'}")
    typer.echo(f"Summary: {reconcile_result['summary']}")
    typer.echo(f"Artifact: {artifact}")
    for item in reconcile_result["results"][: min(12, len(reconcile_result["results"]))]:
        typer.echo(
            f"- {item.get('name') or item.get('contact_id')} | status={item['normalized_status']} | "
            f"action={item['action']} | applied={item['applied']}"
        )


@app.command("reconcile-linkedin-messages")
def reconcile_linkedin_messages(
    snapshot_artifact: Annotated[
        Path | None,
        typer.Option(help="Optional pre-captured LinkedIn message snapshot artifact"),
    ] = None,
    live: Annotated[
        bool,
        typer.Option(help="Read LinkedIn messaging threads from the live browser session"),
    ] = False,
    bootstrap: Annotated[
        bool,
        typer.Option(help="Store the current message thread offset without marking accepts/replies"),
    ] = False,
    apply_changes: Annotated[
        bool,
        typer.Option("--apply", help="Update contacts/touchpoints and advance the message offset"),
    ] = False,
    update_offset: Annotated[
        bool,
        typer.Option(help="Advance the stored message offset after this run"),
    ] = False,
    include_seen: Annotated[
        bool,
        typer.Option(help="Also process threads already present in the stored offset"),
    ] = False,
    deep: Annotated[
        bool,
        typer.Option(help="Scroll the LinkedIn inbox to capture older accepted/replied threads"),
    ] = False,
    limit: Annotated[int, typer.Option(help="Maximum message threads to read")] = 50,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    state_path = linkedin_message_state_path(settings)
    state = load_linkedin_message_state(state_path)

    source_artifact = ""
    if live:
        LinkedInScraper(settings).require_live_cdp_session()
        threads = [
            item.__dict__
            for item in LinkedInScraper(settings).snapshot_message_threads(limit=limit, deep=deep)
        ]
    elif snapshot_artifact is not None:
        with snapshot_artifact.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        threads = list(payload.get("results") or payload.get("threads") or [])
        source_artifact = str(snapshot_artifact)
    else:
        typer.echo("Pass --live or --snapshot-artifact to reconcile LinkedIn messages.")
        raise typer.Exit(code=1)

    message_results, next_state = build_linkedin_message_reconcile_results(
        threads=threads,
        contacts=workbook.list_contacts(),
        touchpoints=workbook.list_touchpoints(),
        state=state,
        include_seen=include_seen,
    )

    if bootstrap:
        save_linkedin_message_state(state_path, next_state)
        artifact = write_artifact(
            settings.artifacts_dir,
            "linkedin-message-reconcile",
            {
                "bootstrap": True,
                "apply": False,
                "source_artifact": source_artifact,
                "state_path": str(state_path),
                "thread_count": len(threads),
                "new_result_count": 0,
                "results": [],
            },
        )
        typer.echo(f"Bootstrapped LinkedIn message offset with {len(threads)} threads.")
        typer.echo(f"State: {state_path}")
        typer.echo(f"Artifact: {artifact}")
        return

    reconcile_result = apply_linkedin_reconcile_results(
        workbook=workbook,
        results=message_results,
        source_artifact=source_artifact,
        apply_changes=apply_changes,
    )
    should_update_offset = update_offset or apply_changes
    if should_update_offset:
        save_linkedin_message_state(state_path, next_state)

    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-message-reconcile",
        {
            "bootstrap": False,
            "live": live,
            "deep": deep,
            "apply": apply_changes,
            "offset_updated": should_update_offset,
            "source_artifact": source_artifact,
            "state_path": str(state_path),
            "thread_count": len(threads),
            "new_result_count": len(message_results),
            **reconcile_result,
        },
    )

    typer.echo(f"Read {len(threads)} LinkedIn message threads.")
    typer.echo(f"Detected {len(message_results)} new accepted/replied threads.")
    typer.echo(f"Mode: {'apply' if apply_changes else 'dry run'}")
    typer.echo(f"Offset updated: {should_update_offset}")
    typer.echo(f"Summary: {reconcile_result['summary']}")
    typer.echo(f"Artifact: {artifact}")
    for item in reconcile_result["results"][: min(12, len(reconcile_result["results"]))]:
        typer.echo(
            f"- {item.get('name') or item.get('thread_id')} | status={item['normalized_status']} | "
            f"action={item['action']} | follow_up={item['needs_follow_up']}"
        )


@app.command("pull-linkedin-followups")
def pull_linkedin_followups(
    snapshot_artifact: Annotated[
        Path | None,
        typer.Option(help="Optional pre-captured LinkedIn message snapshot artifact"),
    ] = None,
    live: Annotated[
        bool,
        typer.Option(help="Read LinkedIn messaging threads from the live browser session"),
    ] = True,
    include_seen: Annotated[
        bool,
        typer.Option(help="Also process threads already present in the stored message offset"),
    ] = True,
    apply_reconcile: Annotated[
        bool,
        typer.Option("--apply-reconcile", help="Record accepted/replied statuses before drafting follow-ups"),
    ] = False,
    update_offset: Annotated[
        bool,
        typer.Option(help="Advance the stored message offset after this pull"),
    ] = False,
    limit: Annotated[int, typer.Option(help="Maximum message threads to read")] = 75,
    draft_limit: Annotated[int, typer.Option(help="Maximum drafts to emit")] = 50,
    deep: Annotated[
        bool,
        typer.Option(help="Scroll the LinkedIn inbox to capture older accepted/replied threads"),
    ] = True,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    state_path = linkedin_message_state_path(settings)
    state = load_linkedin_message_state(state_path)

    source_artifact = ""
    if live:
        LinkedInScraper(settings).require_live_cdp_session()
        threads = [
            item.__dict__
            for item in LinkedInScraper(settings).snapshot_message_threads(limit=limit, deep=deep)
        ]
    elif snapshot_artifact is not None:
        with snapshot_artifact.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        threads = list(payload.get("results") or payload.get("threads") or [])
        source_artifact = str(snapshot_artifact)
    else:
        typer.echo("Pass --live or --snapshot-artifact to pull LinkedIn follow-ups.")
        raise typer.Exit(code=1)

    message_results, next_state = build_linkedin_message_reconcile_results(
        threads=threads,
        contacts=workbook.list_contacts(),
        touchpoints=workbook.list_touchpoints(),
        state=state,
        include_seen=include_seen,
    )
    reconcile_result = apply_linkedin_reconcile_results(
        workbook=workbook,
        results=message_results,
        source_artifact=source_artifact,
        apply_changes=apply_reconcile,
    )
    if update_offset or apply_reconcile:
        save_linkedin_message_state(state_path, next_state)

    reconcile_artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-message-reconcile",
        {
            "bootstrap": False,
            "live": live,
            "deep": deep,
            "apply": apply_reconcile,
            "offset_updated": update_offset or apply_reconcile,
            "source_artifact": source_artifact,
            "state_path": str(state_path),
            "thread_count": len(threads),
            "new_result_count": len(message_results),
            **reconcile_result,
        },
    )
    drafts = build_linkedin_followup_drafts(
        reconcile_results=list(reconcile_result.get("results") or []),
        organizations=workbook.list_organizations(),
        contacts=workbook.list_contacts(),
    )[:draft_limit]
    action_summary = summarize_linkedin_followup_actions(drafts, list(reconcile_result.get("results") or []))
    draft_artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-followup-drafts",
        {
            "source_artifact": str(reconcile_artifact),
            "count": len(drafts),
            "summary": action_summary,
            "results": drafts,
        },
    )

    typer.echo("LinkedIn follow-up action list")
    typer.echo(f"- Follow up with accepted invites: {action_summary['follow_up_candidates']}")
    typer.echo(f"- Reply to inbound messages: {action_summary['reply_candidates']}")
    typer.echo(f"- Optional polite closes: {action_summary['optional_closes']}")
    typer.echo(f"- Missing workbook contacts: {action_summary['missing_contacts']}")
    company_counts = action_summary.get("by_company") or {}
    if company_counts:
        typer.echo("- Top companies to clear:")
        for company, count in list(company_counts.items())[:8]:
            typer.echo(f"  {company}: {count}")
    typer.echo(f"Reconcile artifact: {reconcile_artifact}")
    typer.echo(f"Draft artifact: {draft_artifact}")
    for draft in drafts[: min(12, len(drafts))]:
        typer.echo(
            f"- {draft['name']} | {draft.get('title') or '(missing title)'} | {draft['company']} | "
            f"{draft['draft_kind']} | {draft['send_recommendation']}"
        )
        typer.echo(f"  {draft['draft_message']}")


@app.command("draft-linkedin-followups")
def draft_linkedin_followups(
    reconcile_artifact: Annotated[
        Path,
        typer.Option(help="Path to a linkedin-message-reconcile or linkedin-reconcile artifact"),
    ],
    limit: Annotated[int, typer.Option(help="Maximum drafts to emit")] = 25,
) -> None:
    settings = OutreachSettings()
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    with reconcile_artifact.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    drafts = build_linkedin_followup_drafts(
        reconcile_results=list(payload.get("results") or []),
        organizations=workbook.list_organizations(),
        contacts=workbook.list_contacts(),
    )[:limit]

    summary: dict[str, int] = {}
    for draft in drafts:
        key = str(draft["draft_kind"])
        summary[key] = summary.get(key, 0) + 1

    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-followup-drafts",
        {
            "source_artifact": str(reconcile_artifact),
            "count": len(drafts),
            "summary": summary,
            "results": drafts,
        },
    )

    typer.echo(f"Drafted {len(drafts)} LinkedIn follow-ups.")
    typer.echo(f"Summary: {summary}")
    typer.echo(f"Artifact: {artifact}")
    for draft in drafts[: min(12, len(drafts))]:
        typer.echo(
            f"- {draft['name']} | {draft['company']} | {draft['draft_kind']} | "
            f"{draft['send_recommendation']} | len={draft['draft_length']}"
        )
        typer.echo(f"  Title: {draft.get('title') or '(missing)'}")
        typer.echo(
            f"  Audience: {draft.get('followup_audience') or '(unknown)'}"
            f" | contact_type={draft.get('contact_type') or '(missing)'}"
        )
        typer.echo(f"  Original: {draft.get('original_invite_note') or '(missing)'}")
        if draft.get("latest_message") and draft.get("last_sender") != "You":
            typer.echo(f"  Latest from {draft.get('last_sender') or 'contact'}: {draft['latest_message']}")
        typer.echo(f"  {draft['draft_message']}")


@app.command("send-linkedin-followups")
def send_linkedin_followups(
    draft_artifact: Annotated[
        Path,
        typer.Option(help="Path to a linkedin-followup-drafts artifact"),
    ],
    limit: Annotated[int, typer.Option(help="Maximum reviewed drafts to process")] = 25,
    start_at: Annotated[int, typer.Option(help="Start offset into the reviewed draft list")] = 0,
    include_optional: Annotated[
        bool,
        typer.Option(help="Include optional polite-close drafts"),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option(help="Actually send follow-ups instead of doing a guarded dry run"),
    ] = False,
) -> None:
    settings = OutreachSettings()
    if execute:
        LinkedInScraper(settings).require_live_cdp_session()

    with draft_artifact.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    drafts = list(payload.get("results") or [])
    if not drafts:
        typer.echo("No follow-up drafts found in artifact.")
        raise typer.Exit(code=1)

    typer.echo(f"Processing LinkedIn follow-ups from {draft_artifact}")
    typer.echo(f"Mode: {'execute' if execute else 'dry run'}")
    artifact, progress_artifact, status_counts, touchpoints_added = execute_linkedin_followup_send(
        settings=settings,
        draft_artifact=draft_artifact,
        drafts=drafts,
        execute=execute,
        limit=limit,
        start_at=start_at,
        include_optional=include_optional,
    )
    typer.echo(f"Status summary: {status_counts}")
    typer.echo(f"Artifact: {artifact}")
    typer.echo(f"Progress artifact: {progress_artifact}")
    typer.echo(f"Tracked touchpoints_added: {touchpoints_added}")


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

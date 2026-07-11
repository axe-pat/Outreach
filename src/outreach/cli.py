from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from outreach.artifacts import artifact_timestamp, write_artifact
from outreach.config import OutreachSettings
from outreach.communication_lab import (
    build_communication_lab,
    build_rewrite_guidance,
    classify_quality_labels,
    review_email_craft,
    review_outreach_message,
)
from outreach.cadence import build_workbook_cadence_plan, guard_cadence_action
from outreach.discovery.adapters import BuiltInCompaniesAdapter, SourceAdapter, YCombinatorCompanyDirectoryAdapter
from outreach.discovery.http import HttpTextDownloader
from outreach.discovery.registry import get_source_definition, list_source_definitions
from outreach.scoring import score_candidate
from outreach.services.email_finder import (
    EmailFinderResult,
    EmailResearchCandidate,
    build_email_finder_service,
)
from outreach.services.linkedin import FilterRunResult, LinkedInFollowupSendResult, LinkedInScraper
from outreach.services.notes import NoteGenerator
from outreach.messaging_roles import (
    TargetRoleContext,
    infer_target_role_context,
    rewrite_message_for_target_role,
    target_role_context_from_family,
)
from outreach.style_profile import (
    CommunicationStyleProfile,
    load_style_profile_if_exists,
    sync_comms_learning_into_style_profile,
)
from outreach.models import CandidateProfile, LinkedInCompanyQueueItem
from outreach.strategic_accounts import import_strategic_accounts as import_strategic_account_seeds
from outreach.story_fit_targets import (
    DEFAULT_STORY_FIT_TARGETS_PATH,
    import_story_fit_targets as import_story_fit_target_seeds,
)
from outreach.intelligence_commands import register_intelligence_commands
from outreach.resume_jobs_bridge import (
    DEFAULT_INCLUDE_STATUSES,
    DEFAULT_COMPANY_OVERRIDES_FILENAME,
    DEFAULT_SEASON_FOCUS,
    TRANSITION_SEASON_FOCUS,
    build_resume_opportunity_notes,
    build_resume_organization_notes,
    build_resume_outreach_queue,
    classify_resume_role_season,
    ensure_company_overrides_csv,
    infer_opportunity_type,
    infer_company_type_for_job,
    load_resume_jobs,
    load_company_overrides,
    load_company_blocklist,
    map_resume_source_kind,
    normalize_dedupe_text,
    normalize_season_focus,
    opportunity_status_from_resume_status,
    organization_status_from_resume_status,
    organization_type_for_resume_job,
    select_resume_jobs,
    target_lists_from_resume_status,
)
from outreach.relationship_leads import (
    DEFAULT_RELATIONSHIP_LEADS_PATH,
    ensure_relationship_leads_template,
    import_relationship_leads as import_relationship_lead_seeds,
    relationship_source_default_path,
    relationship_source_preset,
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
            single_word_boundary = r"(?![a-z0-9]|\s+[a-z0-9])"
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


def _merge_target_lists(*values: str) -> str:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        for item in (value or "").split(";"):
            clean = item.strip()
            normalized = clean.lower()
            if not clean or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(clean)
    return ";".join(merged)


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
        f"exploring product roles at {company}. Does that background fit anything useful there? If yes, "
        "any recs on who I should talk to about that?"
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


def build_company_note_context(
    workbook: OutreachWorkbook,
    company: str,
    *,
    target_role_title: str = "",
) -> dict[str, object]:
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
    organization_metadata = parse_notes_metadata(organization.notes)
    target_role = infer_target_role_context(
        explicit_title=target_role_title,
        opportunity_titles=[item.title for item in opportunities[:3]],
        note_context={"target_roles": organization_metadata.get("target_roles", "")},
        organization_notes=organization.notes,
    )

    context: dict[str, object] = {
        "organization_type": organization.organization_type.value,
        "target_lists": organization.target_lists,
        "tags": tags,
        "description": description,
        "scale_signal": scale_signal,
        "opportunity_titles": [item.title for item in opportunities[:3]],
        "fit_rationale": fit_rationale,
        "target_roles": organization_metadata.get("target_roles", ""),
        "target_role_family": target_role.family.value,
        "target_role_label": target_role.label,
        "target_role_source": target_role.source,
        "target_role_matched_text": target_role.matched_text,
        "target_role_matched_rule": target_role.matched_rule,
        "target_role_is_concrete": target_role.is_concrete,
        "target_role_title": target_role_title,
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
    target_role_title: str = "",
) -> Path:
    scraper = LinkedInScraper(settings)
    scraper.require_live_cdp_session()
    note_generator = NoteGenerator()
    if note_context is None:
        note_context = build_company_note_context(
            OutreachWorkbook(settings.resolved_tracking_workspace_dir),
            company,
            target_role_title=target_role_title,
        )
    elif target_role_title:
        target_role = infer_target_role_context(explicit_title=target_role_title)
        note_context = {
            **note_context,
            "target_role_title": target_role_title,
            "target_role_family": target_role.family.value,
            "target_role_label": target_role.label,
            "target_role_source": target_role.source,
            "target_role_matched_text": target_role.matched_text,
            "target_role_matched_rule": target_role.matched_rule,
            "target_role_is_concrete": target_role.is_concrete,
        }
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
                    f"source_artifact={source_artifact_path.name} | "
                    f"target_role_family={candidate.get('target_role_family', 'product_pm')} | "
                    f"target_role_label={candidate.get('target_role_label', 'Product / PM')} | "
                    f"target_role_source={candidate.get('target_role_source', 'product_primary_default')} | "
                    f"target_role_matched_text={candidate.get('target_role_matched_text', '')} | "
                    f"target_role_matched_rule={candidate.get('target_role_matched_rule', '')} | "
                    f"target_role_is_concrete={candidate.get('target_role_is_concrete', False)}"
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
        invite_metadata = parse_notes_metadata(latest_invite.notes if latest_invite else "")
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
                "target_role_family": invite_metadata.get("target_role_family", ""),
                "target_role_label": invite_metadata.get("target_role_label", ""),
                "target_role_source": invite_metadata.get("target_role_source", ""),
                "target_role_matched_text": invite_metadata.get("target_role_matched_text", ""),
                "target_role_matched_rule": invite_metadata.get("target_role_matched_rule", ""),
                "target_role_is_concrete": invite_metadata.get("target_role_is_concrete", ""),
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
    touchpoints = workbook.list_touchpoints()
    contact_by_id = {item.contact_id: item for item in contacts}
    contact_by_url = {
        normalize_dedupe_text(item.linkedin_url): item
        for item in contacts
        if item.linkedin_url
    }
    sent_messages_by_contact: dict[str, set[str]] = {}
    accepted_contacts = {
        item.contact_id
        for item in touchpoints
        if item.contact_id
        and (
            (item.status or "").strip().casefold() in {"accepted", "already connected"}
            or (item.message_kind or "").strip().casefold() in {"linkedin_accept", "linkedin_accepted"}
            or (item.message_text or "").strip().casefold() == "linkedin invite accepted."
        )
    }
    for touchpoint in touchpoints:
        if (touchpoint.channel or "") != OutreachChannel.LINKEDIN:
            continue
        if (touchpoint.status or "").strip().lower() != "sent":
            continue
        if not touchpoint.contact_id or not touchpoint.message_text:
            continue
        sent_messages_by_contact.setdefault(touchpoint.contact_id, set()).add(
            normalize_dedupe_text(touchpoint.message_text)
        )

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
        "manual_outbound_touchpoints_added": 0,
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
        existing_status = (contact.status or "").strip().lower()
        if status == "connected":
            summary["connected"] += 1
            if existing_status in {"connected", "replied"}:
                action = "already_connected"
                if existing_status == "connected" and contact.contact_id not in accepted_contacts:
                    action = "record_missing_acceptance"
                    touchpoint_status = "Accepted"
                    message_text = "LinkedIn invite accepted."
            else:
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
        if apply_changes and (new_contact_status or message_text):
            if new_contact_status:
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
                    if status == "connected":
                        accepted_contacts.add(contact.contact_id)
            applied = True

        latest_message = str(raw.get("latest_message") or "").strip()
        latest_sender = str(raw.get("last_sender") or raw.get("live_last_sender") or "").strip().lower()
        latest_message_key = normalize_dedupe_text(latest_message)
        if (
            apply_changes
            and latest_message
            and latest_sender == "you"
            and latest_message_key
            and latest_message_key not in sent_messages_by_contact.get(contact.contact_id, set())
        ):
            _, created = workbook.append_touchpoint(
                TouchpointRecord(
                    touchpoint_id=workbook.make_touchpoint_id(
                        contact.organization_id,
                        contact.contact_id,
                        OutreachChannel.LINKEDIN.value,
                        latest_message,
                        source_artifact=source_artifact,
                    ),
                    organization_id=contact.organization_id,
                    contact_id=contact.contact_id,
                    channel=OutreachChannel.LINKEDIN,
                    status="Sent",
                    message_kind="linkedin_manual_message",
                    message_text=latest_message,
                    recorded_at=utc_now_iso(),
                    sent_at=utc_now_iso(),
                    source_artifact=source_artifact,
                    notes=f"manual_outbound_detected=true | reconcile_status={status} | detail={raw.get('detail', '')}",
                )
            )
            if created:
                summary["touchpoints_added"] += 1
                summary["manual_outbound_touchpoints_added"] += 1
                sent_messages_by_contact.setdefault(contact.contact_id, set()).add(latest_message_key)
                workbook.update_contact(contact.contact_id, last_contacted_at=utc_now_iso())

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


def compact_message_window(
    *,
    thread: dict,
    previous_state: dict | None = None,
    touchpoints: list[TouchpointRecord] | None = None,
    original_invite_note: str = "",
    limit: int = 4,
) -> list[dict[str, str]]:
    """Build a small useful context window without storing full LinkedIn history."""
    previous_state = previous_state or {}
    touchpoints = touchpoints or []
    messages: list[dict[str, str]] = []

    def add(sender: str, message: str, *, timestamp: str = "", source: str = "") -> None:
        clean = " ".join(str(message or "").split()).strip()
        if not clean:
            return
        lowered = clean.lower()
        if lowered in {"linkedin reply detected.", "linkedin reply detected", "linkedin invite accepted."}:
            return
        messages.append(
            {
                "sender": sender or "",
                "message": clean,
                "timestamp_text": timestamp or "",
                "source": source or "",
            }
        )

    for raw in list(previous_state.get("message_window") or []):
        if isinstance(raw, dict):
            add(
                str(raw.get("sender") or ""),
                str(raw.get("message") or ""),
                timestamp=str(raw.get("timestamp_text") or ""),
                source=str(raw.get("source") or "state"),
            )

    if original_invite_note:
        add("You", original_invite_note, source="original_invite")

    outbound_kinds = {"linkedin_invite", "linkedin_followup", "linkedin_message"}
    for touchpoint in sorted(touchpoints, key=lambda item: item.recorded_at)[-6:]:
        if touchpoint.message_kind in outbound_kinds:
            add("You", touchpoint.message_text, timestamp=touchpoint.recorded_at, source=touchpoint.message_kind)

    latest = str(thread.get("latest_message") or thread.get("message_text") or "").strip()
    sender = str(thread.get("last_sender") or "").strip()
    if latest:
        add(sender or "contact", latest, timestamp=str(thread.get("timestamp_text") or ""), source="linkedin_latest")

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        key = (
            normalize_dedupe_text(message.get("sender", "")),
            normalize_dedupe_text(message.get("message", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(message)
    return deduped[-limit:]


def compact_context_text(message_window: list[dict[str, str]]) -> str:
    return "\n".join(
        f"{item.get('sender') or 'contact'}: {item.get('message') or ''}"
        for item in message_window
        if item.get("message")
    )


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
        contact = match_contact_for_message_thread(thread, contacts)
        original_invite_note = (
            latest_invite_note_for_contact(contact.contact_id, touchpoints)
            if contact is not None
            else ""
        )
        contact_touchpoints = [
            item for item in touchpoints
            if contact is not None and item.contact_id == contact.contact_id
        ]
        latest_invite_touchpoint = latest_invite_touchpoint_for_contact(contact_touchpoints)
        invite_metadata = parse_notes_metadata(
            latest_invite_touchpoint.notes if latest_invite_touchpoint else ""
        )
        message_window = compact_message_window(
            thread=thread,
            previous_state=next_thread_states.get(key, {}),
            touchpoints=contact_touchpoints,
            original_invite_note=original_invite_note,
        )
        next_thread_states[key] = {
            **next_thread_states.get(key, {}),
            "signature": current_signature,
            "name": str(thread.get("name") or ""),
            "latest_message": str(thread.get("latest_message") or ""),
            "last_sender": str(thread.get("last_sender") or ""),
            "timestamp_text": str(thread.get("timestamp_text") or ""),
            "thread_url": str(thread.get("thread_url") or ""),
            "message_window": message_window,
            "last_seen_at": snapshot_at,
            "first_seen_at": str(next_thread_states.get(key, {}).get("first_seen_at") or snapshot_at),
        }
        if not include_seen and not is_new_thread and not thread_changed:
            continue

        if contact is None:
            results.append(
                {
                    "thread_id": key,
                    "name": thread.get("name", ""),
                    "status": "unknown",
                    "detail": "Message thread did not match a workbook contact.",
                    "thread_url": thread.get("thread_url", ""),
                    "latest_message": thread.get("latest_message", ""),
                    "message_window": message_window,
                    "is_new_thread": is_new_thread,
                    "thread_changed": thread_changed,
                    "thread_signature": current_signature,
                    "previous_thread_signature": previous_signature,
                    "state_reason": state_reason,
                }
            )
            continue

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
                "message_window": message_window,
                "unread": bool(thread.get("unread")),
                "is_new_thread": is_new_thread,
                "thread_changed": thread_changed,
                "thread_signature": current_signature,
                "previous_thread_signature": previous_signature,
                "state_reason": state_reason,
                "original_invite_note": original_invite_note,
                "target_role_family": invite_metadata.get("target_role_family", ""),
                "target_role_label": invite_metadata.get("target_role_label", ""),
                "target_role_source": invite_metadata.get("target_role_source", ""),
                "target_role_matched_text": invite_metadata.get("target_role_matched_text", ""),
                "target_role_matched_rule": invite_metadata.get("target_role_matched_rule", ""),
                "target_role_is_concrete": invite_metadata.get("target_role_is_concrete", ""),
            }
        )

    next_state = {
        **state,
        "seen_thread_ids": sorted(all_thread_ids),
        "thread_states": next_thread_states,
        "last_snapshot_at": snapshot_at,
    }
    return results, next_state


def build_persisted_inbound_reconcile_results(
    *,
    state: dict[str, object],
    contacts: list[ContactRecord],
    touchpoints: list[TouchpointRecord],
    exclude_contact_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Recover still-unanswered inbound threads from the persistent inbox state.

    The live inbox snapshot is bounded and may not include an older reply.  A
    reply must not disappear merely because it fell below today's scroll
    window.  Current live results win; this function only fills missing
    contacts from the durable state ledger.
    """
    excluded = exclude_contact_ids or set()
    results: list[dict[str, object]] = []
    for thread_id, raw_state in dict(state.get("thread_states") or {}).items():
        if not isinstance(raw_state, dict):
            continue
        latest_message = str(raw_state.get("latest_message") or "").strip()
        last_sender = str(raw_state.get("last_sender") or "").strip()
        if not latest_message or last_sender.casefold() in {"you", "akshat"}:
            continue
        thread = {**raw_state, "thread_id": str(thread_id)}
        contact = match_contact_for_message_thread(thread, contacts)
        if contact is None or contact.contact_id in excluded:
            continue
        contact_touchpoints = [
            item for item in touchpoints if item.contact_id == contact.contact_id
        ]
        original_invite_note = latest_invite_note_for_contact(
            contact.contact_id,
            touchpoints,
        )
        latest_invite = latest_invite_touchpoint_for_contact(contact_touchpoints)
        invite_metadata = parse_notes_metadata(latest_invite.notes if latest_invite else "")
        results.append(
            {
                "thread_id": str(thread_id),
                "contact_id": contact.contact_id,
                "organization_id": contact.organization_id,
                "name": contact.full_name,
                "linkedin_url": contact.linkedin_url,
                "status": "replied",
                "detail": "Persistent inbox state contains an unanswered inbound reply.",
                "thread_url": str(raw_state.get("thread_url") or ""),
                "latest_message": latest_message,
                "last_sender": last_sender,
                "timestamp_text": str(raw_state.get("timestamp_text") or ""),
                "message_window": compact_message_window(
                    thread=thread,
                    previous_state=raw_state,
                    touchpoints=contact_touchpoints,
                    original_invite_note=original_invite_note,
                ),
                "state_reason": "persistent_unanswered_inbound",
                "original_invite_note": original_invite_note,
                "target_role_family": invite_metadata.get("target_role_family", ""),
                "target_role_label": invite_metadata.get("target_role_label", ""),
                "target_role_source": invite_metadata.get("target_role_source", ""),
                "target_role_matched_text": invite_metadata.get("target_role_matched_text", ""),
                "target_role_matched_rule": invite_metadata.get("target_role_matched_rule", ""),
                "target_role_is_concrete": invite_metadata.get("target_role_is_concrete", ""),
            }
        )
    results.sort(
        key=lambda item: (
            str(item.get("timestamp_text") or ""),
            str(item.get("name") or "").casefold(),
        ),
        reverse=True,
    )
    return results


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


def infer_contact_seniority(contact: ContactRecord) -> str:
    text = " ".join([contact.contact_type, contact.title]).lower()
    if any(token in text for token in ["founder", "co-founder", "cofounder", "ceo", "cto", "chief "]):
        return "founder_exec"
    if any(
        token in text
        for token in [
            "principal",
            "staff",
            "distinguished",
            "director",
            "head of",
            "vp ",
            "vice president",
            "lead ",
            "senior manager",
        ]
    ):
        return "senior"
    if any(token in text for token in ["senior", "sr.", "sr "]):
        return "mid_senior"
    if any(token in text for token in ["associate", "junior", "intern", "analyst", "swe 1", "swe 2"]):
        return "junior_mid"
    return "mid"


def classify_linkedin_reply_intent(
    *,
    latest_message: str,
    message_window: list[dict[str, str]] | None = None,
) -> str:
    lower = latest_message.lower().strip()
    context = compact_context_text(message_window or []).lower()
    if not lower:
        return "unknown"
    if any(token in lower for token in ["won't be able to help", "wont be able to help", "can't help", "cannot help", "unable to help", "not able to help"]):
        return "soft_no"
    if any(
        token in lower
        for token in ["no idea", "don't know", "dont know", "do not know", "not sure", "no clue"]
    ):
        return "does_not_know"
    if extract_email_addresses(latest_message) and any(token in lower for token in ["resume", "cv", "profile"]):
        return "send_resume_to_email"
    if "share your profile" in lower or "share your resume" in lower or "send your resume" in lower:
        return "referral_offer"
    ack_tokens = {"sure", "sure, let me know", "absolutely", "ok", "okay", "sounds good", "yes", "yep", "👍", "👏", "😊"}
    compact_lower = re.sub(r"\s+", " ", lower).strip(" .!")
    asked_already = bool(
        re.search(
            r"\b(referral path|hiring contact|product/recruiting|point(?:ing)? me|recs? on who|"
            r"who (?:i|we) should talk to|send (you )?(a )?(tight )?resume|short blurb|"
            r"only send you a fit|real match|pm/product opening)\b",
            context,
        )
    )
    promised_specific_fit = bool(
        re.search(
            r"\b(only send you a fit|real match|send (you )?(a )?(tight )?resume|short blurb|"
            r"send (?:the|a) posting|share (?:the|a) role)\b",
            context,
        )
    )
    if compact_lower in ack_tokens or any(emoji in lower for emoji in ["👍", "👏", "😊"]):
        if promised_specific_fit:
            return "permission_to_send_fit"
        return "already_asked_wait" if asked_already else "permission_to_send_fit"
    if "let me know" in lower and asked_already:
        return "permission_to_send_fit" if promised_specific_fit else "already_asked_wait"
    if "let me know" in lower:
        return "permission_to_send_fit"
    if "hr" in lower or "recruiter" in lower or "hiring" in lower:
        return "routing_signal"
    if any(token in lower for token in ["small team", "high ownership", "feedback loop"]):
        return "company_insight"
    return "needs_routing_ask"


def _shorten_sentence(value: str, max_length: int = 220) -> str:
    clean = " ".join(str(value or "").split()).strip()
    if not clean:
        return ""
    if len(clean) > max_length:
        clean = clean[: max_length - 1].rsplit(" ", maxsplit=1)[0].rstrip(".,;:")
    return clean.rstrip(".") + "."


def _story_fit_metadata(organization: OrganizationRecord | None) -> dict[str, str]:
    if organization is None:
        return {}
    return parse_notes_metadata(organization.notes)


def linkedin_story_fit_line(company: str, organization: OrganizationRecord | None) -> str:
    metadata = _story_fit_metadata(organization)
    reason = _shorten_sentence(metadata.get("story_fit_reason", ""), max_length=165)
    if reason:
        return f"My reason for looking at {company} is specific: {reason}"
    evidence = _shorten_sentence(metadata.get("profile_evidence", ""), max_length=150)
    if evidence:
        return f"My reason for looking at {company} is grounded in {evidence}"
    return ""


def email_story_fit_line(organization: OrganizationRecord) -> str:
    metadata = _story_fit_metadata(organization)
    reason = _shorten_sentence(metadata.get("story_fit_reason", ""), max_length=240)
    evidence = _shorten_sentence(metadata.get("profile_evidence", ""), max_length=180)
    if reason and evidence:
        return f"The story-fit is concrete: {reason} The proof point is {evidence}"
    if reason:
        return f"The story-fit is concrete: {reason}"
    return ""


def founder_context_line(company: str, organization: OrganizationRecord | None) -> str:
    story_line = linkedin_story_fit_line(company, organization)
    if story_line:
        return story_line
    organization_text = " ".join(
        [
            organization.notes if organization else "",
            organization.target_lists if organization else "",
        ]
    ).lower()
    if "agent analytics" in organization_text or "ai agents" in organization_text:
        return f"{company}'s AI agent analytics work maps well to my data/platform + applied AI experience."
    return ""


def product_context_line(contact: ContactRecord, organization: OrganizationRecord | None = None) -> str:
    story_line = linkedin_story_fit_line(organization.name if organization else "", organization)
    if story_line:
        return story_line
    title = contact.title.lower()
    if "ai" in title or "data infrastructure" in title:
        return "Your AI/data infrastructure work feels close to problems I've worked around."
    if "security" in title or "developer" in title:
        return "Your developer-facing product work feels close to problems I've worked around."
    return ""


def _accepted_followup_draft_product(
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
                "engineering background is useful. If there is a relevant opening, would you be open to a referral "
                "or pointing me to the right hiring contact?"
            ),
        )
    if audience == "founder":
        context_line = founder_context_line(company, organization)
        context_sentence = f"{context_line} " if context_line else ""
        return (
            "review",
            (
                f"Thanks for connecting, {name}. I'm exploring product roles where my engineering + MBA background "
                f"can be useful. {context_sentence}Does that background fit anything useful at {company}? Any recs "
                "on who I should talk to about that?"
            ),
        )
    if audience == "product":
        context_line = product_context_line(contact, organization)
        context_sentence = f"{context_line} " if context_line else ""
        return (
            "review",
            (
                f"Thanks for connecting, {name}. I'm exploring product roles at {company} from an engineering + "
                f"data/platform background. {context_sentence}Does that background seem relevant to product work there?"
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
        seniority = infer_contact_seniority(contact)
        story_line = linkedin_story_fit_line(company, organization)
        if seniority in {"senior", "founder_exec"}:
            context_sentence = f" {story_line}" if story_line else ""
            return (
                "review",
                (
                    f"Thanks for connecting, {name}. I'm exploring technical PM/product paths at {company} from a "
                    f"backend/data engineering background.{context_sentence} Does that background fit product work "
                    "there? Any recs on who I should talk to about that?"
                ),
            )
        return (
            "safe_to_review",
            (
                f"Thanks for connecting, {name}. I'm trying to get on the radar at {company} for PM/product roles "
                "where my data/platform engineering background helps. If there is a relevant opening, would you be "
                "open to a referral or pointing me to the right hiring contact?"
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


def accepted_followup_draft(
    *,
    company: str,
    contact: ContactRecord,
    original_invite_note: str,
    organization: OrganizationRecord | None = None,
    target_role: TargetRoleContext | None = None,
) -> tuple[str, str]:
    recommendation, message = _accepted_followup_draft_product(
        company=company,
        contact=contact,
        original_invite_note=original_invite_note,
        organization=organization,
    )
    effective_target = target_role or infer_target_role_context(
        organization_notes=organization.notes if organization else ""
    )
    return recommendation, rewrite_message_for_target_role(message, effective_target)


def _reply_followup_draft_product(
    *,
    company: str,
    contact: ContactRecord,
    latest_message: str,
    message_window: list[dict[str, str]] | None = None,
    target_role: TargetRoleContext | None = None,
) -> tuple[str, str, str]:
    name = first_name(contact.full_name)
    lower = latest_message.lower()
    emails = extract_email_addresses(latest_message)
    intent = classify_linkedin_reply_intent(latest_message=latest_message, message_window=message_window)
    if "let me know if" in lower and any(
        token in lower for token in ["opening", "opens", "role", "interested", "pm", "product"]
    ):
        return (
            "conversation_reply",
            "review",
            (
                f"Thanks {name}, this is helpful. I'm focused on PM/product paths for now, so I'd really "
                f"appreciate being kept in mind if something opens up at {company}. Happy to send a short fit "
                "summary if that would be useful."
            ),
        )
    if intent == "permission_to_send_fit":
        concrete_role_sources = {
            "explicit_title",
            "note_context.target_role_title",
            "opportunity_title",
            "note_context.opportunity_title",
            "note_context.latest_opportunity_title",
        }
        if (
            target_role is None
            or not target_role.is_concrete
            or target_role.source not in concrete_role_sources
        ):
            return (
                "already_asked_wait",
                "hold",
                (
                    f"Hold for now. {name} is open to the next step, but there is no concrete {company} "
                    "role/fit to send yet."
                ),
            )
        role = target_role.matched_text or target_role.label
        return (
            "conversation_reply",
            "auto_send",
            (
                f"Thanks {name}. I found one concrete fit: {role}. I spent 5 years building backend/data platforms "
                "before Marshall, so the technical and cross-functional side lines up well. I can send the posting "
                "plus a tight resume/fit blurb here if useful."
            ),
        )
    if intent == "does_not_know":
        return (
            "conversation_reply",
            "review",
            (
                f"Sure, thanks {name}. Is there a PM/product internship path at {company}? Any recs on who "
                "I should talk to about that?"
            ),
        )
    if intent == "already_asked_wait":
        return (
            "already_asked_wait",
            "hold",
            (
                f"Hold for now. {name} already acknowledged the ask; send a follow-up only when there is a specific "
                f"{company} PM/product fit to share."
            ),
        )
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
    if emails and any(token in lower for token in ["resume", "cv", "profile"]):
        target = emails[0]
        return (
            "conversation_reply",
            "review",
            (
                f"Thanks {name}, appreciate it. I'll email {target} with my resume and a short note "
                f"on where my engineering + MBA background could be useful for product work at {company}."
            ),
        )
    if "share your profile" in lower or "share your resume" in lower or "hr" in lower:
        return (
            "referral_offer_reply",
            "review",
            (
                f"That would be amazing, thanks {name}. Short context if useful: MBA + 5 yrs backend/data "
                "platform engineering, now targeting PM/product roles where technical depth helps. Happy to send "
                "resume too if HR wants it."
            ),
        )
    if any(token in lower for token in ["small team", "high-impact", "high ownership", "feedback loop"]):
        return (
            "conversation_reply",
            "review",
            (
                f"This is helpful, thanks {name}. The small-team/high-ownership + customer-feedback loop "
                f"at {company} is exactly what I'm looking for. Do you think there's a PM/product internship path "
                "there? Any recs on who I should talk to about that?"
            ),
        )
    return (
        "conversation_reply",
        "review",
        (
            f"Thanks {name}, this is helpful. I'm exploring product roles where my engineering + MBA background "
            f"could be useful. Does that fit anything at {company}? Any recs on who I should talk to about that?"
        ),
    )


def reply_followup_draft(
    *,
    company: str,
    contact: ContactRecord,
    latest_message: str,
    message_window: list[dict[str, str]] | None = None,
    target_role: TargetRoleContext | None = None,
) -> tuple[str, str, str]:
    draft_kind, recommendation, message = _reply_followup_draft_product(
        company=company,
        contact=contact,
        latest_message=latest_message,
        message_window=message_window,
        target_role=target_role,
    )
    effective_target = target_role or infer_target_role_context()
    return (
        draft_kind,
        recommendation,
        rewrite_message_for_target_role(message, effective_target),
    )


def extract_email_addresses(text: str) -> list[str]:
    seen: set[str] = set()
    emails: list[str] = []
    for match in re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text or "", flags=re.I):
        normalized = match.strip(".,;:()[]{}<>").lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            emails.append(match.strip(".,;:()[]{}<>"))
    return emails


def extract_linkedin_conversation_action_items(item: dict) -> list[dict[str, str]]:
    latest_message = str(item.get("latest_message") or item.get("message_text") or "").strip()
    latest_lower = latest_message.lower()
    if not latest_message:
        return []
    last_sender = str(item.get("last_sender") or "").strip().lower()
    if last_sender in {"you", "akshat"}:
        return []

    name = str(item.get("name") or "").strip()
    company = str(item.get("company") or "").strip()
    emails = extract_email_addresses(latest_message)
    actions: list[dict[str, str]] = []

    if emails and any(token in latest_lower for token in ["resume", "cv", "profile"]):
        actions.append(
            {
                "action_type": "email_resume",
                "priority": "high",
                "contact_name": name,
                "company": company,
                "email": emails[0],
                "description": f"Email resume and a short role-fit note to {emails[0]} for {company or 'this company'}.",
                "source_message": latest_message,
            }
        )
    elif emails:
        actions.append(
            {
                "action_type": "email_contact",
                "priority": "medium",
                "contact_name": name,
                "company": company,
                "email": emails[0],
                "description": f"Email {emails[0]} with the context requested in the LinkedIn reply.",
                "source_message": latest_message,
            }
        )

    has_resume_email_action = any(action.get("action_type") == "email_resume" for action in actions)
    if not has_resume_email_action and any(
        token in latest_lower for token in ["send your resume", "share your resume", "send me your resume", "share your profile"]
    ):
        actions.append(
            {
                "action_type": "send_resume_or_profile",
                "priority": "high",
                "contact_name": name,
                "company": company,
                "email": emails[0] if emails else "",
                "description": f"Send resume/profile context to {name or 'the contact'} for {company or 'this company'}.",
                "source_message": latest_message,
            }
        )

    if any(token in latest_lower for token in ["short blurb", "3-line blurb", "three-line blurb", "brief blurb"]):
        actions.append(
            {
                "action_type": "send_short_blurb",
                "priority": "high",
                "contact_name": name,
                "company": company,
                "email": emails[0] if emails else "",
                "description": f"Send a short referral blurb to {name or 'the contact'} for {company or 'this company'}.",
                "source_message": latest_message,
            }
        )

    if "let me know if" in latest_lower and any(token in latest_lower for token in ["interested", "opening", "role", "position"]):
        actions.append(
            {
                "action_type": "decide_role_interest",
                "priority": "medium",
                "contact_name": name,
                "company": company,
                "email": emails[0] if emails else "",
                "description": f"Decide whether to pursue the role/opening {name or 'the contact'} mentioned at {company or 'this company'}.",
                "source_message": latest_message,
            }
        )

    is_missing_contact = str(item.get("action") or "").strip().lower() == "missing_contact"
    opportunity_tokens = [
        "opportunity",
        "position",
        "role",
        "opening",
        "job",
        "jobs",
        "apply",
        "application",
        "candidate",
        "hiring",
        "recruiter",
        "inmail",
        "fellowship",
        "project",
        "projects",
    ]
    relevance_tokens = [
        "product",
        "pm",
        "mba",
        "strategy",
        "operations",
        "operator",
        "intern",
        "internship",
        "ai",
        "data",
        "platform",
    ]
    noise_tokens = ["sponsored", "promoted", "sales", "webinar", "newsletter"]
    looks_like_opportunity = any(token in latest_lower for token in opportunity_tokens)
    looks_relevant = any(token in latest_lower for token in relevance_tokens)
    looks_noisy = any(token in latest_lower for token in noise_tokens)
    if is_missing_contact and looks_like_opportunity and (looks_relevant or not looks_noisy):
        priority = "medium" if looks_relevant else "low"
        sender_label = (name or "unknown sender").rstrip(".")
        actions.append(
            {
                "action_type": "review_inbound_opportunity",
                "priority": priority,
                "contact_name": name,
                "company": company,
                "email": emails[0] if emails else "",
                "description": f"Review inbound LinkedIn opportunity from {sender_label}.",
                "source_message": latest_message,
            }
        )

    deduped: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for action in actions:
        key = (action.get("action_type", ""), action.get("email", ""), action.get("description", ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(action)
    return deduped


def build_linkedin_followup_drafts(
    *,
    reconcile_results: list[dict],
    organizations: list[OrganizationRecord],
    contacts: list[ContactRecord],
    opportunities: list[OpportunityRecord] | None = None,
    style_profile: CommunicationStyleProfile | None = None,
) -> list[dict[str, object]]:
    organization_map = {item.organization_id: item for item in organizations}
    contact_map = {item.contact_id: item for item in contacts}
    opportunities_by_org: dict[str, list[OpportunityRecord]] = {}
    for opportunity in opportunities or []:
        opportunities_by_org.setdefault(opportunity.organization_id, []).append(opportunity)
    for organization_opportunities in opportunities_by_org.values():
        organization_opportunities.sort(key=lambda item: item.discovered_at, reverse=True)
    profile = style_profile or load_style_profile_if_exists(
        Path("workspace") / "communication_style_profile.yml"
    )
    drafts: list[dict[str, object]] = []

    for item in reconcile_results:
        contact_id = str(item.get("contact_id") or "")
        contact = contact_map.get(contact_id)
        if contact is None:
            continue
        organization = organization_map.get(contact.organization_id)
        company = organization.name if organization else ""
        organization_opportunities = opportunities_by_org.get(contact.organization_id, [])
        serialized_role_source = str(item.get("target_role_source") or "")
        serialized_role_family = str(item.get("target_role_family") or "")
        target_role = infer_target_role_context(
            explicit_family=(serialized_role_family if serialized_role_family and not serialized_role_source else ""),
            explicit_title=str(item.get("target_role_title") or item.get("target_role") or ""),
            opportunity_titles=[entry.title for entry in organization_opportunities],
            note_context={
                "target_roles": item.get("target_roles") or "",
                "target_role_family": serialized_role_family if serialized_role_source else "",
                "target_role_label": item.get("target_role_label") or "",
                "target_role_source": serialized_role_source,
                "target_role_matched_text": item.get("target_role_matched_text") or "",
                "target_role_matched_rule": item.get("target_role_matched_rule") or "",
                "target_role_is_concrete": item.get("target_role_is_concrete", ""),
            },
            organization_notes=organization.notes if organization else "",
        )
        item_with_company = {**item, "company": company}
        status = str(item.get("normalized_status") or item.get("status") or "")
        if status == "connected" and item.get("needs_follow_up"):
            recommendation, draft = accepted_followup_draft(
                company=company,
                contact=contact,
                original_invite_note=str(item.get("original_invite_note") or ""),
                organization=organization,
                target_role=target_role,
            )
            draft_kind = "accepted_follow_up"
        elif status == "replied":
            draft_kind, recommendation, draft = reply_followup_draft(
                company=company,
                contact=contact,
                latest_message=str(item.get("latest_message") or item.get("message_text") or ""),
                message_window=[
                    dict(message)
                    for message in list(item.get("message_window") or [])
                    if isinstance(message, dict)
                ],
                target_role=target_role,
            )
        else:
            continue

        action_items = extract_linkedin_conversation_action_items(item_with_company)
        recipient_type = email_recipient_type(contact)
        guided_style = profile.guide_draft_from_examples(draft, recipient_type)
        draft = rewrite_message_for_target_role(guided_style.message, target_role)
        communication_review = review_outreach_message(
            body=draft,
            channel="linkedin_followup",
            company=company,
            recipient_type=recipient_type,
            recipient_title=contact.title,
            style_profile=profile,
            grounding_context=compact_context_text(
                [
                    dict(message)
                    for message in list(item.get("message_window") or [])
                    if isinstance(message, dict)
                ]
            ) or str(item.get("latest_message") or item.get("message_text") or ""),
        )
        if recommendation == "hold":
            communication_review.score = min(communication_review.score, 60)
            communication_review.verdict = "needs_rewrite"
            communication_review.recommended_action = "hold"
            communication_review.flags.append("Hold: prior context indicates this would repeat an ask")
        drafts.append(
            {
                "contact_id": contact.contact_id,
                "organization_id": contact.organization_id,
                "company": company,
                "name": contact.full_name,
                "title": contact.title,
                "contact_type": contact.contact_type,
                "target_role_family": target_role.family.value,
                "target_role_label": target_role.label,
                "target_role_source": target_role.source,
                "target_role_matched_text": target_role.matched_text,
                "target_role_matched_rule": target_role.matched_rule,
                "target_role_is_concrete": target_role.is_concrete,
                "target_role_context": target_role.as_dict(),
                "followup_audience": infer_followup_audience(
                    contact,
                    str(item.get("original_invite_note") or ""),
                ),
                "linkedin_url": contact.linkedin_url,
                "draft_kind": draft_kind,
                "send_recommendation": recommendation,
                "draft_message": draft,
                "draft_length": len(draft),
                "communication_review": communication_review.__dict__,
                "communication_recommendation": communication_review.recommended_action,
                "style_guidance": guided_style.prompt_guidance,
                "style_example_labels": list(guided_style.strong_example_labels),
                "style_transformations": list(guided_style.transformations),
                "source_status": status,
                "latest_message": item.get("latest_message", ""),
                "last_sender": item.get("last_sender", ""),
                "timestamp_text": item.get("timestamp_text", ""),
                "message_window": item.get("message_window", []),
                "reply_intent": (
                    classify_linkedin_reply_intent(
                        latest_message=str(item.get("latest_message") or item.get("message_text") or ""),
                        message_window=[
                            dict(message)
                            for message in list(item.get("message_window") or [])
                            if isinstance(message, dict)
                        ],
                    )
                    if status == "replied"
                    else ""
                ),
                "action_items": action_items,
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
        "external_action_items": 0,
        "action_items": [],
        "by_company": {},
    }
    for item in reconcile_results:
        if str(item.get("action") or "") == "missing_contact":
            summary["missing_contacts"] = int(summary["missing_contacts"]) + 1
        for action in extract_linkedin_conversation_action_items(item):
            if isinstance(action, dict):
                summary["external_action_items"] = int(summary["external_action_items"]) + 1
                summary["action_items"].append(action)
    by_company: dict[str, int] = {}
    for draft in drafts:
        if str(draft.get("draft_kind") or "") == "accepted_follow_up":
            summary["follow_up_candidates"] = int(summary["follow_up_candidates"]) + 1
        else:
            summary["reply_candidates"] = int(summary["reply_candidates"]) + 1
        if str(draft.get("send_recommendation") or "") == "optional":
            summary["optional_closes"] = int(summary["optional_closes"]) + 1
        for action in draft.get("action_items") or []:
            if isinstance(action, dict):
                key = (action.get("action_type", ""), action.get("contact_name", ""), action.get("source_message", ""))
                existing_keys = {
                    (
                        existing.get("action_type", ""),
                        existing.get("contact_name", ""),
                        existing.get("source_message", ""),
                    )
                    for existing in summary["action_items"]
                    if isinstance(existing, dict)
                }
                if key not in existing_keys:
                    summary["external_action_items"] = int(summary["external_action_items"]) + 1
                    summary["action_items"].append(action)
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


def _followup_pending_review_path(settings: OutreachSettings) -> Path:
    return settings.resolved_tracking_workspace_dir / "linkedin_followup_pending_review.json"


def _followup_pending_key(draft: dict) -> str:
    return "|".join(
        [
            str(draft.get("contact_id") or ""),
            str(draft.get("thread_id") or ""),
            str(draft.get("draft_kind") or ""),
            str(draft.get("latest_message") or ""),
        ]
    )


def update_linkedin_followup_pending_review(
    *,
    settings: OutreachSettings,
    pending_drafts: list[dict],
    cleared_drafts: list[dict],
    source_artifact: Path,
) -> Path:
    path = _followup_pending_review_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {"results": []}
    existing = {
        _followup_pending_key(item): item
        for item in list(payload.get("results") or [])
        if isinstance(item, dict)
    }
    for draft in cleared_drafts:
        existing.pop(_followup_pending_key(draft), None)
    for draft in pending_drafts:
        existing[_followup_pending_key(draft)] = draft
    results = list(existing.values())
    summary: dict[str, int] = {}
    for draft in results:
        recommendation = str(draft.get("send_recommendation") or "unknown")
        summary[recommendation] = summary.get(recommendation, 0) + 1
    path.write_text(
        json.dumps(
            {
                "updated_at": utc_now_iso(),
                "source_artifact": str(source_artifact),
                "count": len(results),
                "summary": summary,
                "results": results,
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


COMMUNICATION_REVIEW_CSV_FIELDS = [
    "row_id",
    "review_artifact",
    "source_artifact",
    "artifact_row_index",
    "channel",
    "company",
    "name",
    "title",
    "email",
    "recipient_type",
    "target_role_family",
    "target_role_label",
    "target_role_source",
    "target_role_matched_text",
    "target_role_matched_rule",
    "target_role_is_concrete",
    "contact_id",
    "organization_id",
    "draft_kind",
    "reply_intent",
    "subject",
    "message",
    "latest_message",
    "message_window",
    "score",
    "verdict",
    "recommendation",
    "send_recommendation",
    "quality_labels",
    "flags",
    "strengths",
    "rewrite_guidance",
    "suggested_message",
    "user_decision",
    "user_reason",
    "user_edit",
    "user_notes",
]

COMMUNICATION_FEEDBACK_FIELDS = ["imported_at", "feedback_source", *COMMUNICATION_REVIEW_CSV_FIELDS]


def communication_feedback_path(workspace: Path) -> Path:
    return workspace / "communication_feedback.csv"


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return " || ".join(str(item).strip() for item in value if str(item).strip())
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    return str(value)


def _list_value(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split("||") if item.strip()]
    return []


def _communication_review_dict(draft: dict) -> dict:
    raw_review = draft.get("communication_review") or draft.get("craft_review") or {}
    return raw_review if isinstance(raw_review, dict) else {}


def _communication_review_row_id(row: dict[str, str]) -> str:
    base = "|".join(
        [
            row.get("review_artifact", ""),
            row.get("source_artifact", ""),
            row.get("contact_id", ""),
            row.get("organization_id", ""),
            row.get("channel", ""),
            row.get("target_role_family", ""),
            row.get("draft_kind", ""),
            row.get("subject", ""),
            row.get("message", "")[:240],
            row.get("latest_message", "")[:160],
        ]
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _simple_story_reference(draft: dict) -> str:
    company = str(draft.get("company") or "there")
    message = str(draft.get("message") or draft.get("draft_message") or "")
    latest = str(draft.get("latest_message") or "")
    text = " ".join([company, message, latest]).lower()
    if "recruit" in text or "interview" in text or "tractian" in text:
        return "my recruiting/workflow systems work"
    if "snyk" in text or "security" in text or "developer" in text:
        return "my backend/data engineering background"
    if "data" in text or "platform" in text or "infra" in text:
        return "my data/platform engineering background"
    return "my engineering + MBA background"


def suggest_linkedin_followup_message(draft: dict, *, flags: list[str]) -> str:
    company = str(draft.get("company") or "there").strip()
    name = first_name(str(draft.get("name") or "there"))
    draft_kind = str(draft.get("draft_kind") or "")
    reply_intent = str(draft.get("reply_intent") or "")
    title = str(draft.get("title") or "")
    flag_text = "\n".join(flags).lower()
    original = str(draft.get("draft_message") or draft.get("message") or "")
    context_line = ""
    if "my reason for looking at" in original.lower():
        match = re.search(r"(My reason for looking at .*?\.)", original)
        if match:
            context_line = " " + match.group(1)
    elif "tessera" in company.lower() or "generic company insight" in flag_text:
        context_line = ""

    if draft_kind == "already_asked_wait" or reply_intent == "already_asked_wait":
        return (
            f"HOLD - {name} already acknowledged the ask. Follow up only when there is a specific "
            f"{company} PM/product fit to share."
        )
    if reply_intent == "does_not_know":
        return (
            f"Sure, thanks {name}. Is there a PM/product internship path at {company}? Any recs on who I should "
            "talk to about that?"
        )
    if "generic company insight" in flag_text:
        return (
            f"Thanks for connecting, {name}. I'm exploring product roles where my engineering + MBA background "
            f"can be useful.{context_line} Does that background fit anything useful at {company}? Any recs on who "
            "I should talk to about that?"
        )
    if "seniority mismatch" in flag_text or any(
        token in title.lower()
        for token in ["principal", "staff", "director", "head of", "vp ", "vice president", "chief", "cto"]
    ):
        return (
            f"Thanks for connecting, {name}. I'm exploring product roles at {company} from "
            f"{_simple_story_reference(draft)}. Does that background fit product work there? Any recs on who I "
            "should talk to about that?"
        )
    if "generic fit framing" in flag_text:
        return (
            f"Thanks {name}. I don't want to repeat myself here, so I'll hold off unless I find a specific "
            f"{company} PM/product role that fits."
        )
    return ""


def suggest_communication_message(draft: dict, *, channel: str, flags: list[str]) -> str:
    if channel == "linkedin_followup":
        suggestion = suggest_linkedin_followup_message(draft, flags=flags)
        target_role = target_role_context_from_family(
            str(draft.get("target_role_family") or "product_pm"),
            source=str(draft.get("target_role_source") or "review_artifact"),
            matched_text=str(draft.get("target_role_matched_text") or ""),
        )
        return rewrite_message_for_target_role(suggestion, target_role)
    return ""


def build_communication_review_csv_rows(
    *,
    payload: dict,
    review_artifact: Path,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    source_artifact = str(payload.get("source_artifact") or "")
    for index, draft in enumerate(list(payload.get("results") or []), start=1):
        if not isinstance(draft, dict):
            continue
        review = _communication_review_dict(draft)
        channel = str(review.get("channel") or ("email" if draft.get("body") else "linkedin_followup"))
        recipient_type = str(
            draft.get("recipient_type")
            or draft.get("followup_audience")
            or draft.get("contact_type")
            or "general"
        )
        flags = _list_value(review.get("flags") or [])
        strengths = _list_value(review.get("strengths") or [])
        rewrite_guidance = _list_value(review.get("rewrite_guidance") or [])
        if not rewrite_guidance and flags:
            rewrite_guidance = build_rewrite_guidance(
                flags=flags,
                channel=channel,
                recipient_type=recipient_type,
                recipient_title=str(draft.get("title") or ""),
            )
        quality_labels = _list_value(review.get("quality_labels") or [])
        if not quality_labels:
            quality_labels = classify_quality_labels(
                flags=flags,
                strengths=strengths,
                channel=channel,
                recommended_action_hint=str(review.get("recommended_action") or ""),
            )
        message = str(draft.get("body") or draft.get("draft_message") or "")
        suggested_message = suggest_communication_message(draft, channel=channel, flags=flags)
        row = {
            "row_id": "",
            "review_artifact": str(review_artifact),
            "source_artifact": source_artifact,
            "artifact_row_index": str(index),
            "channel": channel,
            "company": str(draft.get("company") or ""),
            "name": str(draft.get("name") or ""),
            "title": str(draft.get("title") or ""),
            "email": str(draft.get("email") or ""),
            "recipient_type": recipient_type,
            "target_role_family": str(draft.get("target_role_family") or "product_pm"),
            "target_role_label": str(draft.get("target_role_label") or "Product / PM"),
            "target_role_source": str(draft.get("target_role_source") or "product_primary_default"),
            "target_role_matched_text": str(draft.get("target_role_matched_text") or ""),
            "target_role_matched_rule": str(draft.get("target_role_matched_rule") or ""),
            "target_role_is_concrete": str(draft.get("target_role_is_concrete", "")),
            "contact_id": str(draft.get("contact_id") or ""),
            "organization_id": str(draft.get("organization_id") or ""),
            "draft_kind": str(draft.get("draft_kind") or ""),
            "reply_intent": str(draft.get("reply_intent") or ""),
            "subject": str(draft.get("subject") or ""),
            "message": message,
            "latest_message": str(draft.get("latest_message") or ""),
            "message_window": _csv_cell(draft.get("message_window") or []),
            "score": str(review.get("score") or ""),
            "verdict": str(review.get("verdict") or ""),
            "recommendation": str(draft.get("communication_recommendation") or review.get("recommended_action") or ""),
            "send_recommendation": str(draft.get("send_recommendation") or ""),
            "quality_labels": _csv_cell(quality_labels),
            "flags": _csv_cell(flags),
            "strengths": _csv_cell(strengths),
            "rewrite_guidance": _csv_cell(rewrite_guidance),
            "suggested_message": suggested_message,
            "user_decision": "",
            "user_reason": "",
            "user_edit": "",
            "user_notes": "",
        }
        row["row_id"] = _communication_review_row_id(row)
        rows.append(row)
    return rows


def write_communication_review_csv(
    *,
    payload: dict,
    review_artifact: Path,
    output_path: Path | None = None,
) -> Path:
    rows = build_communication_review_csv_rows(payload=payload, review_artifact=review_artifact)
    path = output_path or review_artifact.with_suffix(".csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMMUNICATION_REVIEW_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in COMMUNICATION_REVIEW_CSV_FIELDS})
    return path


def communication_feedback_is_marked(row: dict[str, str]) -> bool:
    return any(
        str(row.get(field) or "").strip()
        for field in ["user_decision", "user_reason", "user_edit", "user_notes"]
    )


def _communication_feedback_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        str(row.get("row_id") or ""),
        str(row.get("user_decision") or "").strip().lower(),
        normalize_dedupe_text(str(row.get("user_reason") or "")),
        normalize_dedupe_text(str(row.get("user_edit") or "")),
    )


def read_communication_feedback_csv(feedback_path: Path) -> list[dict[str, str]]:
    with feedback_path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def import_communication_feedback_rows(
    *,
    workspace: Path,
    feedback_path: Path,
    execute: bool = False,
) -> dict[str, object]:
    source_rows = read_communication_feedback_csv(feedback_path)
    marked_rows = [row for row in source_rows if communication_feedback_is_marked(row)]
    destination = communication_feedback_path(workspace)
    existing_rows: list[dict[str, str]] = []
    if destination.exists():
        existing_rows = read_communication_feedback_csv(destination)
    existing_keys = {_communication_feedback_key(row) for row in existing_rows}
    imported_at = utc_now_iso()
    new_rows: list[dict[str, str]] = []
    skipped_duplicates = 0
    for row in marked_rows:
        normalized = {field: str(row.get(field) or "") for field in COMMUNICATION_REVIEW_CSV_FIELDS}
        if not normalized["row_id"]:
            normalized["row_id"] = _communication_review_row_id(normalized)
        feedback_row = {
            "imported_at": imported_at,
            "feedback_source": str(feedback_path),
            **normalized,
        }
        key = _communication_feedback_key(feedback_row)
        if key in existing_keys:
            skipped_duplicates += 1
            continue
        existing_keys.add(key)
        new_rows.append(feedback_row)

    if execute and new_rows:
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_header = not destination.exists()
        with destination.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COMMUNICATION_FEEDBACK_FIELDS)
            if write_header:
                writer.writeheader()
            for row in new_rows:
                writer.writerow({field: row.get(field, "") for field in COMMUNICATION_FEEDBACK_FIELDS})

    summary = summarize_communication_feedback(existing_rows + new_rows)
    return {
        "feedback_path": str(feedback_path),
        "destination": str(destination),
        "execute": execute,
        "source_rows": len(source_rows),
        "marked_rows": len(marked_rows),
        "new_rows": len(new_rows),
        "skipped_duplicates": skipped_duplicates,
        "summary": summary,
        "preview_rows": new_rows[:20],
    }


def summarize_communication_feedback(rows: list[dict[str, str]]) -> dict[str, object]:
    decision_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    label_counts: dict[str, int] = {}
    examples: list[dict[str, str]] = []
    for row in rows:
        decision = str(row.get("user_decision") or "").strip().lower() or "unmarked"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        for reason in re.split(r"[;,|]+", str(row.get("user_reason") or "")):
            clean = reason.strip().lower()
            if clean:
                reason_counts[clean] = reason_counts.get(clean, 0) + 1
        for label in str(row.get("quality_labels") or "").split("||"):
            clean = label.strip().lower()
            if clean:
                label_counts[clean] = label_counts.get(clean, 0) + 1
        if len(examples) < 8 and (row.get("user_edit") or row.get("user_notes")):
            examples.append(
                {
                    "company": str(row.get("company") or ""),
                    "name": str(row.get("name") or ""),
                    "decision": decision,
                    "reason": str(row.get("user_reason") or ""),
                    "original": str(row.get("message") or "")[:500],
                    "user_edit": str(row.get("user_edit") or "")[:500],
                    "notes": str(row.get("user_notes") or "")[:500],
                }
            )
    return {
        "rows": len(rows),
        "decision_counts": dict(sorted(decision_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        "quality_label_counts": dict(sorted(label_counts.items(), key=lambda item: (-item[1], item[0]))),
        "examples": examples,
    }


app = typer.Typer(help="Outreach engine CLI")
register_intelligence_commands(app)

# These are execution labels, not review verdicts. ``safe_to_review`` remains
# for backwards-compatible accepted-connection drafts; new inbound automation
# uses the unambiguous ``auto_send`` label.
SAFE_FOLLOWUP_SEND_RECOMMENDATIONS = {"auto_send", "safe_to_review"}

# Contact mapping is a breadth-building maintenance phase, not a full campaign
# search.  Re-running every affinity and alumni pass for each of 15 companies
# can turn a nightly run into several hours of redundant browser work.  These
# passes preserve existing, product, technical-referral, and broad coverage;
# invite campaigns still use the complete company search plan.
TRACK_2_MAPPING_PASSES = [
    "existing_connections",
    "product_network",
    "engineering_network",
    "broad_fallback",
]


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

    tier_counts = {"A": 0, "B": 0, "C": 0, "L1": 0, "L2": 0, "L3": 0}
    for r in rows:
        tier_counts[r.tier] = tier_counts.get(r.tier, 0) + 1

    typer.echo(f"Scored {len(rows)} companies")
    typer.echo(
        f"  Relationship A: {tier_counts['A']}  |  Relationship B: {tier_counts['B']}  |  "
        f"Large L1: {tier_counts['L1']}  |  Large L2: {tier_counts['L2']}  |  "
        f"Other/C: {tier_counts['C'] + tier_counts['L3']}"
    )
    typer.echo(f"Output: {path}")


@app.command("build-account-campaign-plan")
def build_account_campaign_plan(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    limit: Annotated[int, typer.Option(help="Maximum campaign actions to include")] = 30,
) -> None:
    """Build executable next actions for relationship and large-company account campaigns."""
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
            f"channel={row.campaign_channel} | lane1={row.lane_1_policy} | "
            f"daily_priority={row.daily_action_priority} | base_priority={row.campaign_priority}"
        )
        typer.echo(f"  {row.campaign_reason}")


@app.command("audit-track-2-core")
def audit_track_2_core_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    issue_limit: Annotated[int, typer.Option(help="Maximum issues to print")] = 20,
) -> None:
    """Audit whether Track 2 accounts have coherent actions, channels, and routing."""
    from outreach.account_tracker import audit_track_2_core, build_account_rows

    rows = build_account_rows(workspace)
    audit = audit_track_2_core(rows)
    artifact = write_artifact(
        OutreachSettings().artifacts_dir,
        "track-2-core-audit",
        {
            "workspace": str(workspace),
            **audit,
        },
    )
    typer.echo(
        f"Audited {audit['total_accounts']} accounts "
        f"({audit['priority_accounts']} priority accounts)."
    )
    typer.echo(f"Actions: {audit['action_counts']}")
    typer.echo(f"Channels: {audit['channel_counts']}")
    typer.echo(f"Issues: {audit['issue_counts']}")
    typer.echo(f"Clean: {audit['is_clean']}")
    typer.echo(f"Artifact: {artifact}")
    for issue in list(audit["issues"])[:issue_limit]:
        typer.echo(
            f"- {issue['company']} | tier={issue['tier']} | "
            f"action={issue['campaign_action']} | channel={issue['campaign_channel']} | "
            f"{issue['code']}: {issue['detail']}"
        )


@app.command("build-track-2-daily-plan")
def build_track_2_daily_plan_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    max_total_actions: Annotated[int, typer.Option(help="Maximum total Track 2 actions today")] = 24,
    max_companies: Annotated[int, typer.Option(help="Maximum distinct companies to touch today")] = 18,
    max_linkedin_invites: Annotated[int, typer.Option(help="Maximum new LinkedIn invites today")] = 12,
    max_linkedin_followups: Annotated[int, typer.Option(help="Maximum LinkedIn follow-up/reply messages today")] = 8,
    max_company_mapping: Annotated[int, typer.Option(help="Maximum companies to map contacts for today")] = 5,
    max_email_research: Annotated[int, typer.Option(help="Maximum email/contact-info research tasks today")] = 5,
    max_context_enrichment: Annotated[int, typer.Option(help="Maximum company enrichment tasks today")] = 8,
    max_email_drafts: Annotated[int, typer.Option(help="Maximum cold email drafts today; keep 0 until email engine is ready")] = 0,
) -> None:
    """Build a bounded review-only daily Track 2 operating plan."""
    from outreach.account_tracker import DailyPlanBudget, build_account_rows, build_track_2_daily_plan

    budget = DailyPlanBudget(
        max_total_actions=max_total_actions,
        max_companies=max_companies,
        max_linkedin_invites=max_linkedin_invites,
        max_linkedin_followups=max_linkedin_followups,
        max_company_mapping=max_company_mapping,
        max_email_research=max_email_research,
        max_context_enrichment=max_context_enrichment,
        max_email_drafts=max_email_drafts,
    )
    plan = build_track_2_daily_plan(build_account_rows(workspace), budget=budget)
    artifact = write_artifact(
        OutreachSettings().artifacts_dir,
        "track-2-daily-plan",
        {
            "workspace": str(workspace),
            **plan,
        },
    )
    typer.echo(f"Built Track 2 daily plan with {plan['selected_count']} selected actions.")
    typer.echo(f"Budget: {plan['budget']}")
    typer.echo(f"Used: {plan['used']}")
    typer.echo(f"Summary: {plan['summary']}")
    typer.echo(f"Phases: {plan['phase_summary']}")
    typer.echo(f"Skipped: {plan['skipped_count']}")
    typer.echo(f"Artifact: {artifact}")
    for item in list(plan["selected"])[:15]:
        typer.echo(
            f"- {item['phase']} | {item['company']} | action={item['campaign_action']} | "
            f"channel={item['campaign_channel']} | invites={item['expected_linkedin_invites']} | "
            f"followups={item['expected_linkedin_followups']} | email_research={item['expected_email_research']}"
        )


def build_linkedin_contact_info_email_queue(
    *,
    workspace: Path,
    daily_plan: dict,
    limit: int,
) -> list[dict[str, str]]:
    workbook = OutreachWorkbook(workspace)
    contacts = workbook.list_contacts()
    organizations = {organization.organization_id: organization for organization in workbook.list_organizations()}
    contacts_by_org: dict[str, list[ContactRecord]] = {}
    for contact in contacts:
        contacts_by_org.setdefault(contact.organization_id, []).append(contact)

    selected_orgs = [
        str(item.get("organization_id") or "")
        for item in daily_plan.get("selected", [])
        if int(item.get("expected_email_research") or 0) > 0
    ]
    queue: list[dict[str, str]] = []
    seen_contacts: set[str] = set()
    for org_id in selected_orgs:
        organization = organizations.get(org_id)
        for contact in contacts_by_org.get(org_id, []):
            if contact.contact_id in seen_contacts:
                continue
            if contact.email.strip() or not contact.linkedin_url.strip():
                continue
            queue.append(
                {
                    "contact_id": contact.contact_id,
                    "organization_id": contact.organization_id,
                    "company": organization.name if organization else "",
                    "name": contact.full_name,
                    "title": contact.title,
                    "linkedin_url": contact.linkedin_url,
                    "company_website": organization.website if organization else "",
                    "company_linkedin_url": organization.linkedin_url if organization else "",
                }
            )
            seen_contacts.add(contact.contact_id)
            if len(queue) >= limit:
                return queue
    return queue


def build_external_email_research_queue(
    *,
    workspace: Path,
    daily_plan: dict,
    limit: int,
    exclude_contact_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    workbook = OutreachWorkbook(workspace)
    contacts = workbook.list_contacts()
    organizations = {organization.organization_id: organization for organization in workbook.list_organizations()}
    contacts_by_org: dict[str, list[ContactRecord]] = {}
    for contact in contacts:
        contacts_by_org.setdefault(contact.organization_id, []).append(contact)

    excluded = exclude_contact_ids or set()
    selected_orgs = [
        str(item.get("organization_id") or "")
        for item in daily_plan.get("selected", [])
        if int(item.get("expected_email_research") or 0) > 0
    ]
    queue: list[dict[str, str]] = []
    seen_contacts: set[str] = set()
    for org_id in selected_orgs:
        organization = organizations.get(org_id)
        if organization is None:
            continue
        for contact in sorted(contacts_by_org.get(org_id, []), key=_email_contact_rank):
            if contact.contact_id in seen_contacts or contact.contact_id in excluded:
                continue
            if contact.email.strip():
                continue
            has_provider_input = bool(
                contact.linkedin_url.strip()
                or (
                    contact.full_name.strip()
                    and (organization.website.strip() or organization.name.strip())
                )
            )
            if not has_provider_input:
                continue
            queue.append(
                {
                    "contact_id": contact.contact_id,
                    "organization_id": contact.organization_id,
                    "company": organization.name,
                    "name": contact.full_name,
                    "title": contact.title,
                    "linkedin_url": contact.linkedin_url,
                    "company_website": organization.website,
                    "company_linkedin_url": organization.linkedin_url,
                }
            )
            seen_contacts.add(contact.contact_id)
            if len(queue) >= limit:
                return queue
    return queue


def apply_email_finder_results(
    *,
    workbook: OutreachWorkbook,
    results: list[EmailFinderResult],
    min_confidence: int,
) -> int:
    updated = 0
    for result in results:
        if not result.is_sendable(min_confidence=min_confidence):
            continue
        contact = workbook.update_contact(
            result.contact_id,
            email=result.email,
            notes=_append_note_marker(
                _contact_notes_for_id(workbook, result.contact_id),
                (
                    f"external_email_found={utc_now_iso()};provider={result.provider};"
                    f"confidence={result.confidence or 'unknown'}"
                ),
            ),
        )
        if contact is not None:
            updated += 1
    return updated


@app.command("research-linkedin-contact-info-emails")
def research_linkedin_contact_info_emails_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    daily_plan_artifact: Annotated[
        Path | None,
        typer.Option(help="Optional track-2-daily-plan artifact; if omitted, build a fresh plan"),
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum LinkedIn Contact Info profiles to inspect")] = 10,
    start_at: Annotated[int, typer.Option(help="Start offset into the candidate queue")] = 0,
    execute: Annotated[
        bool,
        typer.Option(help="Write discovered emails back to contacts.csv"),
    ] = False,
) -> None:
    """Inspect LinkedIn Contact Info overlays for emails listed by mapped contacts."""
    from outreach.account_tracker import build_account_rows, build_track_2_daily_plan

    settings = OutreachSettings()
    if daily_plan_artifact:
        daily_plan = json.loads(daily_plan_artifact.read_text(encoding="utf-8"))
    else:
        daily_plan = build_track_2_daily_plan(build_account_rows(workspace))

    queue = build_linkedin_contact_info_email_queue(
        workspace=workspace,
        daily_plan=daily_plan,
        limit=limit + start_at,
    )
    batch = queue[start_at : start_at + limit]
    if not batch:
        artifact = write_artifact(
            settings.artifacts_dir,
            "linkedin-contact-info-email-research",
            {
                "workspace": str(workspace),
                "execute": execute,
                "count": 0,
                "results": [],
                "detail": "No mapped LinkedIn contacts without email were available for selected email-research accounts.",
            },
        )
        typer.echo("No mapped LinkedIn contacts without email were available for selected email-research accounts.")
        typer.echo("For story-fit accounts with zero contacts, run contact mapping first.")
        typer.echo(f"Artifact: {artifact}")
        return

    results = LinkedInScraper(settings).extract_contact_info_emails(batch, limit=limit, start_at=0)
    workbook = OutreachWorkbook(workspace)
    updated = 0
    if execute:
        for result in results:
            if result.status != "found" or not result.email:
                continue
            contact = workbook.update_contact(
                result.contact_id,
                email=result.email,
                notes=_append_note_marker(
                    _contact_notes_for_id(workbook, result.contact_id),
                    f"linkedin_contact_info_email_found={utc_now_iso()}",
                ),
            )
            if contact is not None:
                updated += 1

    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-contact-info-email-research",
        {
            "workspace": str(workspace),
            "execute": execute,
            "count": len(results),
            "updated": updated,
            "results": [result.__dict__ for result in results],
        },
    )
    typer.echo(f"Inspected {len(results)} LinkedIn Contact Info profiles.")
    typer.echo(f"Found emails: {sum(1 for result in results if result.status == 'found')}")
    typer.echo(f"Updated contacts: {updated}")
    typer.echo(f"Artifact: {artifact}")
    for result in results[: min(10, len(results))]:
        shown_email = result.email if result.email else "-"
        typer.echo(f"- {result.name} | {result.status} | {shown_email} | {result.detail}")


@app.command("research-external-contact-emails")
def research_external_contact_emails_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    daily_plan_artifact: Annotated[
        Path | None,
        typer.Option(help="Optional track-2-daily-plan artifact; if omitted, build a fresh plan"),
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum external email-finder lookups")] = 5,
    start_at: Annotated[int, typer.Option(help="Start offset into the candidate queue")] = 0,
    provider: Annotated[str, typer.Option(help="Email finder provider: auto, prospeo, or hunter")] = "auto",
    execute: Annotated[
        bool,
        typer.Option(help="Call the external provider and write accepted emails back to contacts.csv"),
    ] = False,
) -> None:
    """Find contact emails via a configured external service after LinkedIn Contact Info is insufficient."""
    from outreach.account_tracker import build_account_rows, build_track_2_daily_plan

    settings = OutreachSettings()
    if daily_plan_artifact:
        daily_plan = json.loads(daily_plan_artifact.read_text(encoding="utf-8"))
    else:
        daily_plan = build_track_2_daily_plan(build_account_rows(workspace))

    queue = build_external_email_research_queue(
        workspace=workspace,
        daily_plan=daily_plan,
        limit=limit + start_at,
    )
    batch = queue[start_at : start_at + limit]
    candidates = [EmailResearchCandidate.from_dict(item) for item in batch]
    results: list[EmailFinderResult] = []
    updated = 0
    if execute and candidates:
        service = build_email_finder_service(settings, provider=provider)
        results = service.find_many(candidates, limit=limit)
        updated = apply_email_finder_results(
            workbook=OutreachWorkbook(workspace),
            results=results,
            min_confidence=settings.email_finder_min_confidence,
        )

    artifact = write_artifact(
        settings.artifacts_dir,
        "external-contact-email-research",
        {
            "workspace": str(workspace),
            "provider": provider,
            "execute": execute,
            "min_confidence": settings.email_finder_min_confidence,
            "queue_count": len(batch),
            "result_count": len(results),
            "updated": updated,
            "queue": [candidate.__dict__ for candidate in candidates],
            "results": [result.__dict__ for result in results],
            "detail": (
                "Preview only; rerun with --execute to call the configured provider."
                if not execute
                else ""
            ),
        },
    )
    typer.echo(f"{'Ran' if execute else 'Planned'} external email research.")
    typer.echo(f"Queued candidates: {len(batch)}")
    typer.echo(f"Provider: {provider}")
    if execute:
        typer.echo(f"Found emails: {sum(1 for result in results if result.status == 'found')}")
        typer.echo(f"Updated contacts: {updated}")
    typer.echo(f"Artifact: {artifact}")
    for result in results[: min(10, len(results))]:
        shown_email = result.email if result.email else "-"
        typer.echo(
            f"- {result.name} | {result.company} | {result.provider} | "
            f"{result.status} | {shown_email} | confidence={result.confidence or '-'}"
        )


def _contact_notes_for_id(workbook: OutreachWorkbook, contact_id: str) -> str:
    for contact in workbook.list_contacts():
        if contact.contact_id == contact_id:
            return contact.notes
    return ""


def _append_note_marker(notes: str, marker: str) -> str:
    if marker in notes:
        return notes
    return " | ".join(part for part in [notes.strip(), marker] if part)


def email_recipient_type(contact: ContactRecord) -> str:
    text = " ".join([contact.contact_type, contact.title]).lower()
    if any(token in text for token in ["founder", "co-founder", "cofounder", "ceo", "chief executive"]):
        return "founder"
    if any(token in text for token in ["recruiter", "talent", "university recruiting", "campus"]):
        return "recruiter"
    if any(token in text for token in ["apm", "associate product", "product intern"]):
        return "junior_product_apm"
    if any(token in text for token in ["product", "pm ", "product manager", "chief product"]):
        return "senior_product"
    if any(token in text for token in ["india", "bengaluru", "bangalore", "delhi", "gurgaon", "gurugram", "mumbai", "hyderabad", "pune", "chennai"]):
        return "engineer_india"
    if any(token in text for token in ["engineer", "engineering", "software", "developer", "architect"]):
        return "engineer"
    return "general"


def _email_contact_rank(contact: ContactRecord) -> tuple[int, str]:
    recipient_type = email_recipient_type(contact)
    rank = {
        "founder": 0,
        "senior_product": 1,
        "recruiter": 2,
        "engineer_india": 3,
        "engineer": 4,
        "junior_product_apm": 5,
        "general": 6,
    }.get(recipient_type, 6)
    return rank, contact.full_name.lower()


def _email_company_fit_line(organization: OrganizationRecord) -> str:
    story_line = email_story_fit_line(organization)
    if story_line:
        return story_line
    text = " ".join([organization.notes, organization.target_lists, organization.website]).lower()
    if any(token in text for token in ["hiring", "recruit", "talent", "interview"]):
        return "What caught me is the hiring/workflow problem: I'm building recruiting systems right now, so I have fresh scar tissue around where these tools help and where they become theater."
    if any(token in text for token in ["voice ai", "avatar", "video", "conversation"]):
        return "What caught me is the voice/video AI angle; it sits close to the interview and workflow products I'm working around now."
    if any(token in text for token in ["data", "etl", "pipeline", "observability", "developer", "platform", "api"]):
        return "The data/platform side maps cleanly to my Hevo, Gojek, and Intuit engineering background."
    if any(token in text for token in ["marketplace", "logistics", "fleet", "delivery", "mobility"]):
        return "The marketplace/ops side connects with my Gojek experience, especially the messy part where product decisions show up as operational constraints."
    if any(token in text for token in ["fintech", "payments", "billing", "smb finance", "tax"]):
        return "The fintech/SMB workflow side connects with my Intuit experience: systems where the product only works if the operational detail is right."
    if any(token in text for token in ["health", "provider", "clinical", "care", "interoperability"]):
        return "The health workflow side connects with my Optum/provider systems experience, where workflow complexity matters as much as the software."
    if any(token in text for token in ["ai", "agent", "automation", "workflow"]):
        return "The applied AI/workflow angle is close to the product systems I'm building now, especially where AI has to turn messy work into a clearer decision."
    return "The company looks close to the technical product path I'm trying to build toward, not just a generic PM target."


def draft_track_2_email(
    *,
    organization: OrganizationRecord,
    contact: ContactRecord,
    campaign_action: str,
    style_profile: CommunicationStyleProfile,
    cadence_action: str = "email_initial",
    target_role: TargetRoleContext | None = None,
) -> dict[str, object]:
    name = first_name(contact.full_name)
    recipient_type = email_recipient_type(contact)
    fit_line = _email_company_fit_line(organization)
    company = organization.name
    effective_target = target_role or infer_target_role_context(
        organization_notes=organization.notes
    )
    intro = "I'm a Marshall MBA and former data/platform engineer exploring technical product paths."
    if cadence_action == "email_followup_1":
        subject = f"Re: Product fit at {company}"
        body = (
            f"Hi {name},\n\n"
            f"Following up with the specific reason I still think the fit may be real: {fit_line}\n\n"
            "If there is a better person or path for this background, a pointer would be genuinely useful. "
            "If it is not relevant, no worries—I would rather know than keep nudging.\n\n"
            "Best,\nAkshat"
        )
    elif cadence_action == "email_final_optional":
        subject = f"Re: Product fit at {company}"
        body = (
            f"Hi {name},\n\n"
            "One last note to close the loop. I am still interested because the technical/product overlap is specific, "
            f"not just because {company} is hiring. {fit_line}\n\n"
            "If there is someone I should speak with, I would appreciate the pointer. Otherwise I will leave it here.\n\n"
            "Best,\nAkshat"
        )
    elif recipient_type == "founder":
        subject = f"Product fit at {company}"
        body = (
            f"Hi {name},\n\n"
            "I know cold emails from MBA candidates usually blur together, so I'll make the reason specific.\n\n"
            f"{intro} {fit_line}\n\n"
            f"The thing I'm trying to test is whether {company} has a product or internship path where "
            "that mix is actually useful, or whether I'm forcing the fit. If it is directionally relevant, "
            "any recs on who I should talk to about that? If not, a blunt no is genuinely useful too.\n\n"
            "Best,\nAkshat"
        )
    elif recipient_type == "senior_product":
        subject = f"Technical PM fit at {company}"
        body = (
            f"Hi {name},\n\n"
            "I am not trying to send a generic company-praise note, so here is the actual reason I am reaching out.\n\n"
            f"{intro} {fit_line}\n\n"
            f"I'm trying to understand whether that profile is useful for product work at {company}. If yes, "
            "any recs on who I should talk to about that? If not, a blunt no is helpful.\n\n"
            "Best,\nAkshat"
        )
    elif recipient_type == "recruiter":
        subject = f"PM/product path at {company}"
        body = (
            f"Hi {name},\n\n"
            f"{intro} {fit_line}\n\n"
            f"I'm looking for PM/product, product ops, or strategy paths at {company}, but I don't want to route myself "
            "into the wrong process just because the title sounds close. Is there a specific hiring-team contact or "
            "screening path I should use for this background?\n\n"
            "Best,\nAkshat"
        )
    elif recipient_type in {"engineer_india", "engineer"}:
        subject = f"Referral path for {company}"
        body = (
            f"Hi {name},\n\n"
            "I know referral asks from strangers can be annoying, so I do not want to over-ask.\n\n"
            f"{intro} {fit_line}\n\n"
            f"If the fit looks directionally reasonable, would a short resume + 4-line blurb help you judge whether "
            "a referral or hiring-team pointer makes sense? If this is off-base, a quick no is totally fine.\n\n"
            "Best,\nAkshat"
        )
    else:
        subject = f"Product path at {company}"
        body = (
            f"Hi {name},\n\n"
            f"{intro} {fit_line}\n\n"
            "I'm trying to find the right person to speak with about technical PM/product paths without pretending "
            "I know the org from the outside. Would you suggest product, recruiting, or someone else as the best next contact?\n\n"
            "Best,\nAkshat"
        )

    subject = rewrite_message_for_target_role(subject, effective_target)
    body = rewrite_message_for_target_role(body, effective_target)
    guided_style = style_profile.guide_draft_from_examples(body, recipient_type)
    body = rewrite_message_for_target_role(guided_style.message, effective_target)
    review = style_profile.review_message(body, recipient_type)
    message_review = review_outreach_message(
        subject=subject,
        body=body,
            channel="email",
            company=company,
            recipient_type=recipient_type,
            recipient_title=contact.title,
            style_profile=style_profile,
    )
    craft_review = review_email_craft(subject, body, company=company, recipient_type=recipient_type)
    return {
        "organization_id": organization.organization_id,
        "company": company,
        "contact_id": contact.contact_id,
        "name": contact.full_name,
        "title": contact.title,
        "email": contact.email,
        "recipient_type": recipient_type,
        "target_role_family": effective_target.family.value,
        "target_role_label": effective_target.label,
        "target_role_source": effective_target.source,
        "target_role_matched_text": effective_target.matched_text,
        "target_role_matched_rule": effective_target.matched_rule,
        "target_role_is_concrete": effective_target.is_concrete,
        "target_role_context": effective_target.as_dict(),
        "campaign_action": campaign_action,
        "cadence_action": cadence_action,
        "subject": subject,
        "body": body,
        "body_length": len(body),
        "send_recommendation": (
            "review"
            if review.verdict == "style_ok" and message_review.verdict != "needs_rewrite"
            else "needs_rewrite"
        ),
        "style_review": review.model_dump(mode="json"),
        "style_guidance": guided_style.prompt_guidance,
        "style_example_labels": list(guided_style.strong_example_labels),
        "style_transformations": list(guided_style.transformations),
        "communication_review": message_review.__dict__,
        "craft_review": craft_review.__dict__,
    }


def build_track_2_email_drafts(
    *,
    workspace: Path,
    daily_plan: dict,
    limit: int,
    style_profile: CommunicationStyleProfile | None = None,
) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    workbook = OutreachWorkbook(workspace)
    organizations = {org.organization_id: org for org in workbook.list_organizations()}
    opportunities_by_org: dict[str, list[OpportunityRecord]] = {}
    for opportunity in workbook.list_opportunities():
        opportunities_by_org.setdefault(opportunity.organization_id, []).append(opportunity)
    for organization_opportunities in opportunities_by_org.values():
        organization_opportunities.sort(key=lambda item: item.discovered_at, reverse=True)
    contacts_by_org: dict[str, list[ContactRecord]] = {}
    for contact in workbook.list_contacts():
        if contact.email.strip():
            contacts_by_org.setdefault(contact.organization_id, []).append(contact)
    profile = style_profile or load_style_profile_if_exists(workspace / "communication_style_profile.yml")
    cadence_by_contact = {
        item.contact_id: item
        for item in build_workbook_cadence_plan(workbook)
        if item.channel == "email"
    }

    drafts: list[dict[str, object]] = []
    seen_contacts: set[str] = set()
    for item in list(daily_plan.get("selected") or []):
        if int(item.get("expected_email_drafts") or 0) <= 0:
            continue
        organization = organizations.get(str(item.get("organization_id") or ""))
        if organization is None:
            continue
        contacts = sorted(contacts_by_org.get(organization.organization_id, []), key=_email_contact_rank)
        for contact in contacts:
            if contact.contact_id in seen_contacts:
                continue
            cadence = cadence_by_contact.get(contact.contact_id)
            if cadence is None or cadence.state != "due" or not cadence.action.startswith("email_"):
                continue
            drafts.append(
                draft_track_2_email(
                    organization=organization,
                    contact=contact,
                    campaign_action=str(item.get("campaign_action") or ""),
                    style_profile=profile,
                    cadence_action=cadence.action,
                    target_role=infer_target_role_context(
                        explicit_family=str(item.get("target_role_family") or ""),
                        explicit_title=str(item.get("target_role_title") or item.get("target_role") or ""),
                        opportunity_titles=[
                            opportunity.title
                            for opportunity in opportunities_by_org.get(
                                organization.organization_id,
                                [],
                            )
                        ],
                        note_context={"target_roles": item.get("target_roles") or ""},
                        organization_notes=organization.notes,
                    ),
                )
            )
            seen_contacts.add(contact.contact_id)
            break
        if len(drafts) >= limit:
            return drafts
    return drafts


def daily_plan_items_by_phase(daily_plan: dict) -> dict[str, list[dict]]:
    phases: dict[str, list[dict]] = {}
    for item in list(daily_plan.get("selected") or []):
        phase = str(item.get("phase") or "9_other")
        phases.setdefault(phase, []).append(item)
    for items in phases.values():
        items.sort(
            key=lambda item: (
                int(item.get("phase_order") or 999),
                -int(item.get("daily_action_priority") or 0),
                str(item.get("company") or "").lower(),
            )
        )
    return dict(
        sorted(
            phases.items(),
            key=lambda item: (
                int(item[1][0].get("phase_order") or 999) if item[1] else 999,
                item[0],
            ),
        )
    )


def build_daily_execution_manifest(daily_plan: dict) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for phase, items in daily_plan_items_by_phase(daily_plan).items():
        action_counts: dict[str, int] = {}
        for item in items:
            action = str(item.get("campaign_action") or "unknown")
            action_counts[action] = action_counts.get(action, 0) + 1
        manifest.append(
            {
                "phase": phase,
                "phase_order": int(items[0].get("phase_order") or 999) if items else 999,
                "count": len(items),
                "parallelizable": bool(items[0].get("can_parallelize")) if items else False,
                "actions": action_counts,
                "companies": [str(item.get("company") or "") for item in items],
            }
        )
    return manifest


def _daily_plan_items_matching(daily_plan: dict, *, phase_prefix: str | None = None) -> list[dict]:
    items = list(daily_plan.get("selected") or [])
    if phase_prefix is None:
        return items
    return [item for item in items if str(item.get("phase") or "").startswith(phase_prefix)]


def _daily_plan_company_names(daily_plan: dict, *, phase_prefix: str | None = None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in _daily_plan_items_matching(daily_plan, phase_prefix=phase_prefix):
        name = str(item.get("company") or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _daily_plan_org_ids(daily_plan: dict, *, phase_prefixes: tuple[str, ...]) -> set[str]:
    org_ids: set[str] = set()
    for item in list(daily_plan.get("selected") or []):
        phase = str(item.get("phase") or "")
        if not any(phase.startswith(prefix) for prefix in phase_prefixes):
            continue
        org_id = str(item.get("organization_id") or "").strip()
        if org_id:
            org_ids.add(org_id)
    return org_ids


def _filter_reconcile_results_to_orgs(
    reconcile_results: list[dict],
    *,
    contacts: list[ContactRecord],
    organization_ids: set[str],
) -> list[dict]:
    if not organization_ids:
        return []
    contact_org = {contact.contact_id: contact.organization_id for contact in contacts}
    return [
        item
        for item in reconcile_results
        if contact_org.get(str(item.get("contact_id") or "")) in organization_ids
    ]


def _linkedin_reply_is_unanswered(
    touchpoints: list[TouchpointRecord],
    *,
    contact_id: str,
) -> tuple[bool, str]:
    history = sorted(
        [item for item in touchpoints if item.contact_id == contact_id],
        key=lambda item: item.recorded_at,
    )
    inbound = [
        item
        for item in history
        if (item.message_kind or "").strip().casefold()
        in {"linkedin_reply", "inbound_reply", "reply"}
    ]
    if not inbound:
        return False, "No tracker-backed inbound LinkedIn reply exists."
    latest_inbound = inbound[-1]
    outbound_after = [
        item
        for item in history
        if (item.status or "").strip().casefold() == "sent"
        and (item.channel or "").strip().casefold() == "linkedin"
        and (item.message_kind or "").strip().casefold()
        in {"linkedin_followup", "linkedin_message", "linkedin_manual_message"}
        and item.recorded_at > latest_inbound.recorded_at
    ]
    if outbound_after:
        return False, "A later outbound LinkedIn response is already recorded."
    return True, "Latest tracker-backed inbound LinkedIn reply is unanswered."


def _apply_linkedin_cadence_guards(
    *,
    workbook: OutreachWorkbook,
    drafts: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Separate tracker-due LinkedIn drafts from early/stopped/repetitive holds."""
    touchpoints = workbook.list_touchpoints()
    contacts = workbook.list_contacts()
    contact_by_id = {item.contact_id: item for item in contacts}
    current_style_profile = load_style_profile_if_exists(
        workbook.base_dir / "communication_style_profile.yml"
    )
    plan = build_workbook_cadence_plan(workbook)
    recommendation_by_contact = {
        item.contact_id: item for item in plan if item.channel == "linkedin"
    }
    allowed: list[dict[str, object]] = []
    held: list[dict[str, object]] = []
    for draft in drafts:
        contact_id = str(draft.get("contact_id") or "")
        organization_id = str(draft.get("organization_id") or "")
        recommendation = recommendation_by_contact.get(contact_id)
        communication_review = draft.get("communication_review")
        communication_flags = (
            communication_review.get("flags", [])
            if isinstance(communication_review, dict)
            else []
        )
        learned_negative = any(
            "learned negative" in str(flag).casefold()
            for flag in communication_flags
        )
        contact = contact_by_id.get(contact_id)
        recipient_type = str(
            draft.get("recipient_type")
            or draft.get("followup_audience")
            or draft.get("contact_type")
            or (email_recipient_type(contact) if contact is not None else "general")
        )
        current_weak_labels = current_style_profile.weak_example_matches(
            str(draft.get("draft_message") or ""),
            recipient_type,
        )
        if learned_negative or current_weak_labels:
            label_detail = (
                f" ({', '.join(current_weak_labels)})" if current_weak_labels else ""
            )
            held.append(
                {
                    **draft,
                    "send_recommendation": "cadence_hold",
                    "cadence_reasons": [
                        "Draft repeats a learned negative message pattern and requires a rewrite."
                        + label_detail
                    ],
                }
            )
            continue
        if str(draft.get("source_status") or "").casefold() == "replied":
            unanswered, reply_reason = _linkedin_reply_is_unanswered(
                touchpoints,
                contact_id=contact_id,
            )
            enriched_reply = {
                **draft,
                "cadence_action": "linkedin_reply",
                "cadence_state": "due" if unanswered else "held",
                "cadence_due_at": None,
                "cadence_due_by": None,
                "cadence_reasons": [reply_reason],
            }
            if unanswered:
                allowed.append(enriched_reply)
            else:
                enriched_reply["send_recommendation"] = "cadence_hold"
                held.append(enriched_reply)
            continue
        if recommendation is None:
            held.append(
                {
                    **draft,
                    "send_recommendation": "cadence_hold",
                    "cadence_reasons": ["No tracker-backed LinkedIn cadence decision exists."],
                }
            )
            continue
        guard = guard_cadence_action(
            touchpoints,
            organization_id=organization_id,
            contact_id=contact_id,
            channel="linkedin",
            action=recommendation.action,
            proposed_message=str(draft.get("draft_message") or ""),
            contacts=contacts,
        )
        enriched = {
            **draft,
            "cadence_action": recommendation.action,
            "cadence_state": recommendation.state,
            "cadence_due_at": recommendation.as_dict().get("due_at"),
            "cadence_due_by": recommendation.as_dict().get("due_by"),
            "cadence_reasons": list(guard.reasons),
        }
        if guard.allowed:
            allowed.append(enriched)
        else:
            enriched["send_recommendation"] = "cadence_hold"
            held.append(enriched)
    return allowed, held


def _company_mode_for_org(organization: OrganizationRecord) -> str:
    return infer_company_mode(
        organization.organization_type.value,
        extract_team_size_from_notes(organization.notes),
    )


def _build_daily_plan_for_workspace(
    *,
    workspace: Path,
    max_total_actions: int,
    max_companies: int,
    max_linkedin_invites: int,
    max_linkedin_followups: int,
    max_company_mapping: int,
    max_email_research: int,
    max_context_enrichment: int,
    max_email_drafts: int,
) -> dict:
    from outreach.account_tracker import DailyPlanBudget, build_account_rows, build_track_2_daily_plan

    budget = DailyPlanBudget(
        max_total_actions=max_total_actions,
        max_companies=max_companies,
        max_linkedin_invites=max_linkedin_invites,
        max_linkedin_followups=max_linkedin_followups,
        max_company_mapping=max_company_mapping,
        max_email_research=max_email_research,
        max_context_enrichment=max_context_enrichment,
        max_email_drafts=max_email_drafts,
    )
    return build_track_2_daily_plan(build_account_rows(workspace), budget=budget)


def _artifact_snapshot(artifacts_dir: Path) -> set[Path]:
    if not artifacts_dir.exists():
        return set()
    return set(artifacts_dir.glob("*.json"))


def _new_artifacts(before: set[Path], artifacts_dir: Path) -> list[str]:
    return [
        str(path)
        for path in sorted(_artifact_snapshot(artifacts_dir) - before, key=lambda item: item.name)
    ]


def run_external_stage(
    *,
    settings: OutreachSettings,
    label: str,
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
) -> dict[str, object]:
    started_at = utc_now_iso()
    if not cwd.exists():
        summary = {
            "label": label,
            "status": "skipped",
            "reason": f"working directory not found: {cwd}",
            "command": command,
            "cwd": str(cwd),
            "started_at": started_at,
            "finished_at": utc_now_iso(),
        }
    else:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            summary = {
                "label": label,
                "status": "ran" if result.returncode == 0 else "failed",
                "returncode": result.returncode,
                "command": command,
                "cwd": str(cwd),
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "stdout_tail": result.stdout[-12000:],
                "stderr_tail": result.stderr[-12000:],
            }
        except subprocess.TimeoutExpired as exc:
            summary = {
                "label": label,
                "status": "timeout",
                "returncode": None,
                "command": command,
                "cwd": str(cwd),
                "timeout_seconds": timeout_seconds,
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "stdout_tail": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
                "stderr_tail": (exc.stderr or "")[-12000:] if isinstance(exc.stderr, str) else "",
            }
    artifact = write_artifact(settings.artifacts_dir, label, summary)
    summary["artifact"] = str(artifact)
    return summary


def resume_generator_python(resume_generator_root: Path) -> str:
    venv_python = resume_generator_root / "venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return "python3"


def _load_first_artifact(paths: list[str], label: str) -> tuple[Path | None, dict]:
    for item in paths:
        path = Path(item)
        if label not in path.name or not path.exists():
            continue
        try:
            return path, json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return path, {}
    return None, {}


def write_supervised_e2e_report(
    *,
    settings: OutreachSettings,
    payload: dict[str, object],
    summary_artifact: Path,
) -> tuple[Path, Path, Path, Path]:
    track_stage = next(
        (
            stage
            for stage in list(payload.get("stages") or [])
            if isinstance(stage, dict) and stage.get("name") == "track_2_daily_run"
        ),
        {},
    )
    track_artifact, track_payload = _load_first_artifact(
        [str(item) for item in list(track_stage.get("artifacts") or [])],
        "track-2-daily-run",
    )
    plan_artifact_value = str(track_payload.get("plan_artifact") or "") if track_payload else ""
    plan_artifact = Path(plan_artifact_value) if plan_artifact_value else None
    plan_payload = {}
    if plan_artifact is not None and plan_artifact.exists() and plan_artifact.is_file():
        try:
            plan_payload = json.loads(plan_artifact.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            plan_payload = {}
    campaign_artifact = next(
        (
            str(stage.get("artifact") or "")
            for stage in list(payload.get("stages") or [])
            if isinstance(stage, dict) and stage.get("name") == "account_campaign_plan"
        ),
        "",
    )
    pending_review_items: list[dict] = []
    for phase in list(track_payload.get("phase_results") or []) if track_payload else []:
        if not isinstance(phase, dict):
            continue
        pending_path_value = str(phase.get("pending_review_artifact") or "")
        if not pending_path_value:
            continue
        pending_path = Path(pending_path_value)
        if not pending_path.exists() or not pending_path.is_file():
            continue
        try:
            pending_payload = json.loads(pending_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pending_payload = {}
        pending_review_items.extend(
            item for item in list(pending_payload.get("results") or []) if isinstance(item, dict)
        )

    company_status: dict[str, str] = {}
    for item in pending_review_items:
        company = str(item.get("company") or "")
        recommendation = str(item.get("send_recommendation") or "review")
        company_status[company] = "needs review" if recommendation != "hold" else "hold"
    for phase in list(track_payload.get("phase_results") or []) if track_payload else []:
        if not isinstance(phase, dict):
            continue
        if phase.get("queue"):
            for queued in list(phase.get("queue") or []):
                if isinstance(queued, dict):
                    company_status.setdefault(str(queued.get("company") or ""), str(phase.get("status") or "queued"))
        if phase.get("companies"):
            for company in list(phase.get("companies") or []):
                company_status.setdefault(str(company), str(phase.get("status") or "queued"))
        for run in list(phase.get("runs") or []):
            if not isinstance(run, dict):
                continue
            company = str(run.get("company") or "")
            if run.get("sent"):
                status_counts = run.get("status_counts") or {}
                sent_count = int(status_counts.get("sent") or 0) if isinstance(status_counts, dict) else 0
                already = int(status_counts.get("already_connected") or 0) if isinstance(status_counts, dict) else 0
                bits = []
                if sent_count:
                    bits.append(f"{sent_count} invite(s) sent")
                if already:
                    bits.append(f"{already} already connected/pending")
                company_status[company] = ", ".join(bits) or "processed"
            else:
                company_status.setdefault(company, str(phase.get("status") or "ran"))

    selected_by_tier_phase: dict[tuple[str, str], list[dict]] = {}
    for item in list(plan_payload.get("selected") or []):
        if not isinstance(item, dict):
            continue
        tier = str(item.get("tier") or "Unscored")
        phase = str(item.get("phase") or "Other")
        selected_by_tier_phase.setdefault((tier, phase), []).append(item)

    lines = [
        "# Outreach Daily Run Report",
        "",
        f"- Started: `{payload.get('started_at', '')}`",
        f"- Finished: `{payload.get('finished_at', '')}`",
        f"- Mode: execute=`{payload.get('execute')}` live_linkedin=`{payload.get('live_linkedin')}` send_linkedin=`{payload.get('send_linkedin')}`",
        f"- Resume season focus: `{payload.get('resume_season_focus', '')}`",
        f"- Summary artifact: `{summary_artifact}`",
    ]
    if track_artifact:
        lines.append(f"- Track 2 run artifact: `{track_artifact}`")
    if campaign_artifact:
        lines.append(f"- Campaign plan artifact: `{campaign_artifact}`")
    report_path = settings.artifacts_dir / f"{artifact_timestamp()}-supervised-e2e-report.md"
    latest_path = settings.resolved_tracking_workspace_dir / "daily_run_report.md"
    lines.extend(["", "## Workspace Counts", ""])
    lines.append(f"- Before: `{payload.get('before_counts', {})}`")
    lines.append(f"- After: `{payload.get('after_counts', {})}`")
    lines.extend(["", "## Stages", ""])
    for stage in list(payload.get("stages") or []):
        if isinstance(stage, dict):
            lines.append(f"- {stage.get('name', '')}: `{stage.get('status', '')}`")
    if track_payload:
        lines.extend(["", "## Track 2 Budget", ""])
        lines.append(f"- Used: `{track_payload.get('used', {})}`")
        lines.append(f"- Phase summary: `{track_payload.get('phase_summary', {})}`")
        lines.extend(["", "## Company-Level Actions", ""])
        if selected_by_tier_phase:
            for (tier, phase), items in sorted(selected_by_tier_phase.items()):
                rendered = []
                for item in items:
                    company = str(item.get("company") or "")
                    rendered.append(f"{company} - {company_status.get(company, 'planned')}")
                lines.append(f"- Tier {tier} / {phase}: {', '.join(rendered)}")
        else:
            for item in list(track_payload.get("execution_manifest") or []):
                if not isinstance(item, dict):
                    continue
                companies = ", ".join(str(company) for company in list(item.get("companies") or []))
                lines.append(f"- {item.get('phase', '')}: {companies or 'none'}")
        lines.extend(["", "## Phase Results", ""])
        for phase in list(track_payload.get("phase_results") or []):
            if not isinstance(phase, dict):
                continue
            lines.append(f"### {phase.get('phase', '')}")
            lines.append(f"- Status: `{phase.get('status', '')}`")
            if phase.get("planned_companies"):
                lines.append(f"- Companies: {', '.join(str(name) for name in list(phase.get('planned_companies') or []))}")
            if phase.get("companies"):
                lines.append(f"- Companies: {', '.join(str(name) for name in list(phase.get('companies') or []))}")
            if phase.get("queue"):
                lines.append("- Queued people:")
                for queued in list(phase.get("queue") or []):
                    if isinstance(queued, dict):
                        lines.append(
                            f"  - {queued.get('company', '')}: {queued.get('name', '')} - {queued.get('title', '')}"
                        )
            if phase.get("runs"):
                lines.append("- Runs:")
                for run in list(phase.get("runs") or []):
                    if not isinstance(run, dict):
                        continue
                    progress = []
                    if "candidate_count" in run:
                        progress.append(f"candidates={run.get('candidate_count')}")
                    if "contacts_added" in run:
                        progress.append(f"contacts_added={run.get('contacts_added')}")
                    if "touchpoints_added" in run:
                        progress.append(f"touchpoints_added={run.get('touchpoints_added')}")
                    if run.get("status_counts"):
                        progress.append(f"status_counts={run.get('status_counts')}")
                    if run.get("status"):
                        progress.append(f"status={run.get('status')}")
                    if run.get("error"):
                        progress.append(f"error={run.get('error')}")
                    suffix = f" ({'; '.join(progress)})" if progress else ""
                    lines.append(f"  - {run.get('company', '')}: sent=`{run.get('sent', False)}`{suffix}")
            for key in [
                "draft_count",
                "sendable_count",
                "pending_review_count",
                "touchpoints_added",
                "updated",
            ]:
                if key in phase:
                    lines.append(f"- {key}: `{phase.get(key)}`")
            if phase.get("pending_review_artifact"):
                lines.append(f"- Pending review: `{phase.get('pending_review_artifact')}`")
            if phase.get("artifacts"):
                lines.append("- Artifacts:")
                for artifact in list(phase.get("artifacts") or []):
                    lines.append(f"  - `{artifact}`")
            if phase.get("detail"):
                lines.append(f"- Note: {phase.get('detail')}")
            lines.append("")
    if pending_review_items:
        lines.extend(["", "## Messages To Review", ""])
        for item in pending_review_items:
            latest_message = str(item.get("latest_message") or item.get("last_message") or "").strip()
            lines.append(
                f"- {item.get('company', '')} / {item.get('name', '')} "
                f"(`{item.get('send_recommendation', '')}`): {item.get('draft_message', '')}"
            )
            if latest_message:
                lines.append(f"  - Last message: {latest_message}")
    report_text = "\n".join(lines).rstrip() + "\n"
    report_path.write_text(report_text, encoding="utf-8")
    latest_path.write_text(report_text, encoding="utf-8")

    def esc(value: object) -> str:
        return html.escape(str(value))

    report_html_path = report_path.with_suffix(".html")
    latest_html_path = settings.resolved_tracking_workspace_dir / "daily_run_report.html"
    stage_cards = "".join(
        f"<tr><td>{esc(stage.get('name', ''))}</td><td><span class='pill'>{esc(stage.get('status', ''))}</span></td></tr>"
        for stage in list(payload.get("stages") or [])
        if isinstance(stage, dict)
    )
    company_rows = ""
    if selected_by_tier_phase:
        for (tier, phase), items in sorted(selected_by_tier_phase.items()):
            company_list = "".join(
                "<li>"
                f"<strong>{esc(item.get('company', ''))}</strong>"
                f"<span>{esc(company_status.get(str(item.get('company') or ''), 'planned'))}</span>"
                f"<small>{esc(item.get('reason', ''))}</small>"
                "</li>"
                for item in items
            )
            company_rows += (
                "<section class='card'>"
                f"<h3>Tier {esc(tier)} · {esc(phase)}</h3>"
                f"<ul class='company-list'>{company_list}</ul>"
                "</section>"
            )
    review_cards = "".join(
        (
            "<section class='review-card'>"
            f"<div class='review-meta'>{esc(item.get('company', ''))} · {esc(item.get('name', ''))} · {esc(item.get('send_recommendation', ''))}</div>"
        )
        + (
            f"<div class='last-message'><strong>Last msg</strong><span>{esc(item.get('latest_message') or item.get('last_message') or '')}</span></div>"
            if str(item.get("latest_message") or item.get("last_message") or "").strip()
            else ""
        )
        + f"<p>{esc(item.get('draft_message', ''))}</p>"
        + f"<small>{esc(item.get('title', ''))}</small>"
        + "</section>"
        for item in pending_review_items
    )
    phase_cards = ""
    for phase in list(track_payload.get("phase_results") or []) if track_payload else []:
        if not isinstance(phase, dict):
            continue
        details = []
        for key in ["draft_count", "sendable_count", "pending_review_count", "updated", "touchpoints_added"]:
            if key in phase:
                details.append(f"<li>{esc(key)}: <strong>{esc(phase.get(key))}</strong></li>")
        if phase.get("queue"):
            details.append("<li>Queued people:<ul>")
            for queued in list(phase.get("queue") or []):
                if isinstance(queued, dict):
                    details.append(
                        f"<li>{esc(queued.get('company', ''))}: {esc(queued.get('name', ''))} - {esc(queued.get('title', ''))}</li>"
                    )
            details.append("</ul></li>")
        if phase.get("runs"):
            details.append("<li>Runs:<ul>")
            for run in list(phase.get("runs") or []):
                if isinstance(run, dict):
                    details.append(
                        f"<li>{esc(run.get('company', ''))}: sent={esc(run.get('sent', False))} "
                        f"candidates={esc(run.get('candidate_count', ''))} status={esc(run.get('status') or run.get('status_counts', ''))}"
                        + (f"<br><small>{esc(run.get('error'))}</small>" if run.get("error") else "")
                        + "</li>"
                    )
            details.append("</ul></li>")
        phase_cards += (
            "<section class='card'>"
            f"<h3>{esc(phase.get('phase', ''))}</h3>"
            f"<p><span class='pill'>{esc(phase.get('status', ''))}</span></p>"
            f"<ul>{''.join(details)}</ul>"
            "</section>"
        )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Outreach Daily Run Report</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #15171a; }}
    header {{ background: #111827; color: white; padding: 28px 36px; }}
    header h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    header p {{ margin: 0; color: #d1d5db; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }}
    .card, .review-card {{ background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05); }}
    .card h2, .card h3 {{ margin: 0 0 12px; }}
    .pill {{ display: inline-block; background: #e8f0fe; color: #174ea6; border-radius: 999px; padding: 3px 9px; font-size: 12px; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; }}
    td, th {{ padding: 10px 12px; border-bottom: 1px solid #edf0f3; text-align: left; vertical-align: top; }}
    section {{ margin-bottom: 20px; }}
    .company-list {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
    .company-list li {{ display: grid; gap: 4px; padding: 10px; border: 1px solid #edf0f3; border-radius: 6px; }}
    .company-list span {{ color: #0f766e; font-weight: 700; }}
    .company-list small, .review-card small {{ color: #667085; }}
    .review-meta {{ font-weight: 800; color: #7c2d12; margin-bottom: 8px; }}
    .review-card {{ border-left: 4px solid #f97316; }}
    .last-message {{ background: #fff7ed; border: 1px solid #fed7aa; border-radius: 6px; padding: 10px; margin: 10px 0; display: grid; gap: 4px; }}
    .last-message strong {{ color: #9a3412; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }}
    .last-message span {{ color: #431407; }}
    code {{ background: #eef2f7; padding: 2px 5px; border-radius: 4px; }}
  </style>
</head>
<body>
  <header>
    <h1>Outreach Daily Run Report</h1>
    <p>{esc(payload.get('started_at', ''))} → {esc(payload.get('finished_at', ''))}</p>
  </header>
  <main>
    <section class="grid">
      <div class="card"><h2>Mode</h2><p>execute=<code>{esc(payload.get('execute'))}</code> live=<code>{esc(payload.get('live_linkedin'))}</code> send=<code>{esc(payload.get('send_linkedin'))}</code></p></div>
      <div class="card"><h2>Counts</h2><p>Before <code>{esc(payload.get('before_counts', {}))}</code></p><p>After <code>{esc(payload.get('after_counts', {}))}</code></p></div>
      <div class="card"><h2>Budget Used</h2><p><code>{esc(track_payload.get('used', {}) if track_payload else {})}</code></p></div>
    </section>
    <section><h2>Stages</h2><table><tbody>{stage_cards}</tbody></table></section>
    <section><h2>Company-Level Actions</h2><div class="grid">{company_rows or '<div class="card">No company actions selected.</div>'}</div></section>
    <section><h2>Messages To Review</h2>{review_cards or '<div class="card">No messages require review.</div>'}</section>
    <section><h2>Phase Details</h2><div class="grid">{phase_cards}</div></section>
    <section class="card"><h2>Artifacts</h2><p>Summary: <code>{esc(summary_artifact)}</code></p><p>Track 2: <code>{esc(track_artifact or '')}</code></p><p>Campaign: <code>{esc(campaign_artifact)}</code></p></section>
  </main>
</body>
</html>
"""
    report_html_path.write_text(html_text, encoding="utf-8")
    latest_html_path.write_text(html_text, encoding="utf-8")
    return report_path, latest_path, report_html_path, latest_html_path


def _parse_report_datetime(value: str) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _artifacts_since(artifacts_dir: Path, since: datetime | None) -> list[Path]:
    if not artifacts_dir.exists():
        return []
    paths = [path for path in artifacts_dir.glob("*.json") if path.is_file()]
    if since is not None:
        since_epoch = since.timestamp()
        paths = [path for path in paths if path.stat().st_mtime >= since_epoch]
    return sorted(paths, key=lambda path: path.stat().st_mtime)


def _load_json_file(path: Path | None) -> dict[str, object]:
    # Older ResumeGenerator summaries recorded a differently-cased Desktop
    # component; repair only that known local-path spelling before declaring a
    # run artifact unavailable.
    if path is not None and not path.exists():
        repaired = Path(str(path).replace("/Claude projects/", "/Claude Projects/"))
        if repaired.exists():
            path = repaired
    if path is None or not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _count_statuses(rows: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(
            row.get("status")
            or row.get("result")
            or row.get("outcome")
            or row.get("invite_result")
            or "unknown"
        )
        counts[status] = counts.get(status, 0) + 1
    return counts


def _latest_artifact_matching(paths: list[Path], token: str) -> tuple[Path | None, dict[str, object]]:
    matches = [path for path in paths if token in path.name]
    if not matches:
        return None, {}
    path = matches[-1]
    return path, _load_json_file(path)


def _resolve_run_reference(
    value: object,
    *,
    settings: OutreachSettings | object,
    summary_path: Path | None,
) -> Path | None:
    """Resolve one explicitly recorded run pointer without searching directories."""
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    candidates = [path]
    if not path.is_absolute():
        artifacts_dir = Path(str(getattr(settings, "artifacts_dir", "artifacts")))
        candidates.extend(
            [
                artifacts_dir.parent / path,
                artifacts_dir / path.name,
            ]
        )
        if summary_path is not None:
            candidates.append(summary_path.parent / path)
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
        repaired = Path(str(candidate).replace("/Claude projects/", "/Claude Projects/"))
        if repaired.exists() and repaired.is_file():
            return repaired
    return path


def _artifact_values(value: object) -> list[str]:
    """Flatten values stored under a manifest's explicit artifact-pointer key."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_artifact_values(item))
        return values
    if isinstance(value, dict):
        direct_keys = (
            "artifact",
            "path",
            "send_artifact",
            "batch_artifact",
            "draft_artifact",
            "reconcile_artifact",
        )
        direct = [str(value.get(key) or "") for key in direct_keys if value.get(key)]
        if direct:
            return direct
        values = []
        for item in value.values():
            values.extend(_artifact_values(item))
        return values
    return []


def _manifest_values(manifest: dict[str, object], aliases: set[str]) -> list[str]:
    values: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).casefold() in aliases:
                    values.extend(_artifact_values(value))
                elif isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(manifest)
    return values


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _load_daily_engine_manifest(
    nightly_summary: dict[str, object],
    *,
    settings: OutreachSettings | object,
    summary_path: Path | None,
) -> tuple[Path | None, dict[str, object], str]:
    raw = nightly_summary.get("daily_engine_manifest")
    if isinstance(raw, dict):
        return None, raw, "inline"
    path = _resolve_run_reference(raw, settings=settings, summary_path=summary_path)
    if raw in (None, ""):
        return None, {}, "not_recorded"
    payload = _load_json_file(path)
    return path, payload, "loaded" if payload else "missing_or_invalid"


def _exact_run_artifacts(
    *,
    nightly_summary: dict[str, object],
    nightly_summary_path: Path | None,
    settings: OutreachSettings | object,
    track_payload: dict[str, object],
) -> tuple[dict[str, list[Path]], dict[str, object]]:
    """Return only artifacts explicitly owned by the selected nightly run."""
    manifest_path, manifest, manifest_status = _load_daily_engine_manifest(
        nightly_summary,
        settings=settings,
        summary_path=nightly_summary_path,
    )
    alias_groups = {
        "invites": {
            "invite_artifacts",
            "invite_send_artifacts",
            "invite_send_batches",
            "invite_batches",
            "linkedin_invite_artifacts",
            "linkedin_invites",
        },
        "followup_sends": {
            "followup_send_artifact",
            "followup_send_artifacts",
            "followup_send_results",
            "linkedin_followup_send_artifact",
            "linkedin_followup_send_artifacts",
            "linkedin_followup_send_results",
        },
        "followup_drafts": {
            "followup_draft_artifact",
            "followup_draft_artifacts",
            "linkedin_followup_drafts",
            "linkedin_followup_draft_artifact",
            "linkedin_followup_draft_artifacts",
        },
        "reconcile": {
            "reconcile_artifact",
            "reconcile_artifacts",
            "linkedin_message_reconcile",
            "linkedin_message_reconcile_artifact",
            "linkedin_message_reconcile_artifacts",
            "linkedin_reconcile_artifacts",
        },
        "email_sends": {
            "email_send_artifact",
            "email_send_artifacts",
            "email_send_results",
            "track_2_email_send_artifact",
            "track_2_email_send_artifacts",
            "smtp_send_artifacts",
        },
        "email_drafts": {
            "email_draft_artifact",
            "email_draft_artifacts",
            "track_2_email_draft_artifact",
            "track_2_email_draft_artifacts",
        },
    }
    resolved: dict[str, list[Path]] = {key: [] for key in alias_groups}
    for group, aliases in alias_groups.items():
        for value in _manifest_values(manifest, aliases):
            path = _resolve_run_reference(
                value,
                settings=settings,
                summary_path=nightly_summary_path,
            )
            if path is not None:
                resolved[group].append(path)

    # Track 2 owns a single exact run artifact; its child pointers are part of
    # that artifact's phase manifest and are therefore safe to consume.
    for phase in list(track_payload.get("phase_results") or []):
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("phase") or "")
        for value in _artifact_values(phase.get("artifacts") or []):
            lowered = Path(value).name.casefold()
            group = ""
            if "followup-send-results" in lowered:
                group = "followup_sends"
            elif "followup-draft" in lowered:
                group = "followup_drafts"
            elif "message-reconcile" in lowered:
                group = "reconcile"
            if group:
                path = _resolve_run_reference(
                    value,
                    settings=settings,
                    summary_path=nightly_summary_path,
                )
                if path is not None:
                    resolved[group].append(path)
        if phase_name == "5_send_linkedin_invites":
            for run in list(phase.get("runs") or []):
                if not isinstance(run, dict):
                    continue
                path = _resolve_run_reference(
                    run.get("send_artifact"),
                    settings=settings,
                    summary_path=nightly_summary_path,
                )
                if path is not None:
                    resolved["invites"].append(path)
        elif phase_name == "6_draft_email_touch":
            path = _resolve_run_reference(
                phase.get("artifact"),
                settings=settings,
                summary_path=nightly_summary_path,
            )
            if path is not None:
                resolved["email_drafts"].append(path)

    resolved = {key: _dedupe_paths(paths) for key, paths in resolved.items()}
    manifest_display = str(manifest_path) if manifest_path is not None else ("inline" if manifest else "")
    email_channel = manifest.get("email_channel")
    if not isinstance(email_channel, dict):
        email_channel = manifest.get("smtp")
    if not isinstance(email_channel, dict):
        email_channel = manifest.get("email")
    email_channel = email_channel if isinstance(email_channel, dict) else {}
    integrity = {
        "artifact_selection": "explicit_pointers_only",
        "daily_engine_manifest": manifest_display,
        "daily_engine_manifest_status": manifest_status,
        "daily_engine_manifest_schema": str(manifest.get("manifest_schema") or ""),
        "daily_engine_manifest_version": manifest.get("manifest_version"),
        "manifest_source_metrics": str(manifest.get("source_metrics") or ""),
        "manifest_action_queue": str(manifest.get("action_queue") or ""),
        "email_channel": email_channel,
        "exact_artifacts": {key: [str(path) for path in paths] for key, paths in resolved.items()},
        "missing_artifacts": [
            str(path)
            for paths in resolved.values()
            for path in paths
            if not path.exists()
        ],
    }
    return resolved, integrity


def _track_2_execution_status(
    maintenance: dict[str, object],
    track_payload: dict[str, object],
) -> dict[str, object]:
    returncode = maintenance.get("track_2_daily_run_returncode")
    artifact_value = str(maintenance.get("track_2_daily_run_artifact") or "")
    phase_rows = [row for row in list(track_payload.get("phase_results") or []) if isinstance(row, dict)]
    phase_statuses = {str(row.get("phase") or "unknown"): str(row.get("status") or "unknown") for row in phase_rows}
    failed_phases = {
        name: status
        for name, status in phase_statuses.items()
        if "fail" in status.casefold() or status.casefold() in {"error", "timed_out", "timeout"}
    }
    incomplete_phases = {
        name: status
        for name, status in phase_statuses.items()
        if status.casefold() in {"planned", "queued", "prepared", "unknown"}
        or (name == "1_2_linkedin_followups" and status.casefold() == "drafted")
    }
    if returncode is None and not artifact_value:
        status = "not_run"
    elif returncode not in (None, 0):
        status = "failed"
    elif returncode == 0 and not artifact_value:
        status = "failed_missing_artifact"
    elif artifact_value and not track_payload:
        status = "failed_missing_artifact"
    elif failed_phases and len(failed_phases) == len(phase_statuses):
        status = "failed"
    elif failed_phases:
        status = "partial_failed"
    elif incomplete_phases:
        status = "incomplete"
    elif not bool(track_payload.get("execute")):
        status = "planned_not_executed"
    elif not phase_rows:
        planned = track_payload.get("used") if isinstance(track_payload.get("used"), dict) else {}
        status = (
            "completed_zero_actions"
            if not any(int(value or 0) for value in planned.values())
            else "incomplete"
        )
    else:
        status = "completed"
    return {
        "status": status,
        "returncode": returncode,
        "artifact": artifact_value,
        "phase_statuses": phase_statuses,
        "failed_phases": failed_phases,
        "incomplete_phases": incomplete_phases,
        "planned": track_payload.get("used") if isinstance(track_payload.get("used"), dict) else {},
    }


def _render_status_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}: {value}" for key, value in sorted(counts.items())) or "-"


def _reports_dir(settings: OutreachSettings) -> Path:
    return settings.resolved_tracking_workspace_dir / "reports"


def _daily_html_reports_dir(settings: OutreachSettings) -> Path:
    return _reports_dir(settings) / "daily_html"


def _source_breakdown(nightly_summary: dict[str, object]) -> list[dict[str, object]]:
    """Normalize the sources recorded by one ResumeGenerator nightly summary.

    A report must not infer source activity from whichever artifact happens to be
    newest in either checkout. Missing entries therefore mean skipped, not zero
    discovered from a workspace snapshot.
    """
    source_metrics = _load_json_file(Path(str(nightly_summary.get("source_metrics") or "")))
    sources = source_metrics.get("sources") if isinstance(source_metrics.get("sources"), dict) else {}
    stages = source_metrics.get("stage_metrics") if isinstance(source_metrics.get("stage_metrics"), dict) else {}

    def row(label: str, key: str, *, details: dict[str, object] | None = None) -> dict[str, object]:
        metric = sources.get(key) if isinstance(sources, dict) else {}
        metric = metric if isinstance(metric, dict) else {}
        stage = stages.get(key) if isinstance(stages, dict) else {}
        stage = stage if isinstance(stage, dict) else {}
        return {
            "source": label,
            "status": metric.get("status") or stage.get("status") or "skipped",
            "raw": int(metric.get("raw_count") or 0) if metric else 0,
            "kept": int(metric.get("accepted_for_write") or 0) if metric else 0,
            "details": details if details is not None else (metric.get("details") or {}),
        }

    startup_report = source_metrics.get("startup_source_report") if isinstance(source_metrics.get("startup_source_report"), dict) else {}
    startup_report_payload = _load_json_file(
        Path(str((startup_report or {}).get("artifact") or ""))
    )
    startup_apply_metric = sources.get("startup_apply") if isinstance(sources, dict) else {}
    startup_apply_metric = startup_apply_metric if isinstance(startup_apply_metric, dict) else {}
    relationship = sources.get("startup_relationship") if isinstance(sources, dict) else {}
    relationship = relationship if isinstance(relationship, dict) else {}
    apply_status = str(startup_apply_metric.get("status") or "skipped")
    relationship_status = str(relationship.get("status") or "skipped")
    startup_details = {
        "lane_statuses": {
            "startup_apply": apply_status,
            "startup_relationship": relationship_status,
        },
        "apply_discovered": (startup_report or {}).get("startup_apply_discovered", {}),
        "apply_new": (startup_report or {}).get("startup_apply_new", {}),
        "relationship_targets": (startup_report or {}).get("relationship_targets", 0),
        "relationship_sources": (startup_report or {}).get("relationship_source_counts", {}),
        "adapters": [],
    }
    relationship_lane = (
        startup_report_payload.get("relationship_lane")
        if isinstance(startup_report_payload.get("relationship_lane"), dict)
        else {}
    )
    relationship_artifacts = (
        relationship_lane.get("artifacts")
        if isinstance(relationship_lane.get("artifacts"), dict)
        else {}
    )
    relationship_selected = (
        relationship_lane.get("source_counts")
        if isinstance(relationship_lane.get("source_counts"), dict)
        else startup_details["relationship_sources"]
    )
    for source_id, reference in relationship_artifacts.items():
        reference = reference if isinstance(reference, dict) else {}
        artifact = _load_json_file(Path(str(reference.get("artifact") or "")))
        startup_details["adapters"].append(
            {
                "source": str(source_id),
                "lane": "company_relationship_discovery",
                "status": str(reference.get("status") or "skipped"),
                "fetched": int(artifact.get("raw_count") or 0),
                "discovered": int(artifact.get("count") or reference.get("count") or 0),
                "selected": int((relationship_selected or {}).get(source_id) or 0),
                "artifact": str(reference.get("artifact") or ""),
            }
        )
    startup_apply_discovered = (
        startup_details["apply_discovered"]
        if isinstance(startup_details["apply_discovered"], dict)
        else {}
    )
    for source_id, discovered in startup_apply_discovered.items():
        startup_details["adapters"].append(
            {
                "source": str(source_id),
                "lane": "startup_job_discovery",
                "status": apply_status,
                "fetched": int(discovered or 0),
                "discovered": int(discovered or 0),
                "selected": int((startup_details["apply_new"] or {}).get(source_id) or 0),
                "artifact": str((startup_report or {}).get("artifact") or ""),
            }
        )
    canonical_adapters = [
        ("yc_sf_bay_hiring", "company_relationship_discovery", relationship_status),
        ("yc_los_angeles", "company_relationship_discovery", relationship_status),
        ("builtin_sf_companies", "company_relationship_discovery", relationship_status),
        ("builtin_la_companies", "company_relationship_discovery", relationship_status),
        ("yc_sf_bay_hiring", "startup_job_discovery", apply_status),
        ("yc_los_angeles", "startup_job_discovery", apply_status),
        ("builtin_la_job_lists", "startup_job_discovery", apply_status),
        ("builtin_sf_job_lists", "startup_job_discovery", apply_status),
        ("a16z_job_board", "startup_job_discovery", apply_status),
    ]
    recorded_adapters = {
        (str(item.get("source") or ""), str(item.get("lane") or ""))
        for item in startup_details["adapters"]
        if isinstance(item, dict)
    }
    for source_id, lane, lane_status in canonical_adapters:
        if (source_id, lane) in recorded_adapters:
            continue
        startup_details["adapters"].append(
            {
                "source": source_id,
                "lane": lane,
                "status": lane_status,
                "fetched": 0,
                "discovered": 0,
                "selected": 0,
                "artifact": "",
            }
        )
    startup = row("Startup sources", "startup_apply", details=startup_details)
    apply_discovered = startup_details["apply_discovered"]
    apply_new = startup_details["apply_new"]
    apply_ran = apply_status == "ran"
    relationship_ran = relationship_status == "ran"
    startup["raw"] = ((
        sum(int(value or 0) for value in apply_discovered.values())
        if isinstance(apply_discovered, dict)
        else 0
    ) if apply_ran else 0) + (
        int(startup_details["relationship_targets"] or 0) if relationship_ran else 0
    )
    startup["kept"] = ((
        sum(int(value or 0) for value in apply_new.values())
        if isinstance(apply_new, dict)
        else 0
    ) if apply_ran else 0) + (
        int(startup_details["relationship_targets"] or 0) if relationship_ran else 0
    )
    startup["status"] = _combined_source_status(apply_status, relationship_status)

    action_queue = source_metrics.get("action_queue") if isinstance(source_metrics.get("action_queue"), dict) else {}
    action_queue_counts = (action_queue or {}).get("counts", {})
    app_queue = {
        "source": "ResumeGenerator / app queue",
        "status": "ran" if action_queue else "skipped",
        "raw": (
            sum(int(value or 0) for value in action_queue_counts.values())
            if isinstance(action_queue_counts, dict)
            else 0
        ),
        "kept": int(nightly_summary.get("generation_selected_count") or 0),
        "details": {
            "action_queue_counts": action_queue_counts,
            "action_queue_sources": (action_queue or {}).get("source_counts", {}),
            "generation_selected": nightly_summary.get("generation_selected_count", 0),
            "generation_ran": nightly_summary.get("generation_ran", False),
        },
    }
    maintenance = nightly_summary.get("outreach_maintenance") if isinstance(nightly_summary.get("outreach_maintenance"), dict) else {}
    track_artifact = Path(str((maintenance or {}).get("track_2_daily_run_artifact") or ""))
    track_payload = _load_json_file(track_artifact)
    track_used = track_payload.get("used") if isinstance(track_payload.get("used"), dict) else {}
    maintenance_returncodes = {
        key: value
        for key, value in (maintenance or {}).items()
        if key.endswith("_returncode") and value is not None
    }
    track_execution = _track_2_execution_status(maintenance or {}, track_payload)
    actual_actions = {
        "linkedin_invites_sent": 0,
        "linkedin_messages_sent": 0,
        "companies_mapped": 0,
        "profiles_inspected_for_email": 0,
        "emails_found": 0,
        "companies_enriched": 0,
    }
    attempted_actions = 0
    for phase in list(track_payload.get("phase_results") or []):
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("phase") or "")
        if phase_name == "1_2_linkedin_followups":
            counts = phase.get("send_status_counts") if isinstance(phase.get("send_status_counts"), dict) else {}
            actual_actions["linkedin_messages_sent"] += int((counts or {}).get("sent") or 0)
            attempted_actions += sum(int(value or 0) for value in (counts or {}).values())
        elif phase_name == "3_contact_and_email_research":
            inspected = int(phase.get("inspected_count") or 0)
            actual_actions["profiles_inspected_for_email"] += inspected
            actual_actions["emails_found"] += int(phase.get("found_count") or 0)
            attempted_actions += inspected
        elif phase_name == "4_contact_mapping":
            runs = [item for item in list(phase.get("runs") or []) if isinstance(item, dict)]
            actual_actions["companies_mapped"] += len(runs)
            attempted_actions += len(runs)
        elif phase_name == "5_send_linkedin_invites":
            for run in list(phase.get("runs") or []):
                if not isinstance(run, dict):
                    continue
                counts = run.get("status_counts") if isinstance(run.get("status_counts"), dict) else {}
                actual_actions["linkedin_invites_sent"] += int((counts or {}).get("sent") or 0)
                attempted_actions += sum(int(value or 0) for value in (counts or {}).values())
        elif phase_name == "7_context_enrichment":
            count = int(phase.get("count") or 0)
            actual_actions["companies_enriched"] += count
            attempted_actions += count
    completed_actions = sum(actual_actions.values()) - actual_actions["emails_found"]
    track_status = str(track_execution["status"])
    track = {
        "source": "Track 2 imports / maintenance",
        "status": track_status,
        "raw": attempted_actions,
        "kept": completed_actions,
        "details": {
            "returncodes": maintenance_returncodes,
            "artifacts": {key: value for key, value in (maintenance or {}).items() if key.endswith("_artifact") or key == "account_universe_import"},
            "track_2_used": track_used,
            "track_2_summary": track_payload.get("summary", {}),
            "track_2_phase_summary": track_payload.get("phase_summary", {}),
            "execution_status": track_execution,
            "actual_actions": actual_actions,
        },
    }
    intelligence_artifact = str((maintenance or {}).get("linkedin_intelligence_artifact") or "")
    intelligence_path = Path(intelligence_artifact)
    intelligence = _load_json_file(intelligence_path)
    feed = intelligence.get("feed") if isinstance(intelligence.get("feed"), dict) else {}
    viewers = intelligence.get("profile_viewers") if isinstance(intelligence.get("profile_viewers"), dict) else {}
    intelligence_returncode = (maintenance or {}).get("linkedin_intelligence_returncode")
    recorded_status = str((maintenance or {}).get("linkedin_intelligence_status") or "")
    if feed.get("status"):
        feed_status = str(feed["status"])
        feed_reason = str(feed.get("reason") or "")
    elif recorded_status:
        feed_status = recorded_status
        feed_reason = "nightly_summary_recorded_source_status"
    elif intelligence_returncode is not None:
        feed_status = "failed"
        feed_reason = (
            "capture_command_failed"
            if intelligence_returncode != 0
            else "capture_artifact_missing"
        )
    else:
        feed_status = "skipped"
        feed_reason = "not_recorded_for_this_run"
    feed_details = dict(feed)
    if not feed_details:
        feed_details = {
            "reason": feed_reason,
            "returncode": intelligence_returncode,
            "artifact": intelligence_artifact,
        }
    feed_row = {
        "source": "LinkedIn home feed",
        "status": feed_status,
        "raw": feed.get("captured") or 0,
        "kept": feed.get("added") or 0,
        "details": feed_details,
    }
    viewer_details = dict(viewers)
    if viewers.get("status"):
        viewer_status = str(viewers["status"])
    else:
        viewer_status = "skipped"
        viewer_details = {
            "reason": (
                "linkedin_capture_unavailable"
                if feed_status == "failed"
                else "not_recorded_for_this_run"
            ),
            "returncode": intelligence_returncode,
            "artifact": intelligence_artifact,
        }
    viewer_row = {
        "source": "LinkedIn profile viewers",
        "status": viewer_status,
        "raw": viewers.get("captured") or 0,
        "kept": viewers.get("added") or 0,
        "details": {**viewer_details, "passive_context_only": True},
    }
    return [row("LinkedIn", "linkedin"), feed_row, viewer_row, row("Handshake", "handshake"), row("JobSpy", "jobspy"), startup, app_queue, track]


def _combined_source_status(*statuses: str) -> str:
    normalized = [str(status or "skipped").strip().lower() for status in statuses]
    failures = {"failed", "timeout", "timed_out"}
    if normalized and all(status in failures for status in normalized):
        return "failed"
    if any(status in failures for status in normalized):
        return "partial_failed"
    if normalized and all(status == "ran" for status in normalized):
        return "ran"
    if any(status == "ran" for status in normalized):
        return "partial"
    return "skipped"


def _unscoped_source_breakdown() -> list[dict[str, object]]:
    return [
        {
            "source": label,
            "status": "not_scoped",
            "raw": 0,
            "kept": 0,
            "details": {
                "reason": "workspace snapshot mode has no selected nightly run; no source activity is claimed"
            },
        }
        for label in (
            "LinkedIn",
            "LinkedIn home feed",
            "LinkedIn profile viewers",
            "Handshake",
            "JobSpy",
            "Startup sources",
            "ResumeGenerator / app queue",
            "Track 2 imports / maintenance",
        )
    ]


INBOX_ACTION_FIELDS = [
    "action_id",
    "status",
    "priority",
    "action_type",
    "company",
    "person",
    "contact_id",
    "linkedin_url",
    "last_seen_at",
    "message",
    "recommended_action",
    "email",
    "thread_url",
    "source",
    "notes",
]


def _inbox_action_path(workspace: Path) -> Path:
    return workspace / "linkedin_inbox_actions.csv"


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _inbound_action_details(message: str) -> tuple[str, str, str, str]:
    """Return action type, priority, recommended action, and email for an inbound reply."""
    lower = message.casefold()
    emails = extract_email_addresses(message)
    email = emails[0] if emails else ""
    if email and any(token in lower for token in ("resume", "cv", "profile", "send over", "send your")):
        return (
            "email_resume_requested",
            "high",
            f"Email your resume plus a concise role-fit note to {email}.",
            email,
        )
    if any(token in lower for token in ("referral", "send your resume", "share your resume", "share your profile")):
        return (
            "resume_or_referral_requested",
            "high",
            "Send the requested resume/profile and a concise role-fit blurb.",
            email,
        )
    if any(token in lower for token in ("reach out to", "talk to", "contact ", "@")):
        return (
            "routing_signal",
            "medium",
            "Follow the routing suggestion; identify the named person and make the next outreach deliberate.",
            email,
        )
    return (
        "inbound_reply",
        "medium",
        "Read and reply manually; this is an inbound response that has not been resolved in the action ledger.",
        email,
    )


def _sync_open_inbox_actions(
    workspace: Path,
    workbook: OutreachWorkbook,
    *,
    auto_handled_contact_ids: set[str] | None = None,
) -> tuple[Path, list[dict[str, str]]]:
    """Materialize actionable inbound LinkedIn replies without claiming they happened this run.

    The CSV is deliberately persistent: a user can set status to done, snoozed,
    or not_actionable and later daily reports will stop presenting that item as
    an open task.
    """
    state_path = workspace / "linkedin_message_state.json"
    state = _load_json_file(state_path)
    thread_states = state.get("thread_states") if isinstance(state.get("thread_states"), dict) else {}
    contact_by_name = {
        normalize_dedupe_text(contact.full_name): contact
        for contact in workbook.list_contacts()
        if contact.full_name.strip()
    }
    organizations = {item.organization_id: item for item in workbook.list_organizations()}
    path = _inbox_action_path(workspace)
    existing = {row.get("action_id", ""): row for row in _read_csv_rows(path)}
    merged: dict[str, dict[str, str]] = dict(existing)

    for raw_state in thread_states.values():
        if not isinstance(raw_state, dict):
            continue
        sender = str(raw_state.get("last_sender") or "").strip().casefold()
        if not sender or sender in {"you", "akshat", "akshat pathak"}:
            continue
        name = str(raw_state.get("name") or "").strip()
        contact = contact_by_name.get(normalize_dedupe_text(name))
        if contact is None or str(contact.status or "").casefold() != "replied":
            continue
        message = str(raw_state.get("latest_message") or "").strip()
        if not message:
            continue
        signature = str(raw_state.get("signature") or normalize_dedupe_text(message))
        action_id = hashlib.sha1(f"{contact.contact_id}|{signature}".encode("utf-8")).hexdigest()[:16]
        action_type, priority, recommended_action, email = _inbound_action_details(message)
        organization = organizations.get(contact.organization_id)
        prior = dict(existing.get(action_id) or {})
        merged[action_id] = {
            "action_id": action_id,
            "status": prior.get("status") or "open",
            "priority": priority,
            "action_type": action_type,
            "company": organization.name if organization else "",
            "person": contact.full_name,
            "contact_id": contact.contact_id,
            "linkedin_url": contact.linkedin_url,
            "last_seen_at": str(raw_state.get("last_seen_at") or ""),
            "message": message,
            "recommended_action": recommended_action,
            "email": email,
            "thread_url": str(raw_state.get("thread_url") or ""),
            "source": "linkedin_message_state",
            "notes": prior.get("notes") or "",
        }

    resolved_contacts = auto_handled_contact_ids or set()
    for row in merged.values():
        if row.get("contact_id") in resolved_contacts and row.get("status") == "open":
            row["status"] = "auto_handled"
            row["notes"] = _append_note_marker(
                row.get("notes", ""),
                "resolved_by_exact_run_linkedin_send",
            )

    rows = sorted(
        merged.values(),
        key=lambda row: (row.get("status") != "open", row.get("priority") != "high", row.get("last_seen_at", "")),
        reverse=False,
    )
    _write_csv_rows(path, rows, INBOX_ACTION_FIELDS)
    return path, [row for row in rows if row.get("status") == "open"]


def _source_summary(row: dict[str, object]) -> str:
    source = str(row.get("source") or "")
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    if source == "Startup sources":
        relationship = details.get("relationship_sources") if isinstance(details.get("relationship_sources"), dict) else {}
        apply_discovered = details.get("apply_discovered") if isinstance(details.get("apply_discovered"), dict) else {}
        relationship_total = sum(int(value or 0) for value in relationship.values())
        apply_total = sum(int(value or 0) for value in apply_discovered.values())
        return f"{relationship_total} company targets reviewed; {apply_total} startup job leads discovered."
    if source == "LinkedIn":
        return "LinkedIn job discovery; outreach activity is reported separately below."
    if source == "LinkedIn home feed":
        return (
            f"{int(details.get('captured') or 0)} posts captured; "
            f"{int(details.get('added') or 0)} new signals; "
            f"{int(details.get('workspace_pending_review') or 0)} pending review."
        )
    if source == "LinkedIn profile viewers":
        return str(details.get("reason") or "Passive context only.")
    if source == "ResumeGenerator / app queue":
        counts = details.get("action_queue_counts") if isinstance(details.get("action_queue_counts"), dict) else {}
        return (
            f"{int(counts.get('application_plus_outreach') or 0)} application+outreach; "
            f"{int(counts.get('follow_up') or 0)} follow-up candidates."
        )
    if source == "Cold email channel":
        blockers = details.get("blockers") if isinstance(details.get("blockers"), list) else []
        summary = (
            f"{int(details.get('drafts_created') or 0)} drafts created; "
            f"{int(details.get('emails_sent') or 0)} emails sent."
        )
        if blockers:
            summary += " Blockers: " + "; ".join(str(item) for item in blockers)
        return summary
    if source == "Track 2 imports / maintenance":
        actual = details.get("actual_actions") if isinstance(details.get("actual_actions"), dict) else {}
        status = str(row.get("status") or "not_run")
        if status not in {"completed", "completed_zero_actions"}:
            return f"Track 2 status: {status}; planned work is never presented as executed."
        return (
            f"{int(actual.get('linkedin_invites_sent') or 0)} invites sent; "
            f"{int(actual.get('linkedin_messages_sent') or 0)} messages sent; "
            f"{int(actual.get('companies_mapped') or 0)} companies mapped; "
            f"{int(actual.get('profiles_inspected_for_email') or 0)} profiles inspected for email."
        )
    return ""


HUMAN_REVIEW_RECOMMENDATIONS = {
    "review",
    "human_review",
    "human_review_required",
    "rewrite_before_send",
}
SYSTEM_HOLD_RECOMMENDATIONS = {"hold", "cadence_hold", "optional", "wait"}
REPLY_DRAFT_KINDS = {
    "conversation_reply",
    "referral_offer_reply",
    "polite_close_reply",
}


def _message_type(item: dict[str, object]) -> str:
    draft_kind = str(item.get("draft_kind") or "")
    return "reply" if draft_kind in REPLY_DRAFT_KINDS or draft_kind.endswith("_reply") else "follow_up"


def _review_item_key(item: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(item.get("contact_id") or ""),
        str(item.get("company") or ""),
        str(item.get("name") or ""),
        str(item.get("draft_message") or item.get("message") or ""),
    )


def _track_2_actual_actions(
    *,
    track_payload: dict[str, object],
    settings: OutreachSettings | object,
    summary_path: Path | None,
    workbook: OutreachWorkbook,
) -> tuple[list[dict[str, object]], dict[str, dict[str, int]]]:
    actions: list[dict[str, object]] = []
    company_counts: dict[str, dict[str, int]] = {}

    def add(company: str, key: str, amount: int) -> None:
        if not company or amount <= 0:
            return
        bucket = company_counts.setdefault(company, {})
        bucket[key] = bucket.get(key, 0) + amount

    organizations = {item.organization_id: item.name for item in workbook.list_organizations()}
    contact_companies = {
        item.contact_id: organizations.get(item.organization_id, "")
        for item in workbook.list_contacts()
    }
    for phase in list(track_payload.get("phase_results") or []):
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("phase") or "")
        phase_status = str(phase.get("status") or "not_run")
        if phase_name == "1_2_linkedin_followups":
            execution_count = int(
                phase.get("execution_result_count")
                or phase.get("filtered_count")
                or 0
            )
            inbound_count = int(phase.get("inbound_result_count") or 0)
            persistent_inbound_count = int(
                phase.get("persistent_inbound_count") or 0
            )
            planned_company_count = int(
                phase.get("planned_company_result_count")
                or phase.get("filtered_count")
                or 0
            )
            actions.append(
                {
                    "action": "linkedin_followup_reply_triage",
                    "status": phase_status,
                    "count": execution_count,
                    "detail": (
                        f"{int(phase.get('thread_count') or 0)} threads scanned; "
                        f"{inbound_count} inbound replies prioritized "
                        f"({persistent_inbound_count} recovered from persistent state); "
                        f"{planned_company_count} planned-company results; "
                        f"{execution_count} total results executed; "
                        f"{int(phase.get('sendable_count') or 0)} auto-send eligible; "
                        f"{int(phase.get('pending_review_count') or 0)} review/hold."
                    ),
                    "source_lane": "track_2",
                }
            )
        elif phase_name == "3_contact_and_email_research":
            inspected = int(phase.get("inspected_count") or 0)
            found = int(phase.get("found_count") or 0)
            actions.append(
                {
                    "action": "linkedin_contact_info_research",
                    "status": phase_status,
                    "count": inspected,
                    "detail": f"{inspected} profiles inspected; {found} emails found.",
                    "source_lane": "track_2",
                }
            )
            for value in _artifact_values(phase.get("artifacts") or []):
                if "contact-info-email-research" not in Path(value).name:
                    continue
                path = _resolve_run_reference(value, settings=settings, summary_path=summary_path)
                payload = _load_json_file(path)
                for result in list(payload.get("results") or []):
                    if not isinstance(result, dict):
                        continue
                    company = str(result.get("company") or contact_companies.get(str(result.get("contact_id") or ""), ""))
                    add(company, "linkedin_profiles_inspected_for_email", 1)
                    if str(result.get("status") or "") == "found" and result.get("email"):
                        add(company, "emails_found", 1)
        elif phase_name == "4_contact_mapping":
            for run in list(phase.get("runs") or []):
                if not isinstance(run, dict):
                    continue
                company = str(run.get("company") or "")
                path = _resolve_run_reference(run.get("artifact"), settings=settings, summary_path=summary_path)
                payload = _load_json_file(path)
                profiles = int(payload.get("count") or len(list(payload.get("results") or [])))
                contacts_added = int(run.get("contacts_added") or 0)
                add(company, "linkedin_profiles_mapped", profiles)
                add(company, "contacts_added", contacts_added)
                actions.append(
                    {
                        "action": "linkedin_company_contact_mapping",
                        "status": phase_status,
                        "company": company,
                        "count": profiles,
                        "detail": f"{profiles} LinkedIn profiles mapped; {contacts_added} contacts added.",
                        "artifact": str(path or ""),
                        "source_lane": "track_2",
                    }
                )
        elif phase_name == "5_send_linkedin_invites":
            for run in list(phase.get("runs") or []):
                if not isinstance(run, dict):
                    continue
                add(
                    str(run.get("company") or ""),
                    "contacts_added",
                    int(run.get("contacts_added") or 0),
                )
        elif phase_name == "7_context_enrichment" and phase_status == "ran":
            companies = [str(item) for item in list(phase.get("companies") or []) if str(item)]
            for company in companies[: int(phase.get("count") or len(companies))]:
                add(company, "company_context_enriched", 1)
    return actions, company_counts


def _company_execution_rows(
    invite_runs: list[dict[str, object]],
    followup_payloads: list[dict[str, object]],
    extra_counts: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, object]]:
    by_company: dict[str, dict[str, int]] = {}

    def add(company: str, key: str, amount: int) -> None:
        if not company or amount <= 0:
            return
        bucket = by_company.setdefault(company, {})
        bucket[key] = bucket.get(key, 0) + amount

    for run in invite_runs:
        add(str(run.get("company") or ""), "linkedin_invites_sent", int((run.get("status_counts") or {}).get("sent") or 0))
    for payload in followup_payloads:
        for item in list(payload.get("results") or []):
            if isinstance(item, dict) and str(item.get("status") or "") == "sent":
                key = "linkedin_replies_sent" if _message_type(item) == "reply" else "linkedin_followups_sent"
                add(str(item.get("company") or ""), key, 1)
    for company, counts in (extra_counts or {}).items():
        for key, amount in counts.items():
            add(company, key, int(amount or 0))
    rows = []
    for company, counts in by_company.items():
        summary = "; ".join(
            label.replace("_", " ") + f" {count}"
            for label, count in sorted(counts.items())
        )
        rows.append({"company": company, "counts": counts, "summary": summary})
    return sorted(rows, key=lambda row: row["company"].casefold())


def _write_comms_learning_artifact(
    *,
    workspace: Path,
    reports_dir: Path,
    report_stem: str,
    manually_cleared_items: list[dict[str, object]],
    followup_payloads: list[dict[str, object]],
    run_summary: Path | None,
    scope: str = "examples observed in this report run only",
) -> tuple[Path, dict[str, int]]:
    """Persist run-scoped LinkedIn examples with explicit gold/silver/negative labels."""
    examples: list[dict[str, object]] = []
    for item in manually_cleared_items:
        base = {"company": item.get("company", ""), "name": item.get("name", ""), "channel": "linkedin", "run_summary": str(run_summary or "")}
        examples.append({**base, "label": "gold", "message": item.get("manual_latest_message", ""), "reason": "manually sent LinkedIn message"})
        if str(item.get("draft_message") or "").strip():
            examples.append({**base, "label": "negative", "message": item.get("draft_message", ""), "reason": "generated draft replaced or cleared after manual send"})
    for payload in followup_payloads:
        sent_or_cleared = [
            item
            for item in list(payload.get("results") or [])
            if isinstance(item, dict) and str(item.get("status") or "") == "sent"
        ] + [
            item
            for item in list(payload.get("cleared_drafts") or [])
            if isinstance(item, dict)
        ]
        seen_messages: set[tuple[str, str, str]] = set()
        for item in sent_or_cleared:
            if not isinstance(item, dict):
                continue
            message = str(item.get("draft_message") or item.get("message") or "").strip()
            key = (str(item.get("company") or ""), str(item.get("name") or ""), message)
            if not message or key in seen_messages:
                continue
            seen_messages.add(key)
            examples.append({
                "company": item.get("company", ""), "name": item.get("name", ""), "channel": "linkedin", "run_summary": str(run_summary or ""),
                "label": "silver", "message": message, "reason": "approved or automatic draft sent",
            })
    examples = [item for item in examples if str(item.get("message") or "").strip()]
    summary = {label: sum(item["label"] == label for item in examples) for label in ("gold", "negative", "silver")}
    payload = {
        "report_run": report_stem,
        "scope": scope,
        "examples": examples,
        "summary": summary,
    }
    artifact = reports_dir / f"{report_stem}-comms-learning.json"
    artifact.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    corpus_dir = workspace / "comms_learning"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = corpus_dir / "linkedin_examples.jsonl"
    existing = set()
    if corpus_path.exists():
        existing = {line.strip() for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    additions = [json.dumps(item, sort_keys=True, ensure_ascii=True) for item in examples]
    new_lines = [line for line in additions if line not in existing]
    if new_lines:
        with corpus_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(new_lines) + "\n")
    return artifact, summary


def write_artifact_daily_report(
    *,
    settings: OutreachSettings,
    workspace: Path,
    since: datetime | None,
    nightly_summary_path: Path | None = None,
    title: str = "Outreach Daily Run Report",
) -> tuple[Path, Path, Path, Path]:
    """Write the daily HTML/MD report from the artifacts created by the active nightly runner."""
    if (since is None) != (nightly_summary_path is None):
        raise ValueError(
            "Run-scoped reporting requires both since and nightly_summary_path; "
            "omit both for a clearly labeled workspace snapshot."
        )
    run_scoped = since is not None and nightly_summary_path is not None
    report_mode = "run_scoped" if run_scoped else "workspace_snapshot"
    scope_note = (
        "Only artifacts explicitly referenced by the selected nightly summary, daily-engine manifest, and Track 2 phase manifest."
        if run_scoped
        else "Workspace artifact history for troubleshooting; this is not evidence for one run."
    )
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    counts = workbook.summary_counts()
    nightly_summary = _load_json_file(nightly_summary_path)
    maintenance = nightly_summary.get("outreach_maintenance") if isinstance(nightly_summary.get("outreach_maintenance"), dict) else {}
    track_artifact = _resolve_run_reference(
        (maintenance or {}).get("track_2_daily_run_artifact"),
        settings=settings,
        summary_path=nightly_summary_path,
    )
    track_payload = _load_json_file(track_artifact)
    if run_scoped:
        exact_artifacts, run_integrity = _exact_run_artifacts(
            nightly_summary=nightly_summary,
            nightly_summary_path=nightly_summary_path,
            settings=settings,
            track_payload=track_payload,
        )
        artifacts = _dedupe_paths(
            [path for paths in exact_artifacts.values() for path in paths]
        )
    else:
        artifacts = _artifacts_since(settings.artifacts_dir, None)
        exact_artifacts = {
            "invites": [path for path in artifacts if "invite-send-batch" in path.name],
            "followup_sends": [path for path in artifacts if "linkedin-followup-send-results" in path.name],
            "followup_drafts": [path for path in artifacts if "linkedin-followup-draft" in path.name],
            "reconcile": [path for path in artifacts if "linkedin-message-reconcile" in path.name],
            "email_sends": [path for path in artifacts if "email-send-results" in path.name],
            "email_drafts": [path for path in artifacts if "email-draft" in path.name],
        }
        run_integrity = {
            "artifact_selection": "workspace_history",
            "daily_engine_manifest": "",
            "daily_engine_manifest_status": "not_applicable",
            "exact_artifacts": {},
            "missing_artifacts": [],
        }
    manifest_status = str(run_integrity.get("daily_engine_manifest_status") or "")

    track_pointer_text = json.dumps(track_payload, sort_keys=True)

    invite_runs: list[dict[str, object]] = []
    invite_totals: dict[str, int] = {}
    for path in exact_artifacts["invites"]:
        payload = _load_json_file(path)
        rows = list(payload.get("results") or [])
        status_counts = _count_statuses(rows)
        for status, count in status_counts.items():
            invite_totals[status] = invite_totals.get(status, 0) + count
        invite_runs.append(
            {
                "company": str(payload.get("company") or ""),
                "artifact": str(path),
                "count": len(rows),
                "status_counts": status_counts,
                "source_lane": "track_2" if str(path) in track_pointer_text else "daily_engine",
            }
        )

    followup_runs: list[dict[str, object]] = []
    followup_payloads: list[dict[str, object]] = []
    pending_review_items: list[dict[str, object]] = []
    system_held_items: list[dict[str, object]] = []
    auto_handled: list[dict[str, object]] = []
    manual_outbound_by_contact: dict[str, dict[str, object]] = {}
    reconcile_runs: list[dict[str, object]] = []
    for path in exact_artifacts["reconcile"]:
        payload = _load_json_file(path)
        reconcile_runs.append(
            {
                "artifact": str(path),
                "thread_count": int(payload.get("thread_count") or 0),
                "new_result_count": int(payload.get("new_result_count") or 0),
                "filtered_result_count": int(payload.get("filtered_result_count") or len(list(payload.get("results") or []))),
                "status_counts": _count_statuses(list(payload.get("results") or [])),
            }
        )
        for item in list(payload.get("results") or []):
            if not isinstance(item, dict):
                continue
            contact_id = str(item.get("contact_id") or "")
            latest_message = str(item.get("latest_message") or "").strip()
            last_sender = str(item.get("last_sender") or item.get("live_last_sender") or "").strip().lower()
            if contact_id and latest_message and last_sender == "you":
                manual_outbound_by_contact[contact_id] = {
                    "company": item.get("company") or "",
                    "name": item.get("name") or "",
                    "contact_id": contact_id,
                    "latest_message": latest_message,
                    "artifact": str(path),
                }
    for path in exact_artifacts["followup_sends"]:
        payload = _load_json_file(path)
        followup_payloads.append(payload)
        followup_runs.append(
            {
                "artifact": str(path),
                "count": int(payload.get("count") or 0),
                "status_counts": payload.get("status_counts") or {},
                "touchpoints_added": int(payload.get("touchpoints_added") or 0),
                "source_lane": "track_2" if str(path) in track_pointer_text else "daily_engine",
            }
        )
        for item in list(payload.get("results") or []):
            if not isinstance(item, dict) or str(item.get("status") or "") != "sent":
                continue
            auto_handled.append(
                {
                    "company": str(item.get("company") or ""),
                    "person": str(item.get("name") or ""),
                    "contact_id": str(item.get("contact_id") or ""),
                    "message_type": _message_type(item),
                    "draft_kind": str(item.get("draft_kind") or ""),
                    "send_recommendation": str(item.get("send_recommendation") or ""),
                    "message": str(item.get("draft_message") or item.get("message") or ""),
                    "status": "sent",
                    "artifact": str(path),
                    "source_lane": "track_2" if str(path) in track_pointer_text else "daily_engine",
                }
            )
        for item in list(payload.get("skipped_by_recommendation") or []):
            if isinstance(item, dict):
                enriched = {**item, "source_artifact": str(path), "scope": "this_run"}
                recommendation = str(item.get("send_recommendation") or "")
                if recommendation in HUMAN_REVIEW_RECOMMENDATIONS:
                    pending_review_items.append(enriched)
                elif recommendation in SYSTEM_HOLD_RECOMMENDATIONS:
                    system_held_items.append(enriched)

    for path in exact_artifacts["followup_drafts"]:
        payload = _load_json_file(path)
        draft_rows = [
            item for item in list(payload.get("results") or []) if isinstance(item, dict)
        ] + [
            item for item in list(payload.get("cadence_held") or []) if isinstance(item, dict)
        ]
        for item in draft_rows:
            enriched = {**item, "source_artifact": str(path), "scope": "this_run"}
            recommendation = str(item.get("send_recommendation") or "")
            if recommendation in HUMAN_REVIEW_RECOMMENDATIONS:
                pending_review_items.append(enriched)
            elif recommendation in SYSTEM_HOLD_RECOMMENDATIONS:
                system_held_items.append(enriched)

    organization_names = {
        item.organization_id: item.name for item in workbook.list_organizations()
    }
    email_sends: list[dict[str, object]] = []
    for path in exact_artifacts["email_sends"]:
        payload = _load_json_file(path)
        for item in list(payload.get("results") or []):
            if not isinstance(item, dict):
                continue
            status = str(item.get("delivery_status") or item.get("status") or "")
            if status != "sent":
                continue
            email_sends.append(
                {
                    "company": str(
                        item.get("company")
                        or organization_names.get(str(item.get("organization_id") or ""), "")
                    ),
                    "person": str(item.get("name") or ""),
                    "contact_id": str(item.get("contact_id") or ""),
                    "email": str(item.get("email") or ""),
                    "subject": str(item.get("subject") or ""),
                    "status": "sent",
                    "artifact": str(path),
                }
            )
    sent_email_keys = {
        (
            str(item.get("contact_id") or ""),
            str(item.get("email") or "").casefold(),
            str(item.get("subject") or ""),
        )
        for item in email_sends
    }
    email_draft_count = 0
    for path in exact_artifacts["email_drafts"]:
        payload = _load_json_file(path)
        draft_results = list(payload.get("results") or [])
        email_draft_count += sum(isinstance(item, dict) for item in draft_results)
        for item in draft_results:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("contact_id") or ""),
                str(item.get("email") or "").casefold(),
                str(item.get("subject") or ""),
            )
            if key in sent_email_keys:
                continue
            normalized = {
                **item,
                "channel": "email",
                "name": str(item.get("name") or item.get("person") or item.get("recipient_name") or ""),
                "draft_message": str(item.get("body") or item.get("draft_message") or ""),
                "send_recommendation": "human_review",
                "source_artifact": str(path),
                "scope": "this_run",
            }
            decision = str(item.get("user_decision") or item.get("approval_status") or "").casefold()
            if decision in {"approve", "approved"}:
                normalized["send_recommendation"] = "approved_not_sent"
                system_held_items.append(normalized)
            else:
                pending_review_items.append(normalized)

    # The durable queue is a clearly labeled workspace snapshot. It may contain
    # older unresolved review items, but none of its rows count as this-run work.
    pending_queue_payload = _load_json_file(workspace / "linkedin_followup_pending_review.json")
    for item in list(pending_queue_payload.get("results") or []):
        if not isinstance(item, dict):
            continue
        enriched = {**item, "source_artifact": "workspace/linkedin_followup_pending_review.json", "scope": "carried_over"}
        recommendation = str(item.get("send_recommendation") or "")
        if recommendation in HUMAN_REVIEW_RECOMMENDATIONS:
            pending_review_items.append(enriched)
        elif recommendation in SYSTEM_HOLD_RECOMMENDATIONS:
            system_held_items.append(enriched)

    seen_review_keys: set[tuple[str, str, str]] = set()
    deduped_review_items: list[dict[str, object]] = []
    manually_cleared_items: list[dict[str, object]] = []
    for item in pending_review_items:
        contact_id = str(item.get("contact_id") or "")
        manual_outbound = manual_outbound_by_contact.get(contact_id)
        manual_message = str((manual_outbound or {}).get("latest_message") or "").strip()
        stale_latest_message = str(item.get("latest_message") or item.get("last_message") or "").strip()
        draft_message = str(item.get("draft_message") or "").strip()
        should_clear_as_manual = bool(
            manual_outbound
            and manual_message
            and (
                normalize_dedupe_text(manual_message) == normalize_dedupe_text(draft_message)
                or (
                    stale_latest_message
                    and normalize_dedupe_text(manual_message) != normalize_dedupe_text(stale_latest_message)
                )
            )
        )
        if should_clear_as_manual:
            cleared_item = dict(item)
            cleared_item["manual_latest_message"] = manual_message
            cleared_item["manual_source_artifact"] = (manual_outbound or {}).get("artifact", "")
            manually_cleared_items.append(cleared_item)
            continue
        key = _review_item_key(item)[1:]
        if key in seen_review_keys:
            continue
        seen_review_keys.add(key)
        deduped_review_items.append(item)

    sent_review_keys = {
        _review_item_key(item)
        for payload in followup_payloads
        for item in list(payload.get("results") or [])
        if isinstance(item, dict) and str(item.get("status") or "") == "sent"
    }
    deduped_review_items = [
        item for item in deduped_review_items if _review_item_key(item) not in sent_review_keys
    ]
    held_seen: set[tuple[str, str, str, str]] = set()
    deduped_held_items: list[dict[str, object]] = []
    for item in system_held_items:
        key = _review_item_key(item)
        if key in held_seen or key in sent_review_keys:
            continue
        held_seen.add(key)
        deduped_held_items.append(item)

    source_breakdown = (
        _source_breakdown(nightly_summary) if run_scoped else _unscoped_source_breakdown()
    )
    email_channel = (
        run_integrity.get("email_channel")
        if isinstance(run_integrity.get("email_channel"), dict)
        else {}
    )
    raw_email_blockers = email_channel.get("blockers") or []
    if isinstance(raw_email_blockers, str):
        email_blockers = [raw_email_blockers] if raw_email_blockers.strip() else []
    else:
        email_blockers = [str(item) for item in raw_email_blockers if str(item).strip()]
    email_channel_status = "not_scoped" if not run_scoped else str(email_channel.get("status") or "")
    if run_scoped and not email_channel_status:
        if email_blockers:
            email_channel_status = "blocked"
        elif email_sends:
            email_channel_status = "ran"
        elif email_draft_count:
            email_channel_status = "review_required"
        else:
            email_channel_status = "skipped"
    source_breakdown.append(
        {
            "source": "Cold email channel",
            "status": email_channel_status,
            "raw": email_draft_count,
            "kept": len(email_sends),
            "details": {
                **email_channel,
                "drafts_created": email_draft_count,
                "emails_sent": len(email_sends),
                "blockers": email_blockers,
            },
        }
    )
    startup_source = next(
        (row for row in source_breakdown if row.get("source") == "Startup sources"),
        {},
    )
    startup_details = startup_source.get("details") if isinstance(startup_source.get("details"), dict) else {}
    discovery_rows = list(startup_details.get("adapters") or [])

    if run_scoped:
        campaign_artifact = _resolve_run_reference(
            (maintenance or {}).get("campaign_plan_artifact"),
            settings=settings,
            summary_path=nightly_summary_path,
        )
        campaign_payload = _load_json_file(campaign_artifact)
    else:
        campaign_artifact, campaign_payload = _latest_artifact_matching(artifacts, "account-campaign-plan")
    campaign_rows = [row for row in list(campaign_payload.get("results") or []) if isinstance(row, dict)]
    campaign_summary = campaign_payload.get("summary") if isinstance(campaign_payload.get("summary"), dict) else {}

    if run_scoped:
        enrichment_artifact = _resolve_run_reference(
            (maintenance or {}).get("context_enrichment_artifact"),
            settings=settings,
            summary_path=nightly_summary_path,
        )
        enrichment_payload = _load_json_file(enrichment_artifact)
    else:
        enrichment_artifact, enrichment_payload = _latest_artifact_matching(artifacts, "company-context-enrichment")
    enrichment_summary = enrichment_payload.get("summary") if isinstance(enrichment_payload.get("summary"), dict) else {}

    if run_scoped:
        website_artifact = _resolve_run_reference(
            (maintenance or {}).get("website_resolution_artifact"),
            settings=settings,
            summary_path=nightly_summary_path,
        )
        website_payload = _load_json_file(website_artifact)
    else:
        website_artifact, website_payload = _latest_artifact_matching(artifacts, "company-website-resolution")
    website_summary = website_payload.get("summary") if isinstance(website_payload.get("summary"), dict) else {}

    source_metrics_path = _resolve_run_reference(
        nightly_summary.get("source_metrics"),
        settings=settings,
        summary_path=nightly_summary_path,
    )
    source_metrics = _load_json_file(source_metrics_path)
    required_pointer_errors: list[str] = []
    if run_scoped:
        if source_metrics_path is None or not source_metrics:
            required_pointer_errors.append("nightly_summary.source_metrics missing_or_invalid")
        manifest_source_metrics = _resolve_run_reference(
            run_integrity.get("manifest_source_metrics"),
            settings=settings,
            summary_path=nightly_summary_path,
        )
        if manifest_status in {"loaded", "inline"} and (
            manifest_source_metrics is None or not _load_json_file(manifest_source_metrics)
        ):
            required_pointer_errors.append("daily_engine_manifest.source_metrics missing_or_invalid")
        elif source_metrics_path is not None and manifest_source_metrics is not None:
            summary_metrics_key = str(source_metrics_path.resolve()) if source_metrics_path.exists() else str(source_metrics_path)
            manifest_metrics_key = str(manifest_source_metrics.resolve()) if manifest_source_metrics.exists() else str(manifest_source_metrics)
            if summary_metrics_key != manifest_metrics_key:
                required_pointer_errors.append("source_metrics pointer_mismatch")
        manifest_action_queue = _resolve_run_reference(
            run_integrity.get("manifest_action_queue"),
            settings=settings,
            summary_path=nightly_summary_path,
        )
        if manifest_status in {"loaded", "inline"} and (
            manifest_action_queue is None or not _load_json_file(manifest_action_queue)
        ):
            required_pointer_errors.append("daily_engine_manifest.action_queue missing_or_invalid")
        manifest_email_sent_count = int(email_channel.get("sent_count") or 0)
        if email_channel.get("draft_count") is not None and int(email_channel.get("draft_count") or 0) != email_draft_count:
            required_pointer_errors.append("email_channel draft_count pointer_mismatch")
        if email_channel_status == "sent" and not email_sends:
            required_pointer_errors.append("email_channel claims sent without exact send result")
        if manifest_email_sent_count != len(email_sends):
            required_pointer_errors.append("email_channel sent_count pointer_mismatch")
    run_integrity["source_metrics"] = str(source_metrics_path or "")
    run_integrity["required_pointer_errors"] = required_pointer_errors
    stage_metrics = source_metrics.get("stage_metrics") if isinstance(source_metrics.get("stage_metrics"), dict) else {}
    jobspy_metrics = nightly_summary.get("jobspy_metrics") if isinstance(nightly_summary.get("jobspy_metrics"), dict) else {}
    if run_scoped:
        company_discovery = _load_json_file(_resolve_run_reference((maintenance or {}).get("company_discovery_artifact"), settings=settings, summary_path=nightly_summary_path))
        role_surface = _load_json_file(_resolve_run_reference((maintenance or {}).get("role_surface_artifact"), settings=settings, summary_path=nightly_summary_path))
        cadence_report = _load_json_file(_resolve_run_reference((maintenance or {}).get("cadence_report_artifact"), settings=settings, summary_path=nightly_summary_path))
        outcome_learning = _load_json_file(_resolve_run_reference((maintenance or {}).get("outcome_learning_artifact"), settings=settings, summary_path=nightly_summary_path))
    else:
        _, company_discovery = _latest_artifact_matching(artifacts, "company-discovery-review")
        _, role_surface = _latest_artifact_matching(artifacts, "role-surface-report")
        _, cadence_report = _latest_artifact_matching(artifacts, "outreach-cadence-report")
        _, outcome_learning = _latest_artifact_matching(artifacts, "outcome-learning-report")

    reports_dir = _reports_dir(settings)
    daily_html_dir = _daily_html_reports_dir(settings)
    reports_dir.mkdir(parents=True, exist_ok=True)
    daily_html_dir.mkdir(parents=True, exist_ok=True)
    report_stem = f"{artifact_timestamp()}-daily-run-report"
    track_execution = _track_2_execution_status(maintenance or {}, track_payload)
    track_linkedin_actions, track_company_counts = _track_2_actual_actions(
        track_payload=track_payload,
        settings=settings,
        summary_path=nightly_summary_path,
        workbook=workbook,
    )
    for item in email_sends:
        company = str(item.get("company") or "")
        if not company:
            continue
        counts_for_company = track_company_counts.setdefault(company, {})
        counts_for_company["emails_sent"] = counts_for_company.get("emails_sent", 0) + 1
    company_execution = _company_execution_rows(
        invite_runs,
        followup_payloads,
        track_company_counts,
    )
    auto_reply_contact_ids = {
        str(item.get("contact_id") or "")
        for item in auto_handled
        if item.get("message_type") == "reply" and item.get("contact_id")
    }
    inbox_action_path, open_inbox_actions = _sync_open_inbox_actions(
        workspace,
        workbook,
        auto_handled_contact_ids=auto_reply_contact_ids,
    )
    review_contact_ids = {
        str(item.get("contact_id") or "")
        for item in deduped_review_items
        if item.get("contact_id")
    }
    what_needs_you = [
        {**action, "scope": "workspace_open_queue"}
        for action in open_inbox_actions
        if action.get("action_type") != "inbound_reply"
        or action.get("contact_id") not in review_contact_ids
    ]

    linkedin_actions: list[dict[str, object]] = []
    for run in invite_runs:
        linkedin_actions.append(
            {
                "action": "linkedin_invites",
                "status": "ran",
                "company": run.get("company", ""),
                "count": int((run.get("status_counts") or {}).get("sent") or 0),
                "status_counts": run.get("status_counts") or {},
                "detail": _render_status_counts(run.get("status_counts") or {}),
                "artifact": run.get("artifact", ""),
                "source_lane": run.get("source_lane", ""),
            }
        )
    for run in reconcile_runs:
        linkedin_actions.append(
            {
                "action": "linkedin_inbox_refresh",
                "status": "ran",
                "count": run["thread_count"],
                "detail": (
                    f"{run['thread_count']} threads scanned; {run['new_result_count']} results detected; "
                    f"{run['filtered_result_count']} retained for this lane; "
                    f"statuses {_render_status_counts(run['status_counts'])}."
                ),
                "artifact": run["artifact"],
            }
        )
    for item in auto_handled:
        linkedin_actions.append(
            {
                "action": f"linkedin_{item['message_type']}_sent",
                "status": "sent",
                "company": item["company"],
                "person": item["person"],
                "count": 1,
                "detail": item["message"],
                "artifact": item["artifact"],
                "source_lane": item["source_lane"],
            }
        )
    linkedin_actions.extend(track_linkedin_actions)
    for item in deduped_review_items:
        if item.get("channel") == "email":
            continue
        linkedin_actions.append(
            {
                "action": "linkedin_message_review_required",
                "status": "review_required",
                "company": item.get("company", ""),
                "person": item.get("name", ""),
                "count": 1,
                "detail": item.get("draft_message", ""),
                "scope": item.get("scope", "this_run"),
            }
        )
    for item in deduped_held_items:
        if item.get("channel") == "email":
            continue
        linkedin_actions.append(
            {
                "action": "linkedin_message_system_hold",
                "status": str(item.get("send_recommendation") or "hold"),
                "company": item.get("company", ""),
                "person": item.get("name", ""),
                "count": 1,
                "detail": item.get("draft_message", ""),
                "scope": item.get("scope", "this_run"),
            }
        )
    for source_label, action_name in (
        ("LinkedIn home feed", "linkedin_home_feed_capture"),
        ("LinkedIn profile viewers", "linkedin_profile_viewer_capture"),
    ):
        source_row = next((row for row in source_breakdown if row.get("source") == source_label), {})
        linkedin_actions.append(
            {
                "action": action_name,
                "status": str(source_row.get("status") or "skipped"),
                "count": int(source_row.get("raw") or 0),
                "detail": _source_summary(source_row),
            }
        )

    track_2_returncode = track_execution.get("returncode")
    track_2_failed = str(track_execution.get("status") or "").startswith("failed") or track_execution.get("status") == "partial_failed"
    failures = list(nightly_summary.get("failures") or [])
    track_status = str(track_execution.get("status") or "not_run")
    track_complete = track_status in {"completed", "completed_zero_actions"}
    daily_engine_returncode = nightly_summary.get("daily_engine_returncode")
    if not run_scoped:
        run_status = "workspace_snapshot"
    elif (
        failures
        or track_2_failed
        or not track_complete
        or daily_engine_returncode != 0
        or run_integrity.get("missing_artifacts")
        or required_pointer_errors
    ):
        run_status = "failed_or_incomplete"
    elif manifest_status not in {"loaded", "inline"}:
        run_status = "incomplete_missing_daily_engine_manifest"
    else:
        run_status = "completed"
    pending_company_reviews = int(
        (company_discovery.get("workspace_summary") or {}).get("pending_review") or 0
    )
    if pending_company_reviews:
        what_needs_you.append(
            {
                "action_type": "company_discovery_review",
                "priority": "low",
                "company": "Company discovery",
                "person": "",
                "message": "",
                "recommended_action": (
                    f"Review {pending_company_reviews} company-discovery candidates before promotion."
                ),
                "count": pending_company_reviews,
                "scope": "workspace_open_queue",
            }
        )
    if deduped_review_items:
        email_reviews = sum(item.get("channel") == "email" for item in deduped_review_items)
        linkedin_reviews = len(deduped_review_items) - email_reviews
        what_needs_you.append(
            {
                "action_type": "message_review",
                "priority": "high" if email_reviews else "medium",
                "company": "Message review",
                "person": "",
                "message": "",
                "recommended_action": (
                    f"Review {len(deduped_review_items)} unsent drafts in Messages to review "
                    f"({linkedin_reviews} LinkedIn, {email_reviews} email)."
                ),
                "count": len(deduped_review_items),
                "scope": "workspace_open_queue",
            }
        )
    for blocker in email_blockers:
        what_needs_you.append(
            {
                "action_type": "email_channel_blocker",
                "priority": "high",
                "company": "Cold email channel",
                "person": "",
                "message": "",
                "recommended_action": blocker,
                "count": 1,
                "scope": "run_configuration",
            }
        )
    messages_sent = len(auto_handled)
    replies_sent = sum(item.get("message_type") == "reply" for item in auto_handled)
    followups_sent = messages_sent - replies_sent
    invites_sent = int(invite_totals.get("sent") or 0)
    emails_sent = len(email_sends)
    total_outbound_sends = invites_sent + messages_sent + emails_sent
    email_actions: list[dict[str, object]] = [
        {
            "action": "cold_email_sent",
            "status": "sent",
            "company": item.get("company", ""),
            "person": item.get("person", ""),
            "email": item.get("email", ""),
            "count": 1,
            "detail": item.get("subject", ""),
            "artifact": item.get("artifact", ""),
        }
        for item in email_sends
    ]
    email_actions.extend(
        {
            "action": "cold_email_draft_review",
            "status": "review_required",
            "company": item.get("company", ""),
            "person": item.get("name", ""),
            "email": item.get("email", ""),
            "count": 1,
            "detail": item.get("subject", ""),
            "artifact": item.get("source_artifact", ""),
        }
        for item in deduped_review_items
        if item.get("channel") == "email"
    )
    email_actions.extend(
        {
            "action": "cold_email_approved_not_sent",
            "status": str(item.get("send_recommendation") or "approved_not_sent"),
            "company": item.get("company", ""),
            "person": item.get("name", ""),
            "email": item.get("email", ""),
            "count": 1,
            "detail": item.get("subject", ""),
            "artifact": item.get("source_artifact", ""),
        }
        for item in deduped_held_items
        if item.get("channel") == "email"
    )
    email_actions.extend(
        {
            "action": "cold_email_channel_blocker",
            "status": "blocked",
            "company": "",
            "person": "",
            "email": "",
            "count": 1,
            "detail": blocker,
            "artifact": "",
        }
        for blocker in email_blockers
    )
    comms_artifact, comms_summary = _write_comms_learning_artifact(
        workspace=workspace,
        reports_dir=reports_dir,
        report_stem=report_stem,
        manually_cleared_items=manually_cleared_items,
        followup_payloads=followup_payloads,
        run_summary=nightly_summary_path,
        scope=(
            "examples from exact artifacts referenced by this nightly run"
            if run_scoped
            else "examples found across workspace artifact history; not one run"
        ),
    )
    style_sync_summary = sync_comms_learning_into_style_profile(
        profile_path=workspace / "communication_style_profile.yml",
        examples_path=workspace / "comms_learning" / "linkedin_examples.jsonl",
        contacts=workbook.list_contacts(),
        organizations=workbook.list_organizations(),
    ).as_dict()
    report_payload = {
        "created_at": utc_now_iso(),
        "report_mode": report_mode,
        "scope_note": scope_note,
        "since": since.isoformat(timespec="seconds") if since else "",
        "workspace": str(workspace),
        "nightly_summary": str(nightly_summary_path or ""),
        "run_status": run_status,
        "run_integrity": run_integrity,
        "workspace_counts": counts,
        "discovery": discovery_rows,
        "stage_metrics": stage_metrics,
        "jobspy_metrics": jobspy_metrics,
        "generation_selected_count": nightly_summary.get("generation_selected_count", ""),
        "generation_ran": nightly_summary.get("generation_ran", ""),
        "invite_runs": invite_runs,
        "invite_totals": invite_totals,
        "followup_runs": followup_runs,
        "pending_review_count": len(deduped_review_items),
        "messages_to_review": deduped_review_items,
        "system_held_messages": deduped_held_items,
        "auto_handled": auto_handled,
        "email_sends": email_sends,
        "email_actions": email_actions,
        "linkedin_actions": linkedin_actions,
        "what_needs_you": what_needs_you,
        "open_inbox_actions": open_inbox_actions,
        "inbox_action_queue": str(inbox_action_path),
        "manually_cleared_review_count": len(manually_cleared_items),
        "manually_cleared_review_items": manually_cleared_items,
        "campaign_summary": campaign_summary,
        "campaign_artifact": str(campaign_artifact or ""),
        "campaign_rows": campaign_rows,
        "company_execution": company_execution,
        "track_2_failed": track_2_failed,
        "track_2_returncode": track_2_returncode,
        "track_2_execution": track_execution,
        "run_outcome": {
            "total_outbound_sends": total_outbound_sends,
            "linkedin_invites_sent": invites_sent,
            "linkedin_followups_sent": followups_sent,
            "linkedin_replies_sent": replies_sent,
            "emails_sent": emails_sent,
            "companies_touched": len(company_execution),
            "human_actions_open": len(what_needs_you),
            "messages_to_review": len(deduped_review_items),
            "system_held_messages": len(deduped_held_items),
        },
        "enrichment_summary": enrichment_summary,
        "enrichment_artifact": str(enrichment_artifact or ""),
        "website_summary": website_summary,
        "website_artifact": str(website_artifact or ""),
        "artifact_count": len(artifacts),
        "source_breakdown": source_breakdown,
        "comms_learning_artifact": str(comms_artifact),
        "comms_learning_summary": comms_summary,
        "style_profile_sync": style_sync_summary,
        "company_discovery": company_discovery,
        "role_surface": role_surface,
        "cadence_report": cadence_report,
        "outcome_learning": outcome_learning,
    }
    summary_artifact = reports_dir / f"{report_stem}.json"
    summary_artifact.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    report_path = reports_dir / f"{report_stem}.md"
    latest_path = reports_dir / "daily_run_report.md"
    legacy_latest_path = settings.resolved_tracking_workspace_dir / "daily_run_report.md"
    report_html_path = daily_html_dir / f"{report_stem}.html"
    latest_html_path = daily_html_dir / "daily_run_report.html"
    reports_latest_html_path = reports_dir / "daily_run_report.html"
    legacy_latest_html_path = settings.resolved_tracking_workspace_dir / "daily_run_report.html"

    lines = [
        f"# {title}{'' if run_scoped else ' — Workspace Snapshot'}",
        "",
        f"- Created: `{report_payload['created_at']}`",
        f"- Report mode: `{report_mode}`",
        f"- Run status: `{run_status}`",
        f"- Scope: {scope_note}",
        f"- Run started: `{report_payload['since'] or 'workspace history'}`",
        f"- Workspace counts: `{counts}`",
        f"- Nightly summary: `{nightly_summary_path or ''}`",
        f"- Daily-engine manifest: `{run_integrity.get('daily_engine_manifest') or 'not recorded'}` ({manifest_status or 'not applicable'})",
        f"- Report artifact: `{summary_artifact}`",
        "",
        "## Run outcome",
        "",
        f"- Total outbound sends: `{total_outbound_sends}`",
        f"- LinkedIn invites sent: `{invites_sent}`",
        f"- LinkedIn follow-ups sent: `{followups_sent}`",
        f"- LinkedIn replies sent: `{replies_sent}`",
        f"- Cold emails sent: `{emails_sent}`",
        f"- Companies actually touched: `{len(company_execution)}`",
        f"- App generation selected: `{nightly_summary.get('generation_selected_count', '')}`",
        f"- Track 2 execution: `{track_execution['status']}` (return code `{track_2_returncode}`)",
    ]
    if run_integrity.get("missing_artifacts"):
        lines.append(f"- Missing exact artifacts: `{run_integrity['missing_artifacts']}`")
    if required_pointer_errors:
        lines.append(f"- Required pointer errors: `{required_pointer_errors}`")

    lines.extend(["", "## What needs you", ""])
    if what_needs_you:
        for action in what_needs_you:
            lines.append(
                f"- **{str(action.get('priority') or 'medium').upper()} · {action.get('company', '')} · {action.get('person', '')}** — {action.get('recommended_action', '')}"
            )
            if action.get("message"):
                lines.append(f"  - Inbound: {action['message']}")
    else:
        lines.append("- No open human action was detected.")
    lines.append(
        f"- Resolve or snooze inbox items in `{inbox_action_path}`; non-open rows stop appearing here."
    )

    lines.extend(["", "## Messages to review", ""])
    if deduped_review_items:
        for item in deduped_review_items:
            lines.append(
                f"- **{item.get('channel', 'linkedin')} · {item.get('company', '')} · {item.get('name', '')} · {item.get('scope', 'this_run')}**"
            )
            if item.get("email") or item.get("subject"):
                lines.append(
                    f"  - Recipient: {item.get('email', '')}; subject: {item.get('subject', '')}"
                )
            lines.append(
                f"  - Last message: {item.get('latest_message') or item.get('last_message') or ''}"
            )
            lines.append(f"  - Draft: {item.get('draft_message', '')}")
            lines.append(
                f"  - Gate: `{item.get('send_recommendation', '')}`; this was not auto-sent."
            )
    else:
        lines.append("- No message currently requires human review.")
    lines.append(
        f"- System-held (no human action yet): `{len(deduped_held_items)}`."
    )

    lines.extend(["", "## Auto-handled messages (this run)", ""])
    if auto_handled:
        for item in auto_handled:
            lines.append(
                f"- **{item['company']} · {item['person']}** — {item['message_type']} `sent` via {item['source_lane']}."
            )
            lines.append(f"  - Sent: {item['message']}")
    else:
        lines.append("- No LinkedIn follow-up or reply was auto-sent in this run.")

    lines.extend(["", "## Cold email actions (this run)", ""])
    if email_actions:
        for action in email_actions:
            lines.append(
                f"- **{action.get('company', '')} · {action.get('person', '')}** — "
                f"{str(action.get('action') or '').replace('_', ' ')} `{action.get('status', '')}`. "
                f"{action.get('detail', '')}"
            )
    else:
        lines.append("- Cold email channel: `skipped` · 0 drafts · 0 sends.")

    lines.extend(["", "## LinkedIn actions (this run)", ""])
    if linkedin_actions:
        for action in linkedin_actions:
            subject = " · ".join(
                value
                for value in [str(action.get("company") or ""), str(action.get("person") or "")]
                if value
            )
            prefix = f"{subject}: " if subject else ""
            lines.append(
                f"- {prefix}{str(action.get('action') or '').replace('_', ' ')} — `{action.get('status', '')}` · `{action.get('count', 0)}`. {action.get('detail', '')}"
            )
    else:
        lines.append("- LinkedIn work was not recorded for this run.")

    lines.extend(["", "## Execution by company (this run)", ""])
    if company_execution:
        for row in company_execution:
            lines.append(f"- **{row['company']}** — {row['summary']}")
    else:
        lines.append("- No per-company execution was recorded.")

    lines.extend(["", "## Planned next (not executed)", ""])
    if campaign_rows:
        grouped: dict[str, list[str]] = {}
        for row in campaign_rows:
            grouped.setdefault(str(row.get("campaign_action") or "Other"), []).append(str(row.get("company") or ""))
        for action, companies in sorted(grouped.items()):
            lines.append(f"- {action}: {', '.join(companies[:12])}")
    else:
        lines.append("- No campaign plan was generated.")

    lines.extend(["", "## Discovery and source health", ""])
    for row in source_breakdown:
        lines.append(
            f"- {row['source']}: `{row['status']}` · kept `{row['kept']}` / raw `{row['raw']}`"
            + (f" — {_source_summary(row)}" if _source_summary(row) else "")
        )
    lines.extend(["", "### Startup adapter and lane detail", ""])
    if discovery_rows:
        for row in discovery_rows:
            lines.append(
                f"- {row.get('source', '')} · `{row.get('lane', '')}` · `{row.get('status', '')}` — "
                f"fetched `{row.get('fetched', 0)}`, discovered `{row.get('discovered', 0)}`, selected/new `{row.get('selected', 0)}`."
            )
    else:
        lines.append("- Startup adapters did not run or did not record exact artifacts.")
    if jobspy_metrics:
        lines.append(
            f"- JobSpy detail: `{jobspy_metrics.get('raw_jobs', 0)}` scanned; "
            f"`{jobspy_metrics.get('jobspy_app_score_now', 0)}` score-now; "
            f"`{jobspy_metrics.get('jobspy_app_review', 0)}` review."
        )
    lines.extend(["", "## Maintenance completed", ""])
    lines.append(f"- Website resolution: `{website_summary}`")
    lines.append(f"- Company context enrichment: `{enrichment_summary}`")
    lines.append(f"- Campaign plan created: `{campaign_summary}`")
    lines.extend(["", "## Manually cleared messages", ""])
    if manually_cleared_items:
        for item in manually_cleared_items:
            lines.append(
                f"- {item.get('company', '')} / {item.get('name', '')}: already sent manually."
            )
            lines.append(f"  - Sent msg: {item.get('manual_latest_message', '')}")
            stale_latest = str(item.get("latest_message") or item.get("last_message") or "").strip()
            if stale_latest:
                lines.append(f"  - Previous last msg: {stale_latest}")
    else:
        lines.append("- No manually cleared review items.")
    lines.extend([
        "",
        "## Comms Learning" + (" (exact run artifacts)" if run_scoped else " (workspace artifact history)"),
        "",
    ])
    lines.append(f"- Gold (manual sends): `{comms_summary['gold']}`")
    lines.append(f"- Negative (replaced/cleared drafts): `{comms_summary['negative']}`")
    lines.append(f"- Silver (approved/automatic drafts sent): `{comms_summary['silver']}`")
    lines.append(f"- Reusable style profile sync: `{style_sync_summary}`")
    lines.append(f"- Reusable corpus artifact: `{comms_artifact}`")
    report_text = "\n".join(lines).rstrip() + "\n"
    report_path.write_text(report_text, encoding="utf-8")
    latest_path.write_text(report_text, encoding="utf-8")
    legacy_latest_path.write_text(report_text, encoding="utf-8")

    def esc(value: object) -> str:
        return html.escape(str(value))

    stage_rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{esc((metric or {}).get('status', ''))}</td><td>{esc((metric or {}).get('runtime_seconds', ''))}</td></tr>"
        for name, metric in stage_metrics.items()
        if isinstance(metric, dict)
    )
    source_breakdown_table = "".join(
        f"<tr><td>{esc(row['source'])}</td><td>{esc(row['status'])}</td><td>{esc(row['kept'])}</td><td>{esc(row['raw'])}</td><td>{esc(_source_summary(row))}</td></tr>"
        for row in source_breakdown
    )
    linkedin_action_rows = "".join(
        "<tr>"
        f"<td>{esc(str(item.get('action') or '').replace('_', ' '))}</td>"
        f"<td>{esc(item.get('company', ''))}</td>"
        f"<td>{esc(item.get('person', ''))}</td>"
        f"<td>{esc(item.get('status', ''))}</td>"
        f"<td>{esc(item.get('count', 0))}</td>"
        f"<td>{esc(item.get('detail', ''))}</td>"
        "</tr>"
        for item in linkedin_actions
    )
    execution_cards = "".join(
        "<section class='card'>"
        f"<h3>{esc(row['company'])}</h3>"
        f"<p>{esc(row['summary'])}</p>"
        "</section>"
        for row in company_execution
    )
    inbox_action_cards = "".join(
        "<section class='review-card'>"
        f"<div class='review-meta'>{esc(action['priority'].upper())} · {esc(action['company'])} · {esc(action['person'])}</div>"
        f"<p><strong>Do:</strong> {esc(action['recommended_action'])}</p>"
        f"<div class='last-message'><strong>Inbound</strong><span>{esc(action['message'])}</span></div>"
        "</section>"
        for action in what_needs_you
    )
    review_rows = "".join(
        (
            "<tr>"
            f"<td>{esc(item.get('channel', 'linkedin'))}</td>"
            f"<td>{esc(item.get('company', ''))}</td>"
            f"<td>{esc(item.get('name', ''))}</td>"
            f"<td>{esc(item.get('email', ''))}</td>"
            f"<td>{esc(item.get('subject', ''))}</td>"
            f"<td>{esc(item.get('send_recommendation', ''))}</td>"
            f"<td>{esc(item.get('scope', 'this_run'))}</td>"
            f"<td>{esc(item.get('latest_message') or item.get('last_message') or '')}</td>"
            f"<td>{esc(item.get('draft_message', ''))}</td>"
            "</tr>"
        )
        for item in deduped_review_items
    )
    auto_handled_rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('company', ''))}</td>"
        f"<td>{esc(item.get('person', ''))}</td>"
        f"<td>{esc(item.get('message_type', ''))}</td>"
        f"<td>{esc(item.get('status', ''))}</td>"
        f"<td>{esc(item.get('message', ''))}</td>"
        "</tr>"
        for item in auto_handled
    )
    email_action_rows = "".join(
        "<tr>"
        f"<td>{esc(str(item.get('action') or '').replace('_', ' '))}</td>"
        f"<td>{esc(item.get('company', ''))}</td>"
        f"<td>{esc(item.get('person', ''))}</td>"
        f"<td>{esc(item.get('email', ''))}</td>"
        f"<td>{esc(item.get('status', ''))}</td>"
        f"<td>{esc(item.get('detail', ''))}</td>"
        "</tr>"
        for item in email_actions
    )
    startup_adapter_rows = "".join(
        "<tr>"
        f"<td>{esc(item.get('source', ''))}</td>"
        f"<td>{esc(item.get('lane', ''))}</td>"
        f"<td>{esc(item.get('status', ''))}</td>"
        f"<td>{esc(item.get('fetched', 0))}</td>"
        f"<td>{esc(item.get('discovered', 0))}</td>"
        f"<td>{esc(item.get('selected', 0))}</td>"
        "</tr>"
        for item in discovery_rows
    )
    html_source_heading = (
        "Source Breakdown (this run)"
        if run_scoped
        else "Source Breakdown (not scoped — no nightly run selected)"
    )
    html_comms_heading = (
        "Comms Learning (exact run artifacts)"
        if run_scoped
        else "Comms Learning (workspace artifact history)"
    )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}{'' if run_scoped else ' — Workspace Snapshot'}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fa; color: #15171a; }}
    header {{ background: #102033; color: white; padding: 28px 36px; }}
    header h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    header p {{ margin: 0; color: #dbe4ef; }}
    main {{ max-width: 1240px; margin: 0 auto; padding: 28px; }}
    section {{ margin-bottom: 22px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 16px; }}
    .card {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05); }}
    h2 {{ margin: 0 0 12px; font-size: 19px; }}
    h3 {{ margin: 0 0 10px; font-size: 15px; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf2f7; text-align: left; vertical-align: top; }}
    th {{ background: #f1f5f9; font-size: 12px; text-transform: uppercase; color: #475569; letter-spacing: 0; }}
    code {{ background: #edf2f7; padding: 2px 5px; border-radius: 4px; }}
    ul {{ margin: 0; padding-left: 18px; }}
    .metric-list {{ list-style: none; padding: 0; display: grid; gap: 8px; }}
    .metric-list li {{ display: flex; justify-content: space-between; gap: 16px; border-bottom: 1px solid #edf2f7; padding-bottom: 6px; }}
    .metric-list span {{ color: #475569; }}
    .action-list {{ list-style: none; padding: 0; display: grid; gap: 10px; }}
    .action-list li {{ display: grid; gap: 4px; border: 1px solid #edf2f7; border-radius: 6px; padding: 10px; }}
    .action-list span {{ color: #0f766e; font-weight: 700; }}
    .review-table td:nth-child(4), .review-table td:nth-child(5) {{ max-width: 360px; }}
  </style>
</head>
<body>
  <header>
    <h1>{esc(title)}{'' if run_scoped else ' — Workspace Snapshot'}</h1>
    <p>Mode {esc(report_mode)} · status {esc(run_status)} · created {esc(report_payload['created_at'])} · run started {esc(report_payload['since'] or 'workspace history')}</p>
    <p>{esc(scope_note)}</p>
  </header>
  <main>
    <section class="grid">
      <div class="card"><h2>Actual outbound sends</h2><p><strong>{esc(total_outbound_sends)}</strong> total · {esc(invites_sent)} invites · {esc(followups_sent)} follow-ups · {esc(replies_sent)} replies · {esc(emails_sent)} emails</p></div>
      <div class="card"><h2>Open actions for you</h2><p><strong>{esc(len(what_needs_you))}</strong> human actions · <strong>{esc(len(deduped_review_items))}</strong> messages to review</p><p><small>Persistent inbox queue: {esc(inbox_action_path)}</small></p></div>
      <div class="card"><h2>Track 2</h2><p><strong>{esc(track_execution['status'])}</strong></p><p>Return code {esc(track_2_returncode)}. Planned counts are never shown as completed work.</p></div>
    </section>
    <section class="grid">
      <div class="card"><h2>Run integrity</h2><p><strong>{esc(run_integrity.get('artifact_selection', ''))}</strong></p><p>Daily-engine manifest: {esc(manifest_status)} · missing exact artifacts: {esc(len(run_integrity.get('missing_artifacts') or []))} · required pointer errors: {esc(len(required_pointer_errors))}</p></div>
      <div class="card"><h2>Company review</h2><p><strong>{esc((company_discovery.get('workspace_summary') or {}).get('pending_review', 0))}</strong> candidates await a disposition before promotion.</p></div>
      <div class="card"><h2>Run health</h2><p>Daily engine <code>{esc(nightly_summary.get('daily_engine_returncode', ''))}</code> · JobSpy score-now <code>{esc(jobspy_metrics.get('jobspy_app_score_now', 0))}</code></p></div>
    </section>
    <section><h2>What needs you</h2>{inbox_action_cards or '<div class="card">No open human action.</div>'}</section>
    <section><h2>Messages to review</h2>{review_rows and '<table class="review-table"><thead><tr><th>Channel</th><th>Company</th><th>Person</th><th>Email</th><th>Subject</th><th>Gate</th><th>Scope</th><th>Last msg</th><th>Draft</th></tr></thead><tbody>' + review_rows + '</tbody></table>' or '<div class="card">No message currently requires human review.</div>'}<p>System-held, no action yet: <strong>{esc(len(deduped_held_items))}</strong>.</p></section>
    <section><h2>Auto-handled messages (this run)</h2>{auto_handled_rows and '<table><thead><tr><th>Company</th><th>Person</th><th>Type</th><th>Status</th><th>Sent message</th></tr></thead><tbody>' + auto_handled_rows + '</tbody></table>' or '<div class="card">No LinkedIn follow-up or reply was auto-sent.</div>'}</section>
    <section><h2>Cold email actions (this run)</h2>{email_action_rows and '<table><thead><tr><th>Action</th><th>Company</th><th>Person</th><th>Email</th><th>Status</th><th>Detail</th></tr></thead><tbody>' + email_action_rows + '</tbody></table>' or '<div class="card">Cold email channel skipped: 0 drafts, 0 sends.</div>'}</section>
    <section><h2>LinkedIn actions (this run)</h2>{linkedin_action_rows and '<table><thead><tr><th>Action</th><th>Company</th><th>Person</th><th>Status</th><th>Count</th><th>Detail</th></tr></thead><tbody>' + linkedin_action_rows + '</tbody></table>' or '<div class="card">LinkedIn work was not recorded for this run.</div>'}</section>
    <section><h2>Execution by company (this run)</h2><div class="grid">{execution_cards or '<div class="card">No per-company execution was recorded.</div>'}</div></section>
    <section><h2>{esc(html_source_heading)}</h2><table><thead><tr><th>Source</th><th>Status</th><th>Kept</th><th>Raw</th><th>Human summary</th></tr></thead><tbody>{source_breakdown_table}</tbody></table></section>
    <section><h2>Startup adapter and lane detail</h2>{startup_adapter_rows and '<table><thead><tr><th>Source</th><th>Lane</th><th>Status</th><th>Fetched</th><th>Discovered</th><th>Selected/new</th></tr></thead><tbody>' + startup_adapter_rows + '</tbody></table>' or '<div class="card">Startup adapters did not run or did not record exact artifacts.</div>'}</section>
    <section><h2>Maintenance completed</h2><div class="grid"><div class="card"><h3>Website resolution</h3><p>{esc(website_summary)}</p></div><div class="card"><h3>Company context enrichment</h3><p>{esc(enrichment_summary)}</p></div><div class="card"><h3>Campaign plan created</h3><p>{esc(campaign_summary)}</p></div></div></section>
    <section><h2>Nightly stages</h2><table><thead><tr><th>Stage</th><th>Status</th><th>Seconds</th></tr></thead><tbody>{stage_rows or '<tr><td colspan="3">No stage metrics found.</td></tr>'}</tbody></table></section>
    <section class="card"><h2>{esc(html_comms_heading)}</h2><p>gold/manual sends <code>{esc(comms_summary['gold'])}</code> · negative/replaced drafts <code>{esc(comms_summary['negative'])}</code> · silver/sent approved drafts <code>{esc(comms_summary['silver'])}</code></p><p>Reusable corpus: <code>{esc(comms_artifact)}</code></p></section>
  </main>
</body>
</html>
"""
    report_html_path.write_text(html_text, encoding="utf-8")
    latest_html_path.write_text(html_text, encoding="utf-8")
    reports_latest_html_path.write_text(html_text, encoding="utf-8")
    legacy_latest_html_path.write_text(html_text, encoding="utf-8")
    return summary_artifact, latest_path, report_html_path, latest_html_path


@app.command("write-daily-run-report")
def write_daily_run_report_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    since: Annotated[
        str,
        typer.Option(help="Only include artifacts modified after this local ISO timestamp"),
    ] = "",
    nightly_summary: Annotated[
        Path | None,
        typer.Option(help="Optional ResumeGenerator nightly summary JSON"),
    ] = None,
    title: Annotated[
        str,
        typer.Option(help="Report title"),
    ] = "Outreach Daily Run Report",
) -> None:
    """Write a paired-argument run report or an explicitly unscoped workspace snapshot."""
    settings = OutreachSettings()
    has_since = bool(since.strip())
    has_summary = nightly_summary is not None
    if has_since != has_summary:
        raise typer.BadParameter(
            "Pass both --since and --nightly-summary for run-scoped mode, "
            "or omit both for workspace-snapshot mode."
        )
    since_dt = _parse_report_datetime(since)
    if has_since and since_dt is None:
        raise typer.BadParameter("--since must be a valid ISO timestamp")
    artifact, md_path, html_artifact, html_path = write_artifact_daily_report(
        settings=settings,
        workspace=workspace,
        since=since_dt,
        nightly_summary_path=nightly_summary,
        title=title,
    )
    typer.echo("Wrote daily run report.")
    typer.echo(f"Summary artifact: {artifact}")
    typer.echo(f"Daily report: {md_path}")
    typer.echo(f"HTML report artifact: {html_artifact}")
    typer.echo(f"HTML report: {html_path}")


@app.command("build-communication-lab")
def build_communication_lab_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    resume_root: Annotated[
        Path | None,
        typer.Option(help="Optional ResumeGenerator checkout for story/voice material"),
    ] = Path("../ResumeGenerator v1"),
) -> None:
    """Build a corpus-backed communication brief for non-slop outreach."""
    settings = OutreachSettings()
    lab = build_communication_lab(
        workspace=workspace,
        repo_root=Path.cwd(),
        resume_root=resume_root if resume_root and resume_root.exists() else None,
    )
    artifact = write_artifact(
        settings.artifacts_dir,
        "communication-lab",
        lab,
    )
    typer.echo("Built communication lab brief.")
    typer.echo(f"Sources: {len(lab['source_summary'])}")
    typer.echo(f"Principles: {len(lab['stellar_email_principles'])}")
    typer.echo(f"Artifact: {artifact}")
    for source in list(lab["source_summary"])[:8]:
        typer.echo(f"- {source['source_type']} | {source['items']} items | {source['path']}")


@app.command("export-communication-review-csv")
def export_communication_review_csv_cmd(
    review_artifact: Annotated[
        Path,
        typer.Option(help="Path to a reviewed LinkedIn or email draft artifact"),
    ],
    output: Annotated[
        Path | None,
        typer.Option(help="Optional output CSV path. Defaults to the artifact path with .csv suffix."),
    ] = None,
) -> None:
    """Export a reviewed communication artifact to a markup-ready CSV."""
    payload = json.loads(review_artifact.read_text(encoding="utf-8"))
    csv_path = write_communication_review_csv(
        payload=payload,
        review_artifact=review_artifact,
        output_path=output,
    )
    rows = build_communication_review_csv_rows(payload=payload, review_artifact=review_artifact)
    typer.echo(f"Exported {len(rows)} communication review rows.")
    typer.echo(f"CSV: {csv_path}")
    typer.echo("Fill user_decision, user_reason, user_edit, and/or user_notes, then import it.")


@app.command("import-communication-feedback")
def import_communication_feedback_cmd(
    feedback_path: Annotated[
        Path,
        typer.Option(help="Path to a marked-up communication review CSV"),
    ],
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing communication_feedback.csv"),
    ] = Path("workspace"),
    execute: Annotated[
        bool,
        typer.Option(help="Append marked rows to workspace/communication_feedback.csv"),
    ] = False,
) -> None:
    """Import user-reviewed communication feedback into the durable local feedback ledger."""
    settings = OutreachSettings()
    summary = import_communication_feedback_rows(
        workspace=workspace,
        feedback_path=feedback_path,
        execute=execute,
    )
    artifact = write_artifact(settings.artifacts_dir, "communication-feedback-import", summary)
    typer.echo(f"{'Imported' if execute else 'Previewed'} communication feedback.")
    typer.echo(f"Marked rows: {summary['marked_rows']}")
    typer.echo(f"New rows: {summary['new_rows']}")
    typer.echo(f"Skipped duplicates: {summary['skipped_duplicates']}")
    typer.echo(f"Destination: {summary['destination']}")
    typer.echo(f"Artifact: {artifact}")
    feedback_summary = summary.get("summary") or {}
    typer.echo(f"Decisions: {feedback_summary.get('decision_counts', {})}")
    typer.echo(f"Reasons: {feedback_summary.get('reason_counts', {})}")


@app.command("review-track-2-email-drafts")
def review_track_2_email_drafts_cmd(
    draft_artifact: Annotated[
        Path,
        typer.Option(help="Path to a track-2-email-drafts artifact"),
    ],
) -> None:
    """Review Track 2 email drafts for slop, specificity, and send readiness."""
    settings = OutreachSettings()
    style_profile = load_style_profile_if_exists(settings.resolved_tracking_workspace_dir / "communication_style_profile.yml")
    payload = json.loads(draft_artifact.read_text(encoding="utf-8"))
    reviewed: list[dict[str, object]] = []
    verdict_counts: dict[str, int] = {}
    for draft in list(payload.get("results") or []):
        communication_review = review_outreach_message(
            subject=str(draft.get("subject") or ""),
            body=str(draft.get("body") or ""),
            channel="email",
            company=str(draft.get("company") or ""),
            recipient_type=str(draft.get("recipient_type") or "general"),
            recipient_title=str(draft.get("title") or ""),
            style_profile=style_profile,
        )
        review = review_email_craft(
            str(draft.get("subject") or ""),
            str(draft.get("body") or ""),
            company=str(draft.get("company") or ""),
            recipient_type=str(draft.get("recipient_type") or "general"),
        )
        enriched = {
            **draft,
            "communication_review": communication_review.__dict__,
            "communication_recommendation": communication_review.recommended_action,
            "craft_review": review.__dict__,
        }
        reviewed.append(enriched)
        verdict_counts[communication_review.verdict] = verdict_counts.get(communication_review.verdict, 0) + 1
    review_payload = {
        "source_artifact": str(draft_artifact),
        "count": len(reviewed),
        "verdict_counts": verdict_counts,
        "results": reviewed,
    }
    artifact = write_artifact(
        settings.artifacts_dir,
        "track-2-email-draft-review",
        review_payload,
    )
    csv_path = write_communication_review_csv(payload=review_payload, review_artifact=artifact)
    review_payload["review_csv"] = str(csv_path)
    artifact.write_text(json.dumps(review_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    typer.echo(f"Reviewed {len(reviewed)} email drafts.")
    typer.echo(f"Verdicts: {verdict_counts}")
    typer.echo(f"Artifact: {artifact}")
    typer.echo(f"Review CSV: {csv_path}")
    for draft in reviewed[: min(8, len(reviewed))]:
        review = draft["craft_review"]
        typer.echo(
            f"- {draft.get('company')} | {draft.get('name')} | "
            f"{review['verdict']} | score={review['score']}"
        )
        for flag in list(review.get("flags") or [])[:3]:
            typer.echo(f"  flag: {flag}")


@app.command("review-linkedin-followup-drafts")
def review_linkedin_followup_drafts_cmd(
    draft_artifact: Annotated[
        Path,
        typer.Option(help="Path to a track-2-linkedin-followup-drafts artifact"),
    ],
) -> None:
    """Review LinkedIn follow-up drafts with the shared communication engine."""
    settings = OutreachSettings()
    style_profile = load_style_profile_if_exists(settings.resolved_tracking_workspace_dir / "communication_style_profile.yml")
    workbook = OutreachWorkbook(settings.resolved_tracking_workspace_dir)
    touchpoints_by_contact: dict[str, list[TouchpointRecord]] = {}
    for touchpoint in workbook.list_touchpoints():
        if touchpoint.contact_id:
            touchpoints_by_contact.setdefault(touchpoint.contact_id, []).append(touchpoint)
    payload = json.loads(draft_artifact.read_text(encoding="utf-8"))
    reviewed: list[dict[str, object]] = []
    verdict_counts: dict[str, int] = {}
    for draft in list(payload.get("results") or []):
        recipient_type = str(draft.get("recipient_type") or draft.get("followup_audience") or draft.get("contact_type") or "general")
        message_window = [
            dict(message)
            for message in list(draft.get("message_window") or [])
            if isinstance(message, dict)
        ]
        if not message_window:
            message_window = compact_message_window(
                thread={
                    "latest_message": str(draft.get("latest_message") or ""),
                    "last_sender": str(draft.get("last_sender") or ""),
                    "timestamp_text": str(draft.get("timestamp_text") or ""),
                },
                touchpoints=touchpoints_by_contact.get(str(draft.get("contact_id") or ""), []),
                original_invite_note=str(draft.get("original_invite_note") or ""),
            )
        reply_intent = (
            classify_linkedin_reply_intent(
                latest_message=str(draft.get("latest_message") or ""),
                message_window=message_window,
            )
            if str(draft.get("source_status") or "").lower() == "replied"
            or str(draft.get("draft_kind") or "").endswith("_reply")
            else ""
        )
        review = review_outreach_message(
            body=str(draft.get("draft_message") or ""),
            channel="linkedin_followup",
            company=str(draft.get("company") or ""),
            recipient_type=recipient_type,
            recipient_title=str(draft.get("title") or ""),
            style_profile=style_profile,
            grounding_context=compact_context_text(message_window) or str(draft.get("latest_message") or ""),
        )
        communication_recommendation = review.recommended_action
        if reply_intent == "already_asked_wait":
            review.score = min(review.score, 60)
            review.verdict = "needs_rewrite"
            review.recommended_action = "hold"
            communication_recommendation = "hold"
            review.flags.append("Hold: prior context indicates this would repeat an ask")
        enriched = {
            **draft,
            "message_window": message_window,
            "reply_intent": reply_intent or draft.get("reply_intent", ""),
            "communication_review": review.__dict__,
            "communication_recommendation": communication_recommendation,
        }
        reviewed.append(enriched)
        verdict_counts[review.verdict] = verdict_counts.get(review.verdict, 0) + 1
    review_payload = {
        "source_artifact": str(draft_artifact),
        "count": len(reviewed),
        "verdict_counts": verdict_counts,
        "results": reviewed,
    }
    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-followup-draft-review",
        review_payload,
    )
    csv_path = write_communication_review_csv(payload=review_payload, review_artifact=artifact)
    review_payload["review_csv"] = str(csv_path)
    artifact.write_text(json.dumps(review_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    typer.echo(f"Reviewed {len(reviewed)} LinkedIn follow-up drafts.")
    typer.echo(f"Verdicts: {verdict_counts}")
    typer.echo(f"Artifact: {artifact}")
    typer.echo(f"Review CSV: {csv_path}")
    for draft in reviewed[: min(8, len(reviewed))]:
        review = draft["communication_review"]
        typer.echo(
            f"- {draft.get('company')} | {draft.get('name')} | "
            f"{review['verdict']} | score={review['score']}"
        )
        for flag in list(review.get("flags") or [])[:3]:
            typer.echo(f"  flag: {flag}")


@app.command("draft-track-2-emails")
def draft_track_2_emails_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    max_total_actions: Annotated[int, typer.Option(help="Maximum total Track 2 actions today")] = 24,
    max_companies: Annotated[int, typer.Option(help="Maximum distinct companies to touch today")] = 18,
    max_linkedin_invites: Annotated[int, typer.Option(help="Maximum new LinkedIn invites today")] = 12,
    max_linkedin_followups: Annotated[int, typer.Option(help="Maximum LinkedIn follow-up/reply messages today")] = 8,
    max_company_mapping: Annotated[int, typer.Option(help="Maximum companies to map contacts for today")] = 5,
    max_email_research: Annotated[int, typer.Option(help="Maximum email/contact-info research tasks today")] = 5,
    max_context_enrichment: Annotated[int, typer.Option(help="Maximum company enrichment tasks today")] = 8,
    max_email_drafts: Annotated[int, typer.Option(help="Maximum cold email drafts to create")] = 5,
) -> None:
    """Draft Track 2 cold emails from daily-plan email actions; does not send."""
    settings = OutreachSettings()
    daily_plan = _build_daily_plan_for_workspace(
        workspace=workspace,
        max_total_actions=max_total_actions,
        max_companies=max_companies,
        max_linkedin_invites=max_linkedin_invites,
        max_linkedin_followups=max_linkedin_followups,
        max_company_mapping=max_company_mapping,
        max_email_research=max_email_research,
        max_context_enrichment=max_context_enrichment,
        max_email_drafts=max_email_drafts,
    )
    drafts = build_track_2_email_drafts(
        workspace=workspace,
        daily_plan=daily_plan,
        limit=max_email_drafts,
    )
    summary: dict[str, int] = {}
    for draft in drafts:
        key = str(draft.get("recipient_type") or "general")
        summary[key] = summary.get(key, 0) + 1
    artifact = write_artifact(
        settings.artifacts_dir,
        "track-2-email-drafts",
        {
            "workspace": str(workspace),
            "count": len(drafts),
            "recipient_summary": summary,
            "daily_plan_used": daily_plan.get("used", {}),
            "results": drafts,
        },
    )
    typer.echo(f"Drafted {len(drafts)} Track 2 emails.")
    typer.echo(f"Recipients: {summary}")
    typer.echo(f"Artifact: {artifact}")
    for draft in drafts[: min(8, len(drafts))]:
        typer.echo(f"- {draft['company']} | {draft['name']} | {draft['recipient_type']} | {draft['email']}")
        typer.echo(f"  Subject: {draft['subject']}")


@app.command("run-track-2-daily-plan")
def run_track_2_daily_plan_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    max_total_actions: Annotated[int, typer.Option(help="Maximum total Track 2 actions today")] = 24,
    max_companies: Annotated[int, typer.Option(help="Maximum distinct companies to touch today")] = 18,
    max_linkedin_invites: Annotated[int, typer.Option(help="Maximum new LinkedIn invites today")] = 12,
    max_linkedin_followups: Annotated[int, typer.Option(help="Maximum LinkedIn follow-up/reply messages today")] = 8,
    max_company_mapping: Annotated[int, typer.Option(help="Maximum companies to map contacts for today")] = 5,
    max_email_research: Annotated[int, typer.Option(help="Maximum LinkedIn Contact Info/email research profiles today")] = 5,
    max_context_enrichment: Annotated[int, typer.Option(help="Maximum company enrichment tasks today")] = 8,
    max_email_drafts: Annotated[int, typer.Option(help="Maximum cold email drafts today; keep 0 until email engine is ready")] = 0,
    execute: Annotated[
        bool,
        typer.Option(help="Run live non-send phases and write safe updates; LinkedIn sends still require --send-linkedin"),
    ] = False,
    send_linkedin: Annotated[
        bool,
        typer.Option(help="Allow LinkedIn invite/follow-up sends. Requires --execute."),
    ] = False,
    refresh_linkedin: Annotated[
        bool,
        typer.Option(help="Read live LinkedIn messages before drafting follow-ups. Requires a live Chrome CDP session."),
    ] = False,
    live_linkedin: Annotated[
        bool,
        typer.Option(help="Allow live LinkedIn browser phases such as Contact Info, company mapping, and invite prep."),
    ] = False,
    deep_messages: Annotated[
        bool,
        typer.Option(help="Scroll the LinkedIn inbox while refreshing messages"),
    ] = True,
    linkedin_message_limit: Annotated[int, typer.Option(help="Maximum LinkedIn message threads to read")] = 75,
    invite_min_score: Annotated[int, typer.Option(help="Minimum score for invite send candidates")] = 35,
    invite_verdict: Annotated[str, typer.Option(help="QC verdict required for invite send candidates")] = "send",
    adaptive_invite_min_score: Annotated[
        bool,
        typer.Option(help="Use startup pool size to adapt invite score gate"),
    ] = True,
    network_enrichment: Annotated[
        bool,
        typer.Option(help="Allow network fetches for context-enrichment phase when executing"),
    ] = True,
    external_email_finder: Annotated[
        bool,
        typer.Option(help="After LinkedIn Contact Info misses, call configured external email finder"),
    ] = False,
    email_finder_provider: Annotated[
        str,
        typer.Option(help="External email finder provider: auto, prospeo, or hunter"),
    ] = "auto",
) -> None:
    """Run the bounded Track 2 daily plan in phase order."""
    if send_linkedin and not execute:
        typer.echo("--send-linkedin requires --execute.")
        raise typer.Exit(code=1)

    settings = OutreachSettings()
    allow_live_linkedin = live_linkedin or refresh_linkedin or send_linkedin
    should_refresh_linkedin = refresh_linkedin or send_linkedin
    daily_plan = _build_daily_plan_for_workspace(
        workspace=workspace,
        max_total_actions=max_total_actions,
        max_companies=max_companies,
        max_linkedin_invites=max_linkedin_invites,
        max_linkedin_followups=max_linkedin_followups,
        max_company_mapping=max_company_mapping,
        max_email_research=max_email_research,
        max_context_enrichment=max_context_enrichment,
        max_email_drafts=max_email_drafts,
    )
    execution_manifest = build_daily_execution_manifest(daily_plan)
    plan_artifact = write_artifact(
        settings.artifacts_dir,
        "track-2-daily-plan",
        {
            "workspace": str(workspace),
            **daily_plan,
        },
    )

    workbook = OutreachWorkbook(workspace)
    organizations = workbook.list_organizations()
    contacts = workbook.list_contacts()
    touchpoints = workbook.list_touchpoints()
    org_by_id = {org.organization_id: org for org in organizations}
    phase_results: list[dict[str, object]] = []

    followup_org_ids = _daily_plan_org_ids(
        daily_plan,
        phase_prefixes=("1_continue_live_conversations", "2_follow_up_warm_accepts"),
    )
    followup_budget = int((daily_plan.get("used") or {}).get("linkedin_followups") or 0)
    if followup_budget:
        followup_result: dict[str, object] = {
            "phase": "1_2_linkedin_followups",
            "planned_companies": sorted(
                org_by_id[org_id].name for org_id in followup_org_ids if org_id in org_by_id
            ),
            "budget": followup_budget,
            "status": "planned",
            "artifacts": [],
        }
        if should_refresh_linkedin:
            scraper = LinkedInScraper(settings)
            scraper.require_live_cdp_session()
            threads = [
                item.__dict__
                for item in scraper.snapshot_message_threads(
                    limit=linkedin_message_limit,
                    deep=deep_messages,
                )
            ]
            state_path = linkedin_message_state_path(settings)
            state = load_linkedin_message_state(state_path)
            message_results, next_state = build_linkedin_message_reconcile_results(
                threads=threads,
                contacts=contacts,
                touchpoints=touchpoints,
                state=state,
                include_seen=True,
            )
            live_contact_ids = {
                str(item.get("contact_id") or "")
                for item in message_results
                if str(item.get("contact_id") or "")
            }
            persisted_inbound_results = build_persisted_inbound_reconcile_results(
                state=next_state,
                contacts=contacts,
                touchpoints=touchpoints,
                exclude_contact_ids=live_contact_ids,
            )
            all_message_results = [*message_results, *persisted_inbound_results]
            reconcile_result = apply_linkedin_reconcile_results(
                workbook=workbook,
                results=all_message_results,
                source_artifact="",
                apply_changes=execute,
            )
            if execute:
                save_linkedin_message_state(state_path, next_state)
            planned_message_results = _filter_reconcile_results_to_orgs(
                list(reconcile_result.get("results") or []),
                contacts=contacts,
                organization_ids=followup_org_ids,
            )
            inbound_results = [
                item
                for item in list(reconcile_result.get("results") or [])
                if str(item.get("normalized_status") or "").casefold() == "replied"
                and str(item.get("last_sender") or "").casefold() not in {"you", "akshat"}
            ]

            execution_results: list[dict[str, object]] = []
            execution_keys: set[str] = set()

            def add_execution_result(item: dict[str, object]) -> None:
                key = str(item.get("contact_id") or item.get("thread_id") or "")
                if not key or key in execution_keys:
                    return
                execution_keys.add(key)
                execution_results.append(item)

            # Unanswered inbound replies are production-priority work even if
            # their company was not selected by today's account campaign.
            for item in inbound_results:
                add_execution_result(item)
            for item in planned_message_results:
                add_execution_result(item)

            profile_reconcile_artifact: Path | None = None
            profile_reconcile_count = 0
            remaining_profile_budget = max(0, followup_budget - len(execution_results))
            if remaining_profile_budget and followup_org_ids:
                profile_candidates = [
                    item
                    for item in build_linkedin_reconcile_queue_items(
                        organizations=workbook.list_organizations(),
                        contacts=workbook.list_contacts(),
                        touchpoints=workbook.list_touchpoints(),
                        include_statuses=("Invited", "Connected"),
                        max_age_days=21,
                        min_age_hours=12,
                    )
                    if str(item.get("organization_id") or "") in followup_org_ids
                ][:remaining_profile_budget]
                if profile_candidates:
                    detected_profiles = [
                        item.__dict__
                        for item in scraper.reconcile_connection_statuses(profile_candidates)
                    ]
                    profile_reconcile = apply_linkedin_reconcile_results(
                        workbook=workbook,
                        results=detected_profiles,
                        source_artifact="",
                        apply_changes=execute,
                    )
                    profile_reconcile_count = len(detected_profiles)
                    profile_reconcile_artifact = write_artifact(
                        settings.artifacts_dir,
                        "track-2-linkedin-profile-reconcile",
                        {
                            "workspace": str(workspace),
                            "execute": execute,
                            "count": len(detected_profiles),
                            **profile_reconcile,
                        },
                    )
                    for item in list(profile_reconcile.get("results") or []):
                        if (
                            str(item.get("normalized_status") or "").casefold() == "connected"
                            and bool(item.get("needs_follow_up"))
                        ):
                            add_execution_result(item)

            execution_results = execution_results[:followup_budget]
            reconcile_artifact = write_artifact(
                settings.artifacts_dir,
                "track-2-linkedin-message-reconcile",
                {
                    "workspace": str(workspace),
                    "execute": execute,
                    "offset_updated": execute,
                    "thread_count": len(threads),
                    "new_result_count": len(message_results),
                    "persistent_inbound_count": len(persisted_inbound_results),
                    "inbound_result_count": len(inbound_results),
                    "planned_company_result_count": len(planned_message_results),
                    "profile_reconcile_count": profile_reconcile_count,
                    "execution_result_count": len(execution_results),
                    "results": execution_results,
                    "summary": reconcile_result.get("summary", {}),
                },
            )
            drafts = build_linkedin_followup_drafts(
                reconcile_results=execution_results,
                organizations=organizations,
                contacts=contacts,
                opportunities=workbook.list_opportunities(),
            )[:followup_budget]
            cadence_allowed_drafts, cadence_held_drafts = _apply_linkedin_cadence_guards(
                workbook=workbook,
                drafts=drafts,
            )
            action_summary = summarize_linkedin_followup_actions(cadence_allowed_drafts, execution_results)
            draft_artifact = write_artifact(
                settings.artifacts_dir,
                "track-2-linkedin-followup-drafts",
                {
                    "source_artifact": str(reconcile_artifact),
                    "count": len(drafts),
                    "summary": action_summary,
                    "results": drafts,
                    "cadence_allowed_count": len(cadence_allowed_drafts),
                    "cadence_held_count": len(cadence_held_drafts),
                    "cadence_held": cadence_held_drafts,
                },
            )
            followup_result.update(
                {
                    "status": "drafted",
                    "thread_count": len(threads),
                    "detected_count": len(message_results),
                    "persistent_inbound_count": len(persisted_inbound_results),
                    "inbound_result_count": len(inbound_results),
                    "planned_company_result_count": len(planned_message_results),
                    "profile_reconcile_count": profile_reconcile_count,
                    "execution_result_count": len(execution_results),
                    "draft_count": len(drafts),
                    "cadence_allowed_count": len(cadence_allowed_drafts),
                    "cadence_held_count": len(cadence_held_drafts),
                    "action_summary": action_summary,
                    "artifacts": [
                        str(reconcile_artifact),
                        str(draft_artifact),
                        *([str(profile_reconcile_artifact)] if profile_reconcile_artifact else []),
                    ],
                }
            )
            if send_linkedin and drafts:
                sendable_drafts = [
                    draft
                    for draft in cadence_allowed_drafts
                    if str(draft.get("send_recommendation") or "") in SAFE_FOLLOWUP_SEND_RECOMMENDATIONS
                ]
                review_drafts = [
                    draft
                    for draft in cadence_allowed_drafts
                    if str(draft.get("send_recommendation") or "") not in SAFE_FOLLOWUP_SEND_RECOMMENDATIONS
                ] + cadence_held_drafts
                pending_path = update_linkedin_followup_pending_review(
                    settings=settings,
                    pending_drafts=review_drafts,
                    cleared_drafts=[],
                    source_artifact=draft_artifact,
                )
                followup_result.update(
                    {
                        "send_policy": sorted(SAFE_FOLLOWUP_SEND_RECOMMENDATIONS),
                        "sendable_count": len(sendable_drafts),
                        "pending_review_count": len(review_drafts),
                        "pending_review_artifact": str(pending_path),
                    }
                )
                if sendable_drafts:
                    send_artifact, progress_artifact, status_counts, touchpoints_added = execute_linkedin_followup_send(
                        settings=settings,
                        draft_artifact=draft_artifact,
                        drafts=sendable_drafts,
                        execute=True,
                        limit=min(followup_budget, len(sendable_drafts)),
                        start_at=0,
                        include_optional=False,
                    )
                    with send_artifact.open(encoding="utf-8") as handle:
                        send_payload = json.load(handle)
                    sent_keys = {
                        _followup_pending_key(item)
                        for item in list(send_payload.get("results") or [])
                        if isinstance(item, dict) and item.get("status") == "sent"
                    }
                    sent_drafts = [
                        draft for draft in sendable_drafts if _followup_pending_key(draft) in sent_keys
                    ]
                    pending_path = update_linkedin_followup_pending_review(
                        settings=settings,
                        pending_drafts=review_drafts,
                        cleared_drafts=sent_drafts,
                        source_artifact=draft_artifact,
                    )
                    followup_result.update(
                        {
                            "status": "sent",
                            "send_status_counts": status_counts,
                            "touchpoints_added": touchpoints_added,
                            "pending_review_artifact": str(pending_path),
                        }
                    )
                    followup_result["artifacts"].extend([str(send_artifact), str(progress_artifact)])
                else:
                    followup_result["status"] = "drafted_review_required"
        else:
            followup_result["detail"] = "Run with --refresh-linkedin to read live LinkedIn messages in an attended browser session."
        phase_results.append(followup_result)

    email_research_limit = int((daily_plan.get("used") or {}).get("email_research") or 0)
    if email_research_limit:
        email_queue = build_linkedin_contact_info_email_queue(
            workspace=workspace,
            daily_plan=daily_plan,
            limit=email_research_limit,
        )
        email_result: dict[str, object] = {
            "phase": "3_contact_and_email_research",
            "status": "queued",
            "budget": email_research_limit,
            "queued_count": len(email_queue),
            "queue": email_queue,
            "artifacts": [],
        }
        queue_artifact = write_artifact(
            settings.artifacts_dir,
            "track-2-contact-info-email-queue",
            {
                "workspace": str(workspace),
                "budget": email_research_limit,
                "count": len(email_queue),
                "results": email_queue,
            },
        )
        email_result["artifacts"].append(str(queue_artifact))
        if execute and allow_live_linkedin:
            results = (
                LinkedInScraper(settings).extract_contact_info_emails(
                    email_queue,
                    limit=email_research_limit,
                    start_at=0,
                )
                if email_queue
                else []
            )
            updated = 0
            for result in results:
                if result.status != "found" or not result.email:
                    continue
                contact = workbook.update_contact(
                    result.contact_id,
                    email=result.email,
                    notes=_append_note_marker(
                        _contact_notes_for_id(workbook, result.contact_id),
                        f"linkedin_contact_info_email_found={utc_now_iso()}",
                    ),
                )
                if contact is not None:
                    updated += 1
            research_artifact = write_artifact(
                settings.artifacts_dir,
                "track-2-linkedin-contact-info-email-research",
                {
                    "workspace": str(workspace),
                    "execute": execute,
                    "count": len(results),
                    "updated": updated,
                    "results": [result.__dict__ for result in results],
                },
            )
            email_result.update(
                {
                    "status": "inspected",
                    "inspected_count": len(results),
                    "found_count": sum(1 for result in results if result.status == "found"),
                    "updated": updated,
                }
            )
            email_result["artifacts"].append(str(research_artifact))
            found_contact_ids = {result.contact_id for result in results if result.status == "found"}
            external_queue = build_external_email_research_queue(
                workspace=workspace,
                daily_plan=daily_plan,
                limit=email_research_limit,
                exclude_contact_ids=found_contact_ids,
            )
            if external_email_finder and external_queue:
                external_candidates = [
                    EmailResearchCandidate.from_dict(item)
                    for item in external_queue[:email_research_limit]
                ]
                external_results = build_email_finder_service(
                    settings,
                    provider=email_finder_provider,
                ).find_many(external_candidates, limit=email_research_limit)
                external_updated = apply_email_finder_results(
                    workbook=workbook,
                    results=external_results,
                    min_confidence=settings.email_finder_min_confidence,
                )
                external_artifact = write_artifact(
                    settings.artifacts_dir,
                    "track-2-external-contact-email-research",
                    {
                        "workspace": str(workspace),
                        "execute": execute,
                        "provider": email_finder_provider,
                        "min_confidence": settings.email_finder_min_confidence,
                        "count": len(external_results),
                        "updated": external_updated,
                        "results": [result.__dict__ for result in external_results],
                    },
                )
                email_result["external_email_finder"] = {
                    "status": "ran",
                    "provider": email_finder_provider,
                    "count": len(external_results),
                    "found_count": sum(1 for result in external_results if result.status == "found"),
                    "updated": external_updated,
                    "artifact": str(external_artifact),
                }
                email_result["artifacts"].append(str(external_artifact))
            elif external_queue:
                email_result["external_email_finder"] = {
                    "status": "disabled",
                    "detail": "Run with --external-email-finder to call a configured external provider for contacts still missing email.",
                    "queue_count": len(external_queue),
                }
        elif execute:
            external_queue = build_external_email_research_queue(
                workspace=workspace,
                daily_plan=daily_plan,
                limit=email_research_limit,
                exclude_contact_ids=set(),
            )
            if external_email_finder and external_queue:
                external_candidates = [
                    EmailResearchCandidate.from_dict(item)
                    for item in external_queue[:email_research_limit]
                ]
                external_results = build_email_finder_service(
                    settings,
                    provider=email_finder_provider,
                ).find_many(external_candidates, limit=email_research_limit)
                external_updated = apply_email_finder_results(
                    workbook=workbook,
                    results=external_results,
                    min_confidence=settings.email_finder_min_confidence,
                )
                external_artifact = write_artifact(
                    settings.artifacts_dir,
                    "track-2-external-contact-email-research",
                    {
                        "workspace": str(workspace),
                        "execute": execute,
                        "provider": email_finder_provider,
                        "min_confidence": settings.email_finder_min_confidence,
                        "count": len(external_results),
                        "updated": external_updated,
                        "results": [result.__dict__ for result in external_results],
                    },
                )
                email_result["external_email_finder"] = {
                    "status": "ran",
                    "provider": email_finder_provider,
                    "count": len(external_results),
                    "found_count": sum(1 for result in external_results if result.status == "found"),
                    "updated": external_updated,
                    "artifact": str(external_artifact),
                }
                email_result["artifacts"].append(str(external_artifact))
            else:
                email_result.update(
                    {
                        "status": "queued",
                        "detail": "Live LinkedIn is disabled; Contact Info inspection is queued for an attended run.",
                    }
                )
                if external_queue:
                    email_result["external_email_finder"] = {
                        "status": "disabled",
                        "detail": "External email finder is opt-in; pass --external-email-finder to spend provider credits.",
                        "queue_count": len(external_queue),
                    }
        phase_results.append(email_result)

    mapping_items = _daily_plan_items_matching(daily_plan, phase_prefix="4_contact_mapping")
    if mapping_items:
        mapping_result: dict[str, object] = {
            "phase": "4_contact_mapping",
            "status": "planned",
            "budget": len(mapping_items),
            "companies": [str(item.get("company") or "") for item in mapping_items],
            "runs": [],
        }
        if execute and allow_live_linkedin:
            for item in mapping_items:
                org = org_by_id.get(str(item.get("organization_id") or ""))
                company = str(item.get("company") or "")
                artifact = execute_linkedin_company_run(
                    settings=settings,
                    company=company,
                    dry_run=True,
                    company_mode=_company_mode_for_org(org) if org else "default",
                    include_pass=TRACK_2_MAPPING_PASSES,
                    target_role_title=str(item.get("target_role") or ""),
                )
                import_summary = workbook.import_linkedin_artifact(
                    artifact_path=artifact,
                    target_lists="referrals;linkedin;track-2",
                    organization_type=(org.organization_type if org else OrganizationType.COMPANY),
                    touchpoint_status="Prepared",
                )
                mapping_result["runs"].append(
                    {
                        "company": company,
                        "artifact": str(artifact),
                        "organization_id": import_summary.organization_id,
                        "source_id": import_summary.source_id,
                        "contacts_added": import_summary.contacts_added,
                        "touchpoints_added": import_summary.touchpoints_added,
                        "search_passes": list(TRACK_2_MAPPING_PASSES),
                    }
                )
            mapping_result["status"] = "ran"
        elif execute:
            mapping_result["status"] = "queued"
            mapping_result["detail"] = "Live LinkedIn is disabled; mapping is queued for an attended run."
        phase_results.append(mapping_result)

    invite_items = _daily_plan_items_matching(daily_plan, phase_prefix="5_send_linkedin_invites")
    if invite_items:
        invite_result: dict[str, object] = {
            "phase": "5_send_linkedin_invites",
            "status": "planned",
            "budget": int((daily_plan.get("used") or {}).get("linkedin_invites") or 0),
            "send_enabled": send_linkedin,
            "runs": [],
        }
        remaining_invites = int(invite_result["budget"])
        if execute and allow_live_linkedin:
            for item in invite_items:
                if remaining_invites <= 0:
                    break
                company = str(item.get("company") or "")
                per_company_limit = min(int(item.get("expected_linkedin_invites") or 0), remaining_invites)
                if per_company_limit <= 0:
                    continue
                org = org_by_id.get(str(item.get("organization_id") or ""))
                pipeline_artifact = execute_linkedin_company_run(
                    settings=settings,
                    company=company,
                    dry_run=True,
                    company_mode=_company_mode_for_org(org) if org else "default",
                    target_role_title=str(item.get("target_role") or ""),
                )
                with pipeline_artifact.open(encoding="utf-8") as handle:
                    payload = json.load(handle)
                effective_min_score_value = effective_send_min_score(
                    payload,
                    requested_min_score=invite_min_score,
                    adaptive=adaptive_invite_min_score,
                )
                batch = select_invite_candidates(
                    list(payload.get("results") or []),
                    verdict=invite_verdict,
                    min_score=effective_min_score_value,
                    limit=per_company_limit,
                    start_at=0,
                )
                batch = attach_search_urls_to_candidates(payload, batch)
                run_entry: dict[str, object] = {
                    "company": company,
                    "pipeline_artifact": str(pipeline_artifact),
                    "candidate_count": len(batch),
                    "effective_min_score": effective_min_score_value,
                    "target_role": str(item.get("target_role") or ""),
                    "sent": False,
                }
                if send_linkedin and batch:
                    try:
                        send_artifact, progress_artifact, status_counts, contacts_added, touchpoints_added = (
                            execute_invite_batch(
                                settings=settings,
                                company=company,
                                source_artifact_path=pipeline_artifact,
                                batch=batch,
                                execute=True,
                                limit=per_company_limit,
                                start_at=0,
                                verdict=invite_verdict,
                                min_score=effective_min_score_value,
                            )
                        )
                    except Exception as exc:
                        run_entry.update(
                            {
                                "sent": False,
                                "status": "send_failed",
                                "error": str(exc),
                            }
                        )
                    else:
                        run_entry.update(
                            {
                                "sent": True,
                                "send_artifact": str(send_artifact),
                                "progress_artifact": str(progress_artifact),
                                "status_counts": status_counts,
                                "contacts_added": contacts_added,
                                "touchpoints_added": touchpoints_added,
                            }
                        )
                        remaining_invites -= len(batch)
                else:
                    candidate_artifact = write_artifact(
                        settings.artifacts_dir,
                        "track-2-linkedin-invite-candidates",
                        {
                            "company": company,
                            "source_artifact": str(pipeline_artifact),
                            "send_enabled": False,
                            "limit": per_company_limit,
                            "count": len(batch),
                            "results": batch,
                        },
                    )
                    run_entry["candidate_artifact"] = str(candidate_artifact)
                    remaining_invites -= len(batch)
                invite_result["runs"].append(run_entry)
            if send_linkedin:
                failed_runs = [
                    run
                    for run in list(invite_result.get("runs") or [])
                    if isinstance(run, dict) and run.get("status") == "send_failed"
                ]
                sent_runs = [
                    run
                    for run in list(invite_result.get("runs") or [])
                    if isinstance(run, dict) and run.get("sent")
                ]
                if failed_runs and sent_runs:
                    invite_result["status"] = "partial_send_failed"
                elif failed_runs:
                    invite_result["status"] = "send_failed"
                else:
                    invite_result["status"] = "sent"
            else:
                invite_result["status"] = "prepared"
        elif execute:
            invite_result["status"] = "queued"
            invite_result["detail"] = "Live LinkedIn is disabled; invite candidate prep is queued for an attended run."
        phase_results.append(invite_result)

    enrichment_companies = _daily_plan_company_names(daily_plan, phase_prefix="7_context_enrichment")
    if enrichment_companies:
        enrichment_result: dict[str, object] = {
            "phase": "7_context_enrichment",
            "status": "planned",
            "budget": len(enrichment_companies),
            "companies": enrichment_companies,
        }
        if execute:
            from outreach.company_enrichment import enrich_company_contexts

            enrichment_rows = enrich_company_contexts(
                workspace,
                limit=len(enrichment_companies),
                companies=set(enrichment_companies),
                execute=True,
                use_network=network_enrichment,
                use_web_search=network_enrichment,
                verify_all=True,
                fetcher=HttpTextDownloader(timeout_seconds=6),
            )
            enrichment_artifact = write_artifact(
                settings.artifacts_dir,
                "track-2-company-context-enrichment",
                {
                    "workspace": str(workspace),
                    "execute": True,
                    "network": network_enrichment,
                    "count": len(enrichment_rows),
                    "results": [row.__dict__ for row in enrichment_rows],
                },
            )
            enrichment_result.update(
                {
                    "status": "ran",
                    "count": len(enrichment_rows),
                    "artifact": str(enrichment_artifact),
                }
            )
        phase_results.append(enrichment_result)

    email_draft_items = [
        item
        for item in list(daily_plan.get("selected") or [])
        if int(item.get("expected_email_drafts") or 0) > 0
    ]
    if email_draft_items:
        email_draft_budget = int((daily_plan.get("used") or {}).get("email_drafts") or len(email_draft_items))
        email_drafts = build_track_2_email_drafts(
            workspace=workspace,
            daily_plan=daily_plan,
            limit=email_draft_budget,
        )
        email_draft_artifact = write_artifact(
            settings.artifacts_dir,
            "track-2-email-drafts",
            {
                "workspace": str(workspace),
                "execute": False,
                "count": len(email_drafts),
                "budget": email_draft_budget,
                "results": email_drafts,
            },
        )
        phase_results.append(
            {
                "phase": "6_draft_email_touch",
                "status": "drafted",
                "count": len(email_draft_items),
                "draft_count": len(email_drafts),
                "artifact": str(email_draft_artifact),
            }
        )

    run_artifact = write_artifact(
        settings.artifacts_dir,
        "track-2-daily-run",
        {
            "workspace": str(workspace),
            "execute": execute,
            "send_linkedin": send_linkedin,
            "refresh_linkedin": should_refresh_linkedin,
            "live_linkedin": allow_live_linkedin,
            "plan_artifact": str(plan_artifact),
            "budget": daily_plan.get("budget", {}),
            "used": daily_plan.get("used", {}),
            "summary": daily_plan.get("summary", {}),
            "phase_summary": daily_plan.get("phase_summary", {}),
            "execution_manifest": execution_manifest,
            "phase_results": phase_results,
        },
    )

    typer.echo(f"{'Ran' if execute else 'Planned'} Track 2 daily plan.")
    typer.echo(f"Selected actions: {daily_plan['selected_count']}")
    typer.echo(f"Used: {daily_plan['used']}")
    typer.echo(f"Phases: {daily_plan['phase_summary']}")
    typer.echo(f"Plan artifact: {plan_artifact}")
    typer.echo(f"Run artifact: {run_artifact}")
    for phase in phase_results:
        typer.echo(
            f"- {phase['phase']} | status={phase['status']} | "
            f"budget={phase.get('budget', phase.get('count', '-'))}"
        )


def run_resume_jobs_import_stage(
    *,
    workspace: Path,
    jobs_xlsx: Path,
    sheet_name: str,
    include_statuses: tuple[str, ...] = DEFAULT_INCLUDE_STATUSES,
    min_score: float = 7.0,
    max_age_days: int = 10,
    season_focus: str = DEFAULT_SEASON_FOCUS,
    account_universe: bool = True,
    resume_blocklist: Path | None = Path("../ResumeGenerator v1/discovery/blocklist.txt"),
    limit: int | None = None,
    execute: bool = False,
) -> dict[str, object]:
    if not jobs_xlsx.exists():
        return {
            "status": "skipped",
            "reason": f"Resume jobs file not found: {jobs_xlsx}",
            "jobs_xlsx": str(jobs_xlsx),
        }

    workbook = OutreachWorkbook(workspace)
    company_overrides_path = ensure_company_overrides_csv(
        workspace / DEFAULT_COMPANY_OVERRIDES_FILENAME
    )
    company_overrides = load_company_overrides(company_overrides_path)
    blocklist_patterns = load_company_blocklist(resume_blocklist)
    rows = load_resume_jobs(jobs_xlsx, sheet_name=sheet_name)
    effective_min_score = 0.0 if account_universe else min_score
    effective_max_age_days = None if account_universe else max_age_days
    normalized_season_focus = normalize_season_focus(season_focus)
    selection = select_resume_jobs(
        rows,
        include_statuses=include_statuses,
        min_score=effective_min_score,
        max_age_days=effective_max_age_days,
        season_focus=normalized_season_focus,
        blocklist_patterns=blocklist_patterns,
    )
    selected_jobs = selection.jobs[:limit] if limit else selection.jobs

    summary: dict[str, object] = {
        "status": "imported" if execute else "previewed",
        "jobs_xlsx": str(jobs_xlsx),
        "sheet_name": sheet_name,
        "execute": execute,
        "account_universe": account_universe,
        "season_focus": normalized_season_focus,
        "rows_scanned": len(rows),
        "eligible_rows": len(selection.jobs),
        "selected_rows": len(selected_jobs),
        "skipped_status": selection.skipped_status,
        "skipped_score": selection.skipped_score,
        "skipped_age": selection.skipped_age,
        "skipped_season_focus": selection.skipped_season_focus,
        "skipped_blocklist": selection.skipped_blocklist,
        "duplicates_removed": selection.duplicates_removed,
        "season_counts_scanned": selection.season_counts_scanned,
        "season_counts_selected": selection.season_counts_selected,
        "organizations_added": 0,
        "opportunities_added": 0,
        "company_overrides": str(company_overrides_path),
        "sample": [
            {
                "row_id": job.row_id,
                "company": job.company,
                "role_title": job.role_title,
                "status": job.normalized_status,
                "fit_score": job.fit_score,
                "date_found": job.date_found.isoformat() if job.date_found else "",
                "season_bucket": classify_resume_role_season(job),
            }
            for job in selected_jobs[:10]
        ],
    }
    if not execute:
        return summary

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
            notes=(
                f"sheet={sheet_name} | min_score={effective_min_score} | "
                f"max_age_days={effective_max_age_days if effective_max_age_days is not None else 'none'} | "
                f"account_universe={account_universe} | season_focus={normalized_season_focus}"
            ),
        )
    )

    organizations_added = 0
    opportunities_added = 0
    for job in selected_jobs:
        override = company_overrides.get(normalize_dedupe_text(job.company))
        target_lists = target_lists_from_resume_status(job.status)
        if account_universe:
            target_lists = _merge_target_lists(target_lists, "account-universe;track-2;resume_generator")
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
        elif account_universe:
            merged_target_lists = _merge_target_lists(organization.target_lists, target_lists)
            updates: dict[str, str] = {}
            if merged_target_lists != organization.target_lists:
                updates["target_lists"] = merged_target_lists
            if not organization.city and job.location:
                updates["city"] = job.location
            if not organization.source_url and job.url:
                updates["source_url"] = job.url
            if updates:
                updates["last_updated_at"] = utc_now_iso()
                organization = workbook.update_organization(organization.organization_id, **updates) or organization

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

    summary["organizations_added"] = organizations_added
    summary["opportunities_added"] = opportunities_added
    summary["source_id"] = source_id
    return summary


def build_resume_outreach_queue_stage(
    *,
    settings: OutreachSettings,
    workspace: Path,
    jobs_xlsx: Path,
    sheet_name: str,
    min_score: float = 7.0,
    max_age_days: int = 10,
    season_focus: str = TRANSITION_SEASON_FOCUS,
    resume_blocklist: Path | None = Path("../ResumeGenerator v1/discovery/blocklist.txt"),
    max_per_company: int = 2,
    limit: int = 15,
) -> dict[str, object]:
    if not jobs_xlsx.exists():
        return {
            "status": "skipped",
            "reason": f"Resume jobs file not found: {jobs_xlsx}",
            "jobs_xlsx": str(jobs_xlsx),
        }

    company_overrides_path = ensure_company_overrides_csv(
        workspace / DEFAULT_COMPANY_OVERRIDES_FILENAME
    )
    company_overrides = load_company_overrides(company_overrides_path)
    blocklist_patterns = load_company_blocklist(resume_blocklist)
    rows = load_resume_jobs(jobs_xlsx, sheet_name=sheet_name)
    normalized_season_focus = normalize_season_focus(season_focus)
    selection = select_resume_jobs(
        rows,
        include_statuses=DEFAULT_INCLUDE_STATUSES,
        min_score=min_score,
        max_age_days=max_age_days,
        season_focus=normalized_season_focus,
        blocklist_patterns=blocklist_patterns,
    )
    queue_items = build_resume_outreach_queue(
        selection.jobs,
        company_overrides=company_overrides,
        max_per_company=max_per_company,
    )[:limit]
    payload = {
        "count": len(queue_items),
        "filters": {
            "sheet_name": sheet_name,
            "min_score": min_score,
            "max_age_days": max_age_days,
            "season_focus": normalized_season_focus,
            "max_per_company": max_per_company,
            "limit": limit,
        },
        "company_overrides_path": str(company_overrides_path),
        "season_counts_scanned": selection.season_counts_scanned,
        "season_counts_selected": selection.season_counts_selected,
        "skipped_season_focus": selection.skipped_season_focus,
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
                "season_bucket": item.season_bucket,
                "source": item.source,
                "source_url": item.source_url,
                "url_hash": item.url_hash,
            }
            for item in queue_items
        ],
    }
    artifact = write_artifact(settings.artifacts_dir, "resume-outreach-queue", payload)
    return {
        "status": "built",
        "artifact": str(artifact),
        "count": len(queue_items),
        "rows_scanned": len(rows),
        "eligible_rows": len(selection.jobs),
        "season_focus": normalized_season_focus,
        "skipped_season_focus": selection.skipped_season_focus,
        "season_counts_scanned": selection.season_counts_scanned,
        "season_counts_selected": selection.season_counts_selected,
    }


def run_supervised_e2e_pipeline(
    *,
    workspace: Path = Path("workspace"),
    account_tracker_output: Path = Path("workspace/account_tracker.xlsx"),
    jobs_xlsx: Path = Path("../ResumeGenerator v1/discovery/jobs.xlsx"),
    sheet_name: str = "Jobs",
    resume_blocklist: Path | None = Path("../ResumeGenerator v1/discovery/blocklist.txt"),
    resume_generator_root: Path = Path("../ResumeGenerator v1"),
    run_resume_generator_discovery: bool = False,
    run_resume_generator_generation: bool = False,
    resume_generator_discovery_hours_old: int = 24,
    resume_generator_discovery_timeout_seconds: int = 5400,
    resume_generator_generation_timeout_seconds: int = 7200,
    resume_generator_with_startup_apply: bool = True,
    resume_generator_min_score: float = 8.0,
    resume_generator_top: int = 10,
    resume_generator_budget_mode: bool = True,
    execute: bool = False,
    send_linkedin: bool = False,
    refresh_linkedin: bool = False,
    live_linkedin: bool = False,
    deep_messages: bool = True,
    linkedin_message_limit: int = 75,
    resume_jobs: bool = True,
    resume_account_universe: bool = True,
    resume_jobs_limit: int | None = None,
    resume_min_score: float = 7.0,
    resume_max_age_days: int = 10,
    resume_season_focus: str = TRANSITION_SEASON_FOCUS,
    resume_outreach_queue: bool = True,
    strategic_accounts: bool = True,
    story_fit_targets: bool = True,
    story_fit_source_path: Path = DEFAULT_STORY_FIT_TARGETS_PATH,
    relationship_leads: bool = True,
    relationship_leads_source_path: Path = DEFAULT_RELATIONSHIP_LEADS_PATH,
    campaign_limit: int = 30,
    max_total_actions: int = 24,
    max_companies: int = 18,
    max_linkedin_invites: int = 12,
    max_linkedin_followups: int = 8,
    max_company_mapping: int = 5,
    max_email_research: int = 5,
    max_context_enrichment: int = 8,
    max_email_drafts: int = 0,
    invite_min_score: int = 35,
    invite_verdict: str = "send",
    adaptive_invite_min_score: bool = True,
    network_enrichment: bool = True,
    external_email_finder: bool = False,
    email_finder_provider: str = "auto",
    run_track_2: bool = True,
) -> tuple[Path, dict[str, object]]:
    if send_linkedin and not execute:
        raise ValueError("--send-linkedin requires --execute.")

    settings = OutreachSettings()
    normalized_resume_season_focus = normalize_season_focus(resume_season_focus)
    workbook = OutreachWorkbook(workspace)
    workbook.initialize()
    stages: list[dict[str, object]] = []
    started_at = utc_now_iso()
    before_counts = workbook.summary_counts()

    def add_stage(name: str, status: str, **data: object) -> None:
        stages.append({"name": name, "status": status, **data})

    add_stage("preflight", "ok", workspace=str(workspace), before_counts=before_counts)

    resume_generator_python_path = resume_generator_python(resume_generator_root)
    if run_resume_generator_discovery:
        command = [
            resume_generator_python_path,
            "discovery/auto/pipeline.py",
            "--hours-old",
            str(resume_generator_discovery_hours_old),
            "--quiet",
        ]
        if resume_generator_with_startup_apply:
            command.append("--with-startup-apply")
        summary = run_external_stage(
            settings=settings,
            label="resume-generator-discovery-run",
            command=command,
            cwd=resume_generator_root,
            timeout_seconds=resume_generator_discovery_timeout_seconds,
        )
        add_stage(
            "resume_generator_discovery",
            str(summary.get("status") or "unknown"),
            artifact=str(summary.get("artifact") or ""),
            returncode=summary.get("returncode"),
        )
    else:
        add_stage("resume_generator_discovery", "skipped", reason="disabled")

    if run_resume_generator_generation:
        command = [
            resume_generator_python_path,
            "jobs.py",
            "pipeline",
            "--min-score",
            str(resume_generator_min_score),
            "--top",
            str(resume_generator_top),
        ]
        if resume_generator_budget_mode:
            command.append("--budget-mode")
        summary = run_external_stage(
            settings=settings,
            label="resume-generator-generation-run",
            command=command,
            cwd=resume_generator_root,
            timeout_seconds=resume_generator_generation_timeout_seconds,
        )
        add_stage(
            "resume_generator_generation",
            str(summary.get("status") or "unknown"),
            artifact=str(summary.get("artifact") or ""),
            returncode=summary.get("returncode"),
        )
    else:
        add_stage("resume_generator_generation", "skipped", reason="disabled")

    if resume_jobs:
        resume_summary = run_resume_jobs_import_stage(
            workspace=workspace,
            jobs_xlsx=jobs_xlsx,
            sheet_name=sheet_name,
            min_score=resume_min_score,
            max_age_days=resume_max_age_days,
            season_focus=normalized_resume_season_focus,
            account_universe=resume_account_universe,
            resume_blocklist=resume_blocklist,
            limit=resume_jobs_limit,
            execute=execute,
        )
        resume_artifact = write_artifact(
            settings.artifacts_dir,
            "supervised-e2e-resume-jobs-import",
            resume_summary,
        )
        add_stage("resume_jobs_import", str(resume_summary["status"]), artifact=str(resume_artifact), summary=resume_summary)
    else:
        add_stage("resume_jobs_import", "skipped", reason="disabled")

    if strategic_accounts:
        summary = import_strategic_account_seeds(workspace, execute=execute)
        artifact = write_artifact(
            settings.artifacts_dir,
            "strategic-account-seed-import",
            {"workspace": str(workspace), "execute": execute, "summary": summary},
        )
        add_stage("strategic_accounts", "imported" if execute else "previewed", artifact=str(artifact), summary=summary)
    else:
        add_stage("strategic_accounts", "skipped", reason="disabled")

    if story_fit_targets:
        if story_fit_source_path.exists():
            summary = import_story_fit_target_seeds(
                workspace,
                source_path=story_fit_source_path,
                execute=execute,
            )
            artifact = write_artifact(
                settings.artifacts_dir,
                "story-fit-target-import",
                {
                    "workspace": str(workspace),
                    "source_path": str(story_fit_source_path),
                    "execute": execute,
                    "summary": summary,
                },
            )
            add_stage("story_fit_targets", "imported" if execute else "previewed", artifact=str(artifact), summary=summary)
        else:
            add_stage(
                "story_fit_targets",
                "skipped",
                reason=f"source file not found: {story_fit_source_path}",
            )
    else:
        add_stage("story_fit_targets", "skipped", reason="disabled")

    if relationship_leads:
        ensure_relationship_leads_template(relationship_leads_source_path)
        summary = import_relationship_lead_seeds(
            workspace,
            source_path=relationship_leads_source_path,
            execute=execute,
        )
        artifact = write_artifact(
            settings.artifacts_dir,
            "relationship-lead-import",
            {
                "workspace": str(workspace),
                "source_path": str(relationship_leads_source_path),
                "execute": execute,
                "summary": summary,
            },
        )
        add_stage("relationship_leads", "imported" if execute else "previewed", artifact=str(artifact), summary=summary)
    else:
        add_stage("relationship_leads", "skipped", reason="disabled")

    if resume_outreach_queue:
        queue_summary = build_resume_outreach_queue_stage(
            settings=settings,
            workspace=workspace,
            jobs_xlsx=jobs_xlsx,
            sheet_name=sheet_name,
            min_score=resume_min_score,
            max_age_days=resume_max_age_days,
            season_focus=normalized_resume_season_focus,
            resume_blocklist=resume_blocklist,
            limit=15,
        )
        add_stage("resume_outreach_queue", str(queue_summary["status"]), summary=queue_summary)
    else:
        add_stage("resume_outreach_queue", "skipped", reason="disabled")

    from outreach.account_tracker import (
        audit_track_2_core,
        build_account_rows,
        build_campaign_plan_rows,
        run as run_tracker,
    )

    rows, tracker_path = run_tracker(workbook_dir=workspace, output_path=account_tracker_output)
    tier_counts: dict[str, int] = {}
    for row in rows:
        tier_counts[row.tier] = tier_counts.get(row.tier, 0) + 1
    add_stage(
        "account_tracker",
        "built",
        output=str(tracker_path),
        account_count=len(rows),
        tier_counts=tier_counts,
    )

    audit = audit_track_2_core(rows)
    audit_artifact = write_artifact(
        settings.artifacts_dir,
        "track-2-core-audit",
        {"workspace": str(workspace), **audit},
    )
    add_stage(
        "track_2_core_audit",
        "clean" if audit.get("is_clean") else "issues",
        artifact=str(audit_artifact),
        priority_accounts=audit.get("priority_accounts", 0),
        issue_counts=audit.get("issue_counts", {}),
    )

    campaign_rows = build_campaign_plan_rows(build_account_rows(workspace))[:campaign_limit]
    campaign_summary: dict[str, int] = {}
    for row in campaign_rows:
        campaign_summary[row.campaign_action] = campaign_summary.get(row.campaign_action, 0) + 1
    campaign_artifact = write_artifact(
        settings.artifacts_dir,
        "account-campaign-plan",
        {
            "count": len(campaign_rows),
            "limit": campaign_limit,
            "summary": campaign_summary,
            "results": [row.__dict__ for row in campaign_rows],
        },
    )
    add_stage(
        "account_campaign_plan",
        "built",
        artifact=str(campaign_artifact),
        count=len(campaign_rows),
        summary=campaign_summary,
    )

    if run_track_2:
        before = _artifact_snapshot(settings.artifacts_dir)
        run_track_2_daily_plan_cmd(
            workspace=workspace,
            max_total_actions=max_total_actions,
            max_companies=max_companies,
            max_linkedin_invites=max_linkedin_invites,
            max_linkedin_followups=max_linkedin_followups,
            max_company_mapping=max_company_mapping,
            max_email_research=max_email_research,
            max_context_enrichment=max_context_enrichment,
            max_email_drafts=max_email_drafts,
            execute=execute,
            send_linkedin=send_linkedin,
            refresh_linkedin=refresh_linkedin,
            live_linkedin=live_linkedin,
            deep_messages=deep_messages,
            linkedin_message_limit=linkedin_message_limit,
            invite_min_score=invite_min_score,
            invite_verdict=invite_verdict,
            adaptive_invite_min_score=adaptive_invite_min_score,
            network_enrichment=network_enrichment,
            external_email_finder=external_email_finder,
            email_finder_provider=email_finder_provider,
        )
        add_stage(
            "track_2_daily_run",
            "ran" if execute else "planned",
            artifacts=_new_artifacts(before, settings.artifacts_dir),
        )
    else:
        add_stage("track_2_daily_run", "skipped", reason="disabled")

    after_counts = workbook.summary_counts()
    payload: dict[str, object] = {
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "workspace": str(workspace),
        "execute": execute,
        "send_linkedin": send_linkedin,
        "refresh_linkedin": refresh_linkedin or send_linkedin,
        "live_linkedin": live_linkedin or refresh_linkedin or send_linkedin,
        "resume_season_focus": normalized_resume_season_focus,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "account_tracker": str(account_tracker_output),
        "budgets": {
            "max_total_actions": max_total_actions,
            "max_companies": max_companies,
            "max_linkedin_invites": max_linkedin_invites,
            "max_linkedin_followups": max_linkedin_followups,
            "max_company_mapping": max_company_mapping,
            "max_email_research": max_email_research,
            "max_context_enrichment": max_context_enrichment,
            "max_email_drafts": max_email_drafts,
        },
        "stages": stages,
        "remaining_known_gaps": [
            "PeopleGrove/USC and recent-MBA-PM source templates are wired, but rows still need to be manually captured from those portals/searches.",
            "cold email drafts stay capped at zero until emails exist and the communication engine is approved.",
            "external email finder is wired but disabled unless --external-email-finder is passed with PROSPEO_API_KEY or HUNTER_API_KEY configured.",
        ],
    }
    artifact = write_artifact(settings.artifacts_dir, "supervised-e2e-run", payload)
    report_artifact, latest_report, report_html, latest_report_html = write_supervised_e2e_report(
        settings=settings,
        payload=payload,
        summary_artifact=artifact,
    )
    payload["daily_report"] = str(report_artifact)
    payload["latest_daily_report"] = str(latest_report)
    payload["daily_report_html"] = str(report_html)
    payload["latest_daily_report_html"] = str(latest_report_html)
    artifact.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return artifact, payload


@app.command("run-supervised-e2e")
def run_supervised_e2e_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    account_tracker_output: Annotated[
        Path,
        typer.Option(help="Output path for the regenerated account tracker workbook"),
    ] = Path("workspace/account_tracker.xlsx"),
    jobs_xlsx: Annotated[
        Path,
        typer.Option(help="Path to ResumeGenerator v1 discovery/jobs.xlsx"),
    ] = Path("../ResumeGenerator v1/discovery/jobs.xlsx"),
    resume_generator_root: Annotated[
        Path,
        typer.Option(help="Path to the ResumeGenerator v1 checkout"),
    ] = Path("../ResumeGenerator v1"),
    run_resume_generator_discovery: Annotated[
        bool,
        typer.Option(help="Run ResumeGenerator discovery before importing jobs.xlsx"),
    ] = False,
    run_resume_generator_generation: Annotated[
        bool,
        typer.Option(help="Run ResumeGenerator jobs.py pipeline before importing jobs.xlsx"),
    ] = False,
    resume_generator_discovery_timeout_seconds: Annotated[
        int,
        typer.Option(help="Max seconds to wait for ResumeGenerator discovery before continuing"),
    ] = 5400,
    resume_generator_generation_timeout_seconds: Annotated[
        int,
        typer.Option(help="Max seconds to wait for ResumeGenerator generation before continuing"),
    ] = 7200,
    resume_generator_top: Annotated[int, typer.Option(help="Max ResumeGenerator jobs to promote/generate")] = 10,
    resume_generator_min_score: Annotated[float, typer.Option(help="Min ResumeGenerator fit score to promote")] = 8.0,
    resume_generator_budget_mode: Annotated[
        bool,
        typer.Option(help="Use ResumeGenerator budget mode for generation"),
    ] = True,
    sheet_name: Annotated[str, typer.Option(help="Worksheet name inside jobs.xlsx")] = "Jobs",
    execute: Annotated[
        bool,
        typer.Option(help="Write imports/safe updates and allow live non-send Track 2 phases"),
    ] = False,
    send_linkedin: Annotated[
        bool,
        typer.Option(help="Allow actual LinkedIn sends. Requires --execute."),
    ] = False,
    refresh_linkedin: Annotated[
        bool,
        typer.Option(help="Read live LinkedIn messages during the Track 2 phase"),
    ] = False,
    live_linkedin: Annotated[
        bool,
        typer.Option(help="Allow live LinkedIn browser phases during Track 2. Nightly cron should usually leave this off."),
    ] = False,
    max_total_actions: Annotated[int, typer.Option(help="Maximum total Track 2 actions today")] = 24,
    max_companies: Annotated[int, typer.Option(help="Maximum distinct companies to touch today")] = 18,
    max_linkedin_invites: Annotated[int, typer.Option(help="Maximum new LinkedIn invites today")] = 12,
    max_linkedin_followups: Annotated[int, typer.Option(help="Maximum LinkedIn follow-up/reply messages today")] = 8,
    max_company_mapping: Annotated[int, typer.Option(help="Maximum companies to map contacts for today")] = 5,
    max_email_research: Annotated[int, typer.Option(help="Maximum email/contact-info research tasks today")] = 5,
    max_context_enrichment: Annotated[int, typer.Option(help="Maximum company enrichment tasks today")] = 8,
    max_email_drafts: Annotated[int, typer.Option(help="Maximum cold email drafts today")] = 0,
    resume_season_focus: Annotated[
        str,
        typer.Option(
            help=(
                "ResumeGenerator season filter: all, fall_ft_transition, full_time, fall, or summer"
            )
        ),
    ] = TRANSITION_SEASON_FOCUS,
    external_email_finder: Annotated[
        bool,
        typer.Option(help="Call configured external email finder during Track 2 email research"),
    ] = False,
    email_finder_provider: Annotated[
        str,
        typer.Option(help="External email finder provider: auto, prospeo, or hunter"),
    ] = "auto",
) -> None:
    """Run the supervised daily engine sequence, ending with bounded Track 2 actions."""
    if send_linkedin and not execute:
        typer.echo("--send-linkedin requires --execute.")
        raise typer.Exit(code=1)

    artifact, payload = run_supervised_e2e_pipeline(
        workspace=workspace,
        account_tracker_output=account_tracker_output,
        jobs_xlsx=jobs_xlsx,
        sheet_name=sheet_name,
        resume_generator_root=resume_generator_root,
        run_resume_generator_discovery=run_resume_generator_discovery,
        run_resume_generator_generation=run_resume_generator_generation,
        resume_generator_discovery_timeout_seconds=resume_generator_discovery_timeout_seconds,
        resume_generator_generation_timeout_seconds=resume_generator_generation_timeout_seconds,
        resume_generator_top=resume_generator_top,
        resume_generator_min_score=resume_generator_min_score,
        resume_generator_budget_mode=resume_generator_budget_mode,
        execute=execute,
        send_linkedin=send_linkedin,
        refresh_linkedin=refresh_linkedin,
        live_linkedin=live_linkedin,
        max_total_actions=max_total_actions,
        max_companies=max_companies,
        max_linkedin_invites=max_linkedin_invites,
        max_linkedin_followups=max_linkedin_followups,
        max_company_mapping=max_company_mapping,
        max_email_research=max_email_research,
        max_context_enrichment=max_context_enrichment,
        max_email_drafts=max_email_drafts,
        resume_season_focus=resume_season_focus,
        external_email_finder=external_email_finder,
        email_finder_provider=email_finder_provider,
    )
    typer.echo(f"{'Ran' if execute else 'Planned'} supervised E2E.")
    typer.echo(f"Workspace counts: {payload['before_counts']} -> {payload['after_counts']}")
    typer.echo(f"Summary artifact: {artifact}")
    typer.echo(f"Daily report: {payload.get('latest_daily_report', '')}")
    typer.echo(f"HTML report: {payload.get('latest_daily_report_html', '')}")
    for stage in payload["stages"]:
        typer.echo(f"- {stage['name']} | {stage['status']}")


@app.command("import-strategic-accounts")
def import_strategic_accounts_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    execute: Annotated[
        bool,
        typer.Option(help="Write built-in strategic account seeds to organizations.csv"),
    ] = False,
) -> None:
    """Import MAANG, major SaaS, AI, data, fintech, and platform accounts for Track 2."""
    summary = import_strategic_account_seeds(workspace, execute=execute)
    artifact = write_artifact(
        OutreachSettings().artifacts_dir,
        "strategic-account-seed-import",
        {
            "workspace": str(workspace),
            "execute": execute,
            "summary": summary,
        },
    )
    typer.echo(f"{'Imported' if execute else 'Planned'} strategic account seeds.")
    typer.echo(f"Summary: {summary}")
    typer.echo(f"Artifact: {artifact}")


@app.command("import-story-fit-targets")
def import_story_fit_targets_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    source_path: Annotated[
        Path,
        typer.Option(help="Path to the curated story-fit target CSV"),
    ] = DEFAULT_STORY_FIT_TARGETS_PATH,
    execute: Annotated[
        bool,
        typer.Option(help="Write story-fit target seeds to organizations.csv"),
    ] = False,
) -> None:
    """Import companies selected because Akshat has a real pitch, not because a role was posted."""
    summary = import_story_fit_target_seeds(workspace, source_path=source_path, execute=execute)
    artifact = write_artifact(
        OutreachSettings().artifacts_dir,
        "story-fit-target-import",
        {
            "workspace": str(workspace),
            "source_path": str(source_path),
            "execute": execute,
            "summary": summary,
        },
    )
    typer.echo(f"{'Imported' if execute else 'Planned'} story-fit targets.")
    typer.echo(f"Summary: {summary}")
    typer.echo(f"Artifact: {artifact}")


@app.command("init-relationship-leads")
def init_relationship_leads_cmd(
    source_path: Annotated[
        Path,
        typer.Option(help="Path to create if missing"),
    ] = DEFAULT_RELATIONSHIP_LEADS_PATH,
    source_key: Annotated[
        str,
        typer.Option(help="Optional preset: peoplegrove_usc or recent_mba_pm"),
    ] = "",
) -> None:
    """Create the CSV template for one-time PeopleGrove/recent-MBA/USC lead imports."""
    if source_key and source_path == DEFAULT_RELATIONSHIP_LEADS_PATH:
        source_path = relationship_source_default_path(source_key)
    path = ensure_relationship_leads_template(source_path, source_key=source_key)
    typer.echo(f"Relationship lead template ready: {path}")
    if source_key:
        preset = relationship_source_preset(source_key)
        typer.echo(f"Preset: {source_key}")
        typer.echo(f"Defaults: source_type={preset.get('source_type', '')} target_lists={preset.get('target_lists', '')}")
        typer.echo(f"Guide: {path.with_suffix('.md')}")


@app.command("import-relationship-leads")
def import_relationship_leads_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    source_path: Annotated[
        Path,
        typer.Option(help="Path to relationship lead CSV"),
    ] = DEFAULT_RELATIONSHIP_LEADS_PATH,
    source_key: Annotated[
        str,
        typer.Option(help="Optional preset defaults: peoplegrove_usc or recent_mba_pm"),
    ] = "",
    execute: Annotated[
        bool,
        typer.Option(help="Write relationship leads to organizations.csv and contacts.csv"),
    ] = False,
) -> None:
    """Import one-time relationship leads from PeopleGrove, recent MBA PM, USC founder, or manual pulls."""
    if source_key and source_path == DEFAULT_RELATIONSHIP_LEADS_PATH:
        source_path = relationship_source_default_path(source_key)
    ensure_relationship_leads_template(source_path, source_key=source_key)
    summary = import_relationship_lead_seeds(
        workspace,
        source_path=source_path,
        source_key=source_key,
        execute=execute,
    )
    artifact = write_artifact(
        OutreachSettings().artifacts_dir,
        "relationship-lead-import",
        {
            "workspace": str(workspace),
            "source_path": str(source_path),
            "execute": execute,
            "summary": summary,
        },
    )
    typer.echo(f"{'Imported' if execute else 'Planned'} relationship leads.")
    typer.echo(f"Summary: {summary}")
    typer.echo(f"Artifact: {artifact}")


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
        progress=lambda index, total, name: typer.echo(f"[{index}/{total}] enriching {name}"),
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


@app.command("resolve-company-websites")
def resolve_company_websites_cmd(
    workspace: Annotated[
        Path,
        typer.Option(help="Path to the workspace directory containing CSVs"),
    ] = Path("workspace"),
    limit: Annotated[int, typer.Option(help="Maximum companies to resolve")] = 50,
    start_at: Annotated[int, typer.Option(help="Skip this many selected companies before resolving")] = 0,
    company: Annotated[
        list[str] | None,
        typer.Option("--company", help="Only resolve a named company; repeat for multiple companies"),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(help="Write resolved websites back to organizations.csv; default is preview only"),
    ] = False,
    only_non_verified: Annotated[
        bool,
        typer.Option(help="Only target companies without external_verified context"),
    ] = True,
    web_search: Annotated[
        bool,
        typer.Option(help="Use public web search after direct source URLs/outbound links fail"),
    ] = True,
    max_search_results: Annotated[int, typer.Option(help="Maximum search-result URLs to validate per company")] = 5,
    min_score: Annotated[int, typer.Option(help="Minimum website-validation score required to accept a resolved URL")] = 11,
    timeout_seconds: Annotated[int, typer.Option(help="Network fetch timeout per public page")] = 4,
) -> None:
    """Resolve canonical websites for companies that cannot yet be externally verified."""
    from outreach.company_enrichment import resolve_company_websites

    results = resolve_company_websites(
        workspace,
        limit=limit,
        start_at=start_at,
        companies=set(company or []),
        execute=execute,
        only_non_verified=only_non_verified,
        use_web_search=web_search,
        max_search_results=max_search_results,
        min_score=min_score,
        fetcher=HttpTextDownloader(timeout_seconds=timeout_seconds),
        progress=lambda index, total, name: typer.echo(f"[{index}/{total}] resolving {name}"),
    )
    summary: dict[str, int] = {}
    for row in results:
        summary[row.status] = summary.get(row.status, 0) + 1

    artifact = write_artifact(
        OutreachSettings().artifacts_dir,
        "company-website-resolution",
        {
            "workspace": str(workspace),
            "limit": limit,
            "start_at": start_at,
            "companies": company or [],
            "execute": execute,
            "only_non_verified": only_non_verified,
            "web_search": web_search,
            "max_search_results": max_search_results,
            "min_score": min_score,
            "timeout_seconds": timeout_seconds,
            "summary": summary,
            "results": [row.__dict__ for row in results],
        },
    )

    typer.echo(f"{'Resolved' if execute else 'Planned'} websites for {len(results)} companies.")
    typer.echo(f"Summary: {summary}")
    typer.echo(f"Artifact: {artifact}")
    for row in results[: min(20, len(results))]:
        typer.echo(
            f"- {row.company} | status={row.status} | website={row.website or '-'} | "
            f"source={row.source or '-'} | confidence={row.confidence or '-'} | score={row.score}"
        )
        if row.error:
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
    season_focus: Annotated[
        str,
        typer.Option(
            "--resume-season-focus",
            help=(
                "ResumeGenerator season filter: all, fall_ft_transition, full_time, fall, or summer"
            ),
        ),
    ] = DEFAULT_SEASON_FOCUS,
    account_universe: Annotated[
        bool,
        typer.Option(help="Track 2 mode: import a broad company universe from jobs.xlsx, ignoring score and age gates."),
    ] = False,
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
    effective_min_score = 0.0 if account_universe else min_score
    effective_max_age_days = None if account_universe else max_age_days
    effective_statuses = tuple(include_status or DEFAULT_INCLUDE_STATUSES)
    normalized_season_focus = normalize_season_focus(season_focus)
    selection = select_resume_jobs(
        rows,
        include_statuses=effective_statuses,
        min_score=effective_min_score,
        max_age_days=effective_max_age_days,
        season_focus=normalized_season_focus,
        blocklist_patterns=blocklist_patterns,
    )
    selected_jobs = selection.jobs[:limit] if limit else selection.jobs

    typer.echo(f"Scanned {len(rows)} resume-tracker rows from {jobs_xlsx}")
    typer.echo(
        "Eligible rows: "
        f"{len(selection.jobs)}"
        f" | selected_rows={len(selected_jobs)}"
        f" | skipped_status={selection.skipped_status}"
        f" | skipped_score={selection.skipped_score}"
        f" | skipped_age={selection.skipped_age}"
        f" | skipped_season_focus={selection.skipped_season_focus}"
        f" | skipped_blocklist={selection.skipped_blocklist}"
        f" | duplicates_removed={selection.duplicates_removed}"
    )
    typer.echo(
        f"Season focus: {normalized_season_focus} | "
        f"selected_buckets={selection.season_counts_selected}"
    )
    for job in selected_jobs[:10]:
        score_text = f"{job.fit_score:.1f}" if job.fit_score is not None else "n/a"
        found_text = job.date_found.isoformat() if job.date_found else "n/a"
        override = company_overrides.get(normalize_dedupe_text(job.company))
        company_type = infer_company_type_for_job(job, company_override=override)
        season_bucket = classify_resume_role_season(job)
        typer.echo(
            f"- id={job.row_id} | {job.company} | {job.role_title} | "
            f"score={score_text} | status={job.normalized_status} | found={found_text} | "
            f"season={season_bucket} | company_type={company_type}"
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
            notes=(
                f"sheet={sheet_name} | min_score={effective_min_score} | "
                f"max_age_days={effective_max_age_days if effective_max_age_days is not None else 'none'} | "
                f"account_universe={account_universe} | season_focus={normalized_season_focus}"
            ),
        )
    )

    organizations_added = 0
    opportunities_added = 0
    for job in selected_jobs:
        override = company_overrides.get(normalize_dedupe_text(job.company))
        target_lists = target_lists_from_resume_status(job.status)
        if account_universe:
            target_lists = _merge_target_lists(target_lists, "account-universe;track-2;resume_generator")
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
        elif account_universe:
            merged_target_lists = _merge_target_lists(organization.target_lists, target_lists)
            updates: dict[str, str] = {}
            if merged_target_lists != organization.target_lists:
                updates["target_lists"] = merged_target_lists
            if not organization.city and job.location:
                updates["city"] = job.location
            if not organization.source_url and job.url:
                updates["source_url"] = job.url
            if updates:
                updates["last_updated_at"] = utc_now_iso()
                organization = workbook.update_organization(organization.organization_id, **updates) or organization

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
    typer.echo(f"- season_focus: {normalized_season_focus}")


@app.command("build-resume-outreach-queue")
def build_resume_outreach_queue_command(
    jobs_xlsx: Annotated[
        Path,
        typer.Option(help="Path to ResumeGenerator v1 discovery/jobs.xlsx"),
    ] = Path("../ResumeGenerator v1/discovery/jobs.xlsx"),
    sheet_name: Annotated[str, typer.Option(help="Worksheet name inside the xlsx")] = "Jobs",
    min_score: Annotated[float, typer.Option(help="Minimum fit score to include")] = 7.0,
    max_age_days: Annotated[int, typer.Option(help="Maximum age in days")] = 10,
    season_focus: Annotated[
        str,
        typer.Option(
            "--resume-season-focus",
            help=(
                "ResumeGenerator season filter: all, fall_ft_transition, full_time, fall, or summer"
            ),
        ),
    ] = TRANSITION_SEASON_FOCUS,
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
    normalized_season_focus = normalize_season_focus(season_focus)
    selection = select_resume_jobs(
        rows,
        include_statuses=DEFAULT_INCLUDE_STATUSES,
        min_score=min_score,
        max_age_days=max_age_days,
        season_focus=normalized_season_focus,
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
                "season_focus": normalized_season_focus,
                "max_per_company": max_per_company,
                "limit": limit,
            },
            "company_overrides_path": str(company_overrides_path),
            "season_counts_scanned": selection.season_counts_scanned,
            "season_counts_selected": selection.season_counts_selected,
            "skipped_season_focus": selection.skipped_season_focus,
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
                    "season_bucket": item.season_bucket,
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
    typer.echo(
        f"Season focus: {normalized_season_focus} | "
        f"eligible_rows={len(selection.jobs)} | skipped_season_focus={selection.skipped_season_focus}"
    )
    for item in queue_items:
        score_text = f"{item.outreach_priority_score:.1f}"
        fit_text = f"{item.fit_score:.1f}" if item.fit_score is not None else "n/a"
        found_text = item.date_found.isoformat() if item.date_found else "n/a"
        typer.echo(
            f"- {item.company} | {item.role_title} | outreach_score={score_text} | "
            f"fit={fit_text} | season={item.season_bucket} | type={item.company_type} | "
            f"bias={item.startup_bias} | found={found_text}"
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
        opportunities=workbook.list_opportunities(),
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
    typer.echo(f"- External action items for Akshat: {action_summary['external_action_items']}")
    company_counts = action_summary.get("by_company") or {}
    if company_counts:
        typer.echo("- Top companies to clear:")
        for company, count in list(company_counts.items())[:8]:
            typer.echo(f"  {company}: {count}")
    action_items = action_summary.get("action_items") or []
    if action_items:
        typer.echo("- Action items:")
        for action in action_items[:10]:
            typer.echo(f"  [{action.get('priority', 'medium')}] {action.get('description', '')}")
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
        opportunities=workbook.list_opportunities(),
    )[:limit]

    kind_summary: dict[str, int] = {}
    for draft in drafts:
        key = str(draft["draft_kind"])
        kind_summary[key] = kind_summary.get(key, 0) + 1
    action_summary = summarize_linkedin_followup_actions(drafts, list(payload.get("results") or []))

    artifact = write_artifact(
        settings.artifacts_dir,
        "linkedin-followup-drafts",
        {
            "source_artifact": str(reconcile_artifact),
            "count": len(drafts),
            "summary": kind_summary,
            "action_summary": action_summary,
            "results": drafts,
        },
    )

    typer.echo(f"Drafted {len(drafts)} LinkedIn follow-ups.")
    typer.echo(f"Summary: {kind_summary}")
    typer.echo(f"External action items for Akshat: {action_summary['external_action_items']}")
    typer.echo(f"Artifact: {artifact}")
    action_items = action_summary.get("action_items") or []
    if action_items:
        typer.echo("Action items:")
        for action in action_items[:10]:
            typer.echo(f"- [{action.get('priority', 'medium')}] {action.get('description', '')}")
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
        for action in draft.get("action_items") or []:
            typer.echo(f"  Action: [{action.get('priority', 'medium')}] {action.get('description', '')}")
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
    recommendation: Annotated[
        list[str] | None,
        typer.Option("--recommendation", help="Allowed send_recommendation value; repeatable. Defaults to safe_to_review."),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(help="Actually send follow-ups instead of doing a guarded dry run"),
    ] = False,
) -> None:
    settings = OutreachSettings()
    with draft_artifact.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    all_drafts = list(payload.get("results") or [])
    drafts = all_drafts
    if not drafts:
        typer.echo("No follow-up drafts found in artifact.")
        raise typer.Exit(code=0)
    allowed_recommendations = {item.strip() for item in (recommendation or ["safe_to_review"]) if item.strip()}
    skipped_by_recommendation: list[dict] = []
    if allowed_recommendations:
        eligible: list[dict] = []
        for draft in drafts:
            draft_recommendation = str(draft.get("send_recommendation") or "")
            if draft_recommendation in allowed_recommendations or (
                include_optional and draft_recommendation == "optional"
            ):
                eligible.append(draft)
            else:
                skipped_by_recommendation.append(draft)
        drafts = eligible
    cadence_allowed, cadence_held = _apply_linkedin_cadence_guards(
        workbook=OutreachWorkbook(settings.resolved_tracking_workspace_dir),
        drafts=drafts,
    )
    drafts = cadence_allowed
    skipped_by_recommendation.extend(cadence_held)
    if not drafts:
        pending_path = update_linkedin_followup_pending_review(
            settings=settings,
            pending_drafts=skipped_by_recommendation,
            cleared_drafts=[],
            source_artifact=draft_artifact,
        )
        artifact = write_artifact(
            settings.artifacts_dir,
            "linkedin-followup-send-results",
            {
                "source_artifact": str(draft_artifact),
                "progress_artifact": "",
                "execute": execute,
                "limit": limit,
                "start_at": start_at,
                "include_optional": include_optional,
                "allowed_recommendations": sorted(allowed_recommendations),
                "total_drafts": len(all_drafts),
                "eligible_count": 0,
                "skipped_by_recommendation_count": len(skipped_by_recommendation),
                "cadence_held_count": len(cadence_held),
                "count": 0,
                "status_counts": {"skipped_by_recommendation": len(skipped_by_recommendation)},
                "touchpoints_added": 0,
                "pending_review_artifact": str(pending_path),
                "results": [],
                "skipped_by_recommendation": skipped_by_recommendation,
            },
        )
        typer.echo(f"No follow-up drafts matched recommendation filter: {sorted(allowed_recommendations)}")
        typer.echo(f"Artifact: {artifact}")
        raise typer.Exit(code=0)
    if execute:
        LinkedInScraper(settings).require_live_cdp_session()

    typer.echo(f"Processing LinkedIn follow-ups from {draft_artifact}")
    typer.echo(f"Mode: {'execute' if execute else 'dry run'}")
    pending_path = update_linkedin_followup_pending_review(
        settings=settings,
        pending_drafts=skipped_by_recommendation,
        cleared_drafts=[],
        source_artifact=draft_artifact,
    )
    artifact, progress_artifact, status_counts, touchpoints_added = execute_linkedin_followup_send(
        settings=settings,
        draft_artifact=draft_artifact,
        drafts=drafts,
        execute=execute,
        limit=limit,
        start_at=start_at,
        include_optional=include_optional,
    )
    with artifact.open(encoding="utf-8") as handle:
        send_payload = json.load(handle)
    sent_keys = {
        _followup_pending_key(item)
        for item in list(send_payload.get("results") or [])
        if isinstance(item, dict) and item.get("status") == "sent"
    }
    sent_drafts = [draft for draft in drafts if _followup_pending_key(draft) in sent_keys]
    pending_path = update_linkedin_followup_pending_review(
        settings=settings,
        pending_drafts=skipped_by_recommendation,
        cleared_drafts=sent_drafts,
        source_artifact=draft_artifact,
    )
    if skipped_by_recommendation:
        send_payload["allowed_recommendations"] = sorted(allowed_recommendations)
        send_payload["total_drafts"] = len(all_drafts)
        send_payload["eligible_count"] = len(drafts)
        send_payload["skipped_by_recommendation_count"] = len(skipped_by_recommendation)
        send_payload["cadence_held_count"] = len(cadence_held)
        send_payload["skipped_by_recommendation"] = skipped_by_recommendation
        send_payload["pending_review_artifact"] = str(pending_path)
        send_payload["status_counts"]["skipped_by_recommendation"] = len(skipped_by_recommendation)
        artifact.write_text(json.dumps(send_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        status_counts["skipped_by_recommendation"] = len(skipped_by_recommendation)
    else:
        with artifact.open(encoding="utf-8") as handle:
            send_payload = json.load(handle)
        send_payload["allowed_recommendations"] = sorted(allowed_recommendations)
        send_payload["total_drafts"] = len(all_drafts)
        send_payload["eligible_count"] = len(drafts)
        send_payload["skipped_by_recommendation_count"] = 0
        send_payload["cadence_held_count"] = len(cadence_held)
        send_payload["pending_review_artifact"] = str(pending_path)
        artifact.write_text(json.dumps(send_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    typer.echo(f"Status summary: {status_counts}")
    typer.echo(f"Eligible drafts: {len(drafts)}/{len(all_drafts)}")
    if skipped_by_recommendation:
        typer.echo(f"Skipped by recommendation policy: {len(skipped_by_recommendation)}")
    typer.echo(f"Pending review artifact: {pending_path}")
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

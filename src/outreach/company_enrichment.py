from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from outreach.account_tracker import DOMAIN_TAGS
from outreach.discovery.http import HttpTextDownloader, extract_html_segments
from outreach.tracking import OpportunityRecord, OrganizationRecord, OutreachWorkbook


CONTEXT_DATE_KEY = "context_enriched_at"
CONTEXT_SOURCE_KEY = "context_source"
CONTEXT_CONFIDENCE_KEY = "context_confidence"
CONTEXT_REFRESH_AFTER_KEY = "context_refresh_after"
CONTEXT_EVIDENCE_URL_KEY = "context_evidence_url"
WEBSITE_RESOLVED_AT_KEY = "website_resolved_at"
WEBSITE_RESOLUTION_SOURCE_KEY = "website_resolution_source"
WEBSITE_RESOLUTION_CONFIDENCE_KEY = "website_resolution_confidence"
WEBSITE_RESOLUTION_EVIDENCE_URL_KEY = "website_resolution_evidence_url"
INFERRED_FROM_JOB = "inferred_from_job"
EXTERNAL_VERIFIED = "external_verified"

BLOCKED_SEARCH_DOMAINS = {
    "linkedin.com",
    "www.linkedin.com",
    "indeed.com",
    "www.indeed.com",
    "glassdoor.com",
    "www.glassdoor.com",
    "levels.fyi",
    "www.levels.fyi",
    "theorg.com",
    "www.theorg.com",
}

BLOCKED_WEBSITE_DOMAINS = {
    *BLOCKED_SEARCH_DOMAINS,
    "builtin.com",
    "www.builtin.com",
    "builtinsf.com",
    "www.builtinsf.com",
    "builtinla.com",
    "www.builtinla.com",
    "ycombinator.com",
    "www.ycombinator.com",
    "wellfound.com",
    "www.wellfound.com",
    "techcrunch.com",
    "www.techcrunch.com",
    "crunchbase.com",
    "www.crunchbase.com",
    "wikipedia.org",
    "www.wikipedia.org",
    "facebook.com",
    "www.facebook.com",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "youtube.com",
    "www.youtube.com",
    "github.com",
    "www.github.com",
    "bloomberg.com",
    "www.bloomberg.com",
}

TAG_SYNONYMS: dict[str, tuple[str, ...]] = {
    "artificial-intelligence": ("artificial intelligence", "ai", "machine learning", "ml", "genai", "generative ai"),
    "data-platform": ("data platform", "data infrastructure", "analytics platform", "business intelligence"),
    "data-pipeline": ("data pipeline", "etl", "data integration"),
    "integration": ("integration", "connector", "api"),
    "developer-tools": ("developer tools", "devtools", "developer platform", "devex"),
    "observability": ("observability", "monitoring", "incident management"),
    "hiring": ("hiring", "recruiting", "talent acquisition", "hr tech", "workforce"),
    "marketplace": ("marketplace", "two-sided marketplace"),
    "logistics": ("logistics", "delivery", "fleet", "supply chain"),
    "fintech": ("fintech", "financial technology", "payments", "billing", "banking"),
    "healthcare": ("healthcare", "health tech", "healthtech", "clinical", "patient"),
    "workflow-automation": ("workflow automation", "automation", "automate workflows"),
    "productivity": ("productivity", "collaboration", "work management"),
    "saas": ("saas", "software as a service", "enterprise software"),
}


@dataclass
class CompanyContextPatch:
    source: str
    confidence: str
    evidence_url: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    website: str = ""
    team_size: str = ""
    prestige_signals: list[str] = field(default_factory=list)
    prestige_evidence_url: str = ""


@dataclass
class CompanyEnrichmentCandidate:
    organization: OrganizationRecord
    opportunities: list[OpportunityRecord]
    reasons: list[str]


@dataclass
class CompanyEnrichmentResult:
    organization_id: str
    company: str
    status: str
    reasons: list[str]
    source: str = ""
    confidence: str = ""
    tags: list[str] = field(default_factory=list)
    description: str = ""
    website: str = ""
    evidence_url: str = ""
    prestige_signals: list[str] = field(default_factory=list)
    prestige_evidence_url: str = ""
    error: str = ""


@dataclass
class CompanyWebsiteResolutionResult:
    organization_id: str
    company: str
    status: str
    reasons: list[str] = field(default_factory=list)
    website: str = ""
    source: str = ""
    confidence: str = ""
    evidence_url: str = ""
    score: int = 0
    error: str = ""


@dataclass
class CompanyWebsiteCandidate:
    website: str
    source: str
    confidence: str
    evidence_url: str
    score: int


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta_description = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if tag_name == "title":
            self._in_title = True
            return
        if tag_name != "meta":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        name = attrs_dict.get("name", "").lower()
        prop = attrs_dict.get("property", "").lower()
        if name == "description" or prop == "og:description":
            content = attrs_dict.get("content", "").strip()
            if content and not self.meta_description:
                self.meta_description = _clean_text(content, max_length=420)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            text = " ".join(data.split()).strip()
            if text:
                self.title_parts.append(text)

    @property
    def title(self) -> str:
        return _clean_text(" ".join(self.title_parts), max_length=180)


def parse_notes_parts(notes: str) -> tuple[list[str], dict[str, str]]:
    freeform: list[str] = []
    metadata: dict[str, str] = {}
    for part in notes.split("|"):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            freeform.append(item)
            continue
        key, _, value = item.partition("=")
        metadata[key.strip()] = value.strip()
    return freeform, metadata


def format_notes_parts(freeform: list[str], metadata: dict[str, str]) -> str:
    parts = [part.strip() for part in freeform if part.strip()]
    for key, value in metadata.items():
        clean_value = _clean_note_value(value)
        if clean_value:
            parts.append(f"{key}={clean_value}")
    return " | ".join(parts)


def select_company_enrichment_candidates(
    workbook_dir,
    *,
    refresh_days: int = 14,
    companies: set[str] | None = None,
    include_fresh: bool = False,
    verify_all: bool = False,
    force: bool = False,
    require_direct_url: bool = False,
) -> list[CompanyEnrichmentCandidate]:
    workbook = OutreachWorkbook(workbook_dir)
    orgs = workbook.list_organizations()
    opportunities = workbook.list_opportunities()
    opps_by_org: dict[str, list[OpportunityRecord]] = {}
    for opp in opportunities:
        opps_by_org.setdefault(opp.organization_id, []).append(opp)

    normalized_companies = {_normalize_company(name) for name in companies or set()}
    candidates: list[CompanyEnrichmentCandidate] = []
    for org in orgs:
        if normalized_companies and _normalize_company(org.name) not in normalized_companies:
            continue
        if require_direct_url and not _direct_context_urls(org):
            continue
        reasons = (
            ["force_refresh"]
            if force
            else company_context_gap_reasons(
                org,
                opps_by_org.get(org.organization_id, []),
                refresh_days=refresh_days,
                include_fresh=include_fresh,
                verify_all=verify_all,
            )
        )
        if reasons:
            candidates.append(
                CompanyEnrichmentCandidate(
                    organization=org,
                    opportunities=opps_by_org.get(org.organization_id, []),
                    reasons=reasons,
                )
            )
    candidates.sort(
        key=lambda item: (
            "missing_domain_context" in item.reasons,
            bool(item.opportunities),
            item.organization.last_updated_at,
            item.organization.name.lower(),
        ),
        reverse=True,
    )
    return candidates


def company_context_gap_reasons(
    org: OrganizationRecord,
    opportunities: list[OpportunityRecord],
    *,
    refresh_days: int,
    include_fresh: bool,
    verify_all: bool,
) -> list[str]:
    _, metadata = parse_notes_parts(org.notes)
    has_tags = bool(metadata.get("tags", "").strip())
    has_description = bool(metadata.get("description", "").strip())
    has_website = bool(org.website or metadata.get("website", ""))
    reasons: list[str] = []

    if not has_tags or not has_description:
        reasons.append("missing_domain_context")
    if not has_website:
        reasons.append("missing_website")
    if opportunities and (not has_tags or not has_description):
        reasons.append("role_without_domain_context")
    if verify_all and metadata.get(CONTEXT_CONFIDENCE_KEY) != EXTERNAL_VERIFIED:
        reasons.append("needs_external_verification")

    enriched_at = _parse_iso_date(metadata.get(CONTEXT_DATE_KEY, ""))
    if enriched_at and enriched_at < datetime.now(UTC) - timedelta(days=refresh_days):
        reasons.append("stale_context")
    elif include_fresh and not enriched_at:
        reasons.append("missing_context_refresh_marker")

    if not reasons:
        return []
    if reasons == ["missing_website"] and not opportunities and not include_fresh:
        return []
    return list(dict.fromkeys(reasons))


def enrich_company_contexts(
    workbook_dir,
    *,
    limit: int = 50,
    start_at: int = 0,
    refresh_days: int = 14,
    companies: set[str] | None = None,
    execute: bool = False,
    use_network: bool = True,
    use_web_search: bool = True,
    verify_all: bool = False,
    force: bool = False,
    require_direct_url: bool = False,
    fallback_to_jobs: bool = True,
    fetcher: HttpTextDownloader | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[CompanyEnrichmentResult]:
    workbook = OutreachWorkbook(workbook_dir)
    fetcher = fetcher or HttpTextDownloader(timeout_seconds=12)
    candidates = select_company_enrichment_candidates(
        workbook_dir,
        refresh_days=refresh_days,
        companies=companies,
        verify_all=verify_all,
        force=force,
        require_direct_url=require_direct_url,
    )[start_at : start_at + limit]
    results: list[CompanyEnrichmentResult] = []
    total = len(candidates)
    for index, candidate in enumerate(candidates, 1):
        org = candidate.organization
        if progress:
            progress(index, total, org.name)
        try:
            patch = build_company_context_patch(
                org,
                candidate.opportunities,
                fetcher=fetcher,
                use_network=use_network,
                use_web_search=use_web_search,
                fallback_to_jobs=fallback_to_jobs,
            )
            if not patch:
                results.append(
                    CompanyEnrichmentResult(
                        organization_id=org.organization_id,
                        company=org.name,
                        status="no_context_found",
                        reasons=candidate.reasons,
                    )
                )
                continue
            if execute:
                apply_company_context_patch(
                    workbook,
                    org,
                    patch,
                    refresh_days=refresh_days,
                )
            results.append(
                CompanyEnrichmentResult(
                    organization_id=org.organization_id,
                    company=org.name,
                    status="updated" if execute else "planned",
                    reasons=candidate.reasons,
                    source=patch.source,
                    confidence=patch.confidence,
                    tags=patch.tags,
                    description=patch.description,
                    website=patch.website,
                    evidence_url=patch.evidence_url,
                    prestige_signals=patch.prestige_signals,
                    prestige_evidence_url=patch.prestige_evidence_url,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive for flaky public pages
            results.append(
                CompanyEnrichmentResult(
                    organization_id=org.organization_id,
                    company=org.name,
                    status="failed",
                    reasons=candidate.reasons,
                    error=str(exc),
                )
            )
    return results


def resolve_company_websites(
    workbook_dir,
    *,
    limit: int = 50,
    start_at: int = 0,
    companies: set[str] | None = None,
    execute: bool = False,
    only_non_verified: bool = True,
    use_web_search: bool = True,
    allow_domain_guess: bool = False,
    max_search_results: int = 5,
    min_score: int | None = None,
    fetcher: HttpTextDownloader | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[CompanyWebsiteResolutionResult]:
    workbook = OutreachWorkbook(workbook_dir)
    fetcher = fetcher or HttpTextDownloader(timeout_seconds=8)
    normalized_companies = {_normalize_company(name) for name in companies or set()}
    candidates: list[tuple[OrganizationRecord, list[str]]] = []
    for org in workbook.list_organizations():
        if normalized_companies and _normalize_company(org.name) not in normalized_companies:
            continue
        reasons = company_website_gap_reasons(org, only_non_verified=only_non_verified)
        if reasons:
            candidates.append((org, reasons))

    candidates.sort(
        key=lambda item: (
            "context_not_external_verified" in item[1],
            item[0].last_updated_at,
            item[0].name.lower(),
        ),
        reverse=True,
    )

    results: list[CompanyWebsiteResolutionResult] = []
    selected = candidates[start_at : start_at + limit]
    total = len(selected)
    for index, (org, reasons) in enumerate(selected, 1):
        if progress:
            progress(index, total, org.name)
        try:
            candidate = resolve_company_website(
                org,
                fetcher=fetcher,
                use_web_search=use_web_search,
                allow_domain_guess=allow_domain_guess,
                max_search_results=max_search_results,
                min_score=min_score,
            )
            if not candidate:
                results.append(
                    CompanyWebsiteResolutionResult(
                        organization_id=org.organization_id,
                        company=org.name,
                        status="no_url_found",
                        reasons=reasons,
                    )
                )
                continue
            if execute:
                apply_company_website_resolution(workbook, org, candidate)
            results.append(
                CompanyWebsiteResolutionResult(
                    organization_id=org.organization_id,
                    company=org.name,
                    status="resolved" if execute else "planned",
                    reasons=reasons,
                    website=candidate.website,
                    source=candidate.source,
                    confidence=candidate.confidence,
                    evidence_url=candidate.evidence_url,
                    score=candidate.score,
                )
            )
        except Exception as exc:  # pragma: no cover - public pages/search are flaky
            results.append(
                CompanyWebsiteResolutionResult(
                    organization_id=org.organization_id,
                    company=org.name,
                    status="failed",
                    reasons=reasons,
                    error=str(exc),
                )
            )
    return results


def company_website_gap_reasons(org: OrganizationRecord, *, only_non_verified: bool = True) -> list[str]:
    _, metadata = parse_notes_parts(org.notes)
    reasons: list[str] = []
    if org.website:
        return []
    if only_non_verified and metadata.get(CONTEXT_CONFIDENCE_KEY) == EXTERNAL_VERIFIED:
        return []
    reasons.append("missing_website")
    if metadata.get(CONTEXT_CONFIDENCE_KEY) != EXTERNAL_VERIFIED:
        reasons.append("context_not_external_verified")
    if _is_blocked_context_url(org.source_url):
        reasons.append("source_url_not_company_context")
    return reasons


def resolve_company_website(
    org: OrganizationRecord,
    *,
    fetcher: HttpTextDownloader,
    use_web_search: bool,
    allow_domain_guess: bool = False,
    max_search_results: int = 5,
    min_score: int | None = None,
) -> CompanyWebsiteCandidate | None:
    source_url = (org.source_url or "").strip()
    direct_website = _official_website_from_url(source_url)
    if direct_website and not _is_blocked_website_url(direct_website):
        candidate = _validate_company_website(
            org.name,
            direct_website,
            fetcher=fetcher,
            source="source_url_host",
            evidence_url=source_url,
            source_bonus=3,
            min_score=min_score,
        )
        if candidate:
            return candidate

    if source_url and not _is_blocked_context_url(source_url):
        html = _safe_fetch(fetcher, source_url)
        for url in _extract_external_website_links(source_url, html):
            candidate = _validate_company_website(
                org.name,
                url,
                fetcher=fetcher,
                source="source_page_outbound_link",
                evidence_url=source_url,
                source_bonus=2,
                min_score=min_score,
            )
            if candidate:
                return candidate

    if allow_domain_guess:
        for url in _guessed_company_website_urls(org.name):
            candidate = _validate_company_website(
                org.name,
                url,
                fetcher=fetcher,
                source="domain_guess",
                evidence_url=url,
                source_bonus=0,
                min_score=min_score,
            )
            if candidate:
                return candidate

    if not use_web_search:
        return None
    for url in _search_official_website_urls(org.name, fetcher, max_results=max_search_results):
        candidate = _validate_company_website(
            org.name,
            url,
            fetcher=fetcher,
            source="web_search",
            evidence_url=url,
            source_bonus=0,
            min_score=min_score,
        )
        if candidate:
            return candidate
    return None


def apply_company_website_resolution(
    workbook: OutreachWorkbook,
    org: OrganizationRecord,
    candidate: CompanyWebsiteCandidate,
) -> OrganizationRecord | None:
    freeform, metadata = parse_notes_parts(org.notes)
    now = datetime.now(UTC).replace(microsecond=0)
    metadata[WEBSITE_RESOLVED_AT_KEY] = now.isoformat()
    metadata[WEBSITE_RESOLUTION_SOURCE_KEY] = candidate.source
    metadata[WEBSITE_RESOLUTION_CONFIDENCE_KEY] = candidate.confidence
    metadata[WEBSITE_RESOLUTION_EVIDENCE_URL_KEY] = candidate.evidence_url
    return workbook.update_organization(
        org.organization_id,
        website=candidate.website,
        notes=format_notes_parts(freeform, metadata),
        last_updated_at=now.isoformat(),
    )


def build_company_context_patch(
    org: OrganizationRecord,
    opportunities: list[OpportunityRecord],
    *,
    fetcher: HttpTextDownloader,
    use_network: bool,
    use_web_search: bool,
    fallback_to_jobs: bool,
) -> CompanyContextPatch | None:
    prestige_signals: list[str] = []
    prestige_evidence_url = ""
    if use_network:
        patch = _first_external_context_patch(
            org.name,
            _direct_context_urls(org),
            fetcher=fetcher,
            prestige_signals=prestige_signals,
            prestige_evidence_url=prestige_evidence_url,
        )
        if patch:
            return patch
        if use_web_search:
            patch = _first_external_context_patch(
                org.name,
                _search_company_urls(org.name, fetcher),
                fetcher=fetcher,
                prestige_signals=prestige_signals,
                prestige_evidence_url=prestige_evidence_url,
            )
            if patch:
                return patch
    if not fallback_to_jobs:
        return None
    patch = _context_from_opportunities(opportunities)
    if patch:
        patch.prestige_signals = _merge_tags(patch.prestige_signals, prestige_signals)
        patch.prestige_evidence_url = patch.prestige_evidence_url or prestige_evidence_url
    return patch


def _direct_context_urls(org: OrganizationRecord) -> list[str]:
    urls: list[str] = []
    if org.website:
        urls.append(org.website)
    if org.source_url and not _is_blocked_context_url(org.source_url):
        urls.append(org.source_url)
    return list(dict.fromkeys(url for url in urls if url))


def _first_external_context_patch(
    company: str,
    urls: list[str],
    *,
    fetcher: HttpTextDownloader,
    prestige_signals: list[str],
    prestige_evidence_url: str,
) -> CompanyContextPatch | None:
    seen_signals = list(prestige_signals)
    seen_evidence_url = prestige_evidence_url
    for url in urls:
        html = _safe_fetch(fetcher, url)
        if not html:
            continue
        page_signals = infer_prestige_signals(url, html)
        if page_signals:
            seen_signals = _merge_tags(seen_signals, page_signals)
            seen_evidence_url = seen_evidence_url or url
        patch = _context_from_html(company, url, html)
        if patch and (patch.description or patch.tags or patch.website):
            patch.prestige_signals = _merge_tags(patch.prestige_signals, seen_signals)
            patch.prestige_evidence_url = patch.prestige_evidence_url or seen_evidence_url
            return patch
    return None


def apply_company_context_patch(
    workbook: OutreachWorkbook,
    org: OrganizationRecord,
    patch: CompanyContextPatch,
    *,
    refresh_days: int,
) -> OrganizationRecord | None:
    freeform, metadata = parse_notes_parts(org.notes)
    existing_tags = _split_csv(metadata.get("tags", ""))
    merged_tags = _merge_tags(existing_tags, patch.tags)
    if merged_tags:
        metadata["tags"] = ",".join(merged_tags)
    if patch.description and _should_replace_description(metadata, patch):
        metadata["description"] = patch.description
    if patch.team_size and not metadata.get("team_size"):
        metadata["team_size"] = patch.team_size
    if patch.confidence == EXTERNAL_VERIFIED:
        if patch.prestige_signals:
            metadata["prestige_signals"] = ",".join(_merge_tags([], patch.prestige_signals))
        else:
            metadata.pop("prestige_signals", None)
            metadata.pop("prestige_evidence_url", None)
    elif patch.prestige_signals:
        metadata["prestige_signals"] = ",".join(_merge_tags(_split_csv(metadata.get("prestige_signals", "")), patch.prestige_signals))
    if patch.prestige_evidence_url:
        metadata["prestige_evidence_url"] = patch.prestige_evidence_url
    now = datetime.now(UTC).replace(microsecond=0)
    metadata[CONTEXT_DATE_KEY] = now.isoformat()
    metadata[CONTEXT_REFRESH_AFTER_KEY] = (now + timedelta(days=refresh_days)).date().isoformat()
    metadata[CONTEXT_SOURCE_KEY] = patch.source
    metadata[CONTEXT_CONFIDENCE_KEY] = patch.confidence
    if patch.evidence_url:
        metadata[CONTEXT_EVIDENCE_URL_KEY] = patch.evidence_url

    updates = {
        "notes": format_notes_parts(freeform, metadata),
        "last_updated_at": now.isoformat(),
    }
    if patch.website and not org.website:
        updates["website"] = patch.website
    return workbook.update_organization(org.organization_id, **updates)


def _candidate_context_urls(
    org: OrganizationRecord,
    *,
    fetcher: HttpTextDownloader,
    use_web_search: bool,
) -> list[str]:
    urls: list[str] = []
    if org.website:
        urls.append(org.website)
    if org.source_url and not _is_blocked_context_url(org.source_url):
        urls.append(org.source_url)
    if use_web_search:
        urls.extend(_search_company_urls(org.name, fetcher))
    return list(dict.fromkeys(url for url in urls if url))


def _search_company_urls(company: str, fetcher: HttpTextDownloader) -> list[str]:
    query = f'{company} company official website funding investors TechCrunch Crunchbase'
    html = _safe_fetch(fetcher, f"https://duckduckgo.com/html/?q={quote_plus(query)}")
    if not html:
        return []
    segments = extract_html_segments(html)
    candidates: list[str] = []
    normalized_company = _normalize_company(company)
    for segment in segments:
        if segment.get("kind") != "link":
            continue
        href = _unwrap_duckduckgo_url(segment.get("href", ""))
        if not href or _is_blocked_context_url(href):
            continue
        score = _url_company_score(href, normalized_company)
        if score <= 0:
            continue
        candidates.append(href)
        if len(candidates) >= 4:
            break
    return candidates


def _search_official_website_urls(company: str, fetcher: HttpTextDownloader, *, max_results: int = 5) -> list[str]:
    urls: list[str] = []
    query = f'"{company}" official website'
    search_urls = [
        f"https://www.bing.com/search?q={quote_plus(query)}",
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        f"https://duckduckgo.com/html/?q={quote_plus(query)}",
    ]
    for search_url in search_urls:
        html = _safe_fetch(fetcher, search_url)
        if not html:
            continue
        for segment in extract_html_segments(html):
            if segment.get("kind") != "link":
                continue
            href = _unwrap_duckduckgo_url(segment.get("href", ""))
            if not href or _is_blocked_website_url(href):
                continue
            urls.append(href)
            if len(urls) >= max_results:
                return list(dict.fromkeys(urls))
    return list(dict.fromkeys(urls))


def _extract_external_website_links(source_url: str, html: str) -> list[str]:
    if not html:
        return []
    source_host = urlparse(source_url).netloc.lower().removeprefix("www.")
    urls: list[str] = []
    for segment in extract_html_segments(html):
        if segment.get("kind") != "link":
            continue
        href = _unwrap_duckduckgo_url(segment.get("href", ""))
        if not href or not href.startswith(("http://", "https://")):
            continue
        host = urlparse(href).netloc.lower().removeprefix("www.")
        if not host or host == source_host or _is_blocked_website_url(href):
            continue
        link_text = segment.get("text", "").lower()
        if link_text and any(token in link_text for token in ("website", "homepage", "careers", "company")):
            urls.insert(0, href)
        else:
            urls.append(href)
    return list(dict.fromkeys(urls))[:8]


def _guessed_company_website_urls(company: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"(?<!@)\b[a-zA-Z0-9][a-zA-Z0-9-]*\.(?:com|ai|io|co|org|net)\b", company):
        urls.append(f"https://{match.group(0).lower()}")
    tokens = _company_domain_tokens(company)
    if not tokens:
        return list(dict.fromkeys(urls))
    compact = "".join(tokens)
    hyphenated = "-".join(tokens)
    for tld in ("com", "ai", "io", "co"):
        urls.append(f"https://{compact}.{tld}")
    if hyphenated != compact:
        for tld in ("com", "ai", "io"):
            urls.append(f"https://{hyphenated}.{tld}")
    return list(dict.fromkeys(urls))[:6]


def _validate_company_website(
    company: str,
    url: str,
    *,
    fetcher: HttpTextDownloader,
    source: str,
    evidence_url: str,
    source_bonus: int = 0,
    min_score: int | None = None,
) -> CompanyWebsiteCandidate | None:
    website = _homepage_url(url)
    if not website or _is_blocked_website_url(website):
        return None
    html = _safe_fetch(fetcher, website) or _safe_fetch(fetcher, url)
    if not html:
        return None
    text = _website_validation_text(html)
    if _is_low_quality_context_page(website, text):
        return None
    score = _website_candidate_score(company, website, text) + source_bonus
    threshold = max(_website_score_threshold(company), min_score or 0)
    if score < threshold:
        return None
    confidence = "high" if score >= threshold + 3 else "medium"
    return CompanyWebsiteCandidate(
        website=website,
        source=source,
        confidence=confidence,
        evidence_url=evidence_url,
        score=score,
    )


def _website_validation_text(html: str) -> str:
    if not html:
        return ""
    meta = _MetaParser()
    meta.feed(html)
    text_samples = [
        segment.get("text", "")
        for segment in extract_html_segments(html)
        if segment.get("kind") == "text"
    ][:12]
    return " ".join([meta.title, meta.meta_description, *text_samples]).lower()


def _is_low_quality_context_page(url: str, text: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    lowered = " ".join((text or "").lower().split())
    if not lowered:
        return False
    parking_hosts = {
        "afternic.com",
        "bodis.com",
        "dan.com",
        "domainmarket.com",
        "godaddy.com",
        "hugedomains.com",
        "namecheap.com",
        "sedo.com",
        "squadhelp.com",
    }
    if host in parking_hosts or any(host.endswith(f".{blocked}") for blocked in parking_hosts):
        return True
    parking_phrases = (
        "verified premium domain available for purchase",
        "this domain is for sale",
        "domain is for sale",
        "buy this domain",
        "buy now or pay in installments",
        "make an offer for this domain",
        "get this domain",
        "this webpage was generated by the domain owner",
        "first and best source for information about",
        "topics relating to issues of general interest",
        "we hope you find what you are looking for",
        "site is temporarily down",
        "temporarily down and unavailable",
        "requires javascript",
        "enable javascript and reload",
        "doesn't work properly without javascript",
        "does not work properly without javascript",
        "switch to a browser that supports it",
        "user-agent string appears to be from an automated process",
        "parked free",
        "parkingcrew",
        "related searches",
    )
    return any(phrase in lowered for phrase in parking_phrases)


def _website_candidate_score(company: str, website: str, text: str) -> int:
    tokens = _company_signal_tokens(company)
    domain_tokens = _company_domain_tokens(company)
    if _requires_strict_compound_identity(domain_tokens) and not _strict_compound_identity_matched(domain_tokens, website, text):
        return 0
    generic_identity_tokens = _generic_suffix_identity_tokens(company)
    if generic_identity_tokens and not _full_identity_matched(generic_identity_tokens, website, text):
        return 0
    if not tokens:
        return 0
    parsed = urlparse(website)
    host = parsed.netloc.lower().removeprefix("www.")
    host_compact = re.sub(r"[^a-z0-9]+", "", host.split(".")[0])
    company_compact = re.sub(r"[^a-z0-9]+", "", " ".join(tokens))
    text_words = set(re.findall(r"[a-z0-9]+", text.lower()))
    score = 0
    host_compact_matched = bool(company_compact and (company_compact in host_compact or host_compact in company_compact))
    if host_compact_matched:
        score += 8
    host_hits = 0 if host_compact_matched else sum(1 for token in tokens if token in host_compact)
    if host_hits:
        score += min(6, host_hits * 3)
    text_hits = sum(1 for token in tokens if token in text_words)
    if text_hits >= min(2, len(tokens)):
        score += 5
    elif text_hits == 1 and len(tokens) == 1 and len(tokens[0]) >= 5:
        score += 4
    if any(phrase in text for phrase in ("official site", "official website", "careers", "about us")):
        score += 1
    return score


def _website_score_threshold(company: str) -> int:
    tokens = _company_signal_tokens(company)
    if len(tokens) <= 1 and (not tokens or len(tokens[0]) <= 4):
        return 11
    return 8


def _company_domain_tokens(company: str) -> list[str]:
    corporate_suffixes = {
        "inc",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "company",
        "co",
        "group",
        "global",
        "technologies",
        "technology",
        "systems",
        "solutions",
        "labs",
    }
    normalized = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    tokens = [token for token in normalized.split() if token and token not in corporate_suffixes]
    return tokens[:4]


def _requires_strict_compound_identity(tokens: list[str]) -> bool:
    return len(tokens) >= 2 and any(len(token) <= 2 for token in tokens)


def _strict_compound_identity_matched(tokens: list[str], website: str, text: str) -> bool:
    compact = "".join(tokens)
    hyphenated = "-".join(tokens)
    parsed = urlparse(website)
    host_stem = parsed.netloc.lower().removeprefix("www.").split(".")[0]
    host_compact = re.sub(r"[^a-z0-9]+", "", host_stem)
    host_matched = bool(compact and (compact in host_compact or hyphenated in host_stem))
    text_lower = text.lower()
    phrase_pattern = r"\b" + r"[\s\-.]+".join(re.escape(token) for token in tokens) + r"\b"
    text_compact = re.sub(r"[^a-z0-9]+", "", text_lower)
    text_matched = bool(re.search(phrase_pattern, text_lower) or (compact and compact in text_compact))
    return host_matched and text_matched


def _generic_suffix_identity_tokens(company: str) -> list[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    words = normalized.split()
    generic_suffixes = {"solutions"}
    if len(words) >= 2 and words[-1] in generic_suffixes:
        return words[:3]
    return []



def _full_identity_matched(tokens: list[str], website: str, text: str) -> bool:
    compact = "".join(tokens)
    parsed = urlparse(website)
    host_stem = parsed.netloc.lower().removeprefix("www.").split(".")[0]
    host_compact = re.sub(r"[^a-z0-9]+", "", host_stem)
    text_lower = text.lower()
    phrase_pattern = r"\b" + r"[\s\-.]+".join(re.escape(token) for token in tokens) + r"\b"
    text_compact = re.sub(r"[^a-z0-9]+", "", text_lower)
    return bool(
        compact
        and (
            compact in host_compact
            or re.search(phrase_pattern, text_lower)
            or compact in text_compact
        )
    )


def _company_signal_tokens(company: str) -> list[str]:
    stopwords = {
        "ai",
        "inc",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "company",
        "co",
        "group",
        "global",
        "technologies",
        "technology",
        "systems",
        "solutions",
        "labs",
        "formerly",
        "yc",
        "x25",
        "xml",
    }
    normalized = _normalize_company(company)
    tokens = [token for token in normalized.split() if len(token) >= 3 and token not in stopwords]
    return tokens[:4]


def _homepage_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _context_from_html(company: str, url: str, html: str) -> CompanyContextPatch | None:
    meta = _MetaParser()
    meta.feed(html)
    segments = extract_html_segments(html)
    text_samples = [
        segment.get("text", "")
        for segment in segments
        if segment.get("kind") == "text" and _looks_like_context_text(segment.get("text", ""))
    ][:8]
    body_text = _clean_text(" ".join(text_samples), max_length=900)
    description = meta.meta_description or _first_context_sentence(body_text)
    validation_text = " ".join([meta.title, meta.meta_description, body_text])
    if _is_low_quality_context_page(url, validation_text):
        return None
    tags = infer_context_tags(" ".join([company, meta.title, description, body_text]))
    team_size = _extract_team_size(body_text)
    website = _official_website_from_url(url)
    prestige_signals = infer_prestige_signals(url, " ".join([meta.title, description, body_text]))
    if not description and not tags:
        return None
    return CompanyContextPatch(
        source="public_web",
        confidence=EXTERNAL_VERIFIED,
        evidence_url=url,
        description=description,
        tags=tags,
        website=website,
        team_size=team_size,
        prestige_signals=prestige_signals,
        prestige_evidence_url=url if prestige_signals else "",
    )


def _context_from_opportunities(opportunities: list[OpportunityRecord]) -> CompanyContextPatch | None:
    snippets: list[str] = []
    rationales: list[str] = []
    source_url = ""
    for opp in opportunities:
        rationale = _extract_metadata_value(opp.notes, "fit_rationale")
        if rationale:
            snippets.append(rationale)
            rationales.append(rationale)
        snippets.append(opp.title)
        source_url = source_url or opp.source_url
    if not rationales:
        return None
    text = " ".join(snippets)
    tags = infer_context_tags(text)
    description = _clean_text(_strip_priority_prefix(rationales[0]), max_length=360)
    if not tags and not description:
        return None
    return CompanyContextPatch(
        source="resume_job_fit_rationale",
        confidence=INFERRED_FROM_JOB,
        evidence_url=source_url,
        description=description,
        tags=tags,
        prestige_signals=infer_prestige_signals(source_url, text),
        prestige_evidence_url=source_url,
    )


def infer_context_tags(text: str) -> list[str]:
    text_lower = text.lower()
    tags: list[str] = []
    for tag in DOMAIN_TAGS:
        if tag == "hiring" and not any(
            _mentions_term(text_lower, term)
            for term in ("recruiting", "hr tech", "hr-tech", "talent acquisition", "applicant tracking", "workforce")
        ):
            continue
        if _mentions_term(text_lower, tag):
            tags.append(tag)
            continue
        for synonym in TAG_SYNONYMS.get(tag, ()):
            if _mentions_term(text_lower, synonym):
                tags.append(tag)
                break
    return _merge_tags([], tags)[:8]


def infer_prestige_signals(url: str, text: str) -> list[str]:
    text_lower = " ".join([url, text]).lower()
    signals: list[str] = []
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if "techcrunch.com" in host:
        signals.append("techcrunch-covered")
    if "crunchbase.com" in host:
        signals.append("crunchbase-profile")
    if "ycombinator.com" in host or "y combinator" in text_lower or " yc " in f" {text_lower} ":
        signals.append("yc-backed")
    funding_patterns = {
        "series-a": r"\bseries\s+a\b",
        "series-b": r"\bseries\s+b\b",
        "series-c-plus": r"\bseries\s+[cdef]\b",
        "seed-funded": r"\bseed\s+(?:round|funding|funded)\b",
        "venture-backed": (
            r"\b(?:raised|funding|funded|backed by|venture-backed|"
            r"investors include|investors are|led by|investment from)\b"
        ),
    }
    for signal, pattern in funding_patterns.items():
        if re.search(pattern, text_lower):
            signals.append(signal)
    investor_patterns = {
        "sequoia-backed": ("sequoia",),
        "a16z-backed": ("andreessen horowitz", "a16z"),
        "gv-backed": ("google ventures", " gv ", "gv,", "gv.", "gv<"),
        "accel-backed": ("accel",),
        "index-backed": ("index ventures",),
        "khosla-backed": ("khosla",),
        "benchmark-backed": ("benchmark",),
        "founders-fund-backed": ("founders fund", "founder fund"),
    }
    padded = f" {text_lower} "
    for signal, patterns in investor_patterns.items():
        if any(_has_funding_context_near_term(padded, pattern.strip()) for pattern in patterns):
            signals.append(signal)
    return _merge_tags([], signals)[:8]


def _has_funding_context_near_term(text: str, term: str) -> bool:
    normalized = _normalize_tag(term)
    if not normalized:
        return False
    term_pattern = (
        r"(?<![a-z0-9])"
        + r"[\s\-+/&]+".join(re.escape(part) for part in normalized.split("-"))
        + r"(?![a-z0-9])"
    )
    before_relation = (
        r"(?:backed by|funded by|funding from|investment from|capital from|"
        r"investors? (?:include|are|were)|led by|participation from)"
        r"[^.!?]{0,100}"
        + term_pattern
    )
    after_relation = (
        term_pattern
        + r"[^.!?]{0,100}"
        r"(?:led|invested|participated|backed|joined|is an investor|as investor)"
    )
    return bool(
        re.search(before_relation, text, flags=re.IGNORECASE)
        or re.search(after_relation, text, flags=re.IGNORECASE)
    )


def _extract_metadata_value(notes: str, key: str) -> str:
    pattern = re.compile(rf"(?:^|\s\|\s){re.escape(key)}=(.*?)(?=\s\|\s[a-zA-Z_][\w-]*=|$)")
    match = pattern.search(notes or "")
    return match.group(1).strip() if match else ""


def _safe_fetch(fetcher: HttpTextDownloader, url: str) -> str:
    try:
        return fetcher.fetch_text(url)
    except Exception:
        return ""


def _unwrap_duckduckgo_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if "bing.com" in parsed.netloc and parsed.path.startswith("/ck/"):
        query = parse_qs(parsed.query)
        decoded = _decode_bing_redirect(query.get("u", [""])[0])
        if decoded:
            return decoded
    if "duckduckgo.com" in parsed.netloc or parsed.path.startswith("/l/"):
        query = parse_qs(parsed.query)
        if query.get("uddg"):
            return unquote(query["uddg"][0])
    return url


def _decode_bing_redirect(value: str) -> str:
    if not value:
        return ""
    payload = value[2:] if value.startswith("a1") else value
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8", errors="replace")
    except Exception:
        return ""
    return decoded if decoded.startswith(("http://", "https://")) else ""


def _url_company_score(url: str, normalized_company: str) -> int:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host or host in BLOCKED_SEARCH_DOMAINS:
        return 0
    source_hosts = (
        "ycombinator.com",
        "builtin.com",
        "techcrunch.com",
        "crunchbase.com",
        "wellfound.com",
    )
    source_hit = any(host == source or host.endswith(f".{source}") for source in source_hosts)
    if source_hit and _company_in_url(url, normalized_company):
        return 2
    host_words = re.sub(r"[^a-z0-9]+", "", host.split(".")[0])
    company_words = re.sub(r"[^a-z0-9]+", "", normalized_company)
    if company_words and (company_words in host_words or host_words in company_words):
        return 5
    if _company_in_url(url, normalized_company):
        return 1
    return 0


def _company_in_url(url: str, normalized_company: str) -> bool:
    company_words = re.sub(r"[^a-z0-9]+", "", normalized_company)
    if len(company_words) < 3:
        return False
    parsed = urlparse(url)
    url_words = re.sub(r"[^a-z0-9]+", "", f"{parsed.netloc} {parsed.path}".lower())
    return company_words in url_words


def _is_blocked_context_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host in BLOCKED_SEARCH_DOMAINS:
        return True
    return "linkedin.com/jobs" in url or "/jobs/view/" in url


def _is_blocked_website_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host or host in BLOCKED_WEBSITE_DOMAINS:
        return True
    return any(host.endswith(f".{blocked}") for blocked in BLOCKED_WEBSITE_DOMAINS)


def _official_website_from_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    if "builtin" in parsed.netloc.lower() or "ycombinator.com" in parsed.netloc.lower():
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _looks_like_context_text(text: str) -> bool:
    stripped = " ".join((text or "").split()).strip()
    if len(stripped) < 70:
        return False
    lowered = stripped.lower()
    noise = ("cookie", "privacy policy", "terms of use", "sign in", "log in", "subscribe")
    return not any(item in lowered for item in noise)


def _first_context_sentence(text: str) -> str:
    clean = _clean_text(text, max_length=420)
    if not clean:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", clean)
    if parts and len(parts[0]) >= 60:
        return _clean_text(parts[0], max_length=360)
    return clean


def _extract_team_size(text: str) -> str:
    patterns = [
        r"(?P<count>\d[\d,]*)\s+(?:employees|people|team members)",
        r"team(?:\s+of)?\s+(?P<count>\d[\d,]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return f"{match.group('count')} employees"
    return ""


def _should_replace_description(metadata: dict[str, str], patch: CompanyContextPatch) -> bool:
    existing = metadata.get("description", "").strip()
    if not existing:
        return True
    existing_confidence = metadata.get(CONTEXT_CONFIDENCE_KEY, "")
    if existing_confidence == INFERRED_FROM_JOB and patch.confidence == EXTERNAL_VERIFIED:
        return True
    return False


def _merge_tags(existing: list[str], new: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for tag in [*existing, *new]:
        normalized = _normalize_tag(tag)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;]", value or "") if item.strip()]


def _mentions_term(text: str, term: str) -> bool:
    normalized = _normalize_tag(term)
    if not normalized:
        return False
    parts = [re.escape(part) for part in normalized.split("-") if part]
    pattern = r"(?<![a-z0-9])" + r"[\s\-+/&]+".join(parts) + r"(?![a-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _normalize_tag(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _normalize_company(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _clean_note_value(value: str) -> str:
    return _clean_text(value, max_length=900).replace("|", "/")


def _clean_text(value: str, *, max_length: int) -> str:
    clean = " ".join((value or "").split()).strip()
    if len(clean) <= max_length:
        return clean
    return clean[: max_length - 1].rstrip() + "…"


def _strip_priority_prefix(value: str) -> str:
    return re.sub(r"^\[[^\]]+\]\s*", "", value or "").strip()


def _parse_iso_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

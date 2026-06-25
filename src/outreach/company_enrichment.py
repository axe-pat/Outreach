from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from outreach.account_tracker import DOMAIN_TAGS
from outreach.discovery.http import HttpTextDownloader, extract_html_segments
from outreach.tracking import OpportunityRecord, OrganizationRecord, OutreachWorkbook, SourceKind


CONTEXT_DATE_KEY = "context_enriched_at"
CONTEXT_SOURCE_KEY = "context_source"
CONTEXT_CONFIDENCE_KEY = "context_confidence"
CONTEXT_REFRESH_AFTER_KEY = "context_refresh_after"
CONTEXT_EVIDENCE_URL_KEY = "context_evidence_url"
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
    for candidate in candidates:
        org = candidate.organization
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
    if "duckduckgo.com" in parsed.netloc:
        query = parse_qs(parsed.query)
        if query.get("uddg"):
            return unquote(query["uddg"][0])
    return url


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

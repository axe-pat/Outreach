from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import urljoin

from outreach.discovery.http import extract_html_segments
from outreach.discovery.models import (
    DiscoveredContact,
    DiscoveredOpportunity,
    DiscoveredOrganization,
    DiscoverySourceDefinition,
)
from outreach.tracking import OpportunityType


class SourceAdapter(Protocol):
    def discover(
        self,
        source: DiscoverySourceDefinition,
        fetch_text: Callable[[str], str],
        limit: int = 25,
        enrich_details: bool = False,
    ) -> list[DiscoveredOrganization]:
        ...


@dataclass
class _ParsedCompanyCard:
    name: str
    company_url: str
    batch: str = ""
    status: str = ""
    team_size: str = ""
    location: str = ""
    description: str = ""
    tags: list[str] | None = None
    jobs_url: str = ""


@dataclass
class _ParsedBuiltInCard:
    name: str
    company_url: str
    categories: str = ""
    location_or_offices: str = ""
    employee_count: str = ""
    benefits_url: str = ""
    jobs_url: str = ""
    short_blurb: str = ""
    description: str = ""


class YCombinatorCompanyDirectoryAdapter:
    LOCATION_TAGS = {
        "san francisco",
        "los angeles",
        "new york",
        "remote",
        "london",
        "paris",
        "berlin",
        "singapore",
        "toronto",
        "austin",
        "el segundo",
    }
    METADATA_PATTERN = re.compile(
        r"^(?P<name>.+?) Y Combinator Logo "
        r"(?P<batch>[^•]+)"
        r"(?: • (?P<status>[^•]+))?"
        r"(?: • (?P<team_size>[^•]+? employees))?"
        r"(?: • (?P<location>.+))?$"
    )
    FOUNDED_PATTERN = re.compile(r"^Founded:(?P<year>\d{4})$")
    BATCH_PATTERN = re.compile(r"^Batch:(?P<batch>.+)$")
    TEAM_SIZE_PATTERN = re.compile(r"^Team Size:(?P<size>.+)$")
    LOCATION_PATTERN = re.compile(r"^Location:(?P<location>.+)$")

    def discover(
        self,
        source: DiscoverySourceDefinition,
        fetch_text: Callable[[str], str],
        limit: int = 25,
        enrich_details: bool = False,
    ) -> list[DiscoveredOrganization]:
        items: list[DiscoveredOrganization] = []
        seen_names: set[str] = set()
        for url in source.seed_urls:
            html = fetch_text(url)
            cards = self._parse_listing_page(html=html, page_url=url)
            for card in cards:
                normalized_name = card.name.strip().lower()
                if not normalized_name or normalized_name in seen_names:
                    continue
                seen_names.add(normalized_name)
                city = self._first_city(card.location)
                description = card.description.strip()
                item = DiscoveredOrganization(
                    organization_name=card.name.strip(),
                    organization_type=source.organization_type,
                    target_lists=source.target_lists,
                    city=city,
                    company_url=card.company_url,
                    jobs_url=card.jobs_url,
                    description=description,
                    status="Researching" if card.jobs_url else "Discovered",
                    source_kind=source.source_kind,
                    source_page_url=url,
                    source_item_url=card.company_url,
                    tags=card.tags or [],
                    batch=card.batch.strip(),
                    team_size=card.team_size.strip(),
                    location=card.location.strip(),
                    jobs_count=1 if card.jobs_url else 0,
                    opportunity_title="Open roles via YC" if card.jobs_url else "",
                    opportunity_type=source.opportunity_type if card.jobs_url else None,
                )
                if enrich_details:
                    detail_html = fetch_text(card.company_url)
                    item = self.enrich_company(item, detail_html)
                    if item.jobs_url:
                        jobs_html = fetch_text(urljoin(card.company_url, item.jobs_url))
                        item = self.enrich_company(item, detail_html, jobs_html=jobs_html)
                items.append(item)
                if len(items) >= limit:
                    return items
        return items

    def enrich_company(
        self,
        item: DiscoveredOrganization,
        html: str,
        jobs_html: str = "",
    ) -> DiscoveredOrganization:
        segments = extract_html_segments(html)
        website = self._extract_website(segments)
        founded_year, batch, team_size, location = self._extract_company_facts(segments, item)
        jobs_count = self._extract_jobs_count(segments, item.jobs_count)
        contacts = self._extract_founders(segments)
        opportunities = self._extract_job_rows(segments)
        jobs_url = self._extract_jobs_page_url(segments) or item.jobs_url
        if jobs_html:
            opportunities = self._merge_opportunities(
                opportunities,
                self._extract_job_rows(extract_html_segments(jobs_html)),
            )
            jobs_count = max(jobs_count, len(opportunities))
        city = item.city or self._first_city(location)
        primary_opportunity_title = item.opportunity_title
        primary_opportunity_type = item.opportunity_type
        if opportunities:
            primary_opportunity_title = opportunities[0].title
            primary_opportunity_type = opportunities[0].opportunity_type
        return item.model_copy(
            update={
                "website": website or item.website,
                "founded_year": founded_year or item.founded_year,
                "batch": batch or item.batch,
                "team_size": team_size or item.team_size,
                "location": location or item.location,
                "city": city,
                "jobs_count": jobs_count,
                "contacts": contacts or item.contacts,
                "opportunities": opportunities,
                "opportunity_title": primary_opportunity_title,
                "opportunity_type": primary_opportunity_type,
                "jobs_url": jobs_url,
                "status": "Researching" if jobs_count > 0 else item.status,
            }
        )

    def _parse_listing_page(self, html: str, page_url: str) -> list[_ParsedCompanyCard]:
        segments = extract_html_segments(html)
        cards: list[_ParsedCompanyCard] = []
        index = 0
        while index < len(segments):
            segment = segments[index]
            if segment.get("kind") != "link":
                index += 1
                continue

            link_text = segment.get("text", "")
            if link_text.startswith("Image:") or "Y Combinator Logo" not in link_text:
                index += 1
                continue

            match = self.METADATA_PATTERN.match(link_text)
            if not match:
                index += 1
                continue

            card = _ParsedCompanyCard(
                name=match.group("name").strip(),
                company_url=urljoin(page_url, segment.get("href", "")),
                batch=(match.group("batch") or "").strip(),
                status=(match.group("status") or "").strip(),
                team_size=(match.group("team_size") or "").strip(),
                location=(match.group("location") or "").strip(),
                tags=[],
            )

            index += 1
            description_parts: list[str] = []
            tags: list[str] = []
            while index < len(segments):
                next_segment = segments[index]
                next_text = next_segment.get("text", "")
                if (
                    next_segment.get("kind") == "link"
                    and "Y Combinator Logo" in next_text
                    and not next_text.startswith("Image:")
                ):
                    break
                if next_segment.get("kind") == "link" and next_text == "View jobs →":
                    card.jobs_url = urljoin(page_url, next_segment.get("href", ""))
                    index += 1
                    continue
                if next_segment.get("kind") == "text":
                    if self._looks_like_tag(next_text):
                        tags.append(next_text)
                    elif not self._is_noise(next_text):
                        description_parts.append(next_text)
                index += 1

            card.description = " ".join(description_parts).strip()
            card.tags = self._dedupe(tags)
            cards.append(card)

        return cards

    def _looks_like_tag(self, value: str) -> bool:
        stripped = value.strip()
        lowered = stripped.lower()
        if not lowered or len(lowered) > 32:
            return False
        if any(char.isdigit() for char in lowered):
            return False
        if any(char in lowered for char in {".", ",", "!", "?", ":"}):
            return False
        if stripped != lowered:
            return False
        if lowered in self.LOCATION_TAGS:
            return False
        disallowed = {"apply", "view jobs →", "jobs", "company", "founders", "active"}
        return lowered not in disallowed

    def _is_noise(self, value: str) -> bool:
        lowered = value.lower()
        return lowered in {
            "company",
            "jobs",
            "view all jobs",
            "footer",
            "open menu",
            "log in",
            "apply",
        }

    def _extract_website(self, segments: list[dict[str, str]]) -> str:
        company_index = self._find_text_index(segments, "Company")
        start = company_index if company_index >= 0 else 0
        for segment in segments[start : start + 12]:
            if segment.get("kind") != "link":
                continue
            href = segment.get("href", "")
            if not href.startswith("http"):
                continue
            if any(
                blocked in href
                for blocked in {
                    "ycombinator.com",
                    "linkedin.com",
                    "account.ycombinator.com",
                    "startupschool.org",
                    "youtube.com",
                }
            ):
                continue
            return href.strip()
        return ""

    def _extract_company_facts(
        self,
        segments: list[dict[str, str]],
        item: DiscoveredOrganization,
    ) -> tuple[str, str, str, str]:
        founded_year = item.founded_year
        batch = item.batch
        team_size = item.team_size
        location = item.location
        for segment in segments:
            text = segment.get("text", "").strip()
            if not text:
                continue
            if match := self.FOUNDED_PATTERN.match(text):
                founded_year = match.group("year").strip()
            elif match := self.BATCH_PATTERN.match(text):
                batch = match.group("batch").strip()
            elif match := self.TEAM_SIZE_PATTERN.match(text):
                team_size = match.group("size").strip()
            elif match := self.LOCATION_PATTERN.match(text):
                location = match.group("location").strip()
        for index, segment in enumerate(segments):
            text = segment.get("text", "").strip()
            if text == "Founded:" and index + 1 < len(segments):
                founded_year = segments[index + 1].get("text", "").strip() or founded_year
            elif text == "Batch:" and index + 1 < len(segments):
                batch = segments[index + 1].get("text", "").strip() or batch
            elif text == "Team Size:" and index + 1 < len(segments):
                team_size = segments[index + 1].get("text", "").strip() or team_size
            elif text == "Location:" and index + 1 < len(segments):
                location = segments[index + 1].get("text", "").strip() or location
        return founded_year, batch, team_size, location

    def _extract_jobs_count(self, segments: list[dict[str, str]], fallback: int) -> int:
        for index, segment in enumerate(segments):
            if segment.get("kind") != "link" or segment.get("text") != "Jobs":
                continue
            if index + 1 >= len(segments):
                continue
            next_text = segments[index + 1].get("text", "").strip()
            if next_text.isdigit():
                return int(next_text)
        return fallback

    def _extract_jobs_page_url(self, segments: list[dict[str, str]]) -> str:
        for segment in segments:
            if segment.get("kind") != "link":
                continue
            text = segment.get("text", "").strip()
            href = segment.get("href", "").strip()
            if text not in {"Jobs", "View all jobs"}:
                continue
            if "/companies/" in href and href.endswith("/jobs"):
                return href
        return ""

    def _extract_founders(self, segments: list[dict[str, str]]) -> list[DiscoveredContact]:
        start = self._find_text_index(segments, "Active Founders")
        if start < 0:
            return []
        end = self._find_next_section_index(
            segments,
            start + 1,
            {"Jobs at", "Company Launches", "Footer", "Founded:"},
        )
        contacts: list[DiscoveredContact] = []
        seen: set[tuple[str, str]] = set()
        index = start + 1
        while index < end:
            text = segments[index].get("text", "").strip()
            if not self._looks_like_person_name(text):
                index += 1
                continue

            name = text
            linkedin_url = ""
            title = ""
            bio_parts: list[str] = []
            inner = index + 1
            while inner < end:
                segment = segments[inner]
                next_text = segment.get("text", "").strip()
                if self._looks_like_person_name(next_text):
                    break
                if segment.get("kind") == "link":
                    href = segment.get("href", "")
                    if "linkedin.com" in href:
                        linkedin_url = href
                elif next_text and next_text != "Founder":
                    if not title and self._looks_like_role(next_text):
                        title = next_text
                    elif not self._is_noise(next_text):
                        bio_parts.append(next_text)
                else:
                    if next_text == "Founder":
                        title = "Founder"
                inner += 1

            key = (name.lower(), linkedin_url.lower())
            if key not in seen:
                seen.add(key)
                contacts.append(
                    DiscoveredContact(
                        full_name=name,
                        title=title or "Founder",
                        linkedin_url=linkedin_url,
                        bio=" ".join(bio_parts).strip(),
                        contact_type="founder",
                    )
                )
            index = inner
        return contacts

    def _extract_job_rows(self, segments: list[dict[str, str]]) -> list[DiscoveredOpportunity]:
        start = self._find_jobs_section_index(segments)
        if start < 0:
            return []
        end = self._find_next_section_index(
            segments,
            start + 1,
            {"Founded:", "Footer", "Company Launches"},
        )
        opportunities: list[DiscoveredOpportunity] = []
        index = start + 1
        while index < end:
            segment = segments[index]
            text = segment.get("text", "").strip()
            if segment.get("kind") == "link" and text and text not in {"View all jobs", "Apply Now ›"}:
                href = segment.get("href", "")
                if "account.ycombinator.com" in href:
                    index += 1
                    continue
                title = text
                location = ""
                compensation = ""
                equity = ""
                experience = ""
                apply_url = ""
                inner = index + 1
                while inner < end:
                    next_segment = segments[inner]
                    next_text = next_segment.get("text", "").strip()
                    if next_segment.get("kind") == "link":
                        if next_text in {"View all jobs"}:
                            inner += 1
                            continue
                        if next_text == "Apply Now ›":
                            apply_url = next_segment.get("href", "")
                            inner += 1
                            break
                        if next_text and next_text not in {"Jobs"}:
                            break
                    elif next_text:
                        if not location and self._looks_like_job_location(next_text):
                            location = next_text
                        elif "$" in next_text:
                            compensation = next_text
                        elif "%" in next_text:
                            equity = next_text
                        elif "year" in next_text.lower() or "new grads" in next_text.lower() or "intern" in next_text.lower():
                            experience = next_text
                    inner += 1

                if apply_url or location or compensation or experience:
                    opportunities.append(
                        DiscoveredOpportunity(
                            title=title,
                            location=location,
                            compensation_hint=compensation,
                            equity_hint=equity,
                            experience_hint=experience,
                            apply_url=apply_url,
                            opportunity_type=self._infer_opportunity_type(title),
                        )
                    )
                index = inner
                continue
            index += 1
        return opportunities

    def _merge_opportunities(
        self,
        primary: list[DiscoveredOpportunity],
        secondary: list[DiscoveredOpportunity],
    ) -> list[DiscoveredOpportunity]:
        merged: list[DiscoveredOpportunity] = []
        seen: set[tuple[str, str]] = set()
        for item in [*primary, *secondary]:
            key = (item.title.strip().lower(), item.apply_url.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _find_text_index(self, segments: list[dict[str, str]], target: str) -> int:
        for index, segment in enumerate(segments):
            if segment.get("text", "").strip() == target:
                return index
        return -1

    def _find_prefix_index(self, segments: list[dict[str, str]], prefix: str) -> int:
        for index, segment in enumerate(segments):
            if segment.get("text", "").strip().startswith(prefix):
                return index
        return -1

    def _find_jobs_section_index(self, segments: list[dict[str, str]]) -> int:
        exact = self._find_text_index(segments, "Jobs at")
        if exact >= 0:
            return exact
        return self._find_prefix_index(segments, "Jobs at ")

    def _find_next_section_index(
        self,
        segments: list[dict[str, str]],
        start: int,
        section_prefixes: set[str],
    ) -> int:
        for index in range(start, len(segments)):
            text = segments[index].get("text", "").strip()
            if any(text.startswith(prefix) for prefix in section_prefixes):
                return index
        return len(segments)

    def _looks_like_person_name(self, value: str) -> bool:
        if not value or len(value) > 60:
            return False
        if ":" in value or value.lower().startswith("jobs at "):
            return False
        if any(char.isdigit() for char in value):
            return False
        parts = [part for part in value.split() if part]
        if len(parts) < 2 or len(parts) > 4:
            return False
        return all(part[:1].isupper() for part in parts)

    def _looks_like_role(self, value: str) -> bool:
        lowered = value.lower()
        return any(keyword in lowered for keyword in {"founder", "ceo", "cto", "co-founder"})

    def _looks_like_job_location(self, value: str) -> bool:
        lowered = value.lower()
        return "," in value or "remote" in lowered or lowered.endswith(" us")

    def _infer_opportunity_type(self, title: str) -> OpportunityType:
        lowered = title.lower()
        if "intern" in lowered or "internship" in lowered:
            return OpportunityType.INTERNSHIP
        return OpportunityType.FULL_TIME

    def _first_city(self, location: str) -> str:
        if not location:
            return ""
        return location.split(",", maxsplit=1)[0].strip()

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped


class BuiltInCompaniesAdapter:
    LISTING_START_PATTERN = re.compile(r"^Top Tech Companies in .+")
    COMPANY_COUNT_PATTERN = re.compile(r"^\(\d[\d,]*\)$")
    YEAR_FOUNDED_PATTERN = re.compile(r"^Year Founded:\s*(?P<year>\d{4})$")
    EMPLOYEE_PATTERN = re.compile(r"^(?P<count>[\d,]+)\s+Employees?$")
    OFFICES_PATTERN = re.compile(r"^(?P<count>[\d,]+)\s+Offices?$")

    def discover(
        self,
        source: DiscoverySourceDefinition,
        fetch_text: Callable[[str], str],
        limit: int = 25,
        enrich_details: bool = False,
    ) -> list[DiscoveredOrganization]:
        items: list[DiscoveredOrganization] = []
        seen_names: set[str] = set()
        for url in source.seed_urls:
            html = fetch_text(url)
            cards = self._parse_listing_page(html=html, page_url=url)
            for card in cards:
                normalized_name = card.name.strip().lower()
                if not normalized_name or normalized_name in seen_names:
                    continue
                seen_names.add(normalized_name)
                item = DiscoveredOrganization(
                    organization_name=card.name.strip(),
                    organization_type=source.organization_type,
                    target_lists=source.target_lists,
                    city=self._infer_city(card.location_or_offices, source),
                    company_url=card.company_url,
                    jobs_url=card.jobs_url,
                    description=card.description or card.short_blurb,
                    status="Researching" if card.jobs_url else "Discovered",
                    source_kind=source.source_kind,
                    source_page_url=url,
                    source_item_url=card.company_url,
                    tags=self._split_categories(card.categories),
                    team_size=card.employee_count,
                    location=card.location_or_offices,
                    jobs_count=1 if card.jobs_url else 0,
                    opportunity_title="Built In open roles" if card.jobs_url else "",
                    opportunity_type=source.opportunity_type if card.jobs_url else None,
                )
                if enrich_details:
                    detail_html = fetch_text(card.company_url)
                    item = self.enrich_company(item, detail_html)
                items.append(item)
                if len(items) >= limit:
                    return items
        return items

    def enrich_company(self, item: DiscoveredOrganization, html: str) -> DiscoveredOrganization:
        segments = extract_html_segments(html)
        website = self._extract_website(segments)
        city = self._extract_hq_city(segments) or item.city
        team_size = self._extract_total_employees(segments) or item.team_size
        founded_year = self._extract_founded_year(segments) or item.founded_year
        opportunities = self._extract_recent_jobs(segments, item.source_item_url or item.company_url)
        jobs_url = item.jobs_url or self._extract_view_all_jobs(segments, item.source_item_url or item.company_url)
        primary_title = item.opportunity_title
        primary_type = item.opportunity_type
        if opportunities:
            primary_title = opportunities[0].title
            primary_type = opportunities[0].opportunity_type
        return item.model_copy(
            update={
                "website": website or item.website,
                "city": city,
                "team_size": team_size,
                "founded_year": founded_year,
                "jobs_url": jobs_url,
                "jobs_count": len(opportunities) if opportunities else item.jobs_count,
                "opportunities": opportunities,
                "opportunity_title": primary_title,
                "opportunity_type": primary_type,
                "status": "Researching" if (jobs_url or opportunities) else item.status,
            }
        )

    def _parse_listing_page(self, html: str, page_url: str) -> list[_ParsedBuiltInCard]:
        segments = extract_html_segments(html)
        start = self._find_listing_start(segments)
        if start < 0:
            return []
        cards: list[_ParsedBuiltInCard] = []
        index = start
        while index < len(segments):
            segment = segments[index]
            text = segment.get("text", "")
            href = segment.get("href", "")
            if segment.get("kind") == "link" and text and href.startswith("/company/"):
                if text in {"Hiring Now", "See Our Teams", "View Website", "View all jobs"}:
                    index += 1
                    continue
                if text.endswith("Benefits"):
                    index += 1
                    continue
                card = _ParsedBuiltInCard(name=text.strip(), company_url=urljoin(page_url, href))
                index += 1
                while index < len(segments):
                    next_segment = segments[index]
                    next_text = next_segment.get("text", "").strip()
                    next_href = next_segment.get("href", "")
                    if next_segment.get("kind") == "link" and next_text and next_href.startswith("/company/") and next_text not in {
                        "Hiring Now",
                        "See Our Teams",
                        "View Website",
                        "View all jobs",
                    } and not next_text.endswith("Benefits"):
                        break
                    if next_segment.get("kind") == "link":
                        if next_text == "Hiring Now":
                            card.jobs_url = urljoin(page_url, next_href)
                        elif next_text.endswith("Benefits"):
                            card.benefits_url = urljoin(page_url, next_href)
                    elif next_text:
                        if self._is_builtin_noise(next_text):
                            index += 1
                            continue
                        if not card.categories and "•" in next_text:
                            card.categories = next_text
                        elif not card.location_or_offices and (
                            next_text == "Fully Remote"
                            or self.OFFICES_PATTERN.match(next_text)
                            or "," in next_text
                            or self._looks_like_city_label(next_text)
                        ):
                            card.location_or_offices = next_text
                        elif not card.employee_count and self.EMPLOYEE_PATTERN.match(next_text):
                            card.employee_count = next_text
                        elif not card.short_blurb and len(next_text) < 180:
                            card.short_blurb = next_text
                        elif not card.description and len(next_text) > 80:
                            card.description = next_text
                    index += 1
                cards.append(card)
                continue
            index += 1
        return cards

    def _find_listing_start(self, segments: list[dict[str, str]]) -> int:
        for index, segment in enumerate(segments):
            text = segment.get("text", "").strip()
            if self.LISTING_START_PATTERN.match(text):
                return index
        return -1

    def _extract_website(self, segments: list[dict[str, str]]) -> str:
        for index, segment in enumerate(segments):
            if segment.get("kind") == "link" and segment.get("text", "").strip() == "View Website":
                return segment.get("href", "").strip()
            if index > 120:
                break
        return ""

    def _extract_hq_city(self, segments: list[dict[str, str]]) -> str:
        for index, segment in enumerate(segments):
            text = segment.get("text", "").strip()
            if text == "HQ" and index + 1 < len(segments):
                return segments[index + 1].get("text", "").strip()
        return ""

    def _extract_total_employees(self, segments: list[dict[str, str]]) -> str:
        for segment in segments:
            text = segment.get("text", "").strip()
            if "Total Employees" in text or self.EMPLOYEE_PATTERN.match(text):
                return text.replace("Total Employees", "Employees").strip()
        return ""

    def _extract_founded_year(self, segments: list[dict[str, str]]) -> str:
        for segment in segments:
            text = segment.get("text", "").strip()
            if match := self.YEAR_FOUNDED_PATTERN.match(text):
                return match.group("year")
        return ""

    def _extract_view_all_jobs(self, segments: list[dict[str, str]], page_url: str) -> str:
        for segment in segments:
            if segment.get("kind") == "link" and segment.get("text", "").strip() == "View all jobs":
                return urljoin(page_url, segment.get("href", "").strip())
        return ""

    def _extract_recent_jobs(self, segments: list[dict[str, str]], page_url: str) -> list[DiscoveredOpportunity]:
        start = self._find_text_index(segments, "Recently Posted Jobs")
        if start < 0:
            start = self._find_text_index_prefix(segments, "Recently Posted Jobs at ")
        if start < 0:
            return []
        end = self._find_recent_jobs_end(segments, start + 1)
        if end < 0:
            end = len(segments)
        opportunities: list[DiscoveredOpportunity] = []
        index = start + 1
        while index < end:
            segment = segments[index]
            text = segment.get("text", "").strip()
            href = segment.get("href", "")
            if segment.get("kind") == "link" and text and href.startswith("/job/"):
                title = text
                mode = ""
                location = ""
                inner = index + 1
                while inner < end:
                    next_segment = segments[inner]
                    next_text = next_segment.get("text", "").strip()
                    next_href = next_segment.get("href", "")
                    if next_segment.get("kind") == "link" and next_href.startswith("/job/"):
                        break
                    if next_text:
                        if next_text in {"Hybrid", "Remote"}:
                            mode = next_text
                        elif "," in next_text or "Locations" in next_text:
                            location = next_text
                    inner += 1
                opportunities.append(
                    DiscoveredOpportunity(
                        title=title,
                        location=f"{mode} | {location}".strip(" |"),
                        apply_url=urljoin(page_url, href),
                        opportunity_type=self._infer_opportunity_type(title),
                    )
                )
                index = inner
                continue
            index += 1
        return opportunities

    def _find_text_index(self, segments: list[dict[str, str]], target: str) -> int:
        for index, segment in enumerate(segments):
            if segment.get("text", "").strip() == target:
                return index
        return -1

    def _find_text_index_prefix(self, segments: list[dict[str, str]], prefix: str) -> int:
        for index, segment in enumerate(segments):
            if segment.get("text", "").strip().startswith(prefix):
                return index
        return -1

    def _find_text_index_prefix_after(self, segments: list[dict[str, str]], prefix: str, start: int) -> int:
        for index in range(start, len(segments)):
            if segments[index].get("text", "").strip().startswith(prefix):
                return index
        return -1

    def _find_recent_jobs_end(self, segments: list[dict[str, str]], start: int) -> int:
        for index in range(start, len(segments)):
            text = segments[index].get("text", "").strip()
            if text.endswith(" Offices") or text in {"Offices", "Perks + Benefits", "Articles", "FAQs"}:
                return index
        return -1

    def _split_categories(self, categories: str) -> list[str]:
        return [part.strip().lower().replace(" + ", "+") for part in categories.split("•") if part.strip()]

    def _infer_city(self, location_or_offices: str, source: DiscoverySourceDefinition) -> str:
        text = location_or_offices.strip()
        if "," in text:
            return text.split(",", maxsplit=1)[0].strip()
        if "los angeles" in source.target_lists:
            return "Los Angeles"
        if ";la" in source.target_lists or source.label.lower().startswith("built in la"):
            return "Los Angeles"
        if ";sf" in source.target_lists or "san francisco" in source.label.lower():
            return "San Francisco"
        return ""

    def _infer_opportunity_type(self, title: str) -> OpportunityType:
        lowered = title.lower()
        if "intern" in lowered or "internship" in lowered:
            return OpportunityType.INTERNSHIP
        return OpportunityType.FULL_TIME

    def _is_builtin_noise(self, text: str) -> bool:
        return text in {"Save", "Saved", "CREATE JOB ALERT", "ADD COMPANY PROFILE", "•"}

    def _looks_like_city_label(self, text: str) -> bool:
        if len(text) > 40 or any(char.isdigit() for char in text):
            return False
        return text in {
            "Los Angeles",
            "San Francisco",
            "Santa Monica",
            "El Segundo",
            "New York",
            "Chicago",
            "Austin",
        }

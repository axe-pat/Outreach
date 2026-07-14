from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import sync_playwright

from outreach.config import OutreachSettings
from outreach.services.linkedin import LinkedInScraper


DEFAULT_FEED_SIGNALS_PATH = Path("workspace/linkedin_feed_signals.csv")
DEFAULT_PROFILE_VIEWERS_PATH = Path("workspace/linkedin_profile_viewers.csv")
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"
LINKEDIN_PROFILE_VIEWERS_URL = "https://www.linkedin.com/analytics/profile-views/"


class FeedSignalKind(StrEnum):
    COMPANY_DISCOVERY = "company_discovery"
    STARTUP_DISCOVERY = "startup_discovery"
    JOB = "job"
    HIRING = "hiring"
    FUNDING = "funding"
    LAUNCH = "launch"
    WARM_NETWORK = "warm_network"
    RELEVANT_UPDATE = "relevant_update"
    OTHER = "other"


class FeedReviewDisposition(StrEnum):
    PENDING = "pending"
    COMPANY_CANDIDATE = "company_candidate"
    OPPORTUNITY = "opportunity"
    ACCOUNT_SIGNAL = "account_signal"
    CONTACT_RESEARCH = "contact_research"
    KEEP = "keep"
    DISMISSED = "dismissed"


class ViewerRelevance(StrEnum):
    UNREVIEWED = "unreviewed"
    TARGET_COMPANY = "target_company"
    ROLE_RELEVANT = "role_relevant"
    KNOWN_CONTEXT = "known_context"
    LOW = "low"


@dataclass(frozen=True)
class CaptureLimits:
    """Bounds for a browser capture; callers choose the daily operating budget."""

    max_scrolls: int = 5
    max_duration_seconds: float | None = 90.0
    scroll_pause_ms: int = 900
    max_items: int = 100
    stop_after_stable_scrolls: int = 2
    navigation_timeout_ms: int = 30_000
    initial_wait_ms: int = 0

    def __post_init__(self) -> None:
        if self.max_scrolls < 0:
            raise ValueError("max_scrolls must be >= 0")
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive or None")
        if self.scroll_pause_ms < 0:
            raise ValueError("scroll_pause_ms must be >= 0")
        if self.max_items <= 0:
            raise ValueError("max_items must be > 0")
        if self.initial_wait_ms < 0:
            raise ValueError("initial_wait_ms must be >= 0")
        if self.stop_after_stable_scrolls <= 0:
            raise ValueError("stop_after_stable_scrolls must be > 0")


@dataclass(frozen=True)
class FeedPost:
    post_url: str
    author_name: str
    author_url: str
    company: str
    company_url: str
    text: str
    context: str = ""
    posted_at_text: str = ""


@dataclass(frozen=True)
class FeedClassification:
    signal_kinds: tuple[FeedSignalKind, ...]
    relevance: str
    reason: str


@dataclass(frozen=True)
class ProfileViewerObservation:
    profile_url: str
    name: str
    headline: str = ""
    company: str = ""
    context: str = ""


class BrowserPage(Protocol):
    def goto(self, url: str, **kwargs: Any) -> Any: ...

    def evaluate(self, expression: str, arg: Any = None) -> Any: ...

    def wait_for_timeout(self, timeout: float) -> None: ...

    def on(self, event: str, handler: Callable[[Any], None]) -> None: ...

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None: ...


class _FeedPostUrlResolver:
    """Resolve new-feed component keys against bounded LinkedIn response state."""

    _COMPONENT_KEY = re.compile(
        r"^expanded(?P<semantic>.+?)FeedType_MAIN_FEED_RELEVANCE$",
        flags=re.IGNORECASE,
    )
    _STATE_MAPPING = re.compile(
        r"TranslationState-null(?P<semantic>[A-Za-z0-9_-]+)"
        r"(?:(?!TranslationState-null|reactionState-).){0,800}?"
        r"reactionState-(?P<urn>urn:li:(?:activity|ugcPost|share):\d+)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _PAGINATION_PATH = "/flagship-web/rsc-action/actions/pagination"
    _MAX_RESPONSE_CHARS = 8_000_000
    # Main feed plus the five configured scroll pages, while keeping each body bounded.
    _MAX_TOTAL_CHARS = 48_000_000

    def __init__(self) -> None:
        self._component_urls: dict[str, str] = {}
        self._stored_chars = 0
        self._page: Any = None

    def attach(self, page: BrowserPage) -> None:
        on = getattr(page, "on", None)
        if not callable(on):
            return
        self._page = page
        on("response", self._handle_response)

    def detach(self) -> None:
        if self._page is None:
            return
        remove_listener = getattr(self._page, "remove_listener", None)
        if callable(remove_listener):
            try:
                remove_listener("response", self._handle_response)
            except Exception:
                pass
        self._page = None

    def _handle_response(self, response: Any) -> None:
        try:
            response_url = clean_text(response.url)
            parsed = urlparse(response_url)
            is_main_feed = response_url == LINKEDIN_FEED_URL
            is_pagination = (
                parsed.scheme == "https"
                and parsed.netloc.lower() == "www.linkedin.com"
                and parsed.path == self._PAGINATION_PATH
            )
            if not is_main_feed and not is_pagination:
                return
            body = response.text()
        except Exception:
            return
        self.add_response_body(body)

    def add_response_body(self, body: Any) -> None:
        if self._stored_chars >= self._MAX_TOTAL_CHARS:
            return
        normalized = str(body or "").replace(r"\u003A", ":").replace("%3A", ":")
        remaining = self._MAX_TOTAL_CHARS - self._stored_chars
        bounded = normalized[: min(self._MAX_RESPONSE_CHARS, remaining)]
        if not bounded:
            return
        self._stored_chars += len(bounded)
        for match in self._STATE_MAPPING.finditer(bounded):
            semantic = match.group("semantic")
            urn = match.group("urn")
            self._component_urls[semantic] = (
                f"https://www.linkedin.com/feed/update/{urn}/"
            )

    def post_url_for_component_key(self, component_key: Any) -> str:
        match = self._COMPONENT_KEY.match(clean_text(component_key))
        if not match:
            return ""
        return self._component_urls.get(match.group("semantic"), "")

    def resolve_row(self, row: Mapping[str, Any]) -> Mapping[str, Any]:
        post_url = self.post_url_for_component_key(row.get("component_key"))
        if post_url:
            # The response state is tied to this exact visible card. Prefer it
            # over a generic URN found elsewhere in the card's social context.
            return {**row, "post_url": post_url}
        return row


FEED_FIELDS = [
    "signal_id",
    "post_url",
    "author_name",
    "author_url",
    "company",
    "company_url",
    "post_text",
    "context",
    "posted_at_text",
    "signal_kinds",
    "relevance",
    "relevance_reason",
    "first_seen_at",
    "last_seen_at",
    "observed_snapshots",
    "observation_history_json",
    "review_disposition",
    "review_note",
    "reviewed_at",
]

PROFILE_VIEWER_FIELDS = [
    "viewer_id",
    "profile_url",
    "name",
    "headline",
    "company",
    "context",
    "first_seen_at",
    "last_seen_at",
    "observed_snapshots",
    "observation_history_json",
    "relevance",
    "relevance_reason",
    "annotation_source",
    "annotated_at",
    "passive_context_only",
]


DEFAULT_RELEVANCE_KEYWORDS = (
    "product",
    "strategy",
    "business operations",
    "bizops",
    "program management",
    "growth",
    "artificial intelligence",
    " ai ",
    "data platform",
    "developer tools",
    "marketplace",
    "recruiting",
    "future of work",
)

DEFAULT_RELEVANT_VIEWER_ROLES = (
    "founder",
    "recruiter",
    "talent",
    "hiring manager",
    "product",
    "strategy",
    "business operations",
    "bizops",
    "program manager",
    "chief of staff",
    "growth",
)

_SIGNAL_PATTERNS: dict[FeedSignalKind, tuple[str, ...]] = {
    FeedSignalKind.JOB: (
        "job opening",
        "open role",
        "open position",
        "apply now",
        "job posting",
        "career opportunity",
    ),
    FeedSignalKind.HIRING: (
        "we're hiring",
        "we are hiring",
        "hiring for",
        "join our team",
        "grow our team",
        "building the team",
    ),
    FeedSignalKind.FUNDING: (
        "raised a",
        "raised our",
        "funding round",
        "seed round",
        "series a",
        "series b",
        "series c",
        "venture funding",
        "backed by",
    ),
    FeedSignalKind.LAUNCH: (
        "we launched",
        "we're launching",
        "we are launching",
        "introducing ",
        "product launch",
        "now live",
        "general availability",
        "new product",
    ),
    FeedSignalKind.WARM_NETWORK: (
        "started a new position",
        "started a new role",
        "excited to join",
        "happy to share that i've joined",
        "pleased to announce that i've joined",
        "was promoted",
        "your connection",
        "commented on this",
    ),
}

_STARTUP_MARKERS = (
    "startup",
    "founder",
    "founding team",
    "stealth mode",
    "y combinator",
    " yc ",
    "pre-seed",
    "seed-stage",
    "venture-backed",
)


FEED_EXTRACTION_SCRIPT = r"""
() => {
  const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const absolute = (value) => {
    if (!value) return '';
    try { return new URL(value, window.location.origin).href; } catch (_) { return value; }
  };
  const stablePostUrlFromText = (value) => {
    const normalized = clean(value)
      .replace(/&amp;/gi, '&')
      .replace(/%3A/gi, ':')
      .replace(/%2F/gi, '/');
    if (!normalized) return '';
    const absoluteUrl = normalized.match(
      /https?:\/\/(?:www\.)?linkedin\.com\/(?:feed\/update\/urn:li:(?:activity|ugcPost|share):\d+|posts\/[^\s"'<>?&#]+|pulse\/[^\s"'<>?&#]+)/i
    );
    if (absoluteUrl) return absolute(absoluteUrl[0]);
    const relativeUrl = normalized.match(
      /\/(?:feed\/update\/urn:li:(?:activity|ugcPost|share):\d+|posts\/[^\s"'<>?&#]+|pulse\/[^\s"'<>?&#]+)/i
    );
    if (relativeUrl) return absolute(relativeUrl[0]);
    const qliUrn = normalized.match(
      /(?:^|[?&])(?:utm_)?qliurn=(?:urn:li:)?(activity|share)[-:]([0-9]{10,})/i
    );
    return qliUrn
      ? `https://www.linkedin.com/feed/update/urn:li:${qliUrn[1].toLowerCase()}:${qliUrn[2]}/`
      : '';
  };
  const cardSelector = [
    'div.feed-shared-update-v2',
    'div[data-view-name="feed-full-update"]',
    'article[data-urn*="activity:"], article[data-urn*="ugcPost:"], article[data-urn*="share:"]',
    'article[data-id*="activity:"], article[data-id*="ugcPost:"], article[data-id*="share:"]',
  ].join(', ');
  // The 2026 feed keeps an accessible heading even when its update classes and
  // activity URNs are absent from the rendered DOM.
  const feedHeadings = Array.from(document.querySelectorAll('h2')).filter(
    (heading) => clean(heading.textContent).toLowerCase() === 'feed post'
  );
  const accessibleCards = feedHeadings.map(
    (heading) => heading.closest('[role="listitem"]')
  ).filter(Boolean);
  const legacyCandidates = Array.from(document.querySelectorAll(cardSelector));
  const candidates = accessibleCards.length ? accessibleCards : legacyCandidates;
  const cards = Array.from(new Set(candidates)).filter((card) => !candidates.some(
    (other) => other !== card && other.contains(card)
  ));
  return cards.map((card) => {
    const isAccessibleCard = accessibleCards.includes(card);
    const componentKeySelector = [
      '[componentkey^="expanded"][componentkey$="FeedType_MAIN_FEED_RELEVANCE"]',
      '[data-componentkey^="expanded"][data-componentkey$="FeedType_MAIN_FEED_RELEVANCE"]',
      '[data-component-key^="expanded"][data-component-key$="FeedType_MAIN_FEED_RELEVANCE"]',
    ].join(', ');
    const componentKeyNode = card.matches(componentKeySelector)
      ? card
      : (card.querySelector(componentKeySelector) || card.closest(componentKeySelector));
    const componentKey = clean(componentKeyNode ? (
      componentKeyNode.getAttribute('componentkey')
        || componentKeyNode.getAttribute('data-componentkey')
        || componentKeyNode.getAttribute('data-component-key')
    ) : '');
    const urnNode = card.matches('[data-urn], [data-id]')
      ? card
      : card.querySelector(
        '[data-urn*="activity:"], [data-urn*="ugcPost:"], [data-urn*="share:"], '
        + '[data-id*="activity:"], [data-id*="ugcPost:"], [data-id*="share:"]'
      );
    const urn = (urnNode && (urnNode.getAttribute('data-urn')
      || urnNode.getAttribute('data-id'))) || '';
    const permalink = Array.from(card.querySelectorAll(
      'a[href*="/feed/update/"], a[href*="/posts/"], a[href*="/pulse/"]'
    )).find((anchor) => !(anchor.getAttribute('href') || '').includes('/company/'));
    const menuButton = card.querySelector('button[aria-label^="Open control menu for post by "]');
    const menuAuthor = clean(menuButton ? menuButton.getAttribute('aria-label')
      .replace(/^Open control menu for post by\s+/i, '') : '');
    if (isAccessibleCard && !menuAuthor) return null;
    const paragraphs = Array.from(card.querySelectorAll('p'));
    const paragraphText = paragraphs.map((node) => clean(node.textContent));
    const cardLines = (card.innerText || '').split(/\n+/).map(clean).filter(Boolean);
    const isPromoted = paragraphText.concat(cardLines).some(
      (line) => /^promoted(?:\s+by\b.*)?$/i.test(line)
    );
    if (isPromoted) return null;
    const actorBlock = card.querySelector(
      '.update-components-actor, .feed-shared-actor, '
      + '[data-view-name="feed-actor-image"], [data-view-name="feed-actor-name"]'
    ) || card;
    const actorCandidates = Array.from(card.querySelectorAll(
      'a.update-components-actor__meta-link, '
      + 'a.update-components-actor__container-link, '
      + 'a.feed-shared-actor__container-link, '
      + 'a[href*="/in/"], a[href*="/company/"]'
    ));
    const normalizedMenuAuthor = menuAuthor.toLowerCase();
    const normalizedAuthorLabel = (value) => clean(value)
      .replace(/^view\s+company:\s*/i, '')
      .replace(/^view\s+/i, '')
      .replace(/[’']s\s+profile$/i, '')
      .replace(/\s+[•·]\s+.*$/i, '')
      .replace(/\s+verified(?:\s+profile)?(?:\s+\d+(?:st|nd|rd|th))?$/i, '')
      .toLowerCase();
    const matchedAccessibleActor = normalizedMenuAuthor ? actorCandidates.find((anchor) => {
      const image = anchor.querySelector('img[alt]');
      const labels = [
        anchor.textContent,
        anchor.getAttribute('aria-label'),
        anchor.getAttribute('title'),
        image ? image.getAttribute('alt') : '',
      ].filter(Boolean);
      return labels.some(
        (label) => normalizedAuthorLabel(label) === normalizedMenuAuthor
      );
    }) : null;
    const actor = matchedAccessibleActor || (!isAccessibleCard
      ? actorBlock.querySelector(
        'a.update-components-actor__meta-link, '
        + 'a.update-components-actor__container-link, '
        + 'a.feed-shared-actor__container-link, '
        + 'a[href*="/in/"], a[href*="/company/"]'
      )
      : null);
    const authorName = actorBlock.querySelector(
      '.update-components-actor__name, .update-components-actor__title, '
      + '.feed-shared-actor__name'
    );
    const body = card.querySelector(
      '.update-components-text, .feed-shared-update-v2__description, '
      + '[data-test-id="main-feed-activity-card__commentary"], '
      + '[data-testid="expandable-text-box"]'
    );
    const socialContext = card.querySelector(
      '.update-components-header__text-view, .feed-shared-header__text'
    );
    const actorDescription = actorBlock.querySelector(
      '.update-components-actor__description, .feed-shared-actor__description'
    );
    const legacyTimestamp = actorBlock.querySelector(
      '.update-components-actor__sub-description, time, '
      + '.feed-shared-actor__sub-description'
    );
    const semanticTimestamp = paragraphs.find((node) => (
      /^\d+\s*(?:s|m|h|d|w|mo|yr)(?:\s*[\u2022\u00b7].*)?$/i.test(clean(node.textContent))
    ));
    const timestamp = legacyTimestamp || semanticTimestamp;
    const timestampLink = timestamp && timestamp.closest('a[href]');
    const timestampPostUrl = timestampLink
      ? stablePostUrlFromText(timestampLink.getAttribute('href'))
      : '';
    const normalizedUrn = urn.match(/urn:li:(?:activity|ugcPost|share):\d+/i);
    const postUrl = timestampPostUrl || (normalizedUrn
          ? `https://www.linkedin.com/feed/update/${normalizedUrn[0]}/`
          : (!isAccessibleCard && permalink
            ? absolute(permalink.getAttribute('href'))
            : ''));
    const actorUrl = actor ? absolute(actor.getAttribute('href')) : '';
    const actorText = menuAuthor || clean(
      authorName ? authorName.textContent : (actor ? actor.textContent : '')
    );
    const socialPattern = /\b(?:likes this|reposted this|commented on this|celebrates this)\b/i;
    const semanticSocialContext = paragraphText.find((line) => socialPattern.test(line)) || '';
    const socialContextText = clean(
      socialContext ? socialContext.textContent : semanticSocialContext
    );
    const isAfterTimestamp = (node) => Boolean(timestamp && (
      timestamp.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING
    ));
    const isMetaLine = (line) => (
      !line
      || line.toLowerCase() === 'feed post'
      || line.toLowerCase() === actorText.toLowerCase()
      || socialPattern.test(line)
      || /^\d+\s*(?:s|m|h|d|w|mo|yr)(?:\s*[\u2022\u00b7].*)?$/i.test(line)
      || /^\d[\d,.+]*\s+followers?$/i.test(line)
      || /^(?:promoted|follow|following|connect|like|comment|repost|send|more)$/i.test(line)
      || /^(?:like|comment|repost|send)\s+\d*$/i.test(line)
    );
    const semanticBodyCandidates = paragraphs.filter((node) => {
      const line = clean(node.textContent);
      return line.length >= 4 && !isMetaLine(line) && isAfterTimestamp(node);
    });
    const longestSemanticParagraph = paragraphs.reduce((best, node) => {
      const line = clean(node.textContent);
      if (isMetaLine(line) || line.length < 24) return best;
      return !best || line.length > clean(best.textContent).length ? node : best;
    }, null);
    const bodyText = clean(body ? body.textContent : (
      semanticBodyCandidates[0]?.textContent || longestSemanticParagraph?.textContent || ''
    ));
    const headlineCandidates = paragraphs.filter((node) => {
      const line = clean(node.textContent);
      const beforeTimestamp = !timestamp || Boolean(
        node.compareDocumentPosition(timestamp) & Node.DOCUMENT_POSITION_FOLLOWING
      );
      return beforeTimestamp && line.length > 6 && line.length <= 240 && !isMetaLine(line);
    });
    const actorHeadline = clean(actorDescription ? actorDescription.textContent : (
      headlineCandidates[headlineCandidates.length - 1]?.textContent
      || cardLines.find((line) => !isMetaLine(line) && line.length > 6)
      || ''
    ));
    const inferredCompanyMatch = actorHeadline.match(/\s(?:at|@)\s+([^|,;·]+)/i);
    const inferredCompany = inferredCompanyMatch ? clean(inferredCompanyMatch[1]) : '';
    const mentionedCompanyMatch = bodyText.match(
      /(?:[Jj]oined|[Jj]oining|[Cc]alled|[Bb]uilding|[Ll]aunched|[Ss]tarted)\s+@?([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,5})/
    ) || bodyText.match(
      /(?:^|[.!?]\s+)([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,4})\s+(?:is|are)\s+[Hh]iring\b/
    );
    const mentionedCompany = mentionedCompanyMatch ? clean(mentionedCompanyMatch[1]) : '';
    const companyHint = inferredCompany || mentionedCompany;
    const companyAnchor = actorUrl.includes('/company/') ? actor : actorCandidates.find((anchor) => {
      if (!(anchor.getAttribute('href') || '').includes('/company/')) return false;
      const label = clean(anchor.getAttribute('aria-label') || anchor.textContent);
      return companyHint && label.toLowerCase().includes(companyHint.toLowerCase());
    });
    const companyUrl = companyAnchor ? absolute(companyAnchor.getAttribute('href')) : '';
    const companyAnchorName = companyAnchor
      ? clean(companyAnchor.getAttribute('aria-label') || companyAnchor.textContent)
      : '';
    const company = actorUrl.includes('/company/')
      ? actorText
      : (companyAnchorName || inferredCompany || mentionedCompany);
    const itemKeyNode = card.querySelector('[data-testid*="commentList"]');
    const itemKey = itemKeyNode ? clean(itemKeyNode.getAttribute('data-testid')) : '';
    return {
      component_key: componentKey,
      post_url: postUrl,
      author_name: actorText,
      author_url: actorUrl,
      company,
      company_url: companyUrl || (actorUrl.includes('/company/') ? actorUrl : ''),
      text: bodyText,
      context: [
        socialContextText,
        actorHeadline,
        itemKey ? `feed_item_key=${itemKey}` : '',
      ].filter(Boolean).join(' | '),
      posted_at_text: clean(timestamp ? timestamp.textContent : ''),
    };
  }).filter((item) => item && (item.post_url || item.text));
}
"""


PROFILE_VIEWER_EXTRACTION_SCRIPT = r"""
() => {
  const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const absolute = (value) => {
    if (!value) return '';
    try { return new URL(value, window.location.origin).href; } catch (_) { return value; }
  };
  const anchors = Array.from(document.querySelectorAll('main a[href*="/in/"]'))
    .filter((anchor) => !/\/in\/me\/?/i.test(anchor.getAttribute('href') || ''));
  const anonymousCards = Array.from(document.querySelectorAll('main li, main article')).filter((card) => {
    const text = clean(card.innerText).toLowerCase();
    return !card.querySelector('a[href*="/in/"]')
      && /(linkedin member|anonymous viewer|someone at |someone in )/.test(text);
  });
  const seen = new Set();
  const named = anchors.map((anchor) => {
    const card = anchor.closest('li, article, .pvs-list__paged-list-item, div[data-view-name]')
      || anchor;
    const profileUrl = absolute(anchor.getAttribute('href'));
    if (!card || !profileUrl || seen.has(profileUrl)) return null;
    seen.add(profileUrl);
    const lines = (card.innerText || '').split(/\n+/).map(clean).filter(Boolean);
    const nameNode = card.querySelector(
      '.entity-result__title-text, [aria-hidden="true"], strong'
    );
    const headlineNode = card.querySelector(
      '.entity-result__primary-subtitle, .t-14.t-black.t-normal, [data-field="headline"]'
    );
    const nodeName = clean(nameNode ? nameNode.textContent : '');
    const nodeHeadline = clean(headlineNode ? headlineNode.textContent : '');
    const name = nodeName || clean(lines[0] || anchor.textContent || '');
    const headline = nodeHeadline || clean(lines.find((line, index) =>
      index > 0
      && !/^\u2022/.test(line)
      && !/^viewed\b/i.test(line)
      && !/mutual connection/i.test(line)
      && !/^message$/i.test(line)
    ) || '');
    return {
      profile_url: profileUrl,
      name,
      headline,
      company: '',
      context: lines.slice(0, 4).join(' | '),
    };
  }).filter(Boolean);
  const anonymous = anonymousCards.map((card) => {
    const lines = (card.innerText || '').split(/\n+/).map(clean).filter(Boolean);
    const key = lines.slice(0, 3).join('|').toLowerCase();
    if (!key || seen.has(key)) return null;
    seen.add(key);
    return {
      profile_url: '',
      name: lines[0] || 'Anonymous LinkedIn viewer',
      headline: lines[1] || '',
      company: '',
      context: lines.slice(0, 4).join(' | '),
    };
  }).filter(Boolean);
  return named.concat(anonymous);
}
"""

SCROLL_SCRIPT = "window.scrollBy({top: Math.max(window.innerHeight * 0.85, 600), behavior: 'smooth'})"


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def canonical_linkedin_url(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://www.linkedin.com{value}")
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"linkedin.com", "lnkd.in"}:
        return value
    path = re.sub(r"/{2,}", "/", parsed.path)
    if host == "linkedin.com" and path and not path.endswith("/"):
        path += "/"
    canonical_path_prefixes = ("/feed/update/", "/posts/", "/pulse/", "/in/", "/company/")
    if host == "linkedin.com" and path.startswith(canonical_path_prefixes):
        retained_query: list[tuple[str, str]] = []
    else:
        retained_query = [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=False)
            if key.lower() not in {"trk", "trackingid", "lipi", "midtoken", "eid"}
        ]
    return urlunparse(("https", host, path, "", urlencode(retained_query), ""))


def is_stable_linkedin_post_url(value: str) -> bool:
    """Return whether a URL identifies a post, not merely another LinkedIn page."""

    canonical = canonical_linkedin_url(value)
    if not canonical:
        return False
    parsed = urlparse(canonical)
    host = parsed.netloc.lower().removeprefix("www.")
    if host != "linkedin.com":
        return False
    return bool(
        re.fullmatch(
            r"/feed/update/urn:li:(?:activity|ugcPost|share):\d+/"
            r"|/(?:posts|pulse)/[^/?#]+/",
            parsed.path,
            flags=re.IGNORECASE,
        )
    )


def parse_feed_rows(rows: Iterable[Mapping[str, Any]]) -> list[FeedPost]:
    posts: list[FeedPost] = []
    for row in rows:
        post = FeedPost(
            post_url=canonical_linkedin_url(clean_text(row.get("post_url"))),
            author_name=clean_text(row.get("author_name")),
            author_url=canonical_linkedin_url(clean_text(row.get("author_url"))),
            company=clean_text(row.get("company")),
            company_url=canonical_linkedin_url(clean_text(row.get("company_url"))),
            text=clean_text(row.get("text")),
            context=clean_text(row.get("context")),
            posted_at_text=clean_text(row.get("posted_at_text")),
        )
        if post.post_url or post.text:
            posts.append(post)
    return posts


def parse_profile_viewer_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[ProfileViewerObservation]:
    observations: list[ProfileViewerObservation] = []
    for row in rows:
        headline = clean_text(row.get("headline"))
        company = clean_text(row.get("company")) or infer_company_from_headline(headline)
        observation = ProfileViewerObservation(
            profile_url=canonical_linkedin_url(clean_text(row.get("profile_url"))),
            name=clean_text(row.get("name")),
            headline=headline,
            company=company,
            context=clean_text(row.get("context")),
        )
        if observation.profile_url or observation.name:
            observations.append(observation)
    return observations


def infer_company_from_headline(headline: str) -> str:
    match = re.search(r"\s+(?:at|@)\s+([^|,;]+)", headline, flags=re.IGNORECASE)
    return clean_text(match.group(1)) if match else ""


def normalize_extracted_company(company: str, post_text: str) -> str:
    """Trim a greedy DOM inference when the post contains a clearer company mention."""

    clean_company = clean_text(company)
    match = re.search(
        r"(?i:joined|joining|called|building|launched|started)\s+@?"
        r"([A-Z][A-Za-z0-9&'-]*(?:\s+[A-Z][A-Za-z0-9&'-]*){0,5})",
        post_text,
    )
    inferred = clean_text(match.group(1)) if match else ""
    if inferred and (
        not clean_company
        or (
            clean_company.casefold().startswith(inferred.casefold())
            and len(clean_company) > len(inferred)
        )
    ):
        return inferred
    return clean_company


def feed_post_identity(post: FeedPost) -> str:
    if post.post_url:
        seed = post.post_url.lower()
    else:
        seed = "|".join(
            [post.author_url.lower(), post.author_name.lower(), clean_text(post.text).lower()]
        )
    return f"li-feed-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:18]}"


def _content_identity(author_name: str, text: str) -> str:
    author = clean_text(author_name).casefold()
    body = clean_text(text).casefold()
    if not author or not body:
        return ""
    return hashlib.sha256(f"{author}|{body}".encode("utf-8")).hexdigest()


def profile_viewer_identity(observation: ProfileViewerObservation) -> str:
    seed = observation.profile_url.lower() or "|".join(
        [observation.name.lower(), observation.headline.lower(), observation.company.lower()]
    )
    return f"li-viewer-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:18]}"


def classify_feed_post(
    post: FeedPost,
    *,
    known_companies: Iterable[str] = (),
    relevance_keywords: Sequence[str] = DEFAULT_RELEVANCE_KEYWORDS,
) -> FeedClassification:
    blob = f" {clean_text(' '.join([post.context, post.text, post.company])).lower()} "
    kinds: list[FeedSignalKind] = []
    reasons: list[str] = []
    for kind, patterns in _SIGNAL_PATTERNS.items():
        if any(pattern in blob for pattern in patterns):
            kinds.append(kind)
            reasons.append(kind.value.replace("_", " "))

    startup_signal = any(
        _contains_phrase(blob, clean_text(marker).casefold()) for marker in _STARTUP_MARKERS
    )
    if startup_signal:
        kinds.append(FeedSignalKind.STARTUP_DISCOVERY)
        reasons.append("startup/company discovery")

    relevant_matches = [
        clean_text(keyword)
        for keyword in relevance_keywords
        if _contains_phrase(blob, clean_text(keyword).casefold())
    ]
    if relevant_matches and not kinds:
        kinds.append(FeedSignalKind.RELEVANT_UPDATE)
        reasons.append(f"relevant topic: {relevant_matches[0]}")

    normalized_known = {clean_text(company).casefold() for company in known_companies if company}
    is_unknown_company = bool(post.company) and post.company.casefold() not in normalized_known
    if is_unknown_company and (
        startup_signal
        or any(
            kind in kinds
            for kind in (
                FeedSignalKind.JOB,
                FeedSignalKind.HIRING,
                FeedSignalKind.FUNDING,
                FeedSignalKind.LAUNCH,
                FeedSignalKind.RELEVANT_UPDATE,
            )
        )
    ):
        kinds.insert(0, FeedSignalKind.COMPANY_DISCOVERY)
        reasons.insert(0, "new company candidate")

    if not kinds:
        kinds.append(FeedSignalKind.OTHER)
        reasons.append("no actionable feed signal")

    kinds = list(dict.fromkeys(kinds))
    if FeedSignalKind.COMPANY_DISCOVERY in kinds or FeedSignalKind.JOB in kinds:
        relevance = "high"
    elif kinds != [FeedSignalKind.OTHER]:
        relevance = "medium"
    else:
        relevance = "low"
    return FeedClassification(tuple(kinds), relevance, "; ".join(dict.fromkeys(reasons)))


def route_feed_review_disposition(
    signal_kinds: Iterable[FeedSignalKind | str] | str,
    *,
    relevance: str = "",
) -> FeedReviewDisposition:
    """Deterministically route a machine-classified feed signal.

    The order is intentional: a newly discovered company remains a company
    candidate even when its post also mentions a job, while an explicit job at
    a known company remains an opportunity. Ambiguous/other posts are dismissed
    instead of creating a blanket human-review queue.
    """

    raw_kinds: Iterable[FeedSignalKind | str]
    if isinstance(signal_kinds, str):
        raw_kinds = signal_kinds.split(";")
    else:
        raw_kinds = signal_kinds
    kinds = {
        clean_text(kind.value if isinstance(kind, FeedSignalKind) else kind).casefold()
        for kind in raw_kinds
        if clean_text(kind.value if isinstance(kind, FeedSignalKind) else kind)
    }

    if kinds.intersection(
        {
            FeedSignalKind.COMPANY_DISCOVERY.value,
            FeedSignalKind.STARTUP_DISCOVERY.value,
        }
    ):
        return FeedReviewDisposition.COMPANY_CANDIDATE
    if FeedSignalKind.JOB.value in kinds:
        return FeedReviewDisposition.OPPORTUNITY
    if kinds.intersection(
        {
            FeedSignalKind.HIRING.value,
            FeedSignalKind.FUNDING.value,
            FeedSignalKind.LAUNCH.value,
        }
    ):
        return FeedReviewDisposition.ACCOUNT_SIGNAL
    if FeedSignalKind.WARM_NETWORK.value in kinds:
        return FeedReviewDisposition.CONTACT_RESEARCH
    if (
        FeedSignalKind.RELEVANT_UPDATE.value in kinds
        and clean_text(relevance).casefold() in {"medium", "high"}
    ):
        return FeedReviewDisposition.KEEP
    return FeedReviewDisposition.DISMISSED


def classify_viewer_relevance(
    observation: ProfileViewerObservation,
    *,
    target_companies: Iterable[str] = (),
    relevant_role_keywords: Sequence[str] = DEFAULT_RELEVANT_VIEWER_ROLES,
) -> tuple[ViewerRelevance, str]:
    company = observation.company.casefold()
    targets = [clean_text(item).casefold() for item in target_companies if item]
    if company and any(company == target or company in target or target in company for target in targets):
        return ViewerRelevance.TARGET_COMPANY, f"viewer maps to target company: {observation.company}"
    headline = f" {observation.headline.casefold()} "
    matched_role = next(
        (clean_text(keyword) for keyword in relevant_role_keywords if keyword.casefold() in headline),
        "",
    )
    if matched_role:
        return ViewerRelevance.ROLE_RELEVANT, f"potentially relevant role: {matched_role}"
    return ViewerRelevance.UNREVIEWED, "passive context awaiting review"


def capture_feed_posts(
    page: BrowserPage,
    *,
    limits: CaptureLimits | None = None,
    navigate: bool = True,
    feed_url: str = LINKEDIN_FEED_URL,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[FeedPost]:
    def capture_identity(row: Mapping[str, Any]) -> str:
        component_key = clean_text(row.get("component_key"))
        if _FeedPostUrlResolver._COMPONENT_KEY.match(component_key):
            return f"linkedin-feed-component:{component_key}"
        parsed = parse_feed_rows([row])
        return feed_post_identity(parsed[0]) if parsed else ""

    url_resolver = _FeedPostUrlResolver()
    url_resolver.attach(page)
    try:
        rows = _capture_scrolling_rows(
            page,
            extraction_script=FEED_EXTRACTION_SCRIPT,
            target_url=feed_url,
            limits=limits or CaptureLimits(),
            navigate=navigate,
            monotonic=monotonic,
            identity=capture_identity,
            row_transform=url_resolver.resolve_row,
        )
        # A pagination response can finish after its card was first evaluated.
        # Resolve every stored row again, with a bounded settling window for
        # response bodies still completing, then collapse identities that
        # changed from content-based to URL-based once state arrived.
        resolved_rows = [url_resolver.resolve_row(row) for row in rows]
        for _ in range(10):
            unresolved_component = any(
                clean_text(row.get("component_key"))
                and not clean_text(row.get("post_url"))
                for row in resolved_rows
            )
            if not unresolved_component:
                break
            page.wait_for_timeout(500)
            resolved_rows = [url_resolver.resolve_row(row) for row in resolved_rows]
        resolved_posts = parse_feed_rows(resolved_rows)
        unique_posts = {feed_post_identity(post): post for post in resolved_posts}
    finally:
        url_resolver.detach()
    return list(unique_posts.values())


def capture_profile_viewers(
    page: BrowserPage,
    *,
    limits: CaptureLimits | None = None,
    navigate: bool = True,
    viewers_url: str = LINKEDIN_PROFILE_VIEWERS_URL,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[ProfileViewerObservation]:
    rows = _capture_scrolling_rows(
        page,
        extraction_script=PROFILE_VIEWER_EXTRACTION_SCRIPT,
        target_url=viewers_url,
        limits=limits or CaptureLimits(max_scrolls=3, max_items=50),
        navigate=navigate,
        monotonic=monotonic,
        identity=lambda row: profile_viewer_identity(parse_profile_viewer_rows([row])[0])
        if parse_profile_viewer_rows([row])
        else "",
    )
    return parse_profile_viewer_rows(rows)


def _capture_scrolling_rows(
    page: BrowserPage,
    *,
    extraction_script: str,
    target_url: str,
    limits: CaptureLimits,
    navigate: bool,
    monotonic: Callable[[], float],
    identity: Callable[[Mapping[str, Any]], str],
    row_transform: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> list[Mapping[str, Any]]:
    if navigate:
        page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=limits.navigation_timeout_ms,
        )
        if limits.initial_wait_ms:
            page.wait_for_timeout(limits.initial_wait_ms)
    started = monotonic()
    found: dict[str, Mapping[str, Any]] = {}
    stable_scrolls = 0
    for scroll_index in range(limits.max_scrolls + 1):
        raw_rows = page.evaluate(extraction_script) or []
        before = len(found)
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                continue
            if row_transform is not None:
                raw_row = row_transform(raw_row)
            key = identity(raw_row)
            if key:
                found[key] = raw_row
            if len(found) >= limits.max_items:
                break
        if len(found) == before:
            stable_scrolls += 1
        else:
            stable_scrolls = 0
        elapsed = monotonic() - started
        if (
            len(found) >= limits.max_items
            or scroll_index >= limits.max_scrolls
            or stable_scrolls >= limits.stop_after_stable_scrolls
            or (
                limits.max_duration_seconds is not None
                and elapsed >= limits.max_duration_seconds
            )
        ):
            break
        page.evaluate(SCROLL_SCRIPT)
        page.wait_for_timeout(limits.scroll_pause_ms)
    return list(found.values())[: limits.max_items]


class FeedSignalStore:
    """Durable review ledger. It never sends messages or creates outreach actions."""

    def __init__(self, path: Path = DEFAULT_FEED_SIGNALS_PATH) -> None:
        self.path = path

    def upsert_posts(
        self,
        posts: Iterable[FeedPost],
        *,
        observed_at: str | None = None,
        known_companies: Iterable[str] = (),
        relevance_keywords: Sequence[str] = DEFAULT_RELEVANCE_KEYWORDS,
    ) -> dict[str, object]:
        observed_at = observed_at or utc_now_iso()
        existing_rows = _read_csv_rows(self.path)
        rows_by_id = {row.get("signal_id", ""): row for row in existing_rows}
        content_ids = {
            _content_identity(row.get("author_name", ""), row.get("post_text", "")): signal_id
            for signal_id, row in rows_by_id.items()
            if _content_identity(row.get("author_name", ""), row.get("post_text", ""))
        }
        unique_posts: dict[str, FeedPost] = {}
        captured = 0
        for post in posts:
            captured += 1
            unique_posts[feed_post_identity(post)] = post

        added = 0
        updated = 0
        auto_routed_new = 0
        auto_routed_existing_pending = 0
        auto_routed_disposition_counts: Counter[str] = Counter()
        captured_signal_ids: list[str] = []
        run_kind_counts: Counter[str] = Counter()
        run_relevance_counts: Counter[str] = Counter()
        for signal_id, post in unique_posts.items():
            content_id = _content_identity(post.author_name, post.text)
            if signal_id not in rows_by_id and content_id in content_ids:
                signal_id = content_ids[content_id]
            captured_signal_ids.append(signal_id)
            classification = classify_feed_post(
                post,
                known_companies=known_companies,
                relevance_keywords=relevance_keywords,
            )
            run_kind_counts.update(kind.value for kind in classification.signal_kinds)
            run_relevance_counts[classification.relevance] += 1
            incoming = {
                "signal_id": signal_id,
                "post_url": post.post_url,
                "author_name": post.author_name,
                "author_url": post.author_url,
                "company": post.company,
                "company_url": post.company_url,
                "post_text": post.text,
                "context": post.context,
                "posted_at_text": post.posted_at_text,
                "signal_kinds": ";".join(kind.value for kind in classification.signal_kinds),
                "relevance": classification.relevance,
                "relevance_reason": classification.reason,
            }
            existing = rows_by_id.get(signal_id)
            if existing is None:
                disposition = route_feed_review_disposition(
                    classification.signal_kinds,
                    relevance=classification.relevance,
                )
                rows_by_id[signal_id] = {
                    **incoming,
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "observed_snapshots": "1",
                    "observation_history_json": json.dumps([observed_at]),
                    "review_disposition": disposition.value,
                    "review_note": "",
                    "reviewed_at": "",
                }
                auto_routed_new += 1
                auto_routed_disposition_counts[disposition.value] += 1
                added += 1
                continue
            for key, value in incoming.items():
                if value and (
                    not existing.get(key)
                    or key in {"company", "company_url", "signal_kinds", "relevance", "relevance_reason"}
                ):
                    existing[key] = value
            if existing.get("post_url") and existing.get("post_url") == existing.get("company_url") and not post.post_url:
                existing["post_url"] = ""
            history = _read_json_list(existing.get("observation_history_json"))
            if not history:
                history = [
                    value
                    for value in (existing.get("first_seen_at"), existing.get("last_seen_at"))
                    if value
                ]
            if observed_at not in history:
                history.append(observed_at)
            history = sorted(set(history))
            existing["observation_history_json"] = json.dumps(history)
            existing["observed_snapshots"] = str(len(history))
            existing["first_seen_at"] = min(history)
            existing["last_seen_at"] = max(history)
            updated += 1

        # Migrate the whole ledger, not just rows observed in this capture. This
        # clears the legacy blanket-pending queue while preserving every explicit
        # non-pending disposition, including prior human review decisions.
        for row in rows_by_id.values():
            current_disposition = clean_text(row.get("review_disposition")).casefold()
            if current_disposition not in {"", FeedReviewDisposition.PENDING.value}:
                continue
            routed = route_feed_review_disposition(
                row.get("signal_kinds", ""),
                relevance=row.get("relevance", ""),
            )
            row["review_disposition"] = routed.value
            auto_routed_existing_pending += 1
            auto_routed_disposition_counts[routed.value] += 1

        ordered_rows = sorted(
            rows_by_id.values(),
            key=lambda row: (row.get("first_seen_at", ""), row.get("signal_id", "")),
        )
        _write_csv_rows(self.path, FEED_FIELDS, ordered_rows)
        workspace_disposition_counts = Counter(
            clean_text(row.get("review_disposition")) or FeedReviewDisposition.PENDING.value
            for row in ordered_rows
        )
        pending = workspace_disposition_counts[FeedReviewDisposition.PENDING.value]
        return {
            "path": str(self.path),
            "observed_at": observed_at,
            "captured": captured,
            "unique_in_capture": len(unique_posts),
            "duplicates_in_capture": captured - len(unique_posts),
            "added": added,
            "updated": updated,
            "captured_signal_ids": sorted(set(captured_signal_ids)),
            "post_url_count": sum(bool(post.post_url) for post in unique_posts.values()),
            "post_url_missing": sum(not post.post_url for post in unique_posts.values()),
            "run_signal_kind_counts": dict(sorted(run_kind_counts.items())),
            "run_relevance_counts": dict(sorted(run_relevance_counts.items())),
            "auto_routed": auto_routed_new + auto_routed_existing_pending,
            "auto_routed_new": auto_routed_new,
            "auto_routed_existing_pending": auto_routed_existing_pending,
            "auto_routed_disposition_counts": dict(
                sorted(auto_routed_disposition_counts.items())
            ),
            "workspace_review_disposition_counts": dict(
                sorted(workspace_disposition_counts.items())
            ),
            "workspace_pending_review": pending,
        }

    def review(
        self,
        signal_id: str,
        disposition: FeedReviewDisposition | str,
        *,
        note: str = "",
        reviewed_at: str | None = None,
    ) -> dict[str, str]:
        disposition = FeedReviewDisposition(disposition)
        rows = _read_csv_rows(self.path)
        for row in rows:
            if row.get("signal_id") != signal_id:
                continue
            row["review_disposition"] = disposition.value
            row["review_note"] = clean_text(note)
            row["reviewed_at"] = reviewed_at or utc_now_iso()
            _write_csv_rows(self.path, FEED_FIELDS, rows)
            return row
        raise KeyError(f"LinkedIn feed signal not found: {signal_id}")

    def pending_review(self) -> list[dict[str, str]]:
        return [
            row
            for row in _read_csv_rows(self.path)
            if row.get("review_disposition") == FeedReviewDisposition.PENDING.value
        ]


class ProfileViewerStore:
    """Passive interest ledger; deliberately exposes annotation, not action triggering."""

    def __init__(self, path: Path = DEFAULT_PROFILE_VIEWERS_PATH) -> None:
        self.path = path

    def upsert_observations(
        self,
        observations: Iterable[ProfileViewerObservation],
        *,
        observed_at: str | None = None,
        target_companies: Iterable[str] = (),
        relevant_role_keywords: Sequence[str] = DEFAULT_RELEVANT_VIEWER_ROLES,
    ) -> dict[str, object]:
        observed_at = observed_at or utc_now_iso()
        existing_rows = _read_csv_rows(self.path)
        rows_by_id = {row.get("viewer_id", ""): row for row in existing_rows}
        unique: dict[str, ProfileViewerObservation] = {}
        captured = 0
        for observation in observations:
            captured += 1
            unique[profile_viewer_identity(observation)] = observation

        added = 0
        updated = 0
        run_relevance_counts: Counter[str] = Counter()
        for viewer_id, observation in unique.items():
            relevance, relevance_reason = classify_viewer_relevance(
                observation,
                target_companies=target_companies,
                relevant_role_keywords=relevant_role_keywords,
            )
            run_relevance_counts[relevance.value] += 1
            existing = rows_by_id.get(viewer_id)
            if existing is None:
                rows_by_id[viewer_id] = {
                    "viewer_id": viewer_id,
                    "profile_url": observation.profile_url,
                    "name": observation.name,
                    "headline": observation.headline,
                    "company": observation.company,
                    "context": observation.context,
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "observed_snapshots": "1",
                    "observation_history_json": json.dumps([observed_at]),
                    "relevance": relevance.value,
                    "relevance_reason": relevance_reason,
                    "annotation_source": "automatic",
                    "annotated_at": observed_at,
                    "passive_context_only": "true",
                }
                added += 1
                continue
            for key in ("profile_url", "name", "headline", "company", "context"):
                value = clean_text(getattr(observation, key))
                if value:
                    existing[key] = value
            history = _read_json_list(existing.get("observation_history_json"))
            if observed_at not in history:
                history.append(observed_at)
            history = sorted(set(history))
            existing["observation_history_json"] = json.dumps(history)
            existing["observed_snapshots"] = str(len(history))
            existing["first_seen_at"] = min(history)
            existing["last_seen_at"] = max(history)
            if existing.get("annotation_source") != "manual":
                existing["relevance"] = relevance.value
                existing["relevance_reason"] = relevance_reason
                existing["annotation_source"] = "automatic"
                existing["annotated_at"] = observed_at
            existing["passive_context_only"] = "true"
            updated += 1

        ordered_rows = sorted(
            rows_by_id.values(),
            key=lambda row: (row.get("first_seen_at", ""), row.get("viewer_id", "")),
        )
        _write_csv_rows(self.path, PROFILE_VIEWER_FIELDS, ordered_rows)
        return {
            "path": str(self.path),
            "observed_at": observed_at,
            "captured": captured,
            "unique_in_capture": len(unique),
            "duplicates_in_capture": captured - len(unique),
            "added": added,
            "updated": updated,
            "run_relevance_counts": dict(sorted(run_relevance_counts.items())),
            "workspace_passive_records": len(ordered_rows),
        }

    def annotate_relevance(
        self,
        viewer_id: str,
        relevance: ViewerRelevance | str,
        *,
        reason: str,
        annotated_at: str | None = None,
    ) -> dict[str, str]:
        relevance = ViewerRelevance(relevance)
        rows = _read_csv_rows(self.path)
        for row in rows:
            if row.get("viewer_id") != viewer_id:
                continue
            row["relevance"] = relevance.value
            row["relevance_reason"] = clean_text(reason)
            row["annotation_source"] = "manual"
            row["annotated_at"] = annotated_at or utc_now_iso()
            row["passive_context_only"] = "true"
            _write_csv_rows(self.path, PROFILE_VIEWER_FIELDS, rows)
            return row
        raise KeyError(f"LinkedIn profile viewer not found: {viewer_id}")


def capture_and_store_feed(
    page: BrowserPage,
    *,
    path: Path = DEFAULT_FEED_SIGNALS_PATH,
    limits: CaptureLimits | None = None,
    observed_at: str | None = None,
    known_companies: Iterable[str] = (),
    relevance_keywords: Sequence[str] = DEFAULT_RELEVANCE_KEYWORDS,
) -> dict[str, object]:
    posts = capture_feed_posts(page, limits=limits)
    return FeedSignalStore(path).upsert_posts(
        posts,
        observed_at=observed_at,
        known_companies=known_companies,
        relevance_keywords=relevance_keywords,
    )


def capture_and_store_profile_viewers(
    page: BrowserPage,
    *,
    path: Path = DEFAULT_PROFILE_VIEWERS_PATH,
    limits: CaptureLimits | None = None,
    observed_at: str | None = None,
    target_companies: Iterable[str] = (),
) -> dict[str, object]:
    observations = capture_profile_viewers(page, limits=limits)
    return ProfileViewerStore(path).upsert_observations(
        observations,
        observed_at=observed_at,
        target_companies=target_companies,
    )


def capture_linkedin_signals_live(
    settings: OutreachSettings,
    *,
    feed_path: Path = DEFAULT_FEED_SIGNALS_PATH,
    profile_viewers_path: Path = DEFAULT_PROFILE_VIEWERS_PATH,
    feed_limits: CaptureLimits | None = None,
    profile_viewer_limits: CaptureLimits | None = None,
    capture_profile_viewers_this_run: bool = False,
    observed_at: str | None = None,
    known_companies: Iterable[str] = (),
    target_companies: Iterable[str] = (),
    relevance_keywords: Sequence[str] = DEFAULT_RELEVANCE_KEYWORDS,
) -> dict[str, object]:
    """Read LinkedIn signals over the existing CDP session without sending anything.

    Feed capture is the daily lane. Profile viewers are opt-in so the caller can run
    them weekly. Browser/auth failures are returned as source statuses for truthful
    nightly reporting instead of being mistaken for a zero-result run.
    """

    observed_at = observed_at or utc_now_iso()
    viewer_skipped: dict[str, object] = {
        "status": "skipped",
        "reason": "not_scheduled_for_this_run",
        "captured": 0,
    }
    result: dict[str, object] = {
        "status": "failed",
        "observed_at": observed_at,
        "read_only": True,
        "feed": {"status": "not_started", "captured": 0},
        "profile_viewers": viewer_skipped,
    }
    scraper = LinkedInScraper(settings)
    browser: Any = None
    try:
        scraper.require_live_cdp_session()
        with sync_playwright() as playwright:
            browser = scraper._connect_over_cdp(playwright)
            context = browser.contexts[0]
            preflight = scraper._session_preflight(context)
            if not preflight.get("ok"):
                result["feed"] = {
                    "status": "failed",
                    "captured": 0,
                    "reason": "linkedin_session_preflight_failed",
                    "current_url": clean_text(preflight.get("current_url")),
                    "authwall_or_login": bool(preflight.get("authwall_or_login")),
                }
                if capture_profile_viewers_this_run:
                    result["profile_viewers"] = {
                        "status": "skipped",
                        "captured": 0,
                        "reason": "linkedin_session_preflight_failed",
                    }
                return result

            result["feed"] = _capture_feed_live_page(
                context,
                scraper,
                path=feed_path,
                limits=feed_limits,
                observed_at=observed_at,
                known_companies=known_companies,
                relevance_keywords=relevance_keywords,
            )
            if capture_profile_viewers_this_run:
                result["profile_viewers"] = _capture_profile_viewers_live_page(
                    context,
                    scraper,
                    path=profile_viewers_path,
                    limits=profile_viewer_limits,
                    observed_at=observed_at,
                    target_companies=target_companies,
                )

            feed_ok = _source_status(result.get("feed")) == "completed"
            viewer_status = _source_status(result.get("profile_viewers"))
            if feed_ok and viewer_status in {"completed", "skipped"}:
                result["status"] = "completed"
            elif feed_ok or viewer_status == "completed":
                result["status"] = "partial"
            return result
    except Exception as exc:  # Browser setup failures must remain visible in the run report.
        result["feed"] = {
            "status": "failed",
            "captured": 0,
            "reason": "linkedin_live_capture_unavailable",
            "error": f"{type(exc).__name__}: {clean_text(exc)}",
        }
        if capture_profile_viewers_this_run:
            result["profile_viewers"] = {
                "status": "skipped",
                "captured": 0,
                "reason": "linkedin_live_capture_unavailable",
            }
        return result
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def _capture_feed_live_page(
    context: Any,
    scraper: LinkedInScraper,
    *,
    path: Path,
    limits: CaptureLimits | None,
    observed_at: str,
    known_companies: Iterable[str],
    relevance_keywords: Sequence[str],
) -> dict[str, object]:
    page: Any = None
    try:
        page = context.new_page()
        page.set_default_timeout(15_000)
        posts = capture_feed_posts(
            page,
            limits=limits or CaptureLimits(initial_wait_ms=2_500),
        )
        unique_posts = {
            feed_post_identity(post): post
            for post in posts
        }
        captured = len(unique_posts)
        post_url_count = sum(
            is_stable_linkedin_post_url(post.post_url)
            for post in unique_posts.values()
        )
        missing_urls = captured - post_url_count
        if captured <= 0:
            return {
                "status": "failed",
                "path": str(path),
                "captured": 0,
                "unique_in_capture": 0,
                "post_url_count": 0,
                "post_url_missing": 0,
                "persisted": False,
                "reason": "feed_capture_empty",
                "quality_gate": "no_posts_extracted_from_authenticated_feed",
            }
        if post_url_count < captured or missing_urls:
            return {
                "status": "partial_failed",
                "path": str(path),
                "captured": len(posts),
                "unique_in_capture": captured,
                "post_url_count": post_url_count,
                "post_url_missing": missing_urls,
                "added": 0,
                "persisted": False,
                "reason": "feed_capture_missing_permalinks",
                "quality_gate": (
                    f"{post_url_count}/{captured} captured posts have stable LinkedIn URLs"
                ),
            }
        summary = FeedSignalStore(path).upsert_posts(
            posts,
            observed_at=observed_at,
            known_companies=known_companies,
            relevance_keywords=relevance_keywords,
        )
        return {
            "status": "completed",
            **summary,
            "persisted": True,
            "quality_gate": f"{post_url_count}/{captured} posts have stable LinkedIn URLs",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "captured": 0,
            "path": str(path),
            "reason": "feed_capture_failed",
            "error": f"{type(exc).__name__}: {clean_text(exc)}",
        }
    finally:
        scraper._close_page_safely(page)


def _capture_profile_viewers_live_page(
    context: Any,
    scraper: LinkedInScraper,
    *,
    path: Path,
    limits: CaptureLimits | None,
    observed_at: str,
    target_companies: Iterable[str],
) -> dict[str, object]:
    page: Any = None
    try:
        page = context.new_page()
        page.set_default_timeout(15_000)
        summary = capture_and_store_profile_viewers(
            page,
            path=path,
            limits=limits or CaptureLimits(max_scrolls=3, max_items=50, initial_wait_ms=2_500),
            observed_at=observed_at,
            target_companies=target_companies,
        )
        return {"status": "completed", **summary}
    except Exception as exc:
        return {
            "status": "failed",
            "captured": 0,
            "path": str(path),
            "reason": "profile_viewer_capture_failed",
            "error": f"{type(exc).__name__}: {clean_text(exc)}",
        }
    finally:
        scraper._close_page_safely(page)


def _source_status(value: object) -> str:
    return clean_text(value.get("status")) if isinstance(value, Mapping) else ""


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv_rows(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def _contains_phrase(blob: str, phrase: str) -> bool:
    if not phrase:
        return False
    if len(phrase) <= 3 and phrase.isalnum():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", blob))
    return phrase in blob


def _read_json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [clean_text(item) for item in parsed if clean_text(item)]

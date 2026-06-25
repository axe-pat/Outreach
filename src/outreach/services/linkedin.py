from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import signal
import subprocess
from pathlib import Path
import random
import re
import time
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Page, sync_playwright

from outreach.artifacts import artifact_timestamp
from outreach.config import OutreachSettings
from outreach.models import RawSearchCandidate


def normalize_typeahead_text(value: str) -> str:
    return " ".join((value or "").lower().split()).strip()


def primary_typeahead_label(option_text: str) -> str:
    lines = [normalize_typeahead_text(line) for line in (option_text or "").splitlines()]
    lines = [line for line in lines if line]
    return lines[0] if lines else ""


def score_typeahead_option(trigger_text: str, requested_value: str, option_text: str) -> int:
    trigger = normalize_typeahead_text(trigger_text)
    requested = normalize_typeahead_text(requested_value)
    option = primary_typeahead_label(option_text) if trigger == "add a company" else normalize_typeahead_text(option_text)
    if not requested or not option:
        return -10_000

    score = 0
    if option == requested:
        score += 200
    if option.startswith(f"{requested} "):
        score += 120
    if option.startswith(requested):
        score += 90
    if f" {requested} " in f" {option} ":
        score += 70

    requested_tokens = [token for token in re.split(r"[^a-z0-9]+", requested) if token]
    option_tokens = {token for token in re.split(r"[^a-z0-9]+", option) if token}
    overlap = sum(1 for token in requested_tokens if token in option_tokens)
    score += overlap * 15

    if trigger == "add a company":
        if any(word in option for word in {"university", "college", "school"}):
            score -= 120
        if any(word in option for word in {"inc", "labs", "health", "ai", "technologies", "tech"}):
            score += 10
        if option != requested and option.startswith(f"{requested} "):
            score -= 80
    elif trigger == "add a school":
        if any(word in option for word in {"university", "college", "school"}):
            score += 40

    if len(requested) <= 8 and option != requested and option.startswith("santa "):
        score -= 80

    return score


@dataclass
class LinkedInCheckResult:
    ok: bool
    current_url: str
    title: str
    logged_in: bool
    details: str
    steps: list[str]
    screenshot_paths: list[str]


@dataclass
class FilterRunResult:
    candidates: list[RawSearchCandidate]
    final_url: str
    visible_filter_text: list[str]
    screenshot_path: str | None = None


@dataclass
class InviteSendResult:
    name: str
    linkedin_url: str
    status: str
    detail: str
    note: str
    screenshot_path: str | None = None


@dataclass
class LinkedInReconcileResult:
    contact_id: str
    name: str
    linkedin_url: str
    status: str
    detail: str
    screenshot_path: str | None = None


@dataclass
class LinkedInMessageThread:
    thread_id: str
    name: str
    thread_url: str
    latest_message: str
    last_sender: str = ""
    timestamp_text: str = ""
    unread: bool = False


class InviteCandidateTimeoutError(RuntimeError):
    """Raised when a single invite candidate takes too long to process."""


class LinkedInScraper:
    """Browser-native LinkedIn automation."""

    def __init__(self, settings: OutreachSettings) -> None:
        self.settings = settings

    def search_company(self, company: str) -> list[dict]:
        raise NotImplementedError("Implement browser session automation in Phase 1.")

    def extract_company_people_live(self, company: str, limit: int = 10) -> list[RawSearchCandidate]:
        return self.extract_people_live(search_query=company, limit=limit)

    def extract_people_live(self, search_query: str, limit: int = 10, max_pages: int = 1) -> list[RawSearchCandidate]:
        self.require_live_cdp_session()
        with sync_playwright() as playwright:
            browser = self._connect_over_cdp(playwright)
            try:
                context = browser.contexts[0]
                preflight = self._session_preflight(context)
                if not preflight["ok"]:
                    raise RuntimeError(
                        "LinkedIn preflight failed before people extraction: "
                        f"url={preflight['current_url']} authwall_or_login={preflight['authwall_or_login']}"
                    )
                page = context.new_page()
                page.set_default_timeout(15000)
                try:
                    search_url = (
                        "https://www.linkedin.com/search/results/people/"
                        f"?keywords={quote_plus(search_query)}"
                        "&origin=GLOBAL_SEARCH_HEADER"
                    )
                    if not self._safe_goto(page, search_url):
                        raise RuntimeError(f"Could not load LinkedIn people search: {search_url}")
                    self._human_pause(page)
                    return self._collect_people_results(page, limit=limit, max_pages=max_pages)
                finally:
                    self._close_page_safely(page)
            finally:
                browser.close()

    def extract_people_with_filters_live(
        self,
        company: str,
        search_query: str,
        limit: int = 10,
        max_pages: int = 1,
        school: str | None = None,
        connection_degree: str | None = None,
        use_us_location: bool = True,
    ) -> FilterRunResult:
        self.require_live_cdp_session()
        with sync_playwright() as playwright:
            browser = self._connect_over_cdp(playwright)
            try:
                context = browser.contexts[0]
                preflight = self._session_preflight(context)
                if not preflight["ok"]:
                    raise RuntimeError(
                        "LinkedIn preflight failed before filtered people search: "
                        f"url={preflight['current_url']} authwall_or_login={preflight['authwall_or_login']}"
                    )
                page = context.new_page()
                page.set_default_timeout(15000)
                try:
                    base_query = quote_plus(search_query) if search_query else ""
                    search_url = (
                        "https://www.linkedin.com/search/results/people/"
                        f"?keywords={base_query}&origin=GLOBAL_SEARCH_HEADER"
                    )
                    if not self._safe_goto(page, search_url):
                        raise RuntimeError(f"Could not load filtered LinkedIn people search: {search_url}")
                    self._human_pause(page)
                    self._apply_people_filters(
                        page=page,
                        company=company,
                        school=school,
                        connection_degree=connection_degree,
                        use_us_location=use_us_location,
                    )
                    candidates = self._collect_people_results(page, limit=limit, max_pages=max_pages)
                    filter_text = self._read_visible_filter_text(page)
                    screenshot = self._save_screenshot(page, "filtered-results")
                    return FilterRunResult(
                        candidates=candidates,
                        final_url=page.url,
                        visible_filter_text=filter_text,
                        screenshot_path=screenshot,
                    )
                finally:
                    self._close_page_safely(page)
            finally:
                browser.close()

    def prepare_browser(self, headless: bool = False) -> None:
        self.settings.validate_explicit_linkedin_profile()
        user_data_dir = self.settings.resolved_linkedin_user_data_dir
        user_data_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel="chrome",
                headless=headless,
            )
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                page.pause()
            finally:
                browser.close()

    def check_session_via_cdp(self) -> LinkedInCheckResult:
        steps: list[str] = []
        screenshots: list[str] = []

        try:
            self._validate_cdp_owner()
            with sync_playwright() as playwright:
                steps.append(
                    f"Connecting to running Chrome via CDP at http://127.0.0.1:{self.settings.linkedin_debug_port}"
                )
                browser = self._connect_over_cdp(playwright)
                try:
                    context = browser.contexts[0]
                    preflight = self._session_preflight(context)
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(15000)
                    steps.append(f"Connected to page at {page.url}")
                    screenshots.append(self._save_screenshot(page, "cdp-initial"))
                    self._goto(page, "https://www.linkedin.com/feed/", steps, "linkedin-feed")
                    screenshots.append(self._save_screenshot(page, "cdp-linkedin-feed"))
                    logged_in = self._looks_logged_in(page)
                    steps.append(f"Login heuristic result: {logged_in}")
                    steps.append(
                        "Preflight: "
                        f"ok={preflight['ok']} authwall_or_login={preflight['authwall_or_login']} "
                        f"has_li_at_cookie={preflight['has_li_at_cookie']}"
                    )
                    return LinkedInCheckResult(
                        ok=logged_in and preflight["ok"],
                        current_url=page.url,
                        title=page.title(),
                        logged_in=logged_in,
                        details=(
                            "LinkedIn session looks active in running Chrome."
                            if logged_in and preflight["ok"]
                            else "Connected to Chrome, but LinkedIn does not appear logged in."
                        ),
                        steps=steps,
                        screenshot_paths=screenshots,
                    )
                finally:
                    browser.close()
        except PlaywrightError as exc:
            steps.append(f"Playwright error: {exc}")
            return LinkedInCheckResult(
                ok=False,
                current_url="",
                title="",
                logged_in=False,
                details=f"Could not connect to running Chrome via CDP: {exc}",
                steps=steps,
                screenshot_paths=screenshots,
            )

    def require_live_cdp_session(self) -> None:
        self._validate_cdp_owner()

    def check_session(self, headless: bool = False) -> LinkedInCheckResult:
        user_data_dir = self.settings.resolved_linkedin_user_data_dir
        self._validate_user_data_dir(user_data_dir)
        steps: list[str] = []
        screenshots: list[str] = []

        try:
            with sync_playwright() as playwright:
                steps.append("Launching persistent Chrome context")
                browser = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    channel="chrome",
                    headless=headless,
                )
                try:
                    page = browser.pages[0] if browser.pages else browser.new_page()
                    page.set_default_timeout(15000)
                    steps.append(f"Opened initial page at {page.url}")
                    screenshots.append(self._save_screenshot(page, "initial"))

                    self._goto(page, "https://example.com", steps, "example")
                    screenshots.append(self._save_screenshot(page, "example"))

                    self._goto(page, "https://www.linkedin.com", steps, "linkedin-home")
                    screenshots.append(self._save_screenshot(page, "linkedin-home"))

                    self._goto(page, "https://www.linkedin.com/feed/", steps, "linkedin-feed")
                    screenshots.append(self._save_screenshot(page, "linkedin-feed"))
                    logged_in = self._looks_logged_in(page)
                    steps.append(f"Login heuristic result: {logged_in}")
                    title = page.title()
                    current_url = page.url
                    details = (
                        "LinkedIn session looks active."
                        if logged_in
                        else "Chrome launched, but LinkedIn does not appear logged in."
                    )
                    return LinkedInCheckResult(
                        ok=logged_in,
                        current_url=current_url,
                        title=title,
                        logged_in=logged_in,
                        details=details,
                        steps=steps,
                        screenshot_paths=screenshots,
                    )
                finally:
                    browser.close()
        except PlaywrightTimeoutError as exc:
            steps.append(f"Timed out: {exc}")
            return LinkedInCheckResult(
                ok=False,
                current_url="",
                title="",
                logged_in=False,
                details=f"Browser launched, but LinkedIn navigation timed out: {exc}",
                steps=steps,
                screenshot_paths=screenshots,
            )
        except PlaywrightError as exc:
            steps.append(f"Playwright error: {exc}")
            return LinkedInCheckResult(
                ok=False,
                current_url="",
                title="",
                logged_in=False,
                details=f"Playwright could not start the browser session: {exc}",
                steps=steps,
                screenshot_paths=screenshots,
            )

    def send_connection_requests(
        self,
        candidates: list[dict],
        execute: bool = False,
        on_result=None,
    ) -> list[InviteSendResult]:
        self.require_live_cdp_session()
        with sync_playwright() as playwright:
            browser = self._connect_over_cdp(playwright)
            try:
                context = browser.contexts[0]
                preflight = self._session_preflight(context)
                if not preflight["ok"]:
                    raise RuntimeError(
                        "LinkedIn preflight failed before invite send: "
                        f"url={preflight['current_url']} authwall_or_login={preflight['authwall_or_login']}"
                    )
                baseline_pages = list(context.pages)
                try:
                    results: list[InviteSendResult] = []
                    for candidate in candidates:
                        self._close_run_spawned_pages(context, keep_pages=baseline_pages)
                        page = context.new_page()
                        page.set_default_timeout(15000)
                        try:
                            with self._candidate_timeout(self.settings.search.invite_candidate_timeout_seconds):
                                result = self._send_single_invite(page, candidate, execute=execute)
                            results.append(result)
                        except InviteCandidateTimeoutError as exc:
                            result = InviteSendResult(
                                name=str(candidate.get("name") or "Unknown"),
                                linkedin_url=str(candidate.get("linkedin_url") or ""),
                                status="send_error",
                                detail=str(exc),
                                note=str(candidate.get("note") or ""),
                                screenshot_path=self._save_screenshot(page, "invite-timeout"),
                            )
                            results.append(result)
                        except Exception as exc:
                            result = InviteSendResult(
                                name=str(candidate.get("name") or "Unknown"),
                                linkedin_url=str(candidate.get("linkedin_url") or ""),
                                status="send_error",
                                detail=f"Invite processing crashed: {exc}",
                                note=str(candidate.get("note") or ""),
                                screenshot_path=self._save_screenshot(page, "invite-crash"),
                            )
                            results.append(result)
                        finally:
                            if on_result is not None:
                                on_result(candidate, results[-1], list(results))
                            self._close_page_safely(page)
                            self._close_run_spawned_pages(context, keep_pages=baseline_pages)
                    return results
                finally:
                    pass
            finally:
                browser.close()

    def reconcile_connection_statuses(self, candidates: list[dict]) -> list[LinkedInReconcileResult]:
        self.require_live_cdp_session()
        with sync_playwright() as playwright:
            browser = self._connect_over_cdp(playwright)
            try:
                context = browser.contexts[0]
                preflight = self._session_preflight(context)
                if not preflight["ok"]:
                    raise RuntimeError(
                        "LinkedIn preflight failed before reconcile: "
                        f"url={preflight['current_url']} authwall_or_login={preflight['authwall_or_login']}"
                    )
                baseline_pages = list(context.pages)
                results: list[LinkedInReconcileResult] = []
                for candidate in candidates:
                    self._close_run_spawned_pages(context, keep_pages=baseline_pages)
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    try:
                        result = self._reconcile_single_connection(page, candidate)
                    except Exception as exc:
                        result = LinkedInReconcileResult(
                            contact_id=str(candidate.get("contact_id") or ""),
                            name=str(candidate.get("name") or candidate.get("full_name") or "Unknown"),
                            linkedin_url=str(candidate.get("linkedin_url") or ""),
                            status="error",
                            detail=f"Profile reconcile crashed: {exc}",
                            screenshot_path=self._save_screenshot(page, "reconcile-error"),
                        )
                    results.append(result)
                    self._close_page_safely(page)
                    self._close_run_spawned_pages(context, keep_pages=baseline_pages)
                return results
            finally:
                browser.close()

    def snapshot_message_threads(self, limit: int = 50) -> list[LinkedInMessageThread]:
        self.require_live_cdp_session()
        with sync_playwright() as playwright:
            browser = self._connect_over_cdp(playwright)
            try:
                context = browser.contexts[0]
                preflight = self._session_preflight(context, target_url="https://www.linkedin.com/messaging/")
                if not preflight["ok"]:
                    raise RuntimeError(
                        "LinkedIn preflight failed before message snapshot: "
                        f"url={preflight['current_url']} authwall_or_login={preflight['authwall_or_login']}"
                    )
                page = context.new_page()
                page.set_default_timeout(15000)
                try:
                    if not self._safe_goto(page, "https://www.linkedin.com/messaging/"):
                        raise RuntimeError("Could not load LinkedIn messaging.")
                    page.wait_for_timeout(2500)
                    return self._extract_message_threads(page, limit=limit)
                finally:
                    self._close_page_safely(page)
            finally:
                browser.close()

    def _extract_message_threads(self, page: Page, limit: int = 50) -> list[LinkedInMessageThread]:
        script = """
        (limit) => {
          const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
          const containers = Array.from(document.querySelectorAll(
            'li.msg-conversation-listitem, .msg-conversation-listitem, [data-view-name="messages-conversation-list-item"]'
          ));
          const fallbackAnchors = Array.from(document.querySelectorAll('a[href*="/messaging/thread/"]'));
          const seen = new Set();
          const items = [];

          const readContainer = (container) => {
            const anchor = container.querySelector('a[href*="/messaging/thread/"]') || (
              container.matches && container.matches('a[href*="/messaging/thread/"]') ? container : null
            );
            const href = anchor ? anchor.href : "";

            const nameEl = container.querySelector(
              '.msg-conversation-listitem__participant-names, [data-anonymize="person-name"], h3, .entity-result__title-text'
            );
            const snippetEl = container.querySelector(
              '.msg-conversation-listitem__message-snippet, .msg-conversation-card__message-snippet, p'
            );
            const timeEl = container.querySelector(
              'time, .msg-conversation-listitem__time-stamp, .msg-conversation-card__time-stamp'
            );
            const textLines = normalize(container.innerText).split(/ (?=[A-Z][a-z]+\\b)/).map(normalize).filter(Boolean);
            const name = normalize(nameEl ? nameEl.textContent : "") || textLines[0] || "";
            const timeText = normalize(timeEl ? timeEl.textContent : "");
            const latest = normalize(snippetEl ? snippetEl.textContent : "") || textLines.slice(1).join(" ");
            const lastSenderMatch = latest.match(/^([^:]{1,60}):\\s+(.+)$/);
            const inferredLastSender = lastSenderMatch ? normalize(lastSenderMatch[1]) : (/^you sent\\b/i.test(latest) ? "You" : "");
            const unread = /unread/i.test(container.className || "") || container.querySelector('[aria-label*="unread" i]') !== null;
            const threadId = href
              ? (href.split('/messaging/thread/')[1]?.split(/[/?#]/)[0] || href)
              : `synthetic:${name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}`;
            if (!name || !threadId || seen.has(threadId)) return null;
            seen.add(threadId);

            return {
              thread_id: threadId,
              name,
              thread_url: href,
              latest_message: lastSenderMatch ? normalize(lastSenderMatch[2]) : latest,
              last_sender: inferredLastSender,
              timestamp_text: timeText,
              unread,
            };
          };

          for (const container of containers) {
            const item = readContainer(container);
            if (item && item.name) items.push(item);
            if (items.length >= limit) return items;
          }
          for (const anchor of fallbackAnchors) {
            const container = anchor.closest('li') || anchor.closest('[role="listitem"]') || anchor;
            const item = readContainer(container);
            if (item && item.name) items.push(item);
            if (items.length >= limit) return items;
          }
          return items;
        }
        """
        raw_threads = page.evaluate(script, limit)
        return [LinkedInMessageThread(**item) for item in raw_threads]

    def _reconcile_single_connection(self, page: Page, candidate: dict) -> LinkedInReconcileResult:
        contact_id = str(candidate.get("contact_id") or "")
        name = str(candidate.get("name") or candidate.get("full_name") or "Unknown")
        linkedin_url = str(candidate.get("linkedin_url") or "")
        if not linkedin_url:
            return LinkedInReconcileResult(
                contact_id=contact_id,
                name=name,
                linkedin_url="",
                status="skipped",
                detail="Missing LinkedIn URL.",
            )

        if not self._navigate_profile(page, linkedin_url):
            return LinkedInReconcileResult(
                contact_id=contact_id,
                name=name,
                linkedin_url=linkedin_url,
                status="navigation_error",
                detail="Could not load LinkedIn profile reliably.",
                screenshot_path=self._save_screenshot(page, "reconcile-navigation-error"),
            )
        page.evaluate("window.scrollTo(0, 0)")
        self._human_pause(page)

        if self._profile_has_pending_signal(page):
            return LinkedInReconcileResult(
                contact_id=contact_id,
                name=name,
                linkedin_url=linkedin_url,
                status="pending",
                detail="Profile still shows a pending invite.",
                screenshot_path=self._save_screenshot(page, "reconcile-pending"),
            )
        if self._profile_has_connected_signal(page):
            return LinkedInReconcileResult(
                contact_id=contact_id,
                name=name,
                linkedin_url=linkedin_url,
                status="connected",
                detail="Profile shows a connection/message signal.",
                screenshot_path=self._save_screenshot(page, "reconcile-connected"),
            )
        if self._find_connect_button(page, candidate_name=name) is not None:
            return LinkedInReconcileResult(
                contact_id=contact_id,
                name=name,
                linkedin_url=linkedin_url,
                status="not_connected",
                detail="Profile has an available Connect action.",
                screenshot_path=self._save_screenshot(page, "reconcile-not-connected"),
            )
        return LinkedInReconcileResult(
            contact_id=contact_id,
            name=name,
            linkedin_url=linkedin_url,
            status="unknown",
            detail="Profile did not expose a clear pending, connected, or connect signal.",
            screenshot_path=self._save_screenshot(page, "reconcile-unknown"),
        )

    def _validate_user_data_dir(self, path: Path) -> None:
        self.settings.validate_explicit_linkedin_profile()
        if not path.exists():
            raise FileNotFoundError(f"Chrome user data dir does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"Chrome user data dir is not a directory: {path}")

    def _validate_cdp_owner(self) -> None:
        debug_port = self.settings.linkedin_debug_port
        try:
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{debug_port}", "-sTCP:LISTEN"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Nothing is listening on 127.0.0.1:{debug_port}. "
                "Launch your signed-in Chrome with the configured remote debugging port first."
            ) from exc

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            raise RuntimeError(
                f"Nothing is listening on 127.0.0.1:{debug_port}. "
                "Launch your signed-in Chrome with the configured remote debugging port first."
            )

        parts = lines[1].split()
        if len(parts) < 2:
            raise RuntimeError(f"Could not parse CDP owner for port {debug_port}.")
        pid = parts[1]
        command = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if "--remote-debugging-port" not in command:
            raise RuntimeError(
                f"Chrome is listening on port {debug_port}, but the owning process does not look like a "
                "remote-debuggable Chrome launch."
            )
        expected_user_data_dir = str(self.settings.resolved_linkedin_user_data_dir)
        if f"--user-data-dir={expected_user_data_dir}" not in command and f'--user-data-dir="{expected_user_data_dir}"' not in command:
            raise RuntimeError(
                f"Chrome on port {debug_port} is not using the configured LinkedIn profile. "
                f"Expected --user-data-dir={expected_user_data_dir}."
            )

    def _goto(self, page: Page, url: str, steps: list[str], label: str) -> None:
        steps.append(f"Navigating to {label}: {url}")
        if not self._safe_goto(page, url, timeout_ms=20000):
            raise PlaywrightTimeoutError(f"Could not load {label}: {url}")
        page.wait_for_timeout(2000)
        steps.append(f"Arrived at {label}: {page.url}")

    def _send_single_invite(self, page: Page, candidate: dict, execute: bool) -> InviteSendResult:
        name = str(candidate.get("name") or "Unknown")
        linkedin_url = str(candidate.get("linkedin_url") or "")
        note = str(candidate.get("note") or "")
        search_url = str(candidate.get("_search_url") or "")
        if not linkedin_url:
            return InviteSendResult(
                name=name,
                linkedin_url="",
                status="skipped",
                detail="Missing LinkedIn URL",
                note=note,
            )

        if search_url and not execute:
            search_result = self._send_single_invite_from_search_results(
                page,
                candidate=candidate,
                search_url=search_url,
                execute=execute,
            )
            if search_result is not None:
                return search_result

        if not self._navigate_profile(page, linkedin_url):
            return InviteSendResult(
                name=name,
                linkedin_url=linkedin_url,
                status="navigation_error",
                detail="Could not load LinkedIn profile reliably.",
                note=note,
                screenshot_path=self._save_screenshot(page, "invite-navigation-error"),
            )
        page.evaluate("window.scrollTo(0, 0)")
        self._human_pause(page)

        connect_button = self._find_connect_button(page, candidate_name=name)
        if connect_button is not None:
            if not execute:
                return InviteSendResult(
                    name=name,
                    linkedin_url=linkedin_url,
                    status="dry_run_ready",
                    detail="Connect flow looks available; dry run only.",
                    note=note,
                    screenshot_path=self._save_screenshot(page, "invite-dry-run"),
                )

            try:
                invite_flow_ready = False
                for attempt in range(2):
                    active_connect = connect_button if attempt == 0 else self._find_connect_button(page, candidate_name=name)
                    if active_connect is None:
                        break
                    self._activate_connect(page, active_connect)
                    self._human_pause(page)
                    if self._is_wrong_invite_branch(page):
                        if not self._recover_profile_page(page, linkedin_url):
                            break
                        self._human_pause(page)
                        continue
                    if self._invite_flow_available(page, timeout_ms=3000):
                        invite_flow_ready = True
                        break
                if not invite_flow_ready and self._is_wrong_invite_branch(page):
                    raise PlaywrightTimeoutError("Connect flow opened a mutual-connections/search page instead of the invite flow.")
                if not self._invite_flow_available(page, timeout_ms=4000):
                    self._dismiss_transient_overlays(page)
                if not self._invite_flow_available(page, timeout_ms=2000):
                    if self._is_already_connected(page, candidate_name=name):
                        return InviteSendResult(
                            name=name,
                            linkedin_url=linkedin_url,
                            status="already_connected",
                            detail="Profile has an explicit connected or pending state.",
                            note=note,
                            screenshot_path=self._save_screenshot(page, "invite-already-connected"),
                        )
                    return InviteSendResult(
                        name=name,
                        linkedin_url=linkedin_url,
                        status="unavailable",
                        detail="Connect action did not open a usable invite flow.",
                        note=note,
                        screenshot_path=self._save_screenshot(page, "invite-no-connect"),
                    )
                note_supported = self._open_add_note(page)
                self._human_pause(page)
                note_sent = note
                if note_supported:
                    self._fill_invite_note(page, note)
                    self._human_pause(page)
                else:
                    note_sent = ""
                self._click_send_invite(page)
                self._human_pause(page)
                return InviteSendResult(
                    name=name,
                    linkedin_url=linkedin_url,
                    status="sent" if note_supported else "sent_without_note",
                    detail=(
                        "Invitation sent successfully."
                        if note_supported
                        else "Invitation sent without a note because LinkedIn did not expose the note field."
                    ),
                    note=note_sent,
                    screenshot_path=self._save_screenshot(page, "invite-sent"),
                )
            except PlaywrightError as exc:
                return InviteSendResult(
                    name=name,
                    linkedin_url=linkedin_url,
                    status="send_error",
                    detail=f"Connect flow failed: {exc}",
                    note=note,
                    screenshot_path=self._save_screenshot(page, "invite-send-error"),
                )

        if self._is_already_connected(page, candidate_name=name):
            return InviteSendResult(
                name=name,
                linkedin_url=linkedin_url,
                status="already_connected",
                detail="Profile has an explicit connected or pending state.",
                note=note,
                screenshot_path=self._save_screenshot(page, "invite-already-connected"),
            )

        return InviteSendResult(
            name=name,
            linkedin_url=linkedin_url,
            status="unavailable",
            detail="Could not find a Connect action on profile.",
            note=note,
            screenshot_path=self._save_screenshot(page, "invite-no-connect"),
        )

    def _send_single_invite_from_search_results(
        self,
        page: Page,
        *,
        candidate: dict,
        search_url: str,
        execute: bool,
    ) -> InviteSendResult | None:
        name = str(candidate.get("name") or "Unknown")
        linkedin_url = str(candidate.get("linkedin_url") or "")
        note = str(candidate.get("note") or "")
        candidate_name = str(candidate.get("name") or "")

        if not search_url or not self._safe_goto(page, search_url):
            return None
        self._human_pause(page)
        self._dismiss_transient_overlays(page)

        if not self._open_search_result_connect(page, linkedin_url=linkedin_url, candidate_name=candidate_name):
            return None

        if not execute:
            return InviteSendResult(
                name=name,
                linkedin_url=linkedin_url,
                status="dry_run_ready",
                detail="Search-result connect flow looks available; dry run only.",
                note=note,
                screenshot_path=self._save_screenshot(page, "invite-dry-run"),
            )

        try:
            self._human_pause(page)
            if self._is_mutuals_branch_url(page):
                return InviteSendResult(
                    name=name,
                    linkedin_url=linkedin_url,
                    status="send_error",
                    detail="Search-result connect fell into a mutual-connections branch.",
                    note=note,
                    screenshot_path=self._save_screenshot(page, "invite-send-error"),
                )
            if not self._invite_flow_available(page, timeout_ms=4000):
                self._dismiss_transient_overlays(page)
            if not self._invite_flow_available(page, timeout_ms=2000):
                return InviteSendResult(
                    name=name,
                    linkedin_url=linkedin_url,
                    status="unavailable",
                    detail="Search-result connect did not open a usable invite flow.",
                    note=note,
                    screenshot_path=self._save_screenshot(page, "invite-no-connect"),
                )
            note_supported = self._open_add_note(page)
            self._human_pause(page)
            note_sent = note
            if note_supported:
                self._fill_invite_note(page, note)
                self._human_pause(page)
            else:
                note_sent = ""
            self._click_send_invite(page)
            self._human_pause(page)
            return InviteSendResult(
                name=name,
                linkedin_url=linkedin_url,
                status="sent" if note_supported else "sent_without_note",
                detail=(
                    "Invitation sent successfully from search results."
                    if note_supported
                    else "Invitation sent from search results without a note because LinkedIn did not expose the note field."
                ),
                note=note_sent,
                screenshot_path=self._save_screenshot(page, "invite-sent"),
            )
        except PlaywrightError as exc:
            return InviteSendResult(
                name=name,
                linkedin_url=linkedin_url,
                status="send_error",
                detail=f"Search-result connect flow failed: {exc}",
                note=note,
                screenshot_path=self._save_screenshot(page, "invite-send-error"),
            )

    def _navigate_profile(self, page: Page, linkedin_url: str) -> bool:
        return self._safe_goto(page, linkedin_url)

    def _connect_over_cdp(self, playwright):
        endpoint = f"http://127.0.0.1:{self.settings.linkedin_debug_port}"
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                browser = playwright.chromium.connect_over_cdp(endpoint, timeout=30000)
                if browser.contexts:
                    return browser
                last_error = RuntimeError("Connected to Chrome, but no browser contexts were available.")
                browser.close()
            except PlaywrightError as exc:
                last_error = exc
            if attempt == 0:
                time.sleep(1.0)
        detail = f" Underlying error: {last_error}" if last_error else ""
        raise RuntimeError(
            f"Could not attach to Chrome debug session at {endpoint}. "
            "Launch your signed-in Chrome with the configured remote debugging port and keep it open."
            f"{detail}"
        )

    def _session_preflight(self, context, target_url: str = "https://www.linkedin.com/feed/") -> dict:
        page_count_before = len(context.pages)
        page = context.new_page()
        page.set_default_timeout(15000)
        try:
            self._safe_goto(page, target_url)
            cookies = context.cookies(["https://www.linkedin.com"])
            has_li_at = any(cookie.get("name") == "li_at" for cookie in cookies)
            logged_in = self._looks_logged_in(page)
            authwall = self._is_authwall_or_login(page)
            return {
                "ok": logged_in and not authwall,
                "current_url": page.url,
                "title": page.title(),
                "logged_in_heuristic": logged_in,
                "authwall_or_login": authwall,
                "has_li_at_cookie": has_li_at,
                "cookie_names": sorted(cookie.get("name", "") for cookie in cookies),
                "body_preview": self._body_preview(page),
                "context_pages_before": page_count_before,
            }
        finally:
            self._close_page_safely(page)

    def _safe_goto(self, page: Page, url: str, timeout_ms: int = 30000) -> bool:
        def _looks_loaded() -> bool:
            try:
                current_url = page.url.lower()
                if "linkedin.com/authwall" in current_url or "linkedin.com/login" in current_url:
                    return False
                page.wait_for_timeout(1200)
                return True
            except PlaywrightError:
                return False

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return _looks_loaded()
        except PlaywrightTimeoutError:
            try:
                page.goto(url, wait_until="commit", timeout=min(timeout_ms, 12000))
                if url.rstrip("/") in page.url.rstrip("/"):
                    return True
                return _looks_loaded()
            except PlaywrightError:
                return False
        except PlaywrightError:
            return False

    def _body_preview(self, page: Page) -> str:
        try:
            text = page.locator("body").inner_text(timeout=2000)
        except PlaywrightError:
            return ""
        return " ".join(text.split())[:400]

    def _is_authwall_or_login(self, page: Page) -> bool:
        current_url = page.url.lower()
        if "linkedin.com/authwall" in current_url or "linkedin.com/login" in current_url:
            return True
        preview = self._body_preview(page).lower()
        return any(
            token in preview
            for token in (
                "join linkedin",
                "sign in",
                "agree & join",
                "new to linkedin",
                "already on linkedin?",
            )
        )

    def _close_page_safely(self, page: Page | None) -> None:
        if page is None:
            return
        try:
            page.close()
        except PlaywrightError:
            pass

    def _close_run_spawned_pages(self, context, keep_pages: list[Page] | tuple[Page, ...]) -> None:
        keep_ids = {id(page) for page in keep_pages if page is not None}
        for page in list(context.pages):
            if id(page) in keep_ids:
                continue
            self._close_page_safely(page)

    def _open_search_result_connect(self, page: Page, *, linkedin_url: str, candidate_name: str) -> bool:
        normalized_target = self._normalize_linkedin_profile_url(linkedin_url)
        if not normalized_target:
            return False
        target_name = normalize_typeahead_text(candidate_name)
        for page_number in range(1, 4):
            if page_number > 1:
                target_url = self._set_people_search_page(page.url, page_number)
                if not self._safe_goto(page, target_url):
                    break
                self._human_pause(page)
            self._scroll_results(page)
            try:
                clicked = page.evaluate(
                    """
                    ({ targetUrl, targetName }) => {
                      const normalizeUrl = (value) => {
                        try {
                          const url = new URL(value, window.location.origin);
                          return `${url.origin}${url.pathname}`.replace(/\\/$/, '').toLowerCase();
                        } catch (_err) {
                          return (value || '').replace(/\\/$/, '').toLowerCase();
                        }
                      };
                      const normalizeText = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const target = normalizeUrl(targetUrl);
                      const nameTarget = normalizeText(targetName);
                      const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'));
                      for (const anchor of anchors) {
                        const anchorUrl = normalizeUrl(anchor.href);
                        const anchorText = normalizeText(anchor.textContent || '');
                        if (anchorUrl !== target && (!nameTarget || anchorText !== nameTarget)) continue;
                        const container = anchor.closest('li, article, div.entity-result, div[data-view-name], div.reusable-search__result-container') || anchor.parentElement;
                        if (!container) continue;
                        const buttons = Array.from(container.querySelectorAll('button, a'));
                        for (const button of buttons) {
                          const text = normalizeText(button.textContent || '');
                          const aria = normalizeText(button.getAttribute('aria-label') || '');
                          if (text === 'connect' || aria.includes('connect')) {
                            button.click();
                            return true;
                          }
                        }
                      }
                      return false;
                    }
                    """,
                    {"targetUrl": normalized_target, "targetName": target_name},
                )
                if clicked:
                    return True
            except Exception:
                continue
        return False

    def _activate_connect(self, page: Page, connect_button) -> None:
        href = None
        try:
            href = connect_button.get_attribute("href")
        except Exception:
            href = None
        if href and "/preload/custom-invite/" in href:
            for force in (False, True):
                try:
                    connect_button.click(timeout=5000, force=force)
                    self._human_pause(page)
                    if self._invite_flow_available(page, timeout_ms=1500):
                        return
                except Exception:
                    continue
            page.goto(urljoin("https://www.linkedin.com", href), wait_until="commit", timeout=15000)
            return

        for force in (False, True):
            try:
                connect_button.click(timeout=5000, force=force)
                return
            except Exception:
                continue

        try:
            connect_button.evaluate("(el) => el.click()")
            return
        except Exception as exc:
            raise PlaywrightTimeoutError(f"Could not activate Connect action: {exc}") from exc

    @contextmanager
    def _candidate_timeout(self, seconds: int):
        if seconds <= 0 or not hasattr(signal, "setitimer"):
            yield
            return

        def _raise_timeout(_signum, _frame):
            raise InviteCandidateTimeoutError(
                f"Invite candidate exceeded {seconds}s and was skipped."
            )

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

    def _is_wrong_invite_branch(self, page: Page) -> bool:
        current_url = (page.url or "").lower()
        return (
            "linkedin.com/search/results/people" in current_url
            or self._is_mutuals_branch_url(page)
        )

    def _is_mutuals_branch_url(self, page: Page) -> bool:
        current_url = (page.url or "").lower()
        return "member_profile_canned_search" in current_url or "connectionof=" in current_url

    def _normalize_linkedin_profile_url(self, url: str) -> str:
        try:
            parsed = urlparse(url)
        except Exception:
            return url.rstrip("/").lower()
        if not parsed.scheme or not parsed.netloc:
            return url.rstrip("/").lower()
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/").lower()

    def _recover_profile_page(self, page: Page, linkedin_url: str) -> bool:
        self._dismiss_transient_overlays(page)
        if not self._safe_goto(page, linkedin_url, timeout_ms=20000):
            return False
        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        return True

    def _scroll_results(self, page: Page) -> None:
        # Nudge LinkedIn to hydrate the first batch of people cards.
        for _ in range(3):
            page.mouse.wheel(0, 1800)
            self._human_pause(page)

    def _set_people_search_page(self, url: str, page_number: int) -> str:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if page_number <= 1:
            query.pop("page", None)
        else:
            query["page"] = str(page_number)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _collect_people_results(self, page: Page, limit: int, max_pages: int) -> list[RawSearchCandidate]:
        base_url = page.url
        deduped: dict[str, RawSearchCandidate] = {}

        for page_number in range(1, max_pages + 1):
            if page_number > 1:
                target_url = self._set_people_search_page(base_url, page_number)
                if not self._safe_goto(page, target_url):
                    break
                self._human_pause(page)
            self._scroll_results(page)
            for candidate in self._extract_visible_people(page, limit=limit):
                key = candidate.linkedin_url or f"{candidate.name}:{candidate.title or ''}"
                if key not in deduped:
                    deduped[key] = candidate
                if len(deduped) >= limit:
                    return list(deduped.values())[:limit]
        return list(deduped.values())[:limit]

    def _apply_people_filters(
        self,
        page: Page,
        company: str,
        school: str | None,
        connection_degree: str | None,
        use_us_location: bool,
    ) -> None:
        self._click_filter_control(page, "All filters")
        self._human_pause(page)

        if connection_degree in {"1st", "2nd", "3rd+"}:
            self._select_connection_degree(page, connection_degree)
            self._human_pause(page)

        if use_us_location:
            self._fill_filter_typeahead(page, "Add a location", "United States")

        self._fill_filter_typeahead(page, "Add a company", company)

        if school:
            self._fill_filter_typeahead(page, "Add a school", school)

        page.get_by_text("Show results", exact=True).click()
        self._human_pause(page)

    def _select_connection_degree(self, page: Page, connection_degree: str) -> None:
        label_pattern = re.compile(rf"^\s*{re.escape(connection_degree)}\s*$", re.IGNORECASE)
        candidates = [
            page.get_by_role("checkbox", name=label_pattern),
            page.get_by_role("radio", name=label_pattern),
            page.get_by_role("button", name=label_pattern),
            page.get_by_role("option", name=label_pattern),
            page.get_by_label(label_pattern),
            page.locator("label").filter(has_text=label_pattern),
            page.locator('[for]').filter(has_text=label_pattern),
            page.locator("span").filter(has_text=label_pattern),
            page.locator("p").filter(has_text=label_pattern),
        ]
        for locator in candidates:
            try:
                count = locator.count()
            except Exception:
                continue
            for idx in range(count):
                target = locator.nth(idx)
                try:
                    box = target.bounding_box()
                except Exception:
                    box = None
                if not box:
                    continue
                if box["width"] <= 0 or box["height"] <= 0:
                    continue
                try:
                    target.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    target.click(timeout=3000)
                    return
                except Exception:
                    try:
                        target.click(timeout=3000, force=True)
                        return
                    except Exception:
                        continue
        raise PlaywrightTimeoutError(f"Could not select connection degree filter: {connection_degree}")

    def _read_visible_filter_text(self, page: Page) -> list[str]:
        selectors = [
            'button',
            '[aria-pressed="true"]',
            '[data-test-pill-text]',
        ]
        collected: list[str] = []
        seen: set[str] = set()
        for selector in selectors:
            locator = page.locator(selector)
            count = min(locator.count(), 40)
            for idx in range(count):
                try:
                    text = locator.nth(idx).inner_text().strip().replace("\n", " ")
                except Exception:
                    continue
                if not text:
                    continue
                if text in seen:
                    continue
                seen.add(text)
                collected.append(text)
        return collected

    def _is_already_connected(self, page: Page, candidate_name: str | None = None) -> bool:
        try:
            if page.get_by_text("Pending", exact=True).count() > 0:
                return True
        except Exception:
            pass

        if self._has_primary_profile_connected_signal(page):
            return True
        return False

    def _profile_has_pending_signal(self, page: Page) -> bool:
        locators = [
            page.get_by_text("Pending", exact=True),
            page.get_by_role("button", name=re.compile("Pending", re.I)),
            page.locator('button[aria-label*="Pending" i]'),
            page.get_by_role("button", name=re.compile("Remove invitation", re.I)),
            page.locator('button[aria-label*="Remove invitation" i]'),
        ]
        return self._has_visible_profile_action(page, locators)

    def _profile_has_connected_signal(self, page: Page) -> bool:
        locators = [
            page.get_by_role("button", name=re.compile("^Message$", re.I)),
            page.get_by_role("link", name=re.compile("^Message$", re.I)),
            page.locator('button[aria-label*="Message" i]'),
            page.locator('a[aria-label*="Message" i]'),
            page.get_by_role("button", name=re.compile("Remove connection", re.I)),
            page.locator('button[aria-label*="Remove connection" i]'),
        ]
        return self._has_visible_profile_action(page, locators)

    def _has_visible_profile_action(self, page: Page, locators: list) -> bool:
        viewport_width = self._viewport_width(page)
        for locator in locators:
            try:
                count = locator.count()
            except Exception:
                continue
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    box = item.bounding_box()
                except Exception:
                    box = None
                if not box:
                    continue
                if box["y"] > 900:
                    continue
                if box["x"] > viewport_width * 0.86:
                    continue
                return True
        return False

    def _find_connect_button(self, page: Page, candidate_name: str | None = None):
        normalized_name = re.sub(r"\s+", " ", (candidate_name or "")).strip()
        name_tokens = self._candidate_name_tokens(normalized_name)

        profile_invite_links = page.locator('a[href*="/preload/custom-invite/"]')
        named_profile_links = [
            target
            for target in self._visible_action_targets(page, profile_invite_links, max_y=780, max_x_ratio=0.86)
            if self._connect_target_matches_candidate(target, name_tokens)
        ]
        if named_profile_links:
            return named_profile_links[0]
        if not name_tokens:
            visible_profile_links = self._visible_action_targets(page, profile_invite_links, max_y=620, max_x_ratio=0.72)
            if visible_profile_links:
                return visible_profile_links[0]

        toolbar_candidates = []
        if normalized_name:
            toolbar_candidates.extend(
                [
                    page.locator(f'[role="toolbar"] a[aria-label*="{normalized_name}"][aria-label*="connect" i]'),
                    page.locator(f'[role="toolbar"] button[aria-label*="{normalized_name}"][aria-label*="connect" i]'),
                ]
            )
        toolbar_candidates.extend(
            [
                page.locator('[role="toolbar"] a[aria-label*="connect" i]'),
                page.locator('[role="toolbar"] button[aria-label*="connect" i]'),
                page.get_by_role("toolbar").get_by_role("link", name=re.compile("connect", re.I)),
                page.get_by_role("toolbar").get_by_role("button", name=re.compile("connect", re.I)),
            ]
        )
        for locator in toolbar_candidates:
            visible = self._visible_action_targets(page, locator, max_y=620, max_x_ratio=0.66)
            if visible:
                return visible[0]

        candidates = [
            page.locator('a[aria-label*="connect" i]'),
            page.locator('button[aria-label*="connect" i]'),
            page.get_by_role("link", name=re.compile("connect", re.I)),
            page.get_by_role("button", name=re.compile("connect", re.I)),
            page.locator("a", has_text="Connect"),
            page.locator("button", has_text="Connect"),
        ]
        if normalized_name:
            candidates.insert(0, page.locator(f'a[aria-label*="{normalized_name}"][aria-label*="connect" i]'))
            candidates.insert(1, page.locator(f'button[aria-label*="{normalized_name}"][aria-label*="connect" i]'))
        preferred = []
        for locator in candidates:
            for button in self._visible_action_targets(page, locator, max_y=780, max_x_ratio=0.66):
                try:
                    if name_tokens and not self._connect_target_matches_candidate(button, name_tokens):
                        continue
                    box = button.bounding_box()
                    if not box:
                        continue
                    # Prefer the sticky/header action area over recommendation rails.
                    if box["y"] < 700:
                        preferred.append((box["y"], box["x"], button))
                except Exception:
                    continue
        if preferred:
            preferred.sort(key=lambda item: (item[0], item[1]))
            return preferred[0][2]
        for locator in candidates:
            visible = self._visible_action_targets(page, locator, max_y=780, max_x_ratio=0.66)
            if name_tokens:
                visible = [
                    target
                    for target in visible
                    if self._connect_target_matches_candidate(target, name_tokens)
                ]
            if visible:
                return visible[0]

        more_buttons = [
            page.get_by_role("button", name=re.compile("More", re.I)),
            page.get_by_role("button", name=re.compile("More actions", re.I)),
            page.locator("button", has_text="More"),
            page.locator('button[aria-label*="More" i]'),
            page.locator('button[aria-label*="actions" i]'),
        ]
        ranked_more: list = []
        for locator in more_buttons:
            ranked_more.extend(self._visible_action_targets(page, locator, max_y=620, max_x_ratio=0.66))
        for more in ranked_more:
            try:
                more.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            clicked = False
            for force in (False, True):
                try:
                    more.click(timeout=5000, force=force)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                continue
            self._human_pause(page)
            menu_candidates = [
                page.locator('[role="menu"] button', has_text="Connect"),
                page.locator('[role="menu"] [role="menuitem"]', has_text="Connect"),
                page.locator('[role="menu"] a', has_text="Connect"),
                page.get_by_role("menuitem", name="Connect"),
                page.get_by_role("button", name="Connect"),
                page.get_by_role("link", name="Connect"),
            ]
            for connect in menu_candidates:
                visible = self._visible_action_targets(page, connect, max_y=900, max_x_ratio=0.9)
                if visible:
                    return visible[0]
            self._dismiss_transient_overlays(page)
        return None

    def _candidate_name_tokens(self, candidate_name: str) -> list[str]:
        return [
            token
            for token in re.split(r"[^a-z0-9]+", candidate_name.lower())
            if len(token) >= 3
        ]

    def _connect_target_matches_candidate(self, target, name_tokens: list[str]) -> bool:
        if not name_tokens:
            return True
        haystack = self._action_target_text(target)
        if not haystack:
            return False
        if len(name_tokens) == 1:
            return name_tokens[0] in haystack
        if name_tokens[0] in haystack and name_tokens[-1] in haystack:
            return True
        matches = sum(1 for token in name_tokens if token in haystack)
        return matches >= min(2, len(name_tokens))

    def _action_target_text(self, target) -> str:
        pieces: list[str] = []
        for attribute in ("aria-label", "href", "data-control-name"):
            try:
                value = target.get_attribute(attribute)
            except Exception:
                value = None
            if value:
                pieces.append(str(value))
        for reader in ("inner_text", "text_content"):
            try:
                value = getattr(target, reader)(timeout=500)
            except TypeError:
                try:
                    value = getattr(target, reader)()
                except Exception:
                    value = None
            except Exception:
                value = None
            if value:
                pieces.append(str(value))
        return " ".join(" ".join(pieces).lower().split())

    def _has_primary_profile_connected_signal(self, page: Page) -> bool:
        viewport_width = self._viewport_width(page)
        locators = [
            page.get_by_text("Pending", exact=True),
            page.get_by_role("button", name=re.compile("Pending", re.I)),
            page.locator('button[aria-label*="Pending" i]'),
            page.get_by_role("button", name=re.compile("Remove invitation", re.I)),
            page.get_by_role("button", name=re.compile("Remove connection", re.I)),
            page.locator('button[aria-label*="Remove invitation" i]'),
            page.locator('button[aria-label*="Remove connection" i]'),
        ]
        for locator in locators:
            try:
                count = locator.count()
            except Exception:
                continue
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    box = item.bounding_box()
                except Exception:
                    box = None
                if not box:
                    continue
                if box["y"] > 900:
                    continue
                if box["x"] > viewport_width * 0.72:
                    continue
                return True
        return False

    def _viewport_width(self, page: Page) -> float:
        try:
            width = page.evaluate("() => window.innerWidth")
            return float(width or 1600)
        except Exception:
            return 1600.0

    def _visible_action_targets(
        self,
        page: Page,
        locator,
        *,
        max_y: float,
        max_x_ratio: float,
    ) -> list:
        ranked = []
        viewport_width = self._viewport_width(page)
        try:
            count = locator.count()
        except Exception:
            return ranked
        for idx in range(count):
            target = locator.nth(idx)
            try:
                box = target.bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            if box["width"] <= 0 or box["height"] <= 0:
                continue
            if box["y"] > max_y:
                continue
            if box["x"] > viewport_width * max_x_ratio:
                continue
            ranked.append((box["y"], box["x"], target))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [target for _, _, target in ranked]

    def _dismiss_transient_overlays(self, page: Page) -> None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            page.mouse.click(20, 20)
        except Exception:
            pass
        self._human_pause(page)

    def _invite_flow_available(self, page: Page, timeout_ms: int = 5000) -> bool:
        try:
            page.wait_for_function(
                """
                () => {
                  const textarea = Array.from(document.querySelectorAll('textarea')).find(
                    (el) => el.name !== 'g-recaptcha-response'
                      && el.offsetParent !== null
                      && el.closest('[role="dialog"]')
                  );
                  if (textarea) return true;
                  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter(
                    (el) => el.offsetParent !== null
                  );
                  return dialogs.some((dialog) => {
                    const text = (dialog.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (/Add a note to your invitation/i.test(text)) return true;
                    if (/personalize your invite/i.test(text)) return true;
                    const hasAddNote = Array.from(dialog.querySelectorAll('button')).some(
                      (el) => /Add a note/i.test((el.textContent || '').replace(/\\s+/g, ' ').trim())
                    );
                    return hasAddNote && /invitation|invite|connect/i.test(text);
                  });
                }
                """,
                timeout=timeout_ms,
            )
            return True
        except PlaywrightTimeoutError:
            return False

    def _invite_dialog(self, page: Page):
        return page.locator('[role="dialog"]').filter(
            has_text=re.compile(r"Add a note|invitation|invite|connect", re.I)
        )

    def _open_add_note(self, page: Page) -> bool:
        self._invite_flow_available(page, timeout_ms=5000)
        if self._invite_note_textarea(page).count() > 0:
            return True

        invite_dialog = self._invite_dialog(page)
        try:
            if invite_dialog.count() == 0:
                raise PlaywrightTimeoutError("Invite dialog is not available.")
        except PlaywrightError as exc:
            raise PlaywrightTimeoutError("Invite dialog is not available.") from exc

        add_note = [
            invite_dialog.get_by_role("button", name="Add a note"),
            invite_dialog.locator("button", has_text="Add a note"),
            invite_dialog.get_by_text("Add a note", exact=True),
        ]
        for locator in add_note:
            try:
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=5000, force=True)
                if self._invite_flow_available(page, timeout_ms=5000) and self._invite_note_textarea(page).count() > 0:
                    return True
            except Exception:
                continue
        try:
            clicked = page.evaluate(
                """
                () => {
                  const dialog = Array.from(document.querySelectorAll('[role="dialog"]')).find(
                    (el) => /Add a note to your invitation|personalize your invite|invitation|invite/i.test(el.textContent || '')
                  );
                  if (!dialog) return false;
                  const button = Array.from(dialog.querySelectorAll('button')).find(
                    (el) => (el.textContent || '').replace(/\\s+/g, ' ').trim() === 'Add a note'
                  );
                  if (!button) return false;
                  button.click();
                  return true;
                }
                """
            )
            if clicked:
                if self._invite_flow_available(page, timeout_ms=5000) and self._invite_note_textarea(page).count() > 0:
                    return True
        except Exception:
            pass
        # Some variants open the note box directly after Connect.
        if self._invite_note_textarea(page).count() > 0:
            return True
        send_button = invite_dialog.locator("button").filter(has_text=re.compile(r"^Send$", re.I))
        try:
            if send_button.count() > 0:
                return False
        except Exception:
            pass
        raise PlaywrightTimeoutError("Could not find 'Add a note' in invite modal.")

    def _fill_invite_note(self, page: Page, note: str) -> None:
        textarea = self._invite_note_textarea(page)
        if textarea.count() == 0:
            raise PlaywrightTimeoutError("Invite note textarea not available.")
        textarea.fill(note[:300])

    def _invite_note_textarea(self, page: Page):
        invite_dialog = self._invite_dialog(page)
        candidates = [
            invite_dialog.locator('textarea:not([name="g-recaptcha-response"])'),
        ]
        for locator in candidates:
            try:
                count = locator.count()
            except Exception:
                continue
            for idx in range(count):
                textarea = locator.nth(idx)
                try:
                    box = textarea.bounding_box()
                except Exception:
                    box = None
                if box and box["width"] > 50 and box["height"] > 20:
                    return textarea
        return page.locator('textarea[name="__no_visible_invite_note__"]')

    def _click_send_invite(self, page: Page) -> None:
        invite_dialog = self._invite_dialog(page)
        try:
            if invite_dialog.count() == 0:
                raise PlaywrightTimeoutError("Invite dialog is not available.")
        except PlaywrightError as exc:
            raise PlaywrightTimeoutError("Invite dialog is not available.") from exc
        candidates = [
            invite_dialog.get_by_role("button", name=re.compile(r"^Send$", re.I)),
            invite_dialog.locator("button").filter(has_text=re.compile(r"^Send$", re.I)),
        ]
        for locator in candidates:
            try:
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=5000, force=True)
                return
            except Exception:
                continue
        try:
            clicked = page.evaluate(
                """
                () => {
                  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter(
                    (el) => el.offsetParent !== null && /Add a note|invitation|invite|connect/i.test(el.textContent || '')
                  );
                  for (const dialog of dialogs) {
                    const button = Array.from(dialog.querySelectorAll('button')).find(
                      (el) => (el.textContent || '').replace(/\\s+/g, ' ').trim() === 'Send'
                    );
                    if (button) {
                      button.click();
                      return true;
                    }
                  }
                  return false;
                }
                """
            )
            if clicked:
                return
        except Exception:
            pass
        raise PlaywrightTimeoutError("Could not click Send in invite modal.")

    def _fill_filter_typeahead(self, page: Page, trigger_text: str, value: str) -> None:
        self._click_filter_control(page, trigger_text)
        self._human_pause(page)

        active_input = page.locator('input[aria-label*="Add"], input[placeholder*="Add"]').last
        active_input.fill(value)
        self._human_pause(page)

        options = page.get_by_role("option")
        requested = normalize_typeahead_text(value)
        exact_option = None
        best_option = None
        best_score = -10_000
        try:
            option_count = min(options.count(), 20)
        except Exception:
            option_count = 0
        for index in range(option_count):
            option = options.nth(index)
            try:
                text = option.inner_text().strip()
            except Exception:
                continue
            normalized = (
                primary_typeahead_label(text)
                if normalize_typeahead_text(trigger_text) == "add a company"
                else normalize_typeahead_text(text)
            )
            if normalized == requested:
                exact_option = option
                break
            score = score_typeahead_option(trigger_text, value, text)
            if score > best_score:
                best_score = score
                best_option = option

        if exact_option is not None:
            exact_option.click()
        elif normalize_typeahead_text(trigger_text) == "add a company":
            raise PlaywrightTimeoutError(
                f"Could not find an exact company suggestion for '{value}'."
            )
        elif best_option is not None and best_score > 0:
            best_option.click()
        else:
            page.keyboard.press("Enter")
        self._human_pause(page)

    def _extract_visible_people(self, page: Page, limit: int) -> list[RawSearchCandidate]:
        script = """
        (limit) => {
          const normalize = (value) => value ? value.replace(/\\s+/g, ' ').trim() : null;
          const cleanName = (value) => {
            const normalized = normalize(value);
            return normalized
              ? normalized
                  .replace(/\\s*[·•]\\s*(1st|2nd|3rd\\+?)$/i, '')
                  .replace(/\\s*[·•]\\s*\\S+\\s*$/i, '')
                  .trim()
              : null;
          };
          const cards = Array.from(document.querySelectorAll('a[href*="/in/"]'));
          const results = [];
          const seen = new Set();

          for (const card of cards) {
            const href = card.href || null;
            if (!href || seen.has(href)) continue;

            const lines = (card.innerText || "")
              .split("\\n")
              .map((line) => normalize(line))
              .filter(Boolean);
            if (lines.length < 3) continue;
            if (!/(1st|2nd|3rd\\+?)/i.test(lines.join(" "))) continue;
            if (!lines.some((line) =>
              /Current:|Past:|mutual connection|United States|India|Canada|Area|California|Washington|New York|Texas|Massachusetts/i.test(line)
            )) continue;

            seen.add(href);

            const nameLine = normalize(lines[0]);
            const name = cleanName(nameLine);
            if (!name) continue;

            const meaningful = lines.filter((line) => line !== "Connect" && line !== "Follow");
            const titleLine = meaningful.find((line) =>
              line !== name &&
              !/(1st|2nd|3rd\\+?)$/i.test(line) &&
              line !== "Message" &&
              line !== "Connect" &&
              line !== "Follow"
            ) || null;
            const connectionLine = meaningful.find((line) => /(1st|2nd|3rd\\+?)/i.test(line)) || nameLine || null;
            const connectionDegreeMatch = connectionLine && connectionLine.match(/(1st|2nd|3rd\\+?)/i);
            const connectionDegree = connectionDegreeMatch ? connectionDegreeMatch[1] : null;
            const title = titleLine;
            const location = meaningful.find((line, idx) =>
              idx >= 0 &&
              line !== name &&
              line !== title &&
              !line.startsWith("Current:") &&
              !line.startsWith("Past:") &&
              !line.startsWith("Skills:") &&
              !line.includes("followers") &&
              !line.includes("mutual connection") &&
              /,|Area|Division|States|India|United Kingdom|Canada|Germany|Australia/.test(line)
            ) || null;
            const snippet = meaningful.find((line) =>
              line.startsWith("Current:") ||
              line.startsWith("Past:") ||
              line.startsWith("Skills:") ||
              line.includes("mutual connection")
            ) || null;

            results.push({
              name,
              title,
              subtitle: nameLine,
              connection_degree: connectionDegree,
              location,
              linkedin_url: href,
              snippet,
              raw_text: normalize(card.innerText || ""),
            });

            if (results.length >= limit) break;
          }
          return results;
        }
        """
        raw_results = page.evaluate(script, limit)
        return [RawSearchCandidate.model_validate(item) for item in raw_results]

    def _looks_logged_in(self, page: Page) -> bool:
        if "login" in page.url or "checkpoint" in page.url:
            return False

        selectors = [
            '[data-test-global-nav-link="feed"]',
            'input[placeholder*="Search"]',
            'a[href*="/mynetwork/"]',
        ]
        for selector in selectors:
            if page.locator(selector).count() > 0:
                return True
        return False

    def _save_screenshot(self, page: Page, label: str) -> str:
        screenshots_dir = self.settings.artifacts_dir / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        target = screenshots_dir / f"{artifact_timestamp()}-{label}.png"
        try:
            page.screenshot(path=str(target), full_page=True, timeout=5000)
            return str(target)
        except Exception:
            return ""

    def _human_pause(self, page: Page) -> None:
        delay = random.randint(
            self.settings.search.action_delay_min_ms,
            self.settings.search.action_delay_max_ms,
        )
        page.wait_for_timeout(delay)

    def _click_filter_control(self, page: Page, text: str) -> None:
        page.wait_for_load_state("domcontentloaded")
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(750)
        button_pattern = re.compile(rf"^\s*{re.escape(text)}\s*$", re.IGNORECASE)
        for _ in range(4):
            candidates = [
                page.get_by_role("button", name=button_pattern),
                page.locator("button").filter(has_text=button_pattern),
                page.locator(f"text={text}").locator("xpath=ancestor::button[1]"),
                page.get_by_text(text, exact=True),
                page.locator("[role='button']").filter(has_text=button_pattern),
            ]
            for locator in candidates:
                try:
                    if locator.count() == 0:
                        continue
                    target = locator.first
                    target.scroll_into_view_if_needed(timeout=2000)
                    try:
                        target.click(timeout=2500)
                    except Exception:
                        target.click(timeout=2500, force=True)
                    return
                except Exception:
                    continue
            try:
                clicked = page.evaluate(
                    """
                    (targetText) => {
                      const normalizedTarget = (targetText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const elements = Array.from(document.querySelectorAll('button, [role="button"], span, div, a'));
                      for (const element of elements) {
                        const text = (element.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const aria = (element.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const title = (element.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        if (![text, aria, title].includes(normalizedTarget)) continue;
                        const clickable = element.closest('button, [role="button"], a') || element;
                        clickable.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                      }
                      return false;
                    }
                    """,
                    text,
                )
                if clicked:
                    return
            except Exception:
                pass
            page.wait_for_timeout(1000)
        raise PlaywrightTimeoutError(f"Could not click filter control: {text}")

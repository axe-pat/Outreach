from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path
import random
import re
import time
from urllib.parse import quote_plus, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Page, sync_playwright

from outreach.artifacts import artifact_timestamp
from outreach.config import OutreachSettings
from outreach.models import RawSearchCandidate


def normalize_typeahead_text(value: str) -> str:
    return " ".join((value or "").lower().split()).strip()


def score_typeahead_option(trigger_text: str, requested_value: str, option_text: str) -> int:
    trigger = normalize_typeahead_text(trigger_text)
    requested = normalize_typeahead_text(requested_value)
    option = normalize_typeahead_text(option_text)
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


class LinkedInScraper:
    """Browser-native LinkedIn automation."""

    def __init__(self, settings: OutreachSettings) -> None:
        self.settings = settings

    def search_company(self, company: str) -> list[dict]:
        raise NotImplementedError("Implement browser session automation in Phase 1.")

    def extract_company_people_live(self, company: str, limit: int = 10) -> list[RawSearchCandidate]:
        return self.extract_people_live(search_query=company, limit=limit)

    def extract_people_live(self, search_query: str, limit: int = 10) -> list[RawSearchCandidate]:
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
                    self._scroll_results(page)
                    return self._extract_visible_people(page, limit=limit)
                finally:
                    self._close_page_safely(page)
            finally:
                browser.close()

    def extract_people_with_filters_live(
        self,
        company: str,
        search_query: str,
        limit: int = 10,
        school: str | None = None,
        connection_degree: str | None = None,
        use_us_location: bool = True,
    ) -> FilterRunResult:
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
                    self._scroll_results(page)
                    candidates = self._extract_visible_people(page, limit=limit)
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
    ) -> list[InviteSendResult]:
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
                page = context.new_page()
                page.set_default_timeout(15000)
                try:
                    results: list[InviteSendResult] = []
                    for candidate in candidates:
                        results.append(self._send_single_invite(page, candidate, execute=execute))
                    return results
                finally:
                    self._close_page_safely(page)
            finally:
                browser.close()

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
        if not linkedin_url:
            return InviteSendResult(
                name=name,
                linkedin_url="",
                status="skipped",
                detail="Missing LinkedIn URL",
                note=note,
            )

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
                href = None
                try:
                    href = connect_button.get_attribute("href")
                except Exception:
                    href = None
                if href and "/preload/custom-invite/" in href:
                    page.goto(urljoin("https://www.linkedin.com", href), wait_until="domcontentloaded", timeout=30000)
                else:
                    connect_button.click(force=True, timeout=5000)
                self._human_pause(page)
                self._open_add_note(page)
                self._human_pause(page)
                self._fill_invite_note(page, note)
                self._human_pause(page)
                self._click_send_invite(page)
                self._human_pause(page)
                return InviteSendResult(
                    name=name,
                    linkedin_url=linkedin_url,
                    status="sent",
                    detail="Invitation sent successfully.",
                    note=note,
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

    def _scroll_results(self, page: Page) -> None:
        # Nudge LinkedIn to hydrate the first batch of people cards.
        for _ in range(3):
            page.mouse.wheel(0, 1800)
            self._human_pause(page)

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
            page.get_by_text(connection_degree, exact=True).last.click()
            self._human_pause(page)

        if use_us_location:
            self._fill_filter_typeahead(page, "Add a location", "United States")

        self._fill_filter_typeahead(page, "Add a company", company)

        if school:
            self._fill_filter_typeahead(page, "Add a school", school)

        page.get_by_text("Show results", exact=True).click()
        self._human_pause(page)

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

    def _find_connect_button(self, page: Page, candidate_name: str | None = None):
        normalized_name = re.sub(r"\s+", " ", (candidate_name or "")).strip()

        def _visible_actions(locator):
            ranked = []
            viewport_width = self._viewport_width(page)
            try:
                count = locator.count()
            except Exception:
                return ranked
            for idx in range(count):
                button = locator.nth(idx)
                try:
                    box = button.bounding_box()
                    if not box:
                        continue
                    if box["y"] > 950:
                        continue
                    if box["x"] > viewport_width * 0.72:
                        continue
                    ranked.append((box["y"], box["x"], button))
                except Exception:
                    continue
            ranked.sort(key=lambda item: (item[0], item[1]))
            return [button for _, _, button in ranked]

        toolbar_candidates = []
        if normalized_name:
            escaped_name = re.escape(normalized_name)
            toolbar_candidates.extend(
                [
                    page.locator(f'[role="toolbar"] a[aria-label*="{normalized_name}"][aria-label*="connect" i]'),
                    page.locator(f'[role="toolbar"] button[aria-label*="{normalized_name}"][aria-label*="connect" i]'),
                    page.get_by_role("toolbar").get_by_role("link", name=re.compile(escaped_name, re.I)),
                    page.get_by_role("toolbar").get_by_role("button", name=re.compile(escaped_name, re.I)),
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
            visible = _visible_actions(locator)
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
            for button in _visible_actions(locator):
                try:
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
            visible = _visible_actions(locator)
            if visible:
                return visible[0]

        more_buttons = [
            page.get_by_role("button", name=re.compile("More", re.I)),
            page.get_by_role("button", name=re.compile("More actions", re.I)),
            page.locator("button", has_text="More"),
            page.locator('button[aria-label*="More" i]'),
            page.locator('button[aria-label*="actions" i]'),
        ]
        for more in more_buttons:
            try:
                if more.count() == 0:
                    continue
                more.first.click(timeout=5000)
                self._human_pause(page)
                menu_candidates = [
                    page.get_by_role("button", name="Connect"),
                    page.get_by_role("menuitem", name="Connect"),
                    page.get_by_role("link", name="Connect"),
                    page.locator('[role="menu"] button', has_text="Connect"),
                    page.locator('[role="menu"] [role="menuitem"]', has_text="Connect"),
                    page.get_by_text("Connect", exact=True),
                ]
                for connect in menu_candidates:
                    try:
                        if connect.count() > 0:
                            return connect.first
                    except Exception:
                        continue
            except Exception:
                continue
        return None

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

    def _open_add_note(self, page: Page) -> None:
        try:
            page.wait_for_function(
                """
                () => {
                  return !!Array.from(document.querySelectorAll('textarea')).find(
                    (el) => el.name !== 'g-recaptcha-response' && el.offsetParent !== null
                  )
                    || !!Array.from(document.querySelectorAll('[role="dialog"], div')).find(
                      (el) => /Add a note to your invitation\\?/i.test((el.textContent || '').replace(/\\s+/g, ' ').trim())
                    )
                    || !!Array.from(document.querySelectorAll('button')).find(
                      (el) => /Add a note/i.test((el.textContent || '').replace(/\\s+/g, ' ').trim())
                    );
                }
                """,
                timeout=5000,
            )
        except PlaywrightTimeoutError:
            pass

        if self._invite_note_textarea(page).count() > 0:
            return

        add_note = [
            page.get_by_role("button", name="Add a note"),
            page.locator("button", has_text="Add a note"),
            page.get_by_text("Add a note", exact=True),
            page.locator('[role="dialog"] button').filter(has_text="Add a note"),
        ]
        for locator in add_note:
            try:
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=5000, force=True)
                page.wait_for_function(
                    """
                    () => !!Array.from(document.querySelectorAll('textarea')).find(
                      (el) => el.name !== 'g-recaptcha-response' && el.offsetParent !== null
                    )
                    """,
                    timeout=5000,
                )
                return
            except Exception:
                continue
        try:
            clicked = page.evaluate(
                """
                () => {
                  const dialog = Array.from(document.querySelectorAll('[role="dialog"], div')).find(
                    (el) => (el.textContent || '').includes('Add a note to your invitation')
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
                page.wait_for_function(
                    """
                    () => !!Array.from(document.querySelectorAll('textarea')).find(
                      (el) => el.name !== 'g-recaptcha-response' && el.offsetParent !== null
                    )
                    """,
                    timeout=5000,
                )
                return
        except Exception:
            pass
        # Some variants open the note box directly after Connect.
        if self._invite_note_textarea(page).count() > 0:
            return
        raise PlaywrightTimeoutError("Could not find 'Add a note' in invite modal.")

    def _fill_invite_note(self, page: Page, note: str) -> None:
        textarea = self._invite_note_textarea(page)
        if textarea.count() == 0:
            raise PlaywrightTimeoutError("Invite note textarea not available.")
        textarea.fill(note[:300])

    def _invite_note_textarea(self, page: Page):
        candidates = [
            page.locator('[role="dialog"] textarea:not([name="g-recaptcha-response"])'),
            page.locator('textarea[placeholder]:not([name="g-recaptcha-response"])'),
            page.locator('textarea:not([name="g-recaptcha-response"])'),
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
        candidates = [
            page.get_by_role("button", name="Send"),
            page.locator("button", has_text="Send"),
        ]
        for locator in candidates:
            try:
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=5000)
                return
            except Exception:
                continue
        raise PlaywrightTimeoutError("Could not click Send in invite modal.")

    def _fill_filter_typeahead(self, page: Page, trigger_text: str, value: str) -> None:
        self._click_filter_control(page, trigger_text)
        self._human_pause(page)

        active_input = page.locator('input[aria-label*="Add"], input[placeholder*="Add"]').last
        active_input.fill(value)
        self._human_pause(page)

        options = page.get_by_role("option")
        best_option = None
        best_score = -10_000
        try:
            option_count = min(options.count(), 12)
        except Exception:
            option_count = 0
        for index in range(option_count):
            option = options.nth(index)
            try:
                text = option.inner_text().strip()
            except Exception:
                continue
            score = score_typeahead_option(trigger_text, value, text)
            if score > best_score:
                best_score = score
                best_option = option

        if best_option is not None and best_score > 0:
            best_option.click()
        elif trigger_text == "Add a company":
            raise PlaywrightTimeoutError(
                f"Could not confidently match a company suggestion for '{value}'."
            )
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

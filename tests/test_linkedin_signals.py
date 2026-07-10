from __future__ import annotations

import csv
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import outreach.linkedin_signals as linkedin_signals_module
from outreach.config import OutreachSettings
from outreach.linkedin_signals import (
    CaptureLimits,
    FeedPost,
    FeedReviewDisposition,
    FeedSignalKind,
    FeedSignalStore,
    ProfileViewerObservation,
    ProfileViewerStore,
    ViewerRelevance,
    canonical_linkedin_url,
    capture_feed_posts,
    capture_linkedin_signals_live,
    capture_profile_viewers,
    classify_feed_post,
    parse_feed_rows,
    parse_profile_viewer_rows,
    normalize_extracted_company,
)


class _FakePage:
    def __init__(self, snapshots: list[list[dict[str, str]]]) -> None:
        self.snapshots = snapshots
        self.scroll_index = 0
        self.gotos: list[tuple[str, dict[str, object]]] = []
        self.waits: list[float] = []
        self.default_timeout: float | None = None
        self.closed = False

    def goto(self, url: str, **kwargs: object) -> None:
        self.gotos.append((url, kwargs))

    def evaluate(self, expression: str, arg: object = None):
        if expression.startswith("window.scrollBy"):
            self.scroll_index += 1
            return None
        index = min(self.scroll_index, len(self.snapshots) - 1)
        return self.snapshots[index]

    def wait_for_timeout(self, timeout: float) -> None:
        self.waits.append(timeout)

    def set_default_timeout(self, timeout: float) -> None:
        self.default_timeout = timeout

    def close(self) -> None:
        self.closed = True


class _TickClock:
    def __init__(self, ticks: list[float]) -> None:
        self.ticks = iter(ticks)

    def __call__(self) -> float:
        return next(self.ticks)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _post(
    *,
    url: str = "https://www.linkedin.com/feed/update/urn:li:activity:123/",
    company: str = "Signal Labs",
    text: str = "We are hiring for our product team after raising our seed round.",
) -> FeedPost:
    return FeedPost(
        post_url=url,
        author_name="Avery Founder",
        author_url="https://www.linkedin.com/in/avery/",
        company=company,
        company_url="https://www.linkedin.com/company/signal-labs/",
        text=text,
        context="A USC connection commented on this",
        posted_at_text="2h",
    )


def test_canonical_linkedin_url_removes_tracking_and_normalizes_slash() -> None:
    assert canonical_linkedin_url(
        "https://www.linkedin.com/feed/update/urn:li:activity:123?trk=feed&trackingId=abc"
    ) == "https://linkedin.com/feed/update/urn:li:activity:123/"


def test_parse_feed_rows_preserves_source_context_and_skips_empty_rows() -> None:
    posts = parse_feed_rows(
        [
            {
                "post_url": "/feed/update/urn:li:activity:123?trk=feed",
                "author_name": "  Avery   Founder ",
                "author_url": "/in/avery?trk=feed",
                "company": " Signal Labs ",
                "company_url": "/company/signal-labs",
                "text": " We launched  a product. ",
                "context": " Your connection commented ",
                "posted_at_text": " 2h ",
            },
            {"text": "", "post_url": ""},
        ]
    )

    assert len(posts) == 1
    assert posts[0].post_url == "https://linkedin.com/feed/update/urn:li:activity:123/"
    assert posts[0].author_name == "Avery Founder"
    assert posts[0].company == "Signal Labs"
    assert posts[0].text == "We launched a product."
    assert posts[0].context == "Your connection commented"


def test_greedy_company_inference_is_trimmed_to_the_clear_post_mention() -> None:
    assert normalize_extracted_company(
        "PrimeIntellect. Nine months later",
        "In Oct, I joined @PrimeIntellect. Nine months later, we reached a milestone.",
    ) == "PrimeIntellect"
    assert normalize_extracted_company(
        "Apollo.io",
        "Apollo.io launched a new product.",
    ) == "Apollo.io"


def test_classification_covers_company_startup_hiring_funding_launch_and_network() -> None:
    post = _post(
        text=(
            "Our startup is hiring for an open role after our seed round. "
            "We launched our new product today."
        )
    )

    result = classify_feed_post(post, known_companies={"Another Co"})

    assert result.relevance == "high"
    assert set(result.signal_kinds) >= {
        FeedSignalKind.COMPANY_DISCOVERY,
        FeedSignalKind.STARTUP_DISCOVERY,
        FeedSignalKind.JOB,
        FeedSignalKind.HIRING,
        FeedSignalKind.FUNDING,
        FeedSignalKind.LAUNCH,
        FeedSignalKind.WARM_NETWORK,
    }


def test_known_company_is_signal_but_not_mislabeled_as_company_discovery() -> None:
    result = classify_feed_post(_post(), known_companies={"signal labs"})

    assert FeedSignalKind.COMPANY_DISCOVERY not in result.signal_kinds
    assert FeedSignalKind.HIRING in result.signal_kinds
    assert FeedSignalKind.FUNDING in result.signal_kinds


def test_unclassified_algorithmic_feed_post_stays_available_for_human_review() -> None:
    post = FeedPost(
        post_url="https://www.linkedin.com/feed/update/urn:li:activity:999/",
        author_name="Avery",
        author_url="https://www.linkedin.com/in/avery/",
        company="",
        company_url="",
        text="A thoughtful post whose relevance needs judgment.",
    )
    result = classify_feed_post(
        post,
        relevance_keywords=(),
    )

    assert result.signal_kinds == (FeedSignalKind.OTHER,)
    assert result.relevance == "review"
    assert "human" in result.reason


def test_capture_feed_scrolls_with_configurable_limits_and_dedupes() -> None:
    first = {
        "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
        "author_name": "One",
        "text": "We are hiring",
    }
    second = {
        "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:2/",
        "author_name": "Two",
        "text": "We launched",
    }
    page = _FakePage([[first], [first, second]])

    posts = capture_feed_posts(
        page,
        limits=CaptureLimits(
            max_scrolls=1,
            max_duration_seconds=None,
            scroll_pause_ms=17,
            max_items=10,
        ),
    )

    assert len(posts) == 2
    assert page.gotos[0][0] == "https://www.linkedin.com/feed/"
    assert page.gotos[0][1]["timeout"] == 30_000
    assert page.waits == [17]


def test_capture_feed_duration_can_be_tuned_without_a_fixed_sixty_second_budget() -> None:
    page = _FakePage(
        [[{"post_url": "/feed/update/urn:li:activity:1/", "text": "first"}], []]
    )

    posts = capture_feed_posts(
        page,
        limits=CaptureLimits(max_scrolls=20, max_duration_seconds=0.25),
        monotonic=_TickClock([10.0, 10.5]),
    )

    assert len(posts) == 1
    assert page.waits == []


def test_capture_limits_validate_caller_supplied_bounds() -> None:
    with pytest.raises(ValueError, match="max_scrolls"):
        CaptureLimits(max_scrolls=-1)
    with pytest.raises(ValueError, match="max_duration"):
        CaptureLimits(max_duration_seconds=0)


def test_feed_store_dedupes_capture_tracks_history_and_supports_review(tmp_path: Path) -> None:
    path = tmp_path / "linkedin_feed_signals.csv"
    store = FeedSignalStore(path)
    post = _post()

    first = store.upsert_posts(
        [post, post], observed_at="2026-07-10T08:00:00+00:00", known_companies=[]
    )
    repeated_same_snapshot = store.upsert_posts(
        [post], observed_at="2026-07-10T08:00:00+00:00", known_companies=[]
    )
    next_day = store.upsert_posts(
        [post], observed_at="2026-07-11T08:00:00+00:00", known_companies=[]
    )

    assert first["captured"] == 2
    assert first["duplicates_in_capture"] == 1
    assert first["added"] == 1
    assert len(first["captured_signal_ids"]) == 1
    assert first["captured_signal_ids"][0].startswith("li-feed-")
    assert first["post_url_count"] == 1
    assert first["post_url_missing"] == 0
    assert first["run_signal_kind_counts"] == {
        "company_discovery": 1,
        "funding": 1,
        "hiring": 1,
        "warm_network": 1,
    }
    assert repeated_same_snapshot["updated"] == 1
    assert next_day["updated"] == 1
    row = _read_rows(path)[0]
    assert row["observed_snapshots"] == "2"
    assert json.loads(row["observation_history_json"]) == [
        "2026-07-10T08:00:00+00:00",
        "2026-07-11T08:00:00+00:00",
    ]
    assert row["review_disposition"] == "pending"
    assert row["post_text"] == post.text
    assert row["context"] == post.context

    reviewed = store.review(
        row["signal_id"],
        FeedReviewDisposition.COMPANY_CANDIDATE,
        note="Research this startup before promotion.",
        reviewed_at="2026-07-11T09:00:00+00:00",
    )
    assert reviewed["review_disposition"] == "company_candidate"
    assert store.pending_review() == []

    store.upsert_posts([post], observed_at="2026-07-12T08:00:00+00:00")
    assert _read_rows(path)[0]["review_disposition"] == "company_candidate"


def test_feed_review_rejects_unknown_disposition_and_signal(tmp_path: Path) -> None:
    store = FeedSignalStore(tmp_path / "signals.csv")

    with pytest.raises(ValueError):
        store.review("missing", "send_message")
    with pytest.raises(KeyError):
        store.review("missing", FeedReviewDisposition.KEEP)


def test_profile_viewer_parser_preserves_context_and_infers_company() -> None:
    observations = parse_profile_viewer_rows(
        [
            {
                "profile_url": "/in/taylor?trk=profile-viewer",
                "name": "Taylor Recruiter",
                "headline": "Talent Partner at Signal Labs | AI",
                "context": "Viewed your profile • 3h",
            }
        ]
    )

    assert len(observations) == 1
    assert observations[0].profile_url == "https://linkedin.com/in/taylor/"
    assert observations[0].company == "Signal Labs"
    assert observations[0].context == "Viewed your profile • 3h"


def test_capture_profile_viewers_uses_injectable_page_and_limits() -> None:
    row = {
        "profile_url": "/in/taylor",
        "name": "Taylor",
        "headline": "Founder at Signal Labs",
    }
    page = _FakePage([[row], [row]])

    observations = capture_profile_viewers(
        page,
        limits=CaptureLimits(max_scrolls=1, max_duration_seconds=None, scroll_pause_ms=21),
    )

    assert len(observations) == 1
    assert page.gotos[0][0] == "https://www.linkedin.com/analytics/profile-views/"
    assert page.waits == [21]


def test_profile_viewer_store_is_passive_deduped_history_with_relevance(tmp_path: Path) -> None:
    path = tmp_path / "linkedin_profile_viewers.csv"
    store = ProfileViewerStore(path)
    viewer = ProfileViewerObservation(
        profile_url="https://www.linkedin.com/in/taylor/",
        name="Taylor Recruiter",
        headline="Talent Partner at Signal Labs",
        company="Signal Labs",
        context="Viewed your profile",
    )

    first = store.upsert_observations(
        [viewer, viewer],
        observed_at="2026-07-10T08:00:00+00:00",
        target_companies=["Signal Labs"],
    )
    store.upsert_observations(
        [viewer],
        observed_at="2026-07-17T08:00:00+00:00",
        target_companies=["Signal Labs"],
    )

    assert first["duplicates_in_capture"] == 1
    assert first["workspace_passive_records"] == 1
    assert first["run_relevance_counts"] == {"target_company": 1}
    row = _read_rows(path)[0]
    assert row["relevance"] == "target_company"
    assert row["observed_snapshots"] == "2"
    assert json.loads(row["observation_history_json"]) == [
        "2026-07-10T08:00:00+00:00",
        "2026-07-17T08:00:00+00:00",
    ]
    assert row["passive_context_only"] == "true"
    assert not any("action" in field or "message" in field for field in row)


def test_manual_viewer_relevance_annotation_survives_later_capture(tmp_path: Path) -> None:
    path = tmp_path / "viewers.csv"
    store = ProfileViewerStore(path)
    viewer = ProfileViewerObservation(
        profile_url="https://www.linkedin.com/in/taylor/",
        name="Taylor",
        headline="Engineer at Somewhere",
    )
    store.upsert_observations([viewer], observed_at="2026-07-10T08:00:00+00:00")
    viewer_id = _read_rows(path)[0]["viewer_id"]

    annotated = store.annotate_relevance(
        viewer_id,
        ViewerRelevance.KNOWN_CONTEXT,
        reason="Met through USC; useful context only.",
        annotated_at="2026-07-10T09:00:00+00:00",
    )
    store.upsert_observations(
        [viewer],
        observed_at="2026-07-17T08:00:00+00:00",
        target_companies=["Somewhere"],
    )

    row = _read_rows(path)[0]
    assert annotated["passive_context_only"] == "true"
    assert row["relevance"] == "known_context"
    assert row["annotation_source"] == "manual"
    assert row["relevance_reason"] == "Met through USC; useful context only."


def test_live_wrapper_uses_existing_cdp_and_keeps_viewers_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed_page = _FakePage(
        [
            [
                {
                    "post_url": "/feed/update/urn:li:activity:123/",
                    "author_name": "Avery",
                    "company": "Signal Labs",
                    "text": "Our startup is hiring for an open role.",
                }
            ]
        ]
    )

    class _Context:
        def __init__(self) -> None:
            self.pages_to_open = [feed_page]

        def new_page(self) -> _FakePage:
            return self.pages_to_open.pop(0)

    class _Browser:
        def __init__(self) -> None:
            self.contexts = [_Context()]
            self.closed = False

        def close(self) -> None:
            self.closed = True

    browser = _Browser()

    class _Scraper:
        def __init__(self, _settings: OutreachSettings) -> None:
            pass

        def require_live_cdp_session(self) -> None:
            return None

        def _connect_over_cdp(self, _playwright: object) -> _Browser:
            return browser

        def _session_preflight(self, _context: object) -> dict[str, object]:
            return {"ok": True}

        def _close_page_safely(self, page: _FakePage) -> None:
            page.close()

    @contextmanager
    def _playwright_context():
        yield object()

    monkeypatch.setattr(linkedin_signals_module, "LinkedInScraper", _Scraper)
    monkeypatch.setattr(linkedin_signals_module, "sync_playwright", _playwright_context)

    result = capture_linkedin_signals_live(
        OutreachSettings(),
        feed_path=tmp_path / "feed.csv",
        feed_limits=CaptureLimits(max_scrolls=0),
        observed_at="2026-07-10T08:00:00+00:00",
    )

    assert result["status"] == "completed"
    assert result["read_only"] is True
    assert result["feed"]["status"] == "completed"
    assert result["feed"]["captured"] == 1
    assert result["profile_viewers"] == {
        "status": "skipped",
        "reason": "not_scheduled_for_this_run",
        "captured": 0,
    }
    assert feed_page.closed is True
    assert browser.closed is True


def test_live_wrapper_can_capture_weekly_viewers_as_passive_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed_page = _FakePage(
        [[{"post_url": "/feed/update/urn:li:activity:1/", "text": "Product update"}]]
    )
    viewer_page = _FakePage(
        [[{"profile_url": "/in/taylor/", "name": "Taylor", "headline": "Founder at Acme"}]]
    )

    class _Context:
        def __init__(self) -> None:
            self.pages_to_open = [feed_page, viewer_page]

        def new_page(self) -> _FakePage:
            return self.pages_to_open.pop(0)

    class _Browser:
        def __init__(self) -> None:
            self.contexts = [_Context()]

        def close(self) -> None:
            pass

    browser = _Browser()

    class _Scraper:
        def __init__(self, _settings: OutreachSettings) -> None:
            pass

        def require_live_cdp_session(self) -> None:
            pass

        def _connect_over_cdp(self, _playwright: object) -> _Browser:
            return browser

        def _session_preflight(self, _context: object) -> dict[str, object]:
            return {"ok": True}

        def _close_page_safely(self, page: _FakePage) -> None:
            page.close()

    @contextmanager
    def _playwright_context():
        yield object()

    monkeypatch.setattr(linkedin_signals_module, "LinkedInScraper", _Scraper)
    monkeypatch.setattr(linkedin_signals_module, "sync_playwright", _playwright_context)

    result = capture_linkedin_signals_live(
        OutreachSettings(),
        feed_path=tmp_path / "feed.csv",
        profile_viewers_path=tmp_path / "viewers.csv",
        feed_limits=CaptureLimits(max_scrolls=0),
        profile_viewer_limits=CaptureLimits(max_scrolls=0),
        capture_profile_viewers_this_run=True,
        observed_at="2026-07-10T08:00:00+00:00",
        target_companies=["Acme"],
    )

    assert result["status"] == "completed"
    assert result["profile_viewers"]["status"] == "completed"
    assert result["profile_viewers"]["run_relevance_counts"] == {"target_company": 1}
    assert _read_rows(tmp_path / "viewers.csv")[0]["passive_context_only"] == "true"
    assert feed_page.closed is True
    assert viewer_page.closed is True


def test_live_wrapper_reports_preflight_failure_instead_of_a_zero_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Browser:
        contexts = [object()]

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    browser = _Browser()

    class _Scraper:
        def __init__(self, _settings: OutreachSettings) -> None:
            pass

        def require_live_cdp_session(self) -> None:
            pass

        def _connect_over_cdp(self, _playwright: object) -> _Browser:
            return browser

        def _session_preflight(self, _context: object) -> dict[str, object]:
            return {
                "ok": False,
                "current_url": "https://www.linkedin.com/login",
                "authwall_or_login": True,
            }

    @contextmanager
    def _playwright_context():
        yield object()

    monkeypatch.setattr(linkedin_signals_module, "LinkedInScraper", _Scraper)
    monkeypatch.setattr(linkedin_signals_module, "sync_playwright", _playwright_context)

    result = capture_linkedin_signals_live(
        OutreachSettings(),
        feed_path=tmp_path / "feed.csv",
        capture_profile_viewers_this_run=True,
    )

    assert result["status"] == "failed"
    assert result["feed"]["status"] == "failed"
    assert result["feed"]["reason"] == "linkedin_session_preflight_failed"
    assert result["feed"]["authwall_or_login"] is True
    assert result["profile_viewers"]["status"] == "skipped"
    assert not (tmp_path / "feed.csv").exists()
    assert browser.closed is True

from outreach.config import OutreachSettings
from outreach.services.linkedin import LinkedInScraper, normalize_typeahead_text, score_typeahead_option


def test_normalize_typeahead_text_collapses_whitespace_and_case() -> None:
    assert normalize_typeahead_text("  Santa   Clara University ") == "santa clara university"


def test_score_typeahead_option_prefers_exact_company_match_over_university() -> None:
    requested = "Clara"

    clara_score = score_typeahead_option("Add a company", requested, "Clara")
    santa_clara_score = score_typeahead_option("Add a company", requested, "Santa Clara University")

    assert clara_score > santa_clara_score
    assert santa_clara_score < 0


def test_score_typeahead_option_prefers_school_for_school_trigger() -> None:
    scu_score = score_typeahead_option("Add a school", "Santa Clara", "Santa Clara University")
    startup_score = score_typeahead_option("Add a school", "Santa Clara", "Santa Clara Health")

    assert scu_score > startup_score


class _StubLocator:
    def __init__(self, count: int = 0) -> None:
        self._count = count
        self.first = self

    def count(self) -> int:
        return self._count

    def nth(self, _index: int):
        return self

    def bounding_box(self):
        return {"x": 100, "y": 100, "width": 120, "height": 40}


class _StubPage:
    def evaluate(self, _script: str):
        return None

    def get_by_text(self, _text: str, exact: bool = False):
        return _StubLocator(0)

    def get_by_role(self, _role: str, name=None):
        return _StubLocator(0)

    def locator(self, *_args, **_kwargs):
        return _StubLocator(0)


def test_send_single_invite_prefers_connect_over_messageish_connected_signal(monkeypatch) -> None:
    scraper = LinkedInScraper(OutreachSettings())
    page = _StubPage()

    class _ConnectButton:
        def get_attribute(self, _name: str):
            return None

    monkeypatch.setattr(scraper, "_navigate_profile", lambda _page, _url: True)
    monkeypatch.setattr(scraper, "_human_pause", lambda _page: None)
    monkeypatch.setattr(scraper, "_save_screenshot", lambda _page, _label: "shot.png")
    monkeypatch.setattr(scraper, "_find_connect_button", lambda _page, candidate_name=None: _ConnectButton())
    monkeypatch.setattr(scraper, "_is_already_connected", lambda _page, candidate_name=None: True)

    result = scraper._send_single_invite(
        page,
        {"name": "Test User", "linkedin_url": "https://www.linkedin.com/in/test-user/", "note": "hello"},
        execute=False,
    )

    assert result.status == "dry_run_ready"


def test_is_already_connected_requires_explicit_connected_signal(monkeypatch) -> None:
    scraper = LinkedInScraper(OutreachSettings())
    page = _StubPage()

    monkeypatch.setattr(scraper, "_has_primary_profile_connected_signal", lambda _page: False)

    assert scraper._is_already_connected(page) is False

from outreach.config import OutreachSettings
from outreach.services.email_finder import (
    EmailFinderService,
    EmailResearchCandidate,
    HunterEmailFinder,
    ProspeoEmailFinder,
    build_email_finder_service,
    domain_from_url,
    linkedin_handle,
    split_person_name,
)


def test_email_finder_candidate_helpers_normalize_inputs() -> None:
    assert domain_from_url("https://www.example.com/path") == "example.com"
    assert linkedin_handle("https://www.linkedin.com/in/akshat-pathak/") == "akshat-pathak"
    assert split_person_name("Jeff Pickett, MBA, MFA") == ("Jeff", "Pickett")


def test_hunter_email_finder_parses_found_response(monkeypatch) -> None:
    captured_url = ""

    def fake_get_json(url: str, *, timeout_seconds: int):
        nonlocal captured_url
        captured_url = url
        return {
            "data": {
                "email": "Test.User@Example.com",
                "score": 91,
                "verification": {"status": "valid"},
            }
        }

    monkeypatch.setattr("outreach.services.email_finder._get_json", fake_get_json)
    provider = HunterEmailFinder("key")

    result = provider.find_email(
        EmailResearchCandidate(
            contact_id="ct-1",
            organization_id="org-1",
            name="Test User",
            company="Example",
            linkedin_url="https://www.linkedin.com/in/test-user/",
            company_website="https://www.example.com",
        )
    )

    assert result.status == "found"
    assert result.email == "test.user@example.com"
    assert result.confidence == 91
    assert result.verification_status == "valid"
    assert "linkedin_handle=test-user" in captured_url
    assert "domain=example.com" in captured_url


def test_prospeo_email_finder_posts_person_enrichment_payload(monkeypatch) -> None:
    captured_payload = {}

    def fake_post_json(
        url: str,
        payload: dict[str, object],
        *,
        headers: dict[str, str],
        timeout_seconds: int,
    ):
        nonlocal captured_payload
        captured_payload = payload
        return {
            "data": {
                "email": {
                    "email": "Founder@Company.ai",
                    "confidence": 100,
                    "status": "verified",
                }
            }
        }

    monkeypatch.setattr("outreach.services.email_finder._post_json", fake_post_json)
    provider = ProspeoEmailFinder("key", only_verified=True)

    result = provider.find_email(
        EmailResearchCandidate(
            contact_id="ct-2",
            organization_id="org-2",
            name="Founder Person",
            company="Company AI",
            linkedin_url="https://www.linkedin.com/in/founder-person/",
            company_website="https://company.ai",
        )
    )

    assert result.status == "found"
    assert result.email == "founder@company.ai"
    assert result.confidence == 100
    assert result.verification_status == "verified"
    assert captured_payload["only_verified_email"] is True
    assert captured_payload["data"]["linkedin_url"] == "https://www.linkedin.com/in/founder-person/"


def test_email_finder_service_falls_back_to_second_provider() -> None:
    class Provider:
        def __init__(self, name: str, status: str, email: str = "") -> None:
            self.name = name
            self.status = status
            self.email = email

        def find_email(self, candidate: EmailResearchCandidate):
            from outreach.services.email_finder import EmailFinderResult

            return EmailFinderResult(
                contact_id=candidate.contact_id,
                organization_id=candidate.organization_id,
                name=candidate.name,
                company=candidate.company,
                provider=self.name,
                status=self.status,
                detail="mock",
                email=self.email,
                confidence=95 if self.email else 0,
            )

    service = EmailFinderService(
        [
            Provider("prospeo", "not_found"),
            Provider("hunter", "found", "person@example.com"),
        ],
        min_confidence=80,
    )

    result = service.find_one(
        EmailResearchCandidate(
            contact_id="ct",
            organization_id="org",
            name="Person Name",
            company="Example",
        )
    )

    assert result.provider == "hunter"
    assert result.email == "person@example.com"
    assert [attempt["provider"] for attempt in result.attempts] == ["prospeo", "hunter"]


def test_build_email_finder_service_requires_configured_keys() -> None:
    settings = OutreachSettings(PROSPEO_API_KEY=None, HUNTER_API_KEY=None)

    service = build_email_finder_service(settings)
    result = service.find_one(
        EmailResearchCandidate(
            contact_id="ct",
            organization_id="org",
            name="Person Name",
            company="Example",
        )
    )

    assert result.status == "skipped"
    assert result.provider == "none"

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from outreach.config import OutreachSettings


@dataclass(frozen=True)
class EmailResearchCandidate:
    contact_id: str
    organization_id: str
    name: str
    company: str
    title: str = ""
    linkedin_url: str = ""
    company_website: str = ""
    company_linkedin_url: str = ""

    @classmethod
    def from_dict(cls, row: dict[str, object]) -> EmailResearchCandidate:
        return cls(
            contact_id=str(row.get("contact_id") or ""),
            organization_id=str(row.get("organization_id") or ""),
            name=str(row.get("name") or row.get("full_name") or ""),
            company=str(row.get("company") or ""),
            title=str(row.get("title") or ""),
            linkedin_url=str(row.get("linkedin_url") or ""),
            company_website=str(row.get("company_website") or ""),
            company_linkedin_url=str(row.get("company_linkedin_url") or ""),
        )

    @property
    def domain(self) -> str:
        return domain_from_url(self.company_website)


@dataclass
class EmailFinderResult:
    contact_id: str
    organization_id: str
    name: str
    company: str
    provider: str
    status: str
    detail: str
    email: str = ""
    confidence: int = 0
    verification_status: str = ""
    source_url: str = ""
    attempts: list[dict[str, object]] = field(default_factory=list)

    def is_sendable(self, *, min_confidence: int) -> bool:
        if self.status != "found" or not self.email:
            return False
        if self.confidence and self.confidence < min_confidence:
            return False
        return True


class EmailFinderProvider(Protocol):
    name: str

    def find_email(self, candidate: EmailResearchCandidate) -> EmailFinderResult:
        ...


class EmailFinderService:
    def __init__(
        self,
        providers: list[EmailFinderProvider],
        *,
        min_confidence: int = 80,
    ) -> None:
        self.providers = providers
        self.min_confidence = min_confidence

    def find_many(
        self,
        candidates: list[EmailResearchCandidate],
        *,
        limit: int,
    ) -> list[EmailFinderResult]:
        results: list[EmailFinderResult] = []
        for candidate in candidates[:limit]:
            results.append(self.find_one(candidate))
        return results

    def find_one(self, candidate: EmailResearchCandidate) -> EmailFinderResult:
        if not self.providers:
            return EmailFinderResult(
                contact_id=candidate.contact_id,
                organization_id=candidate.organization_id,
                name=candidate.name,
                company=candidate.company,
                provider="none",
                status="skipped",
                detail="No external email-finder provider is configured.",
            )

        attempts: list[dict[str, object]] = []
        last_result: EmailFinderResult | None = None
        for provider in self.providers:
            result = provider.find_email(candidate)
            attempts.append(_result_attempt(result))
            if result.is_sendable(min_confidence=self.min_confidence):
                result.attempts = attempts
                return result
            last_result = result

        if last_result is None:
            return EmailFinderResult(
                contact_id=candidate.contact_id,
                organization_id=candidate.organization_id,
                name=candidate.name,
                company=candidate.company,
                provider="none",
                status="skipped",
                detail="No external email-finder provider ran.",
                attempts=attempts,
            )
        last_result.attempts = attempts
        return last_result


class ProspeoEmailFinder:
    name = "prospeo"

    def __init__(
        self,
        api_key: str,
        *,
        only_verified: bool = True,
        timeout_seconds: int = 20,
    ) -> None:
        self.api_key = api_key
        self.only_verified = only_verified
        self.timeout_seconds = timeout_seconds

    def find_email(self, candidate: EmailResearchCandidate) -> EmailFinderResult:
        if not self.api_key:
            return _skipped(candidate, self.name, "PROSPEO_API_KEY is not configured.")
        if not candidate.linkedin_url and not (candidate.name and (candidate.domain or candidate.company)):
            return _skipped(candidate, self.name, "Missing LinkedIn URL or name+company/domain.")

        first, last = split_person_name(candidate.name)
        payload: dict[str, object] = {
            "only_verified_email": self.only_verified,
            "enrich_mobile": False,
            "data": {
                "full_name": candidate.name,
                "first_name": first,
                "last_name": last,
                "linkedin_url": candidate.linkedin_url,
                "company_name": candidate.company,
                "company_website": candidate.company_website,
                "company_domain": candidate.domain,
            },
        }
        try:
            body = _post_json(
                "https://api.prospeo.io/enrich-person",
                payload,
                headers={
                    "Content-Type": "application/json",
                    "X-KEY": self.api_key,
                },
                timeout_seconds=self.timeout_seconds,
            )
        except RuntimeError as exc:
            return _provider_error(candidate, self.name, str(exc))

        parsed = _parse_prospeo_email(body)
        return _result_from_parsed(candidate, self.name, parsed, source_url=candidate.linkedin_url)


class HunterEmailFinder:
    name = "hunter"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = 20,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def find_email(self, candidate: EmailResearchCandidate) -> EmailFinderResult:
        if not self.api_key:
            return _skipped(candidate, self.name, "HUNTER_API_KEY is not configured.")
        first, last = split_person_name(candidate.name)
        params = {
            "api_key": self.api_key,
            "full_name": candidate.name,
            "first_name": first,
            "last_name": last,
            "domain": candidate.domain,
            "company": candidate.company,
            "linkedin_handle": linkedin_handle(candidate.linkedin_url),
        }
        filtered_params = {key: value for key, value in params.items() if value}
        if "full_name" not in filtered_params or not (
            filtered_params.get("domain")
            or filtered_params.get("company")
            or filtered_params.get("linkedin_handle")
        ):
            return _skipped(candidate, self.name, "Missing name plus domain/company/LinkedIn handle.")

        url = f"https://api.hunter.io/v2/email-finder?{urllib.parse.urlencode(filtered_params)}"
        try:
            body = _get_json(url, timeout_seconds=self.timeout_seconds)
        except RuntimeError as exc:
            return _provider_error(candidate, self.name, str(exc))

        parsed = _parse_hunter_email(body)
        return _result_from_parsed(candidate, self.name, parsed, source_url=url)


def build_email_finder_service(
    settings: OutreachSettings,
    *,
    provider: str | None = None,
) -> EmailFinderService:
    provider_name = (provider or settings.email_finder_provider or "auto").strip().lower()
    providers: list[EmailFinderProvider] = []
    if provider_name in {"auto", "prospeo"} and settings.prospeo_api_key:
        providers.append(
            ProspeoEmailFinder(
                settings.prospeo_api_key,
                only_verified=settings.email_finder_only_verified,
            )
        )
    if provider_name in {"auto", "hunter"} and settings.hunter_api_key:
        providers.append(HunterEmailFinder(settings.hunter_api_key))
    return EmailFinderService(providers, min_confidence=settings.email_finder_min_confidence)


def domain_from_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urllib.parse.urlparse(raw)
    domain = (parsed.netloc or parsed.path).split("/")[0].lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if "@" in domain:
        domain = domain.rsplit("@", maxsplit=1)[-1]
    return domain.strip(".")


def linkedin_handle(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    path = parsed.path.strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"in", "pub"}:
        return parts[1]
    return ""


def split_person_name(value: str) -> tuple[str, str]:
    cleaned = re.sub(r"\([^)]*\)", " ", value or "")
    cleaned = re.sub(r"\b(MBA|MFA|PhD|MD|CPA|CFA)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[,|]+", " ", cleaned)
    parts = [part for part in cleaned.split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def _result_attempt(result: EmailFinderResult) -> dict[str, object]:
    return {
        "provider": result.provider,
        "status": result.status,
        "email": result.email,
        "confidence": result.confidence,
        "verification_status": result.verification_status,
        "detail": result.detail,
    }


def _skipped(candidate: EmailResearchCandidate, provider: str, detail: str) -> EmailFinderResult:
    return EmailFinderResult(
        contact_id=candidate.contact_id,
        organization_id=candidate.organization_id,
        name=candidate.name,
        company=candidate.company,
        provider=provider,
        status="skipped",
        detail=detail,
    )


def _provider_error(candidate: EmailResearchCandidate, provider: str, detail: str) -> EmailFinderResult:
    return EmailFinderResult(
        contact_id=candidate.contact_id,
        organization_id=candidate.organization_id,
        name=candidate.name,
        company=candidate.company,
        provider=provider,
        status="provider_error",
        detail=detail,
    )


def _result_from_parsed(
    candidate: EmailResearchCandidate,
    provider: str,
    parsed: dict[str, object],
    *,
    source_url: str,
) -> EmailFinderResult:
    email = normalize_email(str(parsed.get("email") or ""))
    if not email:
        return EmailFinderResult(
            contact_id=candidate.contact_id,
            organization_id=candidate.organization_id,
            name=candidate.name,
            company=candidate.company,
            provider=provider,
            status="not_found",
            detail=str(parsed.get("detail") or "No email returned by provider."),
            source_url=source_url,
        )
    confidence = int(parsed.get("confidence") or 0)
    verification_status = str(parsed.get("verification_status") or "")
    return EmailFinderResult(
        contact_id=candidate.contact_id,
        organization_id=candidate.organization_id,
        name=candidate.name,
        company=candidate.company,
        provider=provider,
        status="found",
        detail=str(parsed.get("detail") or "Email returned by provider."),
        email=email,
        confidence=confidence,
        verification_status=verification_status,
        source_url=source_url,
    )


def normalize_email(value: str) -> str:
    email = value.strip().strip(".,;:()[]{}<>").lower()
    if not re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", email, flags=re.I):
        return ""
    domain = email.rsplit("@", maxsplit=1)[-1]
    if domain in {"linkedin.com", "mail.linkedin.com"}:
        return ""
    return email


def _parse_prospeo_email(body: dict[str, Any]) -> dict[str, object]:
    data = _first_mapping(body.get("data"), body.get("person"), body)
    person = _first_mapping(data.get("person"), data)
    email_node = _first_mapping(
        person.get("email"),
        person.get("email_info"),
        person.get("professional_email"),
        data.get("email"),
        data.get("email_info"),
    )
    email_value = _first_text(
        email_node.get("email"),
        email_node.get("value"),
        email_node.get("address"),
        person.get("email"),
        data.get("email"),
    )
    confidence = _first_int(
        email_node.get("confidence"),
        email_node.get("score"),
        person.get("email_confidence"),
        data.get("confidence"),
        default=100 if email_value else 0,
    )
    status = _first_text(
        email_node.get("status"),
        email_node.get("verification_status"),
        email_node.get("result"),
        data.get("status"),
    )
    return {
        "email": email_value,
        "confidence": confidence,
        "verification_status": status,
        "detail": f"Prospeo response status={status or 'unknown'}",
    }


def _parse_hunter_email(body: dict[str, Any]) -> dict[str, object]:
    data = body.get("data")
    if not isinstance(data, dict):
        data = body
    verifier = _first_mapping(data.get("verification"), data.get("email_verifier"))
    status = _first_text(
        verifier.get("status"),
        verifier.get("result"),
        data.get("verification_status"),
        data.get("status"),
    )
    return {
        "email": _first_text(data.get("email"), data.get("value"), data.get("address")),
        "confidence": _first_int(data.get("score"), data.get("confidence"), default=0),
        "verification_status": status,
        "detail": f"Hunter response status={status or 'unknown'}",
    }


def _first_mapping(*values: object) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_int(*values: object, default: int = 0) -> int:
    for value in values:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return default


def _post_json(
    url: str,
    payload: dict[str, object],
    *,
    headers: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return _request_json(request, timeout_seconds=timeout_seconds)


def _get_json(url: str, *, timeout_seconds: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _request_json(request, timeout_seconds=timeout_seconds)


def _request_json(request: urllib.request.Request, *, timeout_seconds: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Provider returned non-object JSON response.")
    return payload

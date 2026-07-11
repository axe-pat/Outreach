from __future__ import annotations

import csv
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse
from xml.etree import ElementTree

from pydantic import BaseModel, field_validator

from outreach.company_watchlist import (
    CandidateCompanySignal,
    CandidateProvenance,
    CompanyFitRubric,
    RubricDimension,
)


COMPANY_NEWS_SCHEMA_VERSION = "1.0"
DEFAULT_COMPANY_NEWS_LEDGER = Path("workspace/company_news_signals.json")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


class CompanyNewsSource(BaseModel):
    source_id: str
    label: str
    feed_url: str
    source_type: str = "company_news"
    enabled_by_default: bool = True
    notes: str = ""

    @field_validator("source_id", "label", "feed_url", "source_type")
    @classmethod
    def require_identity(cls, value: str) -> str:
        value = clean_text(value)
        if not value:
            raise ValueError("company-news source identity fields are required")
        return value


DEFAULT_COMPANY_NEWS_SOURCES: tuple[CompanyNewsSource, ...] = (
    CompanyNewsSource(
        source_id="techcrunch_startups",
        label="TechCrunch Startups",
        feed_url="https://techcrunch.com/category/startups/feed/",
        source_type="startup_news",
        notes="Broad startup funding, launch, growth, and hiring signals.",
    ),
    CompanyNewsSource(
        source_id="crunchbase_news",
        label="Crunchbase News",
        feed_url="https://news.crunchbase.com/feed/",
        source_type="funding_news",
        notes="Private-market and venture funding signals; review before promotion.",
    ),
    CompanyNewsSource(
        source_id="hacker_news",
        label="Hacker News",
        feed_url="https://news.ycombinator.com/rss",
        source_type="community_news",
        enabled_by_default=False,
        notes="Optional high-noise community feed; headline extraction remains review-gated.",
    ),
)


@dataclass(frozen=True)
class CompanyNewsEntry:
    title: str
    url: str
    summary: str = ""
    published_at: str = ""


@dataclass(frozen=True)
class CompanyNewsCaptureResult:
    source_summaries: list[dict[str, object]]
    captured_signal_ids: list[str]
    added_signal_ids: list[str]
    signals: list[CandidateCompanySignal]

    @property
    def status(self) -> str:
        statuses = {str(item.get("status") or "failed") for item in self.source_summaries}
        if statuses == {"completed"}:
            return "completed"
        if "completed" in statuses:
            return "partial"
        return "failed"


def list_company_news_sources() -> list[CompanyNewsSource]:
    return [item.model_copy(deep=True) for item in DEFAULT_COMPANY_NEWS_SOURCES]


def get_company_news_source(source_id: str) -> CompanyNewsSource:
    source_id = clean_text(source_id)
    for source in DEFAULT_COMPANY_NEWS_SOURCES:
        if source.source_id == source_id:
            return source.model_copy(deep=True)
    available = ", ".join(item.source_id for item in DEFAULT_COMPANY_NEWS_SOURCES)
    raise KeyError(f"Unknown company-news source '{source_id}'. Available: {available}")


def capture_company_news(
    *,
    run_id: str,
    ledger_path: Path,
    fetch_text: Callable[[str], str],
    source_ids: Iterable[str] = (),
    known_companies: Iterable[str] = (),
    per_source_limit: int = 30,
    observed_at: str | None = None,
) -> CompanyNewsCaptureResult:
    """Capture public news feeds into a durable, review-only candidate ledger."""

    selected_ids = [clean_text(item) for item in source_ids if clean_text(item)]
    sources = (
        [get_company_news_source(item) for item in selected_ids]
        if selected_ids
        else [item for item in list_company_news_sources() if item.enabled_by_default]
    )
    timestamp = observed_at or utc_now_iso()
    known = {normalize_company_name(item) for item in known_companies if clean_text(item)}
    source_summaries: list[dict[str, object]] = []
    captured: list[CandidateCompanySignal] = []
    captured_ids: list[str] = []
    for source in sources:
        try:
            feed_text = fetch_text(source.feed_url)
            entries = parse_company_news_feed(feed_text)[: max(0, per_source_limit)]
            signals = company_signals_from_news_entries(
                entries,
                source=source,
                run_id=run_id,
                known_companies=known,
                observed_at=timestamp,
            )
            ids = [company_news_signal_id(item) for item in signals]
            captured.extend(signals)
            captured_ids.extend(ids)
            source_summaries.append(
                {
                    "source_id": source.source_id,
                    "label": source.label,
                    "feed_url": source.feed_url,
                    "status": "completed",
                    "entries_read": len(entries),
                    "company_signals": len(signals),
                    "signal_ids": ids,
                }
            )
        except Exception as exc:  # Feed failures must remain source-explicit and non-destructive.
            source_summaries.append(
                {
                    "source_id": source.source_id,
                    "label": source.label,
                    "feed_url": source.feed_url,
                    "status": "failed",
                    "entries_read": 0,
                    "company_signals": 0,
                    "signal_ids": [],
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )

    added_ids = upsert_company_news_ledger(
        ledger_path,
        captured,
        observed_at=timestamp,
        run_id=run_id,
    )
    return CompanyNewsCaptureResult(
        source_summaries=source_summaries,
        captured_signal_ids=sorted(set(captured_ids)),
        added_signal_ids=sorted(set(added_ids)),
        signals=captured,
    )


def parse_company_news_feed(feed_text: str) -> list[CompanyNewsEntry]:
    root = ElementTree.fromstring(feed_text)
    entries: list[CompanyNewsEntry] = []
    for element in root.iter():
        local = _local_name(element.tag)
        if local not in {"item", "entry"}:
            continue
        title = _child_text(element, "title")
        url = _entry_url(element)
        summary = _child_text(element, "description", "summary", "content", "encoded")
        published_at = _child_text(element, "pubDate", "published", "updated", "date")
        if title and url:
            entries.append(
                CompanyNewsEntry(
                    title=clean_text(unescape(title)),
                    url=clean_text(url),
                    summary=strip_html(summary),
                    published_at=clean_text(published_at),
                )
            )
    return entries


def company_signals_from_news_entries(
    entries: Iterable[CompanyNewsEntry],
    *,
    source: CompanyNewsSource,
    run_id: str,
    known_companies: Iterable[str] = (),
    observed_at: str = "",
) -> list[CandidateCompanySignal]:
    known = {normalize_company_name(item) for item in known_companies if clean_text(item)}
    timestamp = observed_at or utc_now_iso()
    signals: list[CandidateCompanySignal] = []
    seen: set[str] = set()
    for entry in entries:
        company = extract_company_from_headline(entry.title)
        normalized = normalize_company_name(company)
        if not company or normalized in known:
            continue
        identity = f"{normalized}|{canonical_url(entry.url)}"
        if identity in seen:
            continue
        seen.add(identity)
        context = clean_text(" | ".join(item for item in (entry.title, entry.summary) if item))[:1200]
        signal_type = classify_news_signal(entry.title, entry.summary)
        signals.append(
            CandidateCompanySignal(
                company_name=company,
                description=context,
                rubric=company_news_rubric(context, signal_type),
                provenance=[
                    CandidateProvenance(
                        source_name=source.label,
                        source_type=source.source_type,
                        source_run_id=run_id,
                        source_url=entry.url,
                        observed_at=timestamp,
                        signal_type=signal_type,
                        context=context,
                    )
                ],
            )
        )
    return signals


def structured_company_signals_from_path(
    path: Path,
    *,
    run_id: str,
    known_companies: Iterable[str] = (),
    default_source_name: str = "Curated company input",
    observed_at: str = "",
) -> list[CandidateCompanySignal]:
    """Adapt reviewed CSV/JSON/JSONL exports to the canonical company-signal contract."""

    rows = _load_structured_rows(path)
    known = {normalize_company_name(item) for item in known_companies if clean_text(item)}
    timestamp = observed_at or utc_now_iso()
    signals: list[CandidateCompanySignal] = []
    for row in rows:
        company = clean_text(row.get("company_name") or row.get("company") or row.get("organization_name"))
        if not company or normalize_company_name(company) in known:
            continue
        description = clean_text(
            row.get("description") or row.get("context") or row.get("headline") or row.get("title")
        )
        signal_type = clean_text(row.get("signal_type")) or classify_news_signal(description, "")
        signals.append(
            CandidateCompanySignal(
                company_name=company,
                website=clean_text(row.get("website") or row.get("company_website")),
                linkedin_company_url=clean_text(
                    row.get("linkedin_company_url") or row.get("company_linkedin_url")
                ),
                description=description,
                rubric=_structured_rubric(row, description, signal_type),
                provenance=[
                    CandidateProvenance(
                        source_name=clean_text(row.get("source_name")) or default_source_name,
                        source_type=clean_text(row.get("source_type")) or "curated_company_input",
                        source_run_id=run_id,
                        source_url=clean_text(row.get("source_url") or row.get("url")),
                        observed_at=clean_text(row.get("observed_at")) or timestamp,
                        signal_type=signal_type,
                        author_or_actor=clean_text(row.get("author_or_actor") or row.get("author")),
                        context=description,
                    )
                ],
            )
        )
    return signals


def upsert_company_news_ledger(
    path: Path,
    signals: Iterable[CandidateCompanySignal],
    *,
    observed_at: str,
    run_id: str,
) -> list[str]:
    payload = _load_ledger_payload(path)
    existing_rows = payload["signals"]
    by_id = {
        str(row.get("signal_id") or ""): row
        for row in existing_rows
        if isinstance(row, dict) and str(row.get("signal_id") or "")
    }
    added: list[str] = []
    for signal in signals:
        signal_id = company_news_signal_id(signal)
        current = by_id.get(signal_id)
        if current is None:
            by_id[signal_id] = {
                "signal_id": signal_id,
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "seen_run_ids": [run_id],
                "signal": signal.model_dump(mode="json"),
            }
            added.append(signal_id)
            continue
        current["last_seen_at"] = observed_at
        seen_runs_value = current.get("seen_run_ids")
        seen_runs: list[Any] = seen_runs_value if isinstance(seen_runs_value, list) else []
        current["seen_run_ids"] = sorted({str(item) for item in seen_runs} | {run_id})
        current["signal"] = signal.model_dump(mode="json")

    output = {
        "schema_version": COMPANY_NEWS_SCHEMA_VERSION,
        "updated_at": observed_at,
        "signals": sorted(by_id.values(), key=lambda row: str(row.get("signal_id") or "")),
    }
    _atomic_write_json(path, output)
    return added


def load_company_news_signals(
    path: Path,
    *,
    known_companies: Iterable[str] = (),
    signal_ids: Iterable[str] | None = None,
) -> list[CandidateCompanySignal]:
    payload = _load_ledger_payload(path)
    rows = payload["signals"]
    selected = {clean_text(item) for item in signal_ids if clean_text(item)} if signal_ids is not None else None
    known = {normalize_company_name(item) for item in known_companies if clean_text(item)}
    result: list[CandidateCompanySignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        signal_id = clean_text(row.get("signal_id"))
        if selected is not None and signal_id not in selected:
            continue
        signal_payload = row.get("signal")
        if not isinstance(signal_payload, dict):
            continue
        try:
            signal = CandidateCompanySignal.model_validate(signal_payload)
        except ValueError as exc:
            raise ValueError(
                f"Invalid company-news signal {signal_id!r} in {path}: {exc}"
            ) from exc
        if normalize_company_name(signal.company_name) not in known:
            result.append(signal)
    return result


def company_news_signal_id(signal: CandidateCompanySignal) -> str:
    provenance = signal.provenance[0]
    seed = "|".join(
        (
            normalize_company_name(signal.company_name),
            canonical_url(provenance.source_url),
            provenance.source_type.casefold(),
        )
    )
    return f"company-news-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:18]}"


def company_news_capture_snapshots(
    signals: Iterable[CandidateCompanySignal],
) -> tuple[list[dict[str, object]], str]:
    """Serialize one capture's signals so later replay never consults the mutable ledger."""

    by_id: dict[str, dict[str, object]] = {}
    for signal in signals:
        signal_id = company_news_signal_id(signal)
        snapshot = {
            "signal_id": signal_id,
            "signal": signal.model_dump(mode="json"),
        }
        previous = by_id.get(signal_id)
        if previous is not None and previous != snapshot:
            raise ValueError(
                f"Company-news capture produced conflicting snapshots for {signal_id!r}"
            )
        by_id[signal_id] = snapshot
    snapshots = [by_id[signal_id] for signal_id in sorted(by_id)]
    return snapshots, _snapshot_sha256(snapshots)


def load_company_news_capture_snapshots(
    payload: Mapping[str, object],
    *,
    artifact_label: str = "company-news capture artifact",
) -> list[CandidateCompanySignal]:
    """Load immutable exact-run evidence, failing closed for ID-only legacy captures."""

    raw_ids = payload.get("captured_signal_ids")
    if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
        raise ValueError(f"{artifact_label} has invalid captured_signal_ids")
    captured_ids = [clean_text(item) for item in raw_ids]
    if any(not item for item in captured_ids) or len(captured_ids) != len(set(captured_ids)):
        raise ValueError(f"{artifact_label} has blank or duplicate captured_signal_ids")

    raw_snapshots = payload.get("captured_signal_snapshots")
    if raw_snapshots is None:
        if captured_ids:
            raise ValueError(
                f"{artifact_label} predates immutable signal snapshots; recapture it "
                "instead of replaying IDs from the mutable company-news ledger"
            )
        return []
    if not isinstance(raw_snapshots, list):
        raise ValueError(f"{artifact_label} captured_signal_snapshots must be a list")
    expected_sha256 = clean_text(payload.get("captured_signal_snapshots_sha256"))
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError(
            f"{artifact_label} is missing a valid captured_signal_snapshots_sha256"
        )
    if _snapshot_sha256(raw_snapshots) != expected_sha256:
        raise ValueError(f"{artifact_label} immutable signal snapshot hash does not match")

    signals: list[CandidateCompanySignal] = []
    snapshot_ids: list[str] = []
    for index, raw_snapshot in enumerate(raw_snapshots):
        if not isinstance(raw_snapshot, dict):
            raise ValueError(f"{artifact_label} snapshot {index} must be an object")
        signal_id = clean_text(raw_snapshot.get("signal_id"))
        signal_payload = raw_snapshot.get("signal")
        if not signal_id or not isinstance(signal_payload, dict):
            raise ValueError(
                f"{artifact_label} snapshot {index} is missing signal_id or signal"
            )
        try:
            signal = CandidateCompanySignal.model_validate(signal_payload)
        except ValueError as exc:
            raise ValueError(
                f"{artifact_label} snapshot {signal_id!r} has an invalid signal: {exc}"
            ) from exc
        if company_news_signal_id(signal) != signal_id:
            raise ValueError(
                f"{artifact_label} snapshot {signal_id!r} does not match its signal identity"
            )
        snapshot_ids.append(signal_id)
        signals.append(signal)

    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise ValueError(f"{artifact_label} has duplicate immutable signal snapshots")
    if set(snapshot_ids) != set(captured_ids):
        raise ValueError(
            f"{artifact_label} captured_signal_ids do not match its immutable snapshots"
        )
    return signals


def _snapshot_sha256(snapshots: object) -> str:
    encoded = json.dumps(
        snapshots,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_COMPANY_VERB_PATTERN = re.compile(
    r"^(?P<company>.{2,80}?)\s+(?:reportedly\s+|just\s+)?"
    r"(?P<verb>raises?|raised|lands?|landed|secures?|secured|closes?|closed|"
    r"launches?|launched|unveils?|unveiled|announces?|announced|acquires?|acquired|"
    r"nabs?|nabbed|bags?|bagged|hits?|hit|hires?|hired|expands?|expanded|"
    r"releases?|released|debuts?|debuted|wins?|won|files?|filed|gets?|got)\b",
    re.IGNORECASE,
)
_PREFIX_PATTERN = re.compile(
    r"^(?:exclusive:\s*|inside\s+|meet\s+|why\s+|how\s+|with\s+|"
    r"ai\s+startup\s+|fintech\s+startup\s+|startup\s+)",
    re.IGNORECASE,
)
_GENERIC_HEADLINE_STARTS = {
    "a startup",
    "ai startups",
    "almost",
    "former",
    "how",
    "inside",
    "meet",
    "the company",
    "the startup",
    "these startups",
    "this company",
    "this startup",
    "what",
    "why",
}
_DESCRIPTOR_MARKERS = {"app", "company", "firm", "maker", "platform", "startup", "tool"}


def extract_company_from_headline(headline: str) -> str:
    value = clean_text(headline).strip(" -–—:|\"")
    if not value:
        return ""
    match = _COMPANY_VERB_PATTERN.match(value)
    if not match:
        possessive = re.match(
            r"^(?P<company>[A-Za-z0-9][A-Za-z0-9&.+\-’']{1,40})(?:'s|’s)\s+",
            value,
        )
        candidate = possessive.group("company") if possessive else ""
    else:
        candidate = match.group("company")
    candidate = _PREFIX_PATTERN.sub("", candidate).strip(" ,:;|\"'()")
    candidate = re.sub(r"\s+", " ", candidate)
    parts = candidate.split()
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].casefold().strip("-,:;") not in _DESCRIPTOR_MARKERS:
            continue
        suffix = parts[index + 1 :]
        if suffix and all(_looks_like_brand_token(item) for item in suffix):
            candidate = " ".join(suffix)
        break
    lower = candidate.casefold()
    if not candidate or lower in _GENERIC_HEADLINE_STARTS:
        return ""
    if any(lower.startswith(f"{item} ") for item in _GENERIC_HEADLINE_STARTS):
        return ""
    if len(candidate.split()) > 6 or len(candidate) > 64:
        return ""
    if not any(char.isalpha() for char in candidate):
        return ""
    return candidate


def _looks_like_brand_token(value: str) -> bool:
    value = value.strip("-–—:|\"'()")
    if not value:
        return False
    return value[0].isupper() or any(char.isupper() for char in value[1:]) or any(
        char.isdigit() for char in value
    )


def classify_news_signal(title: str, summary: str) -> str:
    text = f" {clean_text(title)} {clean_text(summary)} ".casefold()
    if any(token in text for token in (" raises ", " raised ", " funding ", " series a", " series b", " seed round", " valuation")):
        return "funding"
    if any(token in text for token in (" hiring ", " hires ", " jobs ", " headcount ", " team expansion")):
        return "hiring"
    if any(token in text for token in (" launches ", " launched ", " unveils ", " released ", " debuts ")):
        return "launch"
    if any(token in text for token in (" acquires ", " acquired ", " acquisition ")):
        return "acquisition"
    return "company_news"


def company_news_rubric(context: str, signal_type: str) -> CompanyFitRubric:
    text = f" {clean_text(context)} ".casefold()
    domains = {
        "ai": (" ai ", "artificial intelligence", "machine learning", "agent"),
        "data/platform": (" data ", "platform", "developer", " api ", "infrastructure"),
        "workflow/enterprise": ("workflow", "automation", "enterprise", " saas "),
        "marketplace/operations": ("marketplace", "logistics", "mobility", "operations"),
        "fintech": ("fintech", "payments", "billing", "banking", "finance"),
        "health": ("health", "clinical", "patient", "provider"),
        "robotics": ("robot", "hardware", "autonomous", "manufacturing"),
    }
    matched = [name for name, tokens in domains.items() if any(token in text for token in tokens)]
    geography = [
        label
        for label, tokens in {
            "US/remote": (" remote", "united states", " u.s.", " us-based"),
            "SF Bay Area": ("san francisco", "bay area", "silicon valley"),
            "Los Angeles": ("los angeles", "santa monica", "culver city"),
            "New York": ("new york", " nyc"),
        }.items()
        if any(token in text for token in tokens)
    ]
    role_tokens = [token for token in ("product", "strategy", "operations", "program", "growth") if token in text]
    growth_score = 3 if signal_type in {"funding", "hiring", "launch"} else 2
    role_score = 2 if signal_type == "hiring" or role_tokens else 1
    return CompanyFitRubric(
        domain_fit=RubricDimension(
            score=3 if len(matched) >= 2 else 2 if matched else 1,
            evidence=", ".join(matched) or "news signal; domain needs review",
        ),
        technical_mba_story=RubricDimension(
            score=2 if matched else 1,
            evidence=("technical/operator bridge: " + ", ".join(matched)) if matched else "story fit needs review",
        ),
        geography_remote=RubricDimension(
            score=3 if geography else 1,
            evidence=", ".join(geography) or "news item does not establish target geography",
        ),
        growth_quality=RubricDimension(
            score=growth_score,
            evidence=f"review-gated {signal_type} signal",
        ),
        role_surface=RubricDimension(
            score=role_score,
            evidence=", ".join(role_tokens) or "role surface needs research",
        ),
    )


def _structured_rubric(
    row: Mapping[str, object],
    context: str,
    signal_type: str,
) -> CompanyFitRubric:
    inferred = company_news_rubric(context, signal_type)

    def dimension(name: str, fallback: RubricDimension) -> RubricDimension:
        raw = clean_text(row.get(f"{name}_score"))
        evidence = clean_text(row.get(f"{name}_evidence")) or fallback.evidence
        if not raw:
            return fallback
        try:
            score = min(3, max(0, int(raw)))
        except ValueError:
            return fallback
        return RubricDimension(score=score, evidence=evidence)

    return CompanyFitRubric(
        domain_fit=dimension("domain_fit", inferred.domain_fit),
        technical_mba_story=dimension("technical_mba_story", inferred.technical_mba_story),
        geography_remote=dimension("geography_remote", inferred.geography_remote),
        growth_quality=dimension("growth_quality", inferred.growth_quality),
        role_surface=dimension("role_surface", inferred.role_surface),
    )


def _load_structured_rows(path: Path) -> list[dict[str, object]]:
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix in {".jsonl", ".ndjson"}:
        rows: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        return rows
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("signals") or value.get("items") or value.get("results") or []
    if not isinstance(value, list):
        raise ValueError(f"Expected a JSON list or supported wrapper in {path}")
    return [dict(item) for item in value if isinstance(item, dict)]


def _load_ledger_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": COMPANY_NEWS_SCHEMA_VERSION, "signals": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in company-news ledger {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Company-news ledger must be a JSON object: {path}")
    schema_version = clean_text(value.get("schema_version"))
    if schema_version and schema_version != COMPANY_NEWS_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported company-news ledger schema {schema_version!r} in {path}; "
            f"expected {COMPANY_NEWS_SCHEMA_VERSION!r}."
        )
    rows = value.get("signals")
    if not isinstance(rows, list):
        raise ValueError(f"Company-news ledger signals must be a list: {path}")
    seen_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Company-news ledger row {index} must be an object: {path}")
        signal_id = clean_text(row.get("signal_id"))
        signal_payload = row.get("signal")
        if not signal_id or not isinstance(signal_payload, dict):
            raise ValueError(
                f"Company-news ledger row {index} is missing signal_id or signal: {path}"
            )
        if signal_id in seen_ids:
            raise ValueError(f"Duplicate company-news signal_id {signal_id!r} in {path}")
        try:
            CandidateCompanySignal.model_validate(signal_payload)
        except ValueError as exc:
            raise ValueError(
                f"Invalid company-news signal {signal_id!r} in {path}: {exc}"
            ) from exc
        seen_ids.add(signal_id)
    value["schema_version"] = schema_version or COMPANY_NEWS_SCHEMA_VERSION
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Replace a ledger only after a complete same-directory write succeeds."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _entry_url(element: ElementTree.Element) -> str:
    link_text = _child_text(element, "link")
    if link_text:
        return link_text
    for child in element:
        if _local_name(child.tag) == "link":
            href = clean_text(child.attrib.get("href"))
            rel = clean_text(child.attrib.get("rel"))
            if href and rel in {"", "alternate"}:
                return href
    return _child_text(element, "guid", "id")


def _child_text(element: ElementTree.Element, *names: str) -> str:
    wanted = {item.casefold() for item in names}
    for child in element:
        if _local_name(child.tag).casefold() not in wanted:
            continue
        text = "".join(child.itertext())
        if clean_text(text):
            return text
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def strip_html(value: str) -> str:
    return clean_text(unescape(re.sub(r"<[^>]+>", " ", value or "")))


def canonical_url(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    parsed = urlparse(value)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.casefold() or 'https'}://{host}{path}"


def normalize_company_name(value: str) -> str:
    value = clean_text(value).casefold()
    value = re.sub(r"\b(incorporated|inc|llc|ltd|company|corp|corporation)\b", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)

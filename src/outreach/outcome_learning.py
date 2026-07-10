"""Advisory learning from outreach outcomes and the comms example corpus.

This module produces evidence and recommendations.  It intentionally has no
code path that edits production prompts or sends messages.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OutreachWorkbook,
    TouchpointRecord,
)


@dataclass
class OutcomeMetrics:
    sends: int = 0
    accepts: int = 0
    replies: int = 0
    rejections: int = 0
    gold: int = 0
    silver: int = 0
    negative: int = 0

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            **asdict(self),
            "accept_rate": _rate(self.accepts, self.sends),
            "reply_rate": _rate(self.replies, self.sends),
            "rejection_rate": _rate(self.rejections, self.sends),
        }


@dataclass(frozen=True)
class LearningRecommendation:
    dimension: str
    value: str
    action: str
    rationale: str
    confidence: str
    evidence: dict[str, int | float | None]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class OutcomeLearningReport:
    generated_at: datetime
    totals: OutcomeMetrics
    by_message: dict[str, OutcomeMetrics]
    by_audience: dict[str, OutcomeMetrics]
    by_account: dict[str, OutcomeMetrics]
    recommendations: list[LearningRecommendation]
    unattributed_outcomes: dict[str, int]
    source_scope: str = "Tracker touchpoints plus explicitly labeled comms examples"
    schema_version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "source_scope": self.source_scope,
            "application_contract": {
                "recommendations_are_advisory": True,
                "production_prompts_mutated": False,
                "human_review_required_before_prompt_changes": True,
            },
            "totals": self.totals.as_dict(),
            "by_message": _metrics_payload(self.by_message),
            "by_audience": _metrics_payload(self.by_audience),
            "by_account": _metrics_payload(self.by_account),
            "unattributed_outcomes": dict(self.unattributed_outcomes),
            "recommendations": [item.as_dict() for item in self.recommendations],
        }


@dataclass(frozen=True)
class _SendObservation:
    touchpoint_id: str
    identity_key: str
    sent_at: datetime
    message: str
    audience: str
    account: str


def build_outcome_learning(
    touchpoints: Iterable[TouchpointRecord],
    *,
    contacts: Iterable[ContactRecord] = (),
    organizations: Iterable[OrganizationRecord] = (),
    labeled_examples: Iterable[dict[str, object]] = (),
    generated_at: datetime | None = None,
    recommendation_min_sends: int = 3,
) -> OutcomeLearningReport:
    """Aggregate tracker outcomes across message, audience, and account.

    Accepts are attributed to the most recent prior LinkedIn invite. Replies
    and explicit rejections are attributed to the most recent prior send for
    that person. A send can receive each outcome at most once, which keeps the
    rates interpretable when the tracker records multiple reconciliation rows.
    """

    generated_at = _aware(generated_at or datetime.now(UTC))
    contact_list = list(contacts)
    organization_list = list(organizations)
    touchpoint_list = sorted(list(touchpoints), key=_event_at)
    contacts_by_id = {item.contact_id: item for item in contact_list}
    organizations_by_id = {item.organization_id: item for item in organization_list}

    totals = OutcomeMetrics()
    buckets: dict[str, dict[str, OutcomeMetrics]] = {
        "message": defaultdict(OutcomeMetrics),
        "audience": defaultdict(OutcomeMetrics),
        "account": defaultdict(OutcomeMetrics),
    }
    sends_by_identity: dict[str, list[_SendObservation]] = defaultdict(list)
    label_fingerprints: set[tuple[str, str, str]] = set()

    for item in touchpoint_list:
        if not _is_outbound_send(item):
            continue
        dimensions = _dimensions(item, contacts_by_id, organizations_by_id)
        _increment(totals, buckets, dimensions, "sends")
        label = "gold" if _is_manual_send(item) else "silver"
        _increment(totals, buckets, dimensions, label)
        label_fingerprints.add(
            _label_fingerprint(label, item.message_text, dimensions["account"])
        )
        sends_by_identity[_identity_key(item)].append(
            _SendObservation(
                touchpoint_id=item.touchpoint_id,
                identity_key=_identity_key(item),
                sent_at=_event_at(item),
                message=dimensions["message"],
                audience=dimensions["audience"],
                account=dimensions["account"],
            )
        )

    attributed: dict[str, set[str]] = {
        "accepts": set(),
        "replies": set(),
        "rejections": set(),
    }
    unattributed = {"accepts": 0, "replies": 0, "rejections": 0}
    for item in touchpoint_list:
        outcomes: list[str] = []
        if _is_accept(item):
            outcomes.append("accepts")
        if _is_reply(item):
            outcomes.append("replies")
        if _is_rejection(item):
            outcomes.append("rejections")
        for outcome in outcomes:
            candidates = [
                send
                for send in sends_by_identity.get(_identity_key(item), [])
                if send.sent_at <= _event_at(item)
                and (outcome != "accepts" or send.message == "linkedin_invite")
            ]
            send = candidates[-1] if candidates else None
            if send is None:
                unattributed[outcome] += 1
                totals_value = getattr(totals, outcome)
                setattr(totals, outcome, totals_value + 1)
                continue
            if send.touchpoint_id in attributed[outcome]:
                continue
            attributed[outcome].add(send.touchpoint_id)
            _increment(
                totals,
                buckets,
                {"message": send.message, "audience": send.audience, "account": send.account},
                outcome,
            )

    contact_lookup = _contact_name_lookup(contact_list, organizations_by_id)
    for example in labeled_examples:
        label = _normalize(example.get("label"))
        if label not in {"gold", "silver", "negative"}:
            continue
        message_text = str(example.get("message") or example.get("message_text") or "").strip()
        if not message_text:
            continue
        dimensions = _example_dimensions(example, contact_lookup)
        fingerprint = _label_fingerprint(label, message_text, dimensions["account"])
        if fingerprint in label_fingerprints:
            continue
        label_fingerprints.add(fingerprint)
        _increment(totals, buckets, dimensions, label)

    recommendations = _build_recommendations(
        buckets,
        minimum_sends=max(1, recommendation_min_sends),
    )
    return OutcomeLearningReport(
        generated_at=generated_at,
        totals=totals,
        by_message=dict(buckets["message"]),
        by_audience=dict(buckets["audience"]),
        by_account=dict(buckets["account"]),
        recommendations=recommendations,
        unattributed_outcomes=unattributed,
    )


def build_workbook_outcome_learning(
    workbook: OutreachWorkbook,
    *,
    labeled_examples_path: Path | None = None,
    generated_at: datetime | None = None,
    recommendation_min_sends: int = 3,
) -> OutcomeLearningReport:
    """Convenience API for the CSV tracker and reusable comms corpus."""

    examples = load_labeled_examples(labeled_examples_path) if labeled_examples_path else []
    return build_outcome_learning(
        workbook.list_touchpoints(),
        contacts=workbook.list_contacts(),
        organizations=workbook.list_organizations(),
        labeled_examples=examples,
        generated_at=generated_at,
        recommendation_min_sends=recommendation_min_sends,
    )


def load_labeled_examples(path: Path) -> list[dict[str, object]]:
    """Load either the reusable JSONL corpus or a run-scoped JSON artifact."""

    if not path.exists():
        return []
    if path.suffix.lower() == ".jsonl":
        result: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                result.append(payload)
        return result
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        examples = payload.get("examples") or []
        return [item for item in examples if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def write_outcome_learning_artifact(path: Path, report: OutcomeLearningReport) -> Path:
    """Persist an inspectable advisory artifact for reports and later review."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return path


def concise_learning_summary(report: OutcomeLearningReport, *, limit: int = 3) -> dict[str, object]:
    """Return the small report surface while retaining the full artifact."""

    return {
        "totals": report.totals.as_dict(),
        "recommendations": [
            item.as_dict() for item in report.recommendations[: max(0, limit)]
        ],
        "unattributed_outcomes": dict(report.unattributed_outcomes),
        "advisory_only": True,
    }


def _increment(
    totals: OutcomeMetrics,
    buckets: dict[str, dict[str, OutcomeMetrics]],
    dimensions: dict[str, str],
    field: str,
) -> None:
    setattr(totals, field, getattr(totals, field) + 1)
    for dimension, value in dimensions.items():
        metrics = buckets[dimension][value]
        setattr(metrics, field, getattr(metrics, field) + 1)


def _dimensions(
    item: TouchpointRecord,
    contacts_by_id: dict[str, ContactRecord],
    organizations_by_id: dict[str, OrganizationRecord],
) -> dict[str, str]:
    contact = contacts_by_id.get(item.contact_id)
    organization = organizations_by_id.get(item.organization_id)
    return {
        "message": _message_dimension(item),
        "audience": _audience_dimension(contact),
        "account": organization.name if organization else item.organization_id or "unknown",
    }


def _example_dimensions(
    example: dict[str, object],
    contact_lookup: dict[tuple[str, str], tuple[str, str]],
) -> dict[str, str]:
    account = str(
        example.get("account")
        or example.get("company")
        or example.get("organization_id")
        or "unknown"
    ).strip()
    name = str(example.get("name") or example.get("contact_name") or "").strip()
    inferred_audience, resolved_account = contact_lookup.get(
        (_normalize(name), _normalize(account)),
        ("unknown", account or "unknown"),
    )
    message = str(example.get("message_kind") or "").strip()
    if not message:
        channel = _normalize(example.get("channel")) or "unknown"
        message = f"{channel}_message" if channel != "unknown" else "unknown"
    return {
        "message": _normalize(message) or "unknown",
        "audience": _normalize(example.get("audience")) or inferred_audience,
        "account": resolved_account or "unknown",
    }


def _contact_name_lookup(
    contacts: list[ContactRecord],
    organizations_by_id: dict[str, OrganizationRecord],
) -> dict[tuple[str, str], tuple[str, str]]:
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for contact in contacts:
        organization = organizations_by_id.get(contact.organization_id)
        account = organization.name if organization else contact.organization_id
        result[(_normalize(contact.full_name), _normalize(account))] = (
            _audience_dimension(contact),
            account,
        )
    return result


def _message_dimension(item: TouchpointRecord) -> str:
    channel = _channel(item)
    kind = _normalize(item.message_kind)
    notes = item.notes.lower()
    if channel == "linkedin":
        if kind == "linkedin_manual_message" or "manual_outbound_detected=true" in notes:
            return "linkedin_manual_message"
        if kind in {"linkedin_followup", "linkedin_message"}:
            return "linkedin_followup"
        return "linkedin_invite"
    if channel == "email":
        if "final" in kind:
            return "email_final"
        if "follow" in kind:
            return "email_followup"
        return "email_initial"
    return kind or channel or "unknown"


def _audience_dimension(contact: ContactRecord | None) -> str:
    if contact is None:
        return "unknown"
    explicit = _normalize(contact.contact_type)
    text = " ".join([contact.contact_type, contact.title]).lower()
    patterns = (
        ("founder_executive", ("founder", "co-founder", "chief executive", "ceo", "chief of staff")),
        ("recruiting", ("recruit", "talent", "people partner", "human resources")),
        ("product", ("product manager", "product lead", "product operations", "product ops", "tpm")),
        ("strategy_ops", ("strategy", "business operations", "bizops", "program manager", "growth")),
        ("engineering", ("engineer", "developer", "architect", "data scientist")),
    )
    for label, needles in patterns:
        if any(needle in text for needle in needles):
            return label
    return explicit or "unknown"


def _build_recommendations(
    buckets: dict[str, dict[str, OutcomeMetrics]],
    *,
    minimum_sends: int,
) -> list[LearningRecommendation]:
    recommendations: list[LearningRecommendation] = []
    for dimension in ("message", "audience", "account"):
        for value, metrics in buckets[dimension].items():
            evidence = metrics.as_dict()
            if metrics.sends >= minimum_sends and (metrics.replies / metrics.sends) >= 0.2:
                recommendations.append(
                    LearningRecommendation(
                        dimension=dimension,
                        value=value,
                        action="preserve_and_test",
                        rationale="This segment has a meaningful observed reply rate; preserve its concrete patterns and test on more sends.",
                        confidence=_confidence(metrics.sends),
                        evidence=evidence,
                    )
                )
            if metrics.sends >= minimum_sends and (metrics.rejections / metrics.sends) >= 0.25:
                recommendations.append(
                    LearningRecommendation(
                        dimension=dimension,
                        value=value,
                        action="review_targeting_and_copy",
                        rationale="Explicit rejection is elevated in this segment; inspect targeting and the ask before reusing it.",
                        confidence=_confidence(metrics.sends),
                        evidence=evidence,
                    )
                )
            if metrics.negative >= max(2, metrics.gold + metrics.silver):
                recommendations.append(
                    LearningRecommendation(
                        dimension=dimension,
                        value=value,
                        action="avoid_reusing_negative_pattern",
                        rationale="Replacement/cleared drafts outweigh approved examples in this segment.",
                        confidence="medium" if metrics.negative >= 4 else "low",
                        evidence=evidence,
                    )
                )
    if not recommendations:
        recommendations.append(
            LearningRecommendation(
                dimension="overall",
                value="all",
                action="collect_more_outcomes",
                rationale=(
                    "No segment has enough outcome evidence yet. Keep labels and tracker outcomes attached "
                    "instead of changing production prompts from a tiny sample."
                ),
                confidence="low",
                evidence={"minimum_sends_per_segment": minimum_sends},
            )
        )
    action_order = {
        "review_targeting_and_copy": 0,
        "avoid_reusing_negative_pattern": 1,
        "preserve_and_test": 2,
        "collect_more_outcomes": 3,
    }
    return sorted(
        recommendations,
        key=lambda item: (action_order.get(item.action, 9), item.dimension, item.value.lower()),
    )


def _is_outbound_send(item: TouchpointRecord) -> bool:
    status = _normalize(item.status)
    kind = _normalize(item.message_kind)
    if status not in {"sent", "delivered", "completed"}:
        return False
    return kind not in {
        "linkedin_reply",
        "email_reply",
        "inbound_reply",
        "reply",
    }


def _is_manual_send(item: TouchpointRecord) -> bool:
    return (
        _normalize(item.message_kind) == "linkedin_manual_message"
        or "manual_outbound_detected=true" in item.notes.lower()
        or "manual_send=true" in item.notes.lower()
    )


def _is_accept(item: TouchpointRecord) -> bool:
    if _channel(item) != "linkedin":
        return False
    status = _normalize(item.status)
    text = " ".join([item.message_text, item.notes]).lower()
    return status in {"accepted", "connected"} or "invite accepted" in text or "reconcile_status=connected" in text


def _is_reply(item: TouchpointRecord) -> bool:
    status = _normalize(item.status)
    kind = _normalize(item.message_kind)
    return status in {"replied", "responded"} or kind in {
        "linkedin_reply",
        "email_reply",
        "inbound_reply",
        "reply",
    }


def _is_rejection(item: TouchpointRecord) -> bool:
    status = _normalize(item.status)
    kind = _normalize(item.message_kind)
    if status in {"rejected", "declined", "not_interested", "unsubscribed", "do_not_contact"}:
        return True
    if kind in {"rejection", "unsubscribe", "do_not_contact"}:
        return True
    text = " ".join([item.message_text, item.notes]).lower()
    return any(
        marker in text
        for marker in (
            "not interested",
            "please remove me",
            "do not contact",
            "won't be moving forward",
            "will not be moving forward",
        )
    )


def _identity_key(item: TouchpointRecord) -> str:
    return item.contact_id.strip() or f"org:{item.organization_id.strip()}"


def _channel(item: TouchpointRecord) -> str:
    raw = getattr(item.channel, "value", item.channel)
    normalized = _normalize(raw)
    if "linkedin" in normalized:
        return "linkedin"
    if "email" in normalized:
        return "email"
    return normalized


def _event_at(item: TouchpointRecord) -> datetime:
    return _parse_datetime(item.sent_at) or _parse_datetime(item.recorded_at) or datetime.min.replace(tzinfo=UTC)


def _parse_datetime(value: str) -> datetime | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    try:
        return _aware(datetime.fromisoformat(clean))
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _normalize(value: object) -> str:
    raw = getattr(value, "value", value)
    return re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")


def _label_fingerprint(label: str, message: str, account: str) -> tuple[str, str, str]:
    return (_normalize(label), _normalize(message), _normalize(account))


def _metrics_payload(values: dict[str, OutcomeMetrics]) -> dict[str, dict[str, int | float | None]]:
    return {
        key: values[key].as_dict()
        for key in sorted(values, key=lambda item: item.lower())
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _confidence(sends: int) -> str:
    if sends >= 20:
        return "high"
    if sends >= 8:
        return "medium"
    return "low"

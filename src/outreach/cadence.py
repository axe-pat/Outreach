"""Cross-channel outreach cadence derived from tracker touchpoints.

The tracker is deliberately the source of truth here.  Browser state can add a
new touchpoint, but it cannot silently advance a cadence on its own.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from outreach.tracking import ContactRecord, OutreachWorkbook, TouchpointRecord


SENT_STATUSES = {"sent", "delivered", "completed"}
PAUSE_STATUSES = {
    "replied",
    "responded",
    "engaged",
    "meeting booked",
    "meeting_booked",
    "coffee chat",
    "coffee_chat",
    "warm conversation",
}
TERMINAL_STATUSES = {
    "unsubscribed",
    "do not contact",
    "do_not_contact",
    "rejected",
    "declined",
    "not interested",
    "closed",
    "bounced",
    "blocked",
    "invalid email",
    "invalid_email",
}
REPLY_KINDS = {"linkedin_reply", "email_reply", "inbound_reply", "reply"}
ENGAGEMENT_KINDS = {"meeting", "meeting_booked", "coffee_chat", "engagement"}
TERMINAL_KINDS = {"unsubscribe", "rejection", "do_not_contact", "terminal"}
LINKEDIN_FOLLOWUP_KINDS = {
    "linkedin_followup",
    "linkedin_message",
    "linkedin_manual_message",
}
LINKEDIN_INVITE_KINDS = {"linkedin_invite", "invite", "connection_invite"}


@dataclass(frozen=True)
class CadencePolicy:
    """The agreed default cadence; callers can override it explicitly."""

    linkedin_first_followup_days: int = 4
    # Grace window for the first post-acceptance follow-up. A first useful
    # follow-up stays worth sending well past its ideal day, so keep it
    # auto-sendable for two weeks instead of dropping to manual review after one
    # day. This recovers accepted-but-unworked contacts when nightly runs slip,
    # while genuinely stale accepts (older than the window) still retire.
    linkedin_first_followup_grace_days: int = 14
    linkedin_second_followup_min_days: int = 4
    linkedin_second_followup_max_days: int = 5
    linkedin_max_followups: int = 2
    email_first_followup_days: int = 4
    email_final_followup_min_days: int = 4
    email_final_followup_max_days: int = 5
    email_max_touches: int = 3
    email_window_days: int = 90
    suppress_cross_channel_same_day: bool = True
    day_timezone: str = field(
        default_factory=lambda: os.getenv("TIMEZONE", "America/Los_Angeles")
    )


@dataclass(frozen=True)
class CadenceRecommendation:
    identity_key: str
    organization_id: str
    contact_id: str
    channel: str
    action: str
    state: str
    reason: str
    due_at: datetime | None = None
    due_by: datetime | None = None
    anchor_at: datetime | None = None
    touches_in_window: int = 0
    requires_distinct_value_add: bool = False
    evidence_touchpoint_ids: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for key in ("due_at", "due_by", "anchor_at"):
            value = payload[key]
            payload[key] = value.isoformat() if isinstance(value, datetime) else None
        payload["evidence_touchpoint_ids"] = list(self.evidence_touchpoint_ids)
        return payload


@dataclass(frozen=True)
class CadenceGuardResult:
    allowed: bool
    action: str
    reasons: tuple[str, ...]
    recommendation: CadenceRecommendation | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "reasons": list(self.reasons),
            "recommendation": self.recommendation.as_dict() if self.recommendation else None,
        }


def build_cadence_plan(
    touchpoints: Iterable[TouchpointRecord],
    *,
    contacts: Iterable[ContactRecord] = (),
    as_of: datetime | None = None,
    policy: CadencePolicy | None = None,
) -> list[CadenceRecommendation]:
    """Return one LinkedIn and/or email cadence decision per tracked person.

    Contacts only seed identities that are eligible for a first cold email.
    Every send, accept, reply, stop, and elapsed-day calculation comes from the
    touchpoint ledger.
    """

    policy = policy or CadencePolicy()
    as_of = _aware(as_of or datetime.now(UTC))
    contact_list = list(contacts)
    history_by_identity: dict[str, list[TouchpointRecord]] = defaultdict(list)
    identity_details: dict[str, tuple[str, str]] = {}

    for touchpoint in touchpoints:
        key = _identity_key(touchpoint.organization_id, touchpoint.contact_id)
        history_by_identity[key].append(touchpoint)
        identity_details[key] = (touchpoint.organization_id, touchpoint.contact_id)

    email_eligible: set[str] = set()
    for contact in contact_list:
        key = _identity_key(contact.organization_id, contact.contact_id)
        identity_details[key] = (contact.organization_id, contact.contact_id)
        if contact.email.strip():
            email_eligible.add(key)

    recommendations: list[CadenceRecommendation] = []
    for key in sorted(identity_details):
        organization_id, contact_id = identity_details[key]
        history = sorted(history_by_identity.get(key, []), key=_event_at)
        has_linkedin_history = any(_channel(item) == "linkedin" for item in history)
        has_email_history = any(_channel(item) == "email" for item in history)

        if has_linkedin_history:
            item = _linkedin_recommendation(
                key=key,
                organization_id=organization_id,
                contact_id=contact_id,
                history=history,
                as_of=as_of,
                policy=policy,
            )
            if item is not None:
                recommendations.append(item)

        if key in email_eligible or has_email_history:
            recommendations.append(
                _email_recommendation(
                    key=key,
                    organization_id=organization_id,
                    contact_id=contact_id,
                    history=history,
                    as_of=as_of,
                    policy=policy,
                )
            )

    return recommendations


def build_workbook_cadence_plan(
    workbook: OutreachWorkbook,
    *,
    as_of: datetime | None = None,
    policy: CadencePolicy | None = None,
) -> list[CadenceRecommendation]:
    """Convenience integration API for the existing CSV tracker."""

    return build_cadence_plan(
        workbook.list_touchpoints(),
        contacts=workbook.list_contacts(),
        as_of=as_of,
        policy=policy,
    )


def summarize_cadence_plan(items: Iterable[CadenceRecommendation]) -> dict[str, object]:
    """Create report-friendly counts without losing the underlying decisions."""

    item_list = list(items)
    by_state: dict[str, int] = defaultdict(int)
    by_action: dict[str, int] = defaultdict(int)
    for item in item_list:
        by_state[item.state] += 1
        by_action[item.action] += 1
    return {
        "total": len(item_list),
        "by_state": dict(sorted(by_state.items())),
        "by_action": dict(sorted(by_action.items())),
        "due": [item.as_dict() for item in item_list if item.state == "due"],
        "suppressed": [item.as_dict() for item in item_list if item.state == "suppressed"],
    }


def guard_cadence_action(
    touchpoints: Iterable[TouchpointRecord],
    *,
    organization_id: str,
    contact_id: str,
    channel: str,
    action: str,
    proposed_at: datetime | None = None,
    proposed_message: str = "",
    contacts: Iterable[ContactRecord] = (),
    policy: CadencePolicy | None = None,
) -> CadenceGuardResult:
    """Block an early, excessive, duplicate, or cross-channel double touch."""

    policy = policy or CadencePolicy()
    proposed_at = _aware(proposed_at or datetime.now(UTC))
    touchpoint_list = list(touchpoints)
    contact_list = list(contacts)
    plan = build_cadence_plan(
        touchpoint_list,
        contacts=contact_list,
        as_of=proposed_at,
        policy=policy,
    )
    key = _identity_key(organization_id, contact_id)
    normalized_channel = _normalize(channel)
    recommendation = next(
        (
            item
            for item in plan
            if item.identity_key == key and item.channel == normalized_channel
        ),
        None,
    )
    if recommendation is None:
        return CadenceGuardResult(False, action, ("No tracker-backed cadence decision exists.",))

    reasons: list[str] = []
    if recommendation.action != action:
        reasons.append(
            f"Tracker cadence calls for {recommendation.action}, not {action}."
        )
    if recommendation.state != "due":
        reasons.append(f"Cadence state is {recommendation.state}: {recommendation.reason}")

    relevant_history = [
        item
        for item in touchpoint_list
        if _identity_key(item.organization_id, item.contact_id) == key
    ]
    if action.startswith("linkedin_followup") and _is_generic_nudge(proposed_message):
        reasons.append("LinkedIn follow-ups must add useful context; a generic nudge is blocked.")
    if recommendation.requires_distinct_value_add:
        previous = [
            item.message_text
            for item in relevant_history
            if _is_linkedin_followup(item) and _is_outbound_send(item)
        ]
        if not proposed_message.strip():
            reasons.append("The second LinkedIn follow-up needs a distinct value-add message.")
        elif not is_distinct_value_add(proposed_message, previous):
            reasons.append("The second LinkedIn follow-up repeats the first instead of adding value.")

    return CadenceGuardResult(
        allowed=not reasons,
        action=action,
        reasons=tuple(reasons) if reasons else ("Cadence and cross-channel guards passed.",),
        recommendation=recommendation,
    )


def is_distinct_value_add(proposed_message: str, previous_messages: Iterable[str]) -> bool:
    """Reject exact and near-duplicate second touches."""

    proposed_tokens = set(_message_tokens(proposed_message))
    if len(proposed_tokens) < 4:
        return False
    for previous in previous_messages:
        previous_tokens = set(_message_tokens(previous))
        if not previous_tokens:
            continue
        overlap = len(proposed_tokens & previous_tokens) / len(proposed_tokens | previous_tokens)
        if overlap >= 0.8:
            return False
    return True


def _linkedin_recommendation(
    *,
    key: str,
    organization_id: str,
    contact_id: str,
    history: list[TouchpointRecord],
    as_of: datetime,
    policy: CadencePolicy,
) -> CadenceRecommendation | None:
    stop = _latest_matching(history, _is_terminal)
    pause = _latest_matching(history, _is_pause)
    evidence = tuple(item.touchpoint_id for item in history)
    if stop is not None:
        return _decision(
            key, organization_id, contact_id, "linkedin", "none", "stopped",
            f"Terminal tracker state: {_event_label(stop)}.", evidence=evidence,
        )
    if pause is not None:
        return _decision(
            key, organization_id, contact_id, "linkedin", "none", "paused",
            f"Reply or engagement recorded: {_event_label(pause)}. Continue manually.",
            evidence=evidence,
        )

    invite = _latest_matching(history, _is_linkedin_invite)
    accepted = _latest_matching(history, _is_linkedin_accept)
    if accepted is None:
        if invite is None:
            return None
        return _decision(
            key, organization_id, contact_id, "linkedin", "none", "waiting",
            "Invite is pending; generic follow-ups are not allowed before acceptance.",
            anchor_at=_event_at(invite), evidence=evidence,
        )

    accepted_at = _event_at(accepted)
    followups = [
        item
        for item in history
        if _is_linkedin_followup(item)
        and _is_outbound_send(item)
        and _event_at(item) >= accepted_at
    ]
    if len(followups) >= policy.linkedin_max_followups:
        return _decision(
            key, organization_id, contact_id, "linkedin", "none", "complete",
            "Two post-acceptance LinkedIn touches are already recorded; pause until a real new hook.",
            touches=len(followups), evidence=evidence,
        )

    if not followups:
        anchor = accepted_at
        due_at = anchor + timedelta(days=policy.linkedin_first_followup_days)
        action = "linkedin_followup_1"
        # Wide grace window: a late-but-first follow-up still lands as "due"
        # (auto-sendable) rather than expiring to manual review after one day.
        due_by = due_at + timedelta(days=policy.linkedin_first_followup_grace_days)
        distinct = False
        reason = "First useful follow-up is due four days after invite acceptance."
    else:
        anchor = _event_at(followups[-1])
        due_at = anchor + timedelta(days=policy.linkedin_second_followup_min_days)
        due_by = anchor + timedelta(days=policy.linkedin_second_followup_max_days)
        action = "linkedin_followup_2_value_add"
        distinct = True
        reason = "One final, distinct value-add is due four to five days after the first follow-up."

    state = _due_state(due_at, as_of, due_by)
    if state == "expired":
        reason = "The automated LinkedIn follow-up window expired; review manually before contacting."
    if state == "due" and _other_channel_sent_today(history, "linkedin", as_of, policy):
        state = "suppressed"
        reason = "Another channel already touched this person today; defer to avoid a double tap."
    return _decision(
        key, organization_id, contact_id, "linkedin", action, state, reason,
        due_at=due_at, due_by=due_by, anchor_at=anchor, touches=len(followups),
        distinct=distinct, evidence=evidence,
    )


def _email_recommendation(
    *,
    key: str,
    organization_id: str,
    contact_id: str,
    history: list[TouchpointRecord],
    as_of: datetime,
    policy: CadencePolicy,
) -> CadenceRecommendation:
    evidence = tuple(item.touchpoint_id for item in history)
    stop = _latest_matching(history, _is_terminal)
    pause = _latest_matching(history, _is_pause)
    if stop is not None:
        return _decision(
            key, organization_id, contact_id, "email", "none", "stopped",
            f"Terminal tracker state: {_event_label(stop)}.", evidence=evidence,
        )
    if pause is not None:
        return _decision(
            key, organization_id, contact_id, "email", "none", "paused",
            f"Reply or engagement recorded: {_event_label(pause)}. Continue manually.",
            evidence=evidence,
        )

    window_start = as_of - timedelta(days=policy.email_window_days)
    sent = [
        item
        for item in history
        if _channel(item) == "email"
        and _is_outbound_send(item)
        and _event_at(item) >= window_start
        and _event_at(item) <= as_of
    ]
    if len(sent) >= policy.email_max_touches:
        return _decision(
            key, organization_id, contact_id, "email", "none", "complete",
            "Three email touches are already recorded in the rolling 90-day window.",
            touches=len(sent), evidence=evidence,
        )

    if not sent:
        action = "email_initial"
        anchor = as_of
        due_at = as_of
        due_by = as_of
        reason = "Verified email is available and no email send exists in the last 90 days."
    elif len(sent) == 1:
        action = "email_followup_1"
        anchor = _event_at(sent[-1])
        due_at = anchor + timedelta(days=policy.email_first_followup_days)
        due_by = due_at + timedelta(days=1)
        reason = "First email follow-up is due four days after the initial email."
    else:
        action = "email_final_optional"
        anchor = _event_at(sent[-1])
        due_at = anchor + timedelta(days=policy.email_final_followup_min_days)
        due_by = anchor + timedelta(days=policy.email_final_followup_max_days)
        reason = "Optional final email is due four to five days after the first follow-up."

    state = _due_state(due_at, as_of, due_by)
    if state == "expired":
        reason = "The automated email follow-up window expired; review manually before contacting."
    if state == "due" and _other_channel_sent_today(history, "email", as_of, policy):
        state = "suppressed"
        reason = "LinkedIn already touched this person today; defer email to avoid a double tap."
    return _decision(
        key, organization_id, contact_id, "email", action, state, reason,
        due_at=due_at, due_by=due_by, anchor_at=anchor, touches=len(sent), evidence=evidence,
    )


def _decision(
    key: str,
    organization_id: str,
    contact_id: str,
    channel: str,
    action: str,
    state: str,
    reason: str,
    *,
    due_at: datetime | None = None,
    due_by: datetime | None = None,
    anchor_at: datetime | None = None,
    touches: int = 0,
    distinct: bool = False,
    evidence: tuple[str, ...] = (),
) -> CadenceRecommendation:
    return CadenceRecommendation(
        identity_key=key,
        organization_id=organization_id,
        contact_id=contact_id,
        channel=channel,
        action=action,
        state=state,
        reason=reason,
        due_at=due_at,
        due_by=due_by,
        anchor_at=anchor_at,
        touches_in_window=touches,
        requires_distinct_value_add=distinct,
        evidence_touchpoint_ids=evidence,
    )


def _identity_key(organization_id: str, contact_id: str) -> str:
    return contact_id.strip() or f"org:{organization_id.strip()}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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


def _event_at(item: TouchpointRecord) -> datetime:
    return _parse_datetime(item.sent_at) or _parse_datetime(item.recorded_at) or datetime.min.replace(tzinfo=UTC)


def _normalize(value: object) -> str:
    raw = getattr(value, "value", value)
    return re.sub(r"[\s-]+", "_", str(raw or "").strip().lower())


def _channel(item: TouchpointRecord) -> str:
    value = _normalize(item.channel)
    if "linkedin" in value:
        return "linkedin"
    if "email" in value:
        return "email"
    return value


def _status(item: TouchpointRecord) -> str:
    return _normalize(item.status)


def _kind(item: TouchpointRecord) -> str:
    return _normalize(item.message_kind)


def _is_outbound_send(item: TouchpointRecord) -> bool:
    return _status(item) in {_normalize(value) for value in SENT_STATUSES} and not _is_pause(item) and not _is_terminal(item)


def _is_linkedin_followup(item: TouchpointRecord) -> bool:
    return _channel(item) == "linkedin" and _kind(item) in LINKEDIN_FOLLOWUP_KINDS


def _is_linkedin_invite(item: TouchpointRecord) -> bool:
    if _channel(item) != "linkedin" or not _is_outbound_send(item):
        return False
    if _kind(item) in LINKEDIN_INVITE_KINDS:
        return True
    if "invite_result=" in item.notes.lower():
        return True
    return _kind(item) not in LINKEDIN_FOLLOWUP_KINDS


def _is_linkedin_accept(item: TouchpointRecord) -> bool:
    if _channel(item) != "linkedin":
        return False
    if _status(item) in {"accepted", "connected"}:
        return True
    text = " ".join([item.message_text, item.notes]).lower()
    return "invite accepted" in text or "reconcile_status=connected" in text


def _is_pause(item: TouchpointRecord) -> bool:
    status = _status(item)
    kind = _kind(item)
    if status in {_normalize(value) for value in PAUSE_STATUSES}:
        return True
    return kind in REPLY_KINDS or kind in ENGAGEMENT_KINDS


def _is_terminal(item: TouchpointRecord) -> bool:
    status = _status(item)
    kind = _kind(item)
    if status in {_normalize(value) for value in TERMINAL_STATUSES} or kind in TERMINAL_KINDS:
        return True
    if status in {_normalize(value) for value in SENT_STATUSES}:
        return False
    text = " ".join([item.message_text, item.notes]).lower()
    return any(
        marker in text
        for marker in ("unsubscribe", "do not contact", "not interested", "please remove me")
    )


def _latest_matching(
    history: Iterable[TouchpointRecord],
    predicate: Callable[[TouchpointRecord], bool],
) -> TouchpointRecord | None:
    matches = [item for item in history if predicate(item)]
    return max(matches, key=_event_at) if matches else None


def _due_state(
    due_at: datetime,
    as_of: datetime,
    due_by: datetime | None = None,
) -> str:
    if as_of < due_at:
        return "upcoming"
    if due_by is not None and as_of >= due_by + timedelta(days=1):
        return "expired"
    return "due"


def _other_channel_sent_today(
    history: Iterable[TouchpointRecord],
    channel: str,
    as_of: datetime,
    policy: CadencePolicy,
) -> bool:
    if not policy.suppress_cross_channel_same_day:
        return False
    try:
        day_timezone = ZoneInfo(policy.day_timezone)
    except ZoneInfoNotFoundError:
        day_timezone = as_of.tzinfo or UTC
    proposed_day = as_of.astimezone(day_timezone).date()
    for item in history:
        if not _is_outbound_send(item) or _channel(item) == channel:
            continue
        event_at = _event_at(item).astimezone(day_timezone)
        if event_at.date() == proposed_day:
            return True
    return False


def _event_label(item: TouchpointRecord) -> str:
    return item.status or item.message_kind or item.touchpoint_id


def _message_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _is_generic_nudge(value: str) -> bool:
    normalized = " ".join(_message_tokens(value))
    if not normalized:
        return True
    generic = (
        "just following up",
        "just checking in",
        "bumping this",
        "circling back",
        "any thoughts",
    )
    return len(normalized) < 180 and any(phrase in normalized for phrase in generic)

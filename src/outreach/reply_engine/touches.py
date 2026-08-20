"""Deterministic LinkedIn follow-up touch accounting.

Invites, reconciliation rows and inbound replies are evidence about state, but
they are not outbound follow-up touches.  The tracker remains the source of
truth; backlog metadata cannot silently advance or reset this count.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
import re

from ..tracking import TouchpointRecord


DEFAULT_TOUCH_CAP = 2
TRIGGERED_TOUCH_CAP = 3
OUTBOUND_FOLLOWUP_KINDS = frozenset(
    {"linkedin_followup", "linkedin_message", "linkedin_manual_message"}
)
SENT_STATUSES = frozenset({"sent", "delivered", "completed"})
INBOUND_REPLY_KINDS = frozenset(
    {"linkedin_reply", "inbound_reply", "reply"}
)
_PLACEHOLDER_INBOUND = re.compile(
    r"^(?:linkedin reply detected|reply detected|inbound reply detected|"
    r"new linkedin message thread.*?)[\s.!]*$",
    re.I,
)
_COLD_OPENER = re.compile(r"^(?:hi|hey|hello)\b", re.I)
_CONNECTION_THANKS = re.compile(
    r"\bthanks?(?: you)? for (?:connecting|accepting|the connection)\b",
    re.I,
)
_PURE_RESPONSE = re.compile(
    r"^(?:(?:oh|okay|ok|but)\s*[,!.]?\s*)*(?:"
    r"thanks?\s+anyways?|"
    r"thanks?\s+so\s+much(?:\s+[A-Z][\w'’.-]*)?\s+for\b|"
    r"thank\s+you\s+so\s+much(?:\s+[A-Z][\w'’.-]*)?(?:\s+for\b|[! .😊😃]*$)|"
    r"thanks?\s+for\s+(?:the\s+)?(?:info|information|heads?\s+up|effort|help)|"
    r"no\s+worries\b|"
    r"appreciate\s+(?:you\s+)?(?:sending|sharing|checking|trying)\b"
    r")",
    re.I,
)


def is_outbound_followup_touch(touchpoint: TouchpointRecord) -> bool:
    """True only for a tracker-confirmed outbound LinkedIn follow-up."""

    return (
        str(getattr(touchpoint.channel, "value", touchpoint.channel)).casefold()
        == "linkedin"
        and touchpoint.status.strip().casefold() in SENT_STATUSES
        and touchpoint.message_kind.strip().casefold() in OUTBOUND_FOLLOWUP_KINDS
    )


def outbound_followup_touch_counts(
    touchpoints: Iterable[TouchpointRecord],
) -> dict[str, int]:
    """Count prior outbound follow-ups by contact ID."""

    return dict(
        Counter(
            touchpoint.contact_id
            for touchpoint in touchpoints
            if touchpoint.contact_id and is_outbound_followup_touch(touchpoint)
        )
    )


def outbound_is_purely_responsive(message: str) -> bool:
    """True when outbound wording presupposes something the recipient did."""

    text = " ".join((message or "").split()).strip()
    if not text or _COLD_OPENER.search(text) or _CONNECTION_THANKS.search(text):
        return False
    return bool(_PURE_RESPONSE.search(text))


def inbound_is_substantive(touchpoint: TouchpointRecord) -> bool:
    kind = touchpoint.message_kind.strip().casefold()
    status = touchpoint.status.strip().casefold()
    if kind not in INBOUND_REPLY_KINDS and status not in {"replied", "responded"}:
        return False
    text = " ".join((touchpoint.message_text or "").split()).strip()
    return bool(text) and not _PLACEHOLDER_INBOUND.fullmatch(text)


def _event_at(touchpoint: TouchpointRecord) -> datetime:
    raw = (touchpoint.sent_at or touchpoint.recorded_at or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def inbound_probably_missing(
    touchpoints: Iterable[TouchpointRecord],
) -> TouchpointRecord | None:
    """Return responsive outbound evidence that lacks prior inbound content."""

    substantive_inbound_seen = False
    for touchpoint in sorted(touchpoints, key=_event_at):
        if inbound_is_substantive(touchpoint):
            substantive_inbound_seen = True
            continue
        if (
            not substantive_inbound_seen
            and is_outbound_followup_touch(touchpoint)
            and outbound_is_purely_responsive(touchpoint.message_text)
        ):
            return touchpoint
    return None


def effective_touch_cap(*, reopen_condition_fired: bool) -> int:
    """A fired durable trigger buys exactly one additional touch."""

    return TRIGGERED_TOUCH_CAP if reopen_condition_fired else DEFAULT_TOUCH_CAP


def touch_cap_reached(
    touch_count: int,
    *,
    reopen_condition_fired: bool,
) -> bool:
    return max(0, touch_count) >= effective_touch_cap(
        reopen_condition_fired=reopen_condition_fired
    )

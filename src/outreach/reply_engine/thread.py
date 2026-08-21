"""Layer 0 and Layer 1: make the thread record trustworthy, then state it.

Nothing downstream works on bad input.  Two defects in the existing pipeline
are fixed here:

1. ``message_window`` was not chronologically sorted.  ``original_invite`` rows
   carry an empty timestamp and spliced in arbitrarily, so the composer read
   roughly half of all threads out of order and could not tell who spoke last.

2. Pipeline telemetry was being fed in as conversation.  71 of 185 drafts in
   the 2026-08-07 backlog had a "latest message" that was actually
   ``invite_result=send_unknown_reserved | detail=Invite worker returned
   ambiguous status 'preflight_failed'...``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .models import ThreadState

#: Strings that mean "this is our own plumbing", never a human message.
TELEMETRY_PATTERN = re.compile(
    r"invite_result=|source_artifact=|Invite worker |preflight_failed|"
    r"chrome-error://|Could not attach to Chrome|send_unknown_reserved|"
    r"Delivery is unknown",
    re.I,
)

_APOLOGY_OR_CORRECTION = re.compile(
    r"\b(?:sorry|apolog(?:y|ize|ise|ized|ised)|my mistake|"
    r"pasted here by mistake|sent by mistake|correction|meant to say|"
    r"wrong (?:message|name|thread|company))\b",
    re.I,
)

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan", "feb", "mar", "apr", "may", "jun",
            "jul", "aug", "sep", "oct", "nov", "dec",
        ],
        start=1,
    )
}


@dataclass
class Message:
    sender: str
    text: str
    timestamp_text: str = ""
    source: str = ""
    order: int = 0

    @property
    def is_from_us(self) -> bool:
        return self.sender.strip().lower() in {"you", "me", "akshat", "akshat pathak"}


def is_telemetry(text: str) -> bool:
    return bool(TELEMETRY_PATTERN.search(text or ""))


def _parse_timestamp(
    value: str, *, year: int, today: datetime | None = None
) -> datetime | None:
    """Best-effort parse of LinkedIn's inconsistent timestamp strings.

    LinkedIn renders 'Jul 9' for older messages and '1:16 AM' for today's,
    so we cannot get a total order from these alone - which is exactly why
    :func:`order_messages` reports its own confidence.
    """

    raw = (value or "").strip()
    if not raw:
        return None
    match = re.match(r"([A-Za-z]{3})\s+(\d{1,2})", raw)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            try:
                return datetime(year, month, int(match.group(2)))
            except ValueError:
                return None
    if re.match(r"\d{1,2}:\d{2}\s*(AM|PM)", raw, re.I):
        # LinkedIn shows a bare clock time only for today's messages.
        return today or datetime(year, 12, 31)
    return None


def order_messages(
    raw_window: list[dict],
    *,
    invite_sent_at: datetime | None = None,
    year: int = 2026,
    today: datetime | None = None,
) -> tuple[list[Message], bool]:
    """Return messages in chronological order plus an order-confidence flag.

    ``invite_sent_at`` supplies the missing timestamp for the ``original_invite``
    row.  Without it the invite cannot be placed, and we say so rather than
    guessing - the Sandeep P. thread in the 2026-08-07 backlog produced a
    hallucinated referral precisely because the engine guessed.
    """

    messages: list[Message] = []
    for index, row in enumerate(raw_window or []):
        text = str(row.get("message") or "").strip()
        if not text or is_telemetry(text):
            continue
        messages.append(
            Message(
                sender=str(row.get("sender") or "").strip(),
                text=text,
                timestamp_text=str(row.get("timestamp_text") or "").strip(),
                source=str(row.get("source") or "").strip(),
                order=index,
            )
        )

    if len(messages) <= 1:
        return messages, True

    # The scraper emits the thread in order, so raw order is the base signal.
    # The one known defect is the undated ``original_invite`` row, which was
    # being spliced in arbitrarily - so that is the only thing we reposition.
    invite = next((m for m in messages if m.source == "original_invite"), None)
    others = [m for m in messages if m is not invite]
    if invite is None:
        return messages, True
    if invite_sent_at is None:
        # We cannot place it.  Say so rather than guessing: assuming the invite
        # opened the thread is what turned Sandeep P.'s unrelated profile link
        # into a fabricated referral.
        return [invite, *others], False

    invite_date = invite_sent_at.date()
    before = 0
    for message in others:
        stamp = _parse_timestamp(message.timestamp_text, year=year, today=today)
        # Compared at date granularity: LinkedIn gives no clock time for older
        # messages, and a reply sent the same day as the invite still follows it.
        if stamp is not None and stamp.date() < invite_date:
            before += 1
        else:
            break

    ordered = others[:before] + [invite] + others[before:]
    return ordered, True


def has_real_conversation(messages: list[Message]) -> bool:
    """True when at least one message came from the other person."""

    return any(not message.is_from_us for message in messages)


def original_invite_text(messages: list[Message]) -> str:
    invite = next(
        (message for message in messages if message.source == "original_invite"),
        None,
    )
    if invite is not None:
        return invite.text
    return next((message.text for message in messages if message.is_from_us), "")


def last_inbound_text(messages: list[Message]) -> str:
    return next(
        (message.text for message in reversed(messages) if not message.is_from_us),
        "",
    )


def last_outbound_requires_human(messages: list[Message]) -> bool:
    last_outbound = next(
        (message.text for message in reversed(messages) if message.is_from_us),
        "",
    )
    return bool(last_outbound and _APOLOGY_OR_CORRECTION.search(last_outbound))


def resolve_state(
    messages: list[Message],
    *,
    contact_status: str = "",
    contact_notes: str = "",
    reopen_condition: str = "",
) -> ThreadState:
    """Layer 1.  Derived from the ledger, no model involved."""

    notes = (contact_notes or "").lower()
    status = (contact_status or "").lower()

    if status in {"do_not_contact", "closed_hard"} or "do_not_contact" in notes:
        return ThreadState.CLOSED_HARD
    if "offchannel" in notes or "moved to email" in notes or "spoke by phone" in notes:
        return ThreadState.CLOSED_OFFCHANNEL
    if reopen_condition.strip() or "parked" in notes or "suppress follow-up" in notes:
        return ThreadState.PARKED
    if not has_real_conversation(messages):
        outbound = [message for message in messages if message.is_from_us]
        post_invite_outbound = len(outbound) > 1 or any(
            message.source and message.source != "original_invite"
            for message in outbound
        )
        return (
            ThreadState.OUTBOUND_UNANSWERED
            if post_invite_outbound
            else ThreadState.NO_CONTEXT
        )
    return (
        ThreadState.YOU_REPLIED_LAST
        if messages[-1].is_from_us
        else ThreadState.THEY_REPLIED_UNANSWERED
    )

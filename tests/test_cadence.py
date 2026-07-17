from datetime import UTC, datetime, timedelta

from outreach.cadence import CadencePolicy, build_cadence_plan, guard_cadence_action
from outreach.tracking import ContactRecord, OutreachChannel, TouchpointRecord


START = datetime(2026, 7, 1, 12, tzinfo=UTC)


def touchpoint(
    suffix: str,
    *,
    at: datetime,
    status: str,
    kind: str,
    channel: OutreachChannel = OutreachChannel.LINKEDIN,
    message: str = "Message",
    notes: str = "",
) -> TouchpointRecord:
    sent_at = at.isoformat() if status.lower() == "sent" else ""
    return TouchpointRecord(
        touchpoint_id=f"tp-{suffix}",
        organization_id="org-example",
        contact_id="ct-example",
        channel=channel,
        status=status,
        message_kind=kind,
        message_text=message,
        recorded_at=at.isoformat(),
        sent_at=sent_at,
        notes=notes,
    )


def email_contact() -> ContactRecord:
    return ContactRecord(
        contact_id="ct-example",
        organization_id="org-example",
        full_name="Alex Example",
        email="alex@example.com",
    )


def linkedin_history() -> list[TouchpointRecord]:
    return [
        touchpoint(
            "invite",
            at=START - timedelta(days=2),
            status="Sent",
            kind="linkedin_invite",
            message="Specific invitation note",
        ),
        touchpoint(
            "accepted",
            at=START,
            status="Accepted",
            kind="linkedin_reconcile",
            message="LinkedIn invite accepted.",
        ),
    ]


def recommendation(items, channel: str):
    return next(item for item in items if item.channel == channel)


def test_linkedin_first_followup_is_due_four_days_after_acceptance() -> None:
    history = linkedin_history()

    early = recommendation(
        build_cadence_plan(history, as_of=START + timedelta(days=3)),
        "linkedin",
    )
    due = recommendation(
        build_cadence_plan(history, as_of=START + timedelta(days=4)),
        "linkedin",
    )

    assert early.action == "linkedin_followup_1"
    assert early.state == "upcoming"
    assert due.state == "due"
    assert due.due_at == START + timedelta(days=4)


def test_second_linkedin_touch_is_due_four_to_five_days_later_and_must_be_distinct() -> None:
    first_message = (
        "Thanks for connecting. The data-platform launch maps to work I did at Hevo. "
        "Is product hiring likely this quarter?"
    )
    history = linkedin_history() + [
        touchpoint(
            "followup-1",
            at=START + timedelta(days=4),
            status="Sent",
            kind="linkedin_followup",
            message=first_message,
        )
    ]
    as_of = START + timedelta(days=8)

    item = recommendation(build_cadence_plan(history, as_of=as_of), "linkedin")
    duplicate = guard_cadence_action(
        history,
        organization_id="org-example",
        contact_id="ct-example",
        channel="linkedin",
        action="linkedin_followup_2_value_add",
        proposed_at=as_of,
        proposed_message=first_message + "!",
    )
    distinct = guard_cadence_action(
        history,
        organization_id="org-example",
        contact_id="ct-example",
        channel="linkedin",
        action="linkedin_followup_2_value_add",
        proposed_at=as_of,
        proposed_message=(
            "I saw Example opened a BizOps role yesterday. My marketplace operations work at Gojek "
            "may be relevant; is Jordan the right person to ask about it?"
        ),
    )

    assert item.state == "due"
    assert item.due_at == START + timedelta(days=8)
    assert item.due_by == START + timedelta(days=9)
    assert item.requires_distinct_value_add is True
    assert duplicate.allowed is False
    assert any("repeats" in reason for reason in duplicate.reasons)
    assert distinct.allowed is True


def test_pending_linkedin_invite_never_gets_a_generic_followup() -> None:
    history = [
        touchpoint(
            "invite",
            at=START,
            status="Sent",
            kind="linkedin_invite",
            message="Specific invitation note",
        )
    ]

    item = recommendation(
        build_cadence_plan(history, as_of=START + timedelta(days=20)),
        "linkedin",
    )

    assert item.action == "none"
    assert item.state == "waiting"
    assert "before acceptance" in item.reason


def test_email_followups_use_day_four_and_max_three_touches_per_ninety_days() -> None:
    initial = touchpoint(
        "email-1",
        at=START,
        status="Sent",
        kind="email_initial",
        channel=OutreachChannel.EMAIL,
        message="Initial email",
    )
    first_plan = build_cadence_plan(
        [initial],
        contacts=[email_contact()],
        as_of=START + timedelta(days=4),
    )
    first = recommendation(first_plan, "email")
    email_followup = touchpoint(
        "email-2",
        at=START + timedelta(days=4),
        status="Sent",
        kind="email_followup",
        channel=OutreachChannel.EMAIL,
        message="First follow-up",
    )
    final = recommendation(
        build_cadence_plan(
            [initial, email_followup],
            contacts=[email_contact()],
            as_of=START + timedelta(days=8),
        ),
        "email",
    )
    third = touchpoint(
        "email-3",
        at=START + timedelta(days=8),
        status="Sent",
        kind="email_final",
        channel=OutreachChannel.EMAIL,
        message="Final note",
    )
    complete = recommendation(
        build_cadence_plan(
            [initial, email_followup, third],
            contacts=[email_contact()],
            as_of=START + timedelta(days=9),
        ),
        "email",
    )

    assert first.action == "email_followup_1"
    assert first.state == "due"
    assert final.action == "email_final_optional"
    assert final.state == "due"
    assert final.due_by == START + timedelta(days=9)
    assert complete.state == "complete"
    assert complete.touches_in_window == 3


def test_same_day_cross_channel_touch_suppresses_email() -> None:
    initial = touchpoint(
        "email-1",
        at=START,
        status="Sent",
        kind="email_initial",
        channel=OutreachChannel.EMAIL,
    )
    linkedin_today = touchpoint(
        "linkedin-today",
        at=START + timedelta(days=4, hours=1),
        status="Sent",
        kind="linkedin_manual_message",
        message="Useful LinkedIn message",
    )

    item = recommendation(
        build_cadence_plan(
            [initial, linkedin_today],
            contacts=[email_contact()],
            as_of=START + timedelta(days=4, hours=2),
        ),
        "email",
    )

    assert item.action == "email_followup_1"
    assert item.state == "suppressed"
    assert "double tap" in item.reason


def test_followup_window_expires_instead_of_remaining_automatically_due() -> None:
    item = recommendation(
        build_cadence_plan(
            linkedin_history(),
            as_of=START + timedelta(days=20),
        ),
        "linkedin",
    )

    assert item.state == "expired"
    assert "review manually" in item.reason


def test_first_followup_stays_due_within_grace_window_when_runs_slip() -> None:
    # Ten days past acceptance (six days past the day-4 due date) is well inside
    # the two-week grace window, so a first follow-up must stay auto-sendable
    # instead of expiring to manual review when nightly runs fall behind.
    item = recommendation(
        build_cadence_plan(
            linkedin_history(),
            as_of=START + timedelta(days=10),
        ),
        "linkedin",
    )

    assert item.action == "linkedin_followup_1"
    assert item.state == "due"


def test_same_local_day_is_suppressed_across_a_utc_date_boundary() -> None:
    as_of = datetime(2026, 7, 2, 1, tzinfo=UTC)
    initial = touchpoint(
        "email-local-day",
        at=as_of - timedelta(days=4),
        status="Sent",
        kind="email_initial",
        channel=OutreachChannel.EMAIL,
    )
    linkedin_same_la_day = touchpoint(
        "linkedin-local-day",
        at=datetime(2026, 7, 1, 7, 30, tzinfo=UTC),
        status="Sent",
        kind="linkedin_manual_message",
    )

    item = recommendation(
        build_cadence_plan(
            [initial, linkedin_same_la_day],
            contacts=[email_contact()],
            as_of=as_of,
            policy=CadencePolicy(day_timezone="America/Los_Angeles"),
        ),
        "email",
    )

    assert item.state == "suppressed"


def test_reply_pauses_and_unsubscribe_stops_all_cold_cadences() -> None:
    reply = touchpoint(
        "reply",
        at=START + timedelta(days=1),
        status="Replied",
        kind="linkedin_reply",
        message="LinkedIn reply detected.",
    )
    paused = build_cadence_plan(
        linkedin_history() + [reply],
        contacts=[email_contact()],
        as_of=START + timedelta(days=10),
    )
    unsubscribe = touchpoint(
        "unsubscribe",
        at=START + timedelta(days=2),
        status="Unsubscribed",
        kind="unsubscribe",
        channel=OutreachChannel.EMAIL,
        message="Please remove me",
    )
    stopped = build_cadence_plan(
        linkedin_history() + [reply, unsubscribe],
        contacts=[email_contact()],
        as_of=START + timedelta(days=10),
    )

    assert {item.state for item in paused} == {"paused"}
    assert {item.state for item in stopped} == {"stopped"}

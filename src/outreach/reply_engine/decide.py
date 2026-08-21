"""Layer 3: decide what happens next.

Priority-ordered, first match wins, entirely deterministic.  This table is the
whole decision engine and it is small on purpose - the unbounded variety lives
in :mod:`extract`, not here.

Ordering does the real work.  Rule 5 firing before any ask rule is what stops
the engine talking over a direct question.  Rule 1 producing a *contact row*
rather than prose is what recovers referrals that were previously discarded as
plain text.
"""

from __future__ import annotations

from datetime import datetime

from ..tracking import ContactRecord, OpportunityRecord
from .context import (
    CompanyFacts,
    APPLY_NOW,
    NOT_ACTIONABLE,
    campaign_goal,
    pick_actionable_requisition,
    resolve_capability,
    role_family_from_invite,
    select_ask,
)
from .models import (
    ASK_BUDGETS,
    WORD_BUDGETS,
    Action,
    Ask,
    Capability,
    Decision,
    ThreadRead,
    ThreadState,
    availability_qualifier_for,
    requires_availability_qualifier,
)
from .touches import effective_touch_cap, touch_cap_reached

def _budget(action: Action, ask: Ask) -> int:
    budget = WORD_BUDGETS.get(action, 60)
    if ask in ASK_BUDGETS:
        budget = min(budget, ASK_BUDGETS[ask])
    return budget


def _touch_scaled_budget(budget: int, touch_count: int) -> int:
    """Later unanswered touches earn less space, never more."""

    multiplier = max(0.5, 1.0 - (0.25 * max(0, touch_count)))
    return max(20, round(budget * multiplier))


def _decide(
    *,
    state: ThreadState,
    read: ThreadRead,
    contact: ContactRecord,
    facts: CompanyFacts,
    opportunities: list[OpportunityRecord] | None = None,
    order_confident: bool = True,
    capture_confidence: str = "full",
    inbound_message_count: int = 0,
    touch_count: int = 0,
    reopen_condition_fired: bool = False,
    band: str = "",
    pursuit_season: str = "fall",
    now: datetime | None = None,
) -> Decision:
    opportunities = opportunities or []
    requisition, req_actionability = pick_actionable_requisition(
        opportunities,
        facts,
        pursuit_season=pursuit_season,
        now=now,
    )
    capability = resolve_capability(
        contact,
        facts,
        declared=read.capability,
        state=state,
    )

    # -- Rule 0: state gates ------------------------------------------------
    if state is ThreadState.CLOSED_HARD:
        return Decision(action=Action.SUPPRESS, rule=0, reason="do_not_contact")
    if state is ThreadState.CLOSED_OFFCHANNEL:
        return Decision(
            action=Action.SUPPRESS,
            rule=0,
            reason="conversation moved off LinkedIn; do not redraft here",
        )
    if state is ThreadState.YOU_REPLIED_LAST:
        return Decision(
            action=Action.SUPPRESS, rule=0, reason="ball is in their court"
        )
    if state is ThreadState.PARKED:
        return Decision(
            action=Action.SUPPRESS,
            rule=0,
            reason="parked pending durable reopen condition",
        )
    # A large-company contact is relationship capital for the 2027 cycle.
    # Once the next unanswered follow-up would spend the ordinary cap, park
    # instead of writing a ceremonial closing note. An explicit 2027/new-grad
    # requisition can deterministically reopen this condition later.
    if (
        state in {ThreadState.NO_CONTEXT, ThreadState.OUTBOUND_UNANSWERED}
        and facts.is_large_company
        and max(0, touch_count) + 1
        >= effective_touch_cap(reopen_condition_fired=reopen_condition_fired)
    ):
        return Decision(
            action=Action.SUPPRESS,
            rule=0,
            reason=(
                "large-company relationship touch cap reached; suppress now "
                "and preserve for 2027 re-entry"
            ),
            reopen_condition=(
                f"2027 full-time or new-grad product recruiting opens at {facts.name}"
            ),
        )
    if state in {ThreadState.NO_CONTEXT, ThreadState.OUTBOUND_UNANSWERED} and touch_cap_reached(
        touch_count,
        reopen_condition_fired=reopen_condition_fired,
    ):
        cap = effective_touch_cap(
            reopen_condition_fired=reopen_condition_fired
        )
        return Decision(
            action=Action.SUPPRESS,
            rule=0,
            reason=(
                f"outbound follow-up touch cap reached ({touch_count}/{cap}); "
                "wait for a real external trigger"
            ),
        )
    if capture_confidence != "full" and inbound_message_count <= 1:
        return Decision(
            action=Action.HOLD,
            rule=0,
            reason=(
                "thread capture partial with only one inbound message; "
                "cannot safely infer the conversation"
            ),
        )
    if not order_confident:
        return Decision(
            action=Action.HOLD,
            rule=0,
            reason="thread order unreliable; cannot tell what replied to what",
        )

    # -- Rule 1: they named someone ----------------------------------------
    if read.named_people:
        decision = Decision(
            action=Action.CREATE_CONTACTS,
            rule=1,
            reason="routed us to named people",
            contacts_to_create=list(read.named_people),
        )
        if read.capability is Capability.NO_LONGER_THERE:
            decision.human_tasks.append(
                f"Update {facts.name} record: {contact.full_name} has left"
            )
        decision.word_budget = _budget(Action.CREATE_CONTACTS, Ask.NONE)
        return decision

    # -- Rule 2: they asked for a call -------------------------------------
    if read.explicit_request == "call":
        return Decision(
            action=Action.SCHEDULE,
            rule=2,
            reason="they offered a call; propose times",
            word_budget=_budget(Action.SCHEDULE, Ask.NONE),
        )

    # -- Rule 3: they asked for the resume ---------------------------------
    if read.explicit_request in {"resume", "intro_material"}:
        return Decision(
            action=Action.SEND_ATTACHMENT,
            rule=3,
            reason="they asked for materials",
            word_budget=_budget(Action.SEND_ATTACHMENT, Ask.NONE),
            human_tasks=["Attach resume before sending - message is blocked until done"],
        )

    # -- Rule 4: they named an opening -------------------------------------
    if read.named_opening:
        ask = (
            select_ask(
                capability,
                has_citable_req=requisition is not None,
                req_actionability=req_actionability,
            )
            if req_actionability != NOT_ACTIONABLE
            else Ask.NAME
        )
        return Decision(
            action=Action.RESOLVE_REQ,
            ask=ask,
            rule=4,
            reason="they named an opening",
            citable_req=requisition.title if requisition else "",
            citable_req_url=requisition.source_url if requisition else "",
            req_actionability=req_actionability,
            word_budget=_budget(Action.RESOLVE_REQ, ask),
        )

    # -- Rule 5: they asked us something -----------------------------------
    if read.question_asked_of_me:
        return Decision(
            action=Action.ANSWER,
            rule=5,
            reason="unanswered direct question",
            word_budget=_budget(Action.ANSWER, Ask.NONE),
        )

    # -- Rule 6: they cannot help ------------------------------------------
    if capability in {Capability.CANNOT_HELP, Capability.NO_LONGER_THERE}:
        reopen = (
            read.commitments_i_made[0]
            if read.commitments_i_made
            else f"a concrete {facts.name} product role appears"
        )
        decision = Decision(
            action=Action.PARK,
            rule=6,
            reason="they said they cannot help",
            reopen_condition=reopen,
            word_budget=_budget(Action.PARK, Ask.NONE),
        )
        if read.their_need:
            decision.action = Action.RECIPROCATE
            decision.reason = "they cannot help and they need something themselves"
            decision.word_budget = _budget(Action.RECIPROCATE, Ask.NONE)
        return decision

    # -- Rule 6b: they already said yes to a standing ask -------------------
    if read.acknowledged_standing_ask:
        # "Sure, let me know" / "Yes that will be great" is agreement, not an
        # invitation to send more.  Acknowledging an acknowledgement is noise,
        # so nothing goes out - the next move belongs to the world.
        return Decision(
            action=Action.SUPPRESS,
            rule=6,
            reason="they agreed to a standing ask; waiting on an external trigger",
            reopen_condition=(
                read.commitments_i_made[0]
                if read.commitments_i_made
                else f"a concrete {facts.name} role we can send them"
            ),
        )

    # -- Rule 7: mass blast -------------------------------------------------
    if read.is_mass_blast:
        if capability in {Capability.CAN_CREATE, Capability.CAN_REFER}:
            return Decision(
                action=Action.TRANSACT,
                rule=7,
                reason="decision-maker broadcasting; give real feedback first",
                word_budget=_budget(Action.TRANSACT, Ask.NONE),
                human_tasks=["Actually engage with their launch before this sends"],
                send_after_days=0,
            )
        return Decision(
            action=Action.SUPPRESS, rule=7, reason="broadcast noise, no authority"
        )

    # -- Rule 8: they need something ---------------------------------------
    if read.their_need:
        return Decision(
            action=Action.RECIPROCATE,
            rule=8,
            reason="offer help before asking",
            word_budget=_budget(Action.RECIPROCATE, Ask.NONE),
        )

    # -- Rule 9: they offered something ------------------------------------
    if read.offer_made != "none":
        return Decision(
            action=Action.ACCEPT_OFFER,
            ask=select_ask(
                capability,
                has_citable_req=req_actionability == APPLY_NOW,
                req_actionability=req_actionability,
            ),
            rule=9,
            reason=f"they offered {read.offer_made}",
            citable_req=requisition.title if requisition else "",
            citable_req_url=requisition.source_url if requisition else "",
            req_actionability=req_actionability,
            word_budget=_budget(Action.ACCEPT_OFFER, Ask.NONE),
        )

    # -- Rule 10: nothing to respond to, so we initiate ---------------------
    ask = (
        read.prior_outbound_ask
        if state is ThreadState.OUTBOUND_UNANSWERED
        and read.prior_outbound_ask is not Ask.NONE
        else select_ask(
            capability,
            has_citable_req=req_actionability == APPLY_NOW,
            req_actionability=req_actionability,
        )
    )
    if ask is Ask.NONE:
        return Decision(action=Action.SUPPRESS, rule=11, reason="no viable ask")

    return Decision(
        action=Action.ASK,
        ask=ask,
        rule=11,
        reason=f"initiating with {ask.value} ask",
        citable_req=requisition.title if requisition else "",
        citable_req_url=requisition.source_url if requisition else "",
        req_actionability=req_actionability,
        word_budget=_budget(Action.ASK, ask),
    )


def decide(
    *,
    state: ThreadState,
    read: ThreadRead,
    contact: ContactRecord,
    facts: CompanyFacts,
    opportunities: list[OpportunityRecord] | None = None,
    order_confident: bool = True,
    capture_confidence: str = "full",
    inbound_message_count: int = 0,
    touch_count: int = 0,
    reopen_condition_fired: bool = False,
    band: str = "",
    pursuit_season: str = "fall",
    now: datetime | None = None,
    invite_text: str = "",
    has_prior_outbound: bool = False,
) -> Decision:
    """Run the priority table, then attach required compose fields."""

    # Layer-2 contextual value: verified tiny-team size changes the useful IC
    # question from org mapping to an approach recommendation.  The Layer-3
    # ask remains INTEL; no new decision-table branch is introduced.
    resolved_capability = resolve_capability(
        contact,
        facts,
        declared=read.capability,
        state=state,
    )
    if (
        read.intel_focus != "timing"
        and facts.needs_approach_recommendation
        and resolved_capability is Capability.CAN_OPINE
    ):
        read.intel_focus = "approach"

    decision = _decide(
        state=state,
        read=read,
        contact=contact,
        facts=facts,
        opportunities=opportunities,
        order_confident=order_confident,
        capture_confidence=capture_confidence,
        inbound_message_count=inbound_message_count,
        touch_count=touch_count,
        reopen_condition_fired=reopen_condition_fired,
        band=band,
        pursuit_season=pursuit_season,
        now=now,
    )
    decision.campaign_track = facts.campaign_track
    decision.goal_role_family = role_family_from_invite(invite_text)
    decision.goal = campaign_goal(
        facts,
        role_family=decision.goal_role_family,
        season=pursuit_season,
    )
    if requires_availability_qualifier(decision, read):
        decision.availability_qualifier = (
            "full-time roles after MBA graduation and the 2027 new-grad cycle"
            if facts.is_large_company
            else availability_qualifier_for(pursuit_season)
        )
    decision.touch_number = max(0, touch_count) + 1
    decision.terminal_touch = bool(
        decision.emits_message
        and state in {ThreadState.NO_CONTEXT, ThreadState.OUTBOUND_UNANSWERED}
        and decision.touch_number
        >= effective_touch_cap(reopen_condition_fired=reopen_condition_fired)
    )
    decision.create_direct_ask = bool(
        decision.ask is Ask.CREATE and has_prior_outbound
    )
    if decision.emits_message:
        decision.word_budget = _touch_scaled_budget(
            decision.word_budget,
            touch_count,
        )
    return decision

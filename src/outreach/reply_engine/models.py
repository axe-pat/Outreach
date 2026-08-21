"""Typed vocabulary for the reply engine.

The engine is deliberately split so that *situations* are read by a model and
*decisions* are made by code.  Everything in this module is part of the
deterministic contract between those two halves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ThreadState(str, Enum):
    """Where a conversation actually stands."""

    NO_CONTEXT = "no_context"
    """Invite accepted, but no real message exists yet."""

    OUTBOUND_UNANSWERED = "outbound_unanswered"
    """A real post-invite outbound exists, with no captured reply after it."""

    THEY_REPLIED_UNANSWERED = "they_replied_unanswered"
    YOU_REPLIED_LAST = "you_replied_last"
    PARKED = "parked"
    """No outbound now.  Contact retained.  A reopen condition is recorded."""

    CLOSED_OFFCHANNEL = "closed_offchannel"
    """Conversation moved to email/phone.  Never redraft on LinkedIn."""

    CLOSED_HARD = "closed_hard"


class Capability(str, Enum):
    """What this person can actually do for us."""

    CAN_CREATE = "can_create"
    """Authority to make a role exist: founder/exec at a small company."""

    CAN_REFER = "can_refer"
    CAN_NAME = "can_name"
    DECLINED_REFERRAL = "declined_referral"
    """Will not refer us, but may still give advice or name the right person."""

    CAN_OPINE = "can_opine"
    """No authority, but they are inside and know how hiring works."""

    CANNOT_HELP = "cannot_help"
    NO_LONGER_THERE = "no_longer_there"


class Ask(str, Enum):
    """The ask ladder, ordered by what it costs the recipient."""

    NONE = "none"
    INTEL = "intel"
    """Cheapest real ask: who owns product hiring from an IC's own seat.
    Timing is used only when the thread makes timing the genuine unknown.
    This is the correct ask for anyone without authority."""

    NAME = "name"
    FORWARD = "forward"
    REFER = "refer"
    CREATE = "create"
    """Highest value: ask a founder to make a role.  Never framed as
    'do you have an internship' - that is a yes/no about a category that
    does not exist at a six-person company."""


class Action(str, Enum):
    """What the engine decided to do.  Several of these are not messages."""

    CREATE_CONTACTS = "create_contacts"
    SCHEDULE = "schedule"
    SEND_ATTACHMENT = "send_attachment"
    RESOLVE_REQ = "resolve_req"
    ANSWER = "answer"
    PARK = "park"
    TRANSACT = "transact"
    RECIPROCATE = "reciprocate"
    ACCEPT_OFFER = "accept_offer"
    ASK = "ask"
    HOLD = "hold"
    """Blocked on a data problem.  Needs a human before anything sends."""

    SUPPRESS = "suppress"
    """Deliberately no message: collision policy, no authority, parked band."""


#: Word budget per action.  The current engine averages 73 words, which is
#: long for a cold LinkedIn message to a stranger.
WORD_BUDGETS: dict[Action, int] = {
    Action.PARK: 25,
    Action.CREATE_CONTACTS: 30,
    Action.SCHEDULE: 40,
    Action.SEND_ATTACHMENT: 45,
    Action.ANSWER: 60,
    Action.RESOLVE_REQ: 55,
    Action.ACCEPT_OFFER: 55,
    Action.RECIPROCATE: 45,
    Action.TRANSACT: 50,
    Action.ASK: 70,
}

#: Ask-specific ceilings, applied on top of the action budget.
ASK_BUDGETS: dict[Ask, int] = {
    Ask.INTEL: 45,
    Ask.NAME: 50,
    Ask.FORWARD: 50,
    Ask.REFER: 65,
    Ask.CREATE: 70,
}


@dataclass(frozen=True)
class NamedPerson:
    """Somebody the contact pointed us at.  These become contact rows."""

    name: str
    role_hint: str = ""
    why: str = ""


@dataclass
class ThreadRead:
    """Layer 2 output: the model's structured read of one conversation.

    The model never writes prose here.  Every field is a question about the
    *person*, not a description of the scenario - which is why eleven fields
    cover an unbounded variety of situations.
    """

    question_asked_of_me: str | None = None
    question_kind: str = "none"
    """background_fit | interest_availability_intent | other | none"""

    intern_economics_objection: bool = False
    """They raised budget, headcount, or small-team capacity themselves."""

    intel_focus: str = "routing"
    """routing | timing | approach | opening.

    Timing must be grounded in a thread-specific unknown.  Approach is the
    tiny-team variant: ask whether contacting the founders directly is the
    right move instead of asking an obvious org-chart question. Opening is
    used when an unanswered referral request outlived its requisition: ask
    whether the insider has heard of a relevant opening before asking them to
    refer.
    """

    named_people: list[NamedPerson] = field(default_factory=list)
    named_opening: str | None = None
    explicit_request: str = "none"
    """resume | call | feedback | upvote | intro_material | none"""

    offer_made: str = "none"
    """intro | referral | route_to_recruiter | advice | none"""

    offer_target: str | None = None
    """Who or what the offered routing would reach, when the thread says it."""

    capability: Capability = Capability.CAN_OPINE
    sentiment: str = "neutral"
    is_mass_blast: bool = False
    acknowledged_standing_ask: bool = False
    """They said "sure, let me know" to an ask we already made.  There is
    nothing to send next - the trigger is an external event, not a nudge."""

    their_need: str | None = None
    factual_errors_about_me: list[str] = field(default_factory=list)
    commitments_i_made: list[str] = field(default_factory=list)
    """Promises we made in-thread.  These become reopen conditions and are
    enforced by the critic - e.g. 'only send you a fit if there's a real
    match' must not be followed by a generic resume drop."""

    prior_outbound_ask: Ask = Ask.NONE
    """Ask already made in an unanswered post-invite outbound.

    This is deterministic Layer-2 context.  Rule 11 normally continues this
    ask. A referral is the exception: it may only persist while its exact
    requisition remains actionable, because a referral without an opening is
    not a real recipient action.
    """

    prior_outbound_text: str | None = None
    """The exact prior outbound that the writer must follow up truthfully."""

    source: str = "deterministic"
    """ai | deterministic - how this read was produced."""


@dataclass
class Decision:
    """Layer 3 output.  Deterministic, derived only from state + read."""

    action: Action
    ask: Ask = Ask.NONE
    rule: int = 0
    reason: str = ""
    word_budget: int = 60
    reopen_condition: str = ""
    contacts_to_create: list[NamedPerson] = field(default_factory=list)
    human_tasks: list[str] = field(default_factory=list)
    citable_req: str = ""
    citable_req_url: str = ""
    req_actionability: str = "not_actionable"
    """apply_now | create_wedge | pipeline_signal | not_actionable"""
    send_after_days: int = 0
    """Deliberate delay, used by TRANSACT and collision fallbacks."""

    availability_qualifier: str = ""
    """Required pursuit constraint supplied to the writer as structured data."""

    campaign_track: str = "startup"
    """startup | large_company, derived from deterministic company facts."""

    goal_role_family: str = "product"
    """product | bizops_strategy | engineering | general, inherited from the invite."""

    goal: str = ""
    """Compose-ready objective selected by track and prior framing."""

    touch_number: int = 1
    """One-indexed follow-up number; the connection invite is not counted."""

    terminal_touch: bool = False
    """This message spends the final permitted follow-up touch."""

    create_direct_ask: bool = False
    """A prior founder pitch ran; ask directly instead of re-offering work."""

    @property
    def emits_message(self) -> bool:
        return self.action not in {Action.SUPPRESS, Action.HOLD}


def availability_qualifier_for(season: str = "fall") -> str:
    """Return the current availability constraint as compose-ready data."""

    normalized = (season or "fall").strip().casefold()
    if normalized not in {"fall", "spring", "summer", "winter"}:
        normalized = "fall"
    return (
        f"{normalized} product internship or co-op; not a full-time role "
        "while Akshat is mid-MBA"
    )


def requires_availability_qualifier(decision: Decision, read: ThreadRead) -> bool:
    """Whether this move may cause the recipient to act on Akshat's behalf."""

    if decision.ask in {Ask.REFER, Ask.FORWARD}:
        return True
    if decision.action in {Action.CREATE_CONTACTS, Action.RESOLVE_REQ}:
        return True
    return (
        decision.action is Action.ACCEPT_OFFER
        and read.offer_made in {"intro", "referral", "route_to_recruiter"}
    )


@dataclass
class ReplyDraft:
    """Final artifact row."""

    contact_id: str
    organization_id: str
    name: str
    company: str
    title: str
    thread_state: ThreadState
    read: ThreadRead
    decision: Decision
    segment: str = "reply"
    capability: Capability = Capability.CAN_OPINE
    """Resolved authority (title x company size), not the raw read."""

    message: str = ""
    critic_flags: list[str] = field(default_factory=list)
    critic_passed: bool = True
    compose_source: str = "deterministic"
    last_message: str = ""
    last_sender: str = ""
    capture_confidence: str = "full"
    """full when the LinkedIn scraper reached the start of the thread."""

    captured_message_count: int = 0
    expected_message_count: int | None = None
    touch_count: int = 0
    """Tracker-confirmed prior outbound LinkedIn follow-up touches."""

    touch_cap_reached: bool = False
    reopen_condition_fired: bool = False
    usable_proof: list[str] = field(default_factory=list)
    """Resume-verified proof beat IDs selected for this recipient domain."""

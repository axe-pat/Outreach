"""Facts the writer is allowed to use, and the gates that keep it honest.

Three jobs:

* parse trustworthy company facts out of ``organizations.csv`` notes
* resolve what a recipient can actually do (authority x company size)
* decide whether a job requisition may be cited in a message at all
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ..tracking import (
    ContactRecord,
    OpportunityRecord,
    OpportunityType,
    OrganizationRecord,
)
from .models import Ask, Capability, ThreadState

# --------------------------------------------------------------------------
# Company facts
# --------------------------------------------------------------------------

_TEAM_SIZE = re.compile(r"team_size=([\d,]+)")
_TEAM_SIZE_RANGE = re.compile(
    r"(?:team_size_range|team_size)=~?([\d,]+)\s*-\s*([\d,]+)",
    re.I,
)
_BATCH = re.compile(r"batch=([^|]+)")
_DESCRIPTION = re.compile(r"description=(.*?)(?: \| [a-z_]+=|$)", re.S)

#: Some ``description=`` fields contain story-fit analysis about the candidate
#: rather than a fact about the company.  Entrust reads "Clear APM role with
#: product ownership ... where Akshat's backend and systems architecture
#: experience ...".  Feeding that to the writer produces nonsense, so it is
#: rejected and the message goes out without a company observation.
_ABOUT_US_NOT_THEM = re.compile(
    r"\bakshat\b|\bhis\b|story[_ ]fit|good fit for|leverage from|"
    r"pitch around|maps well to",
    re.I,
)

_SMALL_COMPANY_CEILING = 200
_TIGHT_KNIT_COMPANY_CEILING = 10
_APPROACH_RECOMMENDATION_CEILING = 25
_LARGE_COMPANY_FLOOR = 1000

STARTUP_TRACK = "startup"
LARGE_COMPANY_TRACK = "large_company"
ROLE_PRODUCT = "product"
ROLE_BIZOPS_STRATEGY = "bizops_strategy"
ROLE_ENGINEERING = "engineering"
ROLE_GENERAL = "general"


@dataclass(frozen=True)
class CompanyFacts:
    name: str
    team_size: int | None = None
    team_size_min: int | None = None
    team_size_max: int | None = None
    batch: str = ""
    description: str = ""
    is_startup: bool = False

    @property
    def is_small(self) -> bool:
        if self.team_size is not None:
            return self.team_size <= _SMALL_COMPANY_CEILING
        if self.team_size_max is not None:
            return self.team_size_max <= _SMALL_COMPANY_CEILING
        return self.is_startup and bool(self.batch)

    @property
    def is_tight_knit(self) -> bool:
        """Small enough that an ordinary employee plausibly knows the org."""

        return (
            self.team_size is not None
            and self.team_size <= _TIGHT_KNIT_COMPANY_CEILING
        )

    @property
    def is_large_company(self) -> bool:
        lower_bound = (
            self.team_size
            if self.team_size is not None
            else self.team_size_min
        )
        return lower_bound is not None and lower_bound >= _LARGE_COMPANY_FLOOR

    @property
    def needs_approach_recommendation(self) -> bool:
        """Tiny teams should get an approach question, not org mapping.

        At twenty-five people or fewer, asking who "owns product hiring" is
        usually asking the recipient to state the obvious.  A recommendation
        about whether to approach the founders is the useful low-cost INTEL
        question instead.
        """

        upper_bound = (
            self.team_size
            if self.team_size is not None
            else self.team_size_max
        )
        return (
            upper_bound is not None
            and upper_bound <= _APPROACH_RECOMMENDATION_CEILING
        )

    @property
    def campaign_track(self) -> str:
        return LARGE_COMPANY_TRACK if self.is_large_company else STARTUP_TRACK

    @property
    def has_usable_description(self) -> bool:
        return bool(self.description)


def company_facts(organization: OrganizationRecord | None) -> CompanyFacts:
    if organization is None:
        return CompanyFacts(name="")
    notes = organization.notes or ""

    team_size = None
    team_size_min = None
    team_size_max = None
    if match := _TEAM_SIZE.search(notes):
        try:
            team_size = int(match.group(1).replace(",", ""))
        except ValueError:
            team_size = None
    if range_match := _TEAM_SIZE_RANGE.search(notes):
        try:
            team_size_min = int(range_match.group(1).replace(",", ""))
            team_size_max = int(range_match.group(2).replace(",", ""))
        except ValueError:
            team_size_min = None
            team_size_max = None

    batch = match.group(1).strip() if (match := _BATCH.search(notes)) else ""

    description = ""
    if match := _DESCRIPTION.search(notes):
        candidate = " ".join(match.group(1).split())[:400]
        if candidate and not _ABOUT_US_NOT_THEM.search(candidate):
            description = candidate

    return CompanyFacts(
        name=organization.name or "",
        team_size=team_size,
        team_size_min=team_size_min,
        team_size_max=team_size_max,
        batch=batch,
        description=description,
        is_startup=str(getattr(organization.organization_type, "value", organization.organization_type)) == "startup",
    )


# --------------------------------------------------------------------------
# Authority
# --------------------------------------------------------------------------

_FOUNDER_EXEC = re.compile(
    r"\b(co[- ]?founder|founder|ceo|cto|cpo|coo|chief|executive chairman|"
    r"engineering leader|senior principal|principal|lead|"
    r"vp of product|vice president of product|head of product)\b",
    re.I,
)
_SENIOR = re.compile(
    r"\b(vp|vice president|head of|director|principal|staff|senior manager|"
    r"sr\.? director|engineering leader|manager)\b",
    re.I,
)
_FOUNDING_ENGINEER = re.compile(r"\bfounding engineer\b", re.I)
#: Students, interns and contractors cannot refer.  The previous engine asked
#: a game-development undergraduate "who owns product there".
#:
#: The "<program> @ <school>" idiom is how LinkedIn headlines encode "I am a
#: student" - "GDD @ RIT", "CS @ FIU", "MSCS @ USC".  Matching only known
#: academic programmes keeps "Product @ Cisco" and "PM @ Google" out of it.
_ACADEMIC_PROGRAM = (
    r"gdd|hci|cs|ee|me|mis|ds|is|ml|statistics|mscs|msc|ms|ba|bs|bsc|mba|meng|phd|mfa|econ"
)
_JUNIOR = re.compile(
    r"\binterns?\b|\bstudent\b|\bincoming\b|\bfellow\b|\bgrad\b|\bcandidate\b|"
    rf"\b(?:{_ACADEMIC_PROGRAM})\b(?:[,/& ]+[a-z]{{2,12}})?\s*@|"
    r"\bb\.?s\.?\b|\bm\.?s\.?\s|['’]2[6-9]\b",
    re.I,
)
_NON_FTE = re.compile(r"\bcontractor\b|\bconsultant\b|\bfreelance\b|\bopentowork\b", re.I)
_NAME_FUNCTION = re.compile(
    r"\bproduct\b|\bproduct management\b|\bproduct operations?\b|"
    r"\b(?:business|biz|strategy|revenue|growth|sales) ops\b|"
    r"\boperations\b|\bchief of staff\b",
    re.I,
)
_RECRUITING_AUTHORITY = re.compile(
    r"\brecruit(?:er|ing|ment)\b|\btalent acquisition\b|\btalent partner\b|"
    r"\bhuman resources?\b|\bhr\b|人事|採用",
    re.I,
)
_ROUTING_AUTHORITY = re.compile(
    r"\b(?:svp|evp|vp|vice president|head of|director|manager|chief)\b|"
    r"\b(?:group |senior |staff )?product manager\b",
    re.I,
)

_INVITE_BIZOPS = re.compile(
    r"\bbiz\s*ops\b|\bbusiness operations?\b|"
    r"\bstrategy(?:\s*&\s*operations?)?\b|\bstrategic operations?\b",
    re.I,
)
_INVITE_PRODUCT = re.compile(r"\bproduct\b|\bpm\b", re.I)
_INVITE_ENGINEERING = re.compile(
    r"\bengineering\b|\bsoftware\b|\bswe\b|\bdeveloper\b",
    re.I,
)


def role_family_from_invite(invite_text: str) -> str:
    """Preserve what the original outreach said Akshat was pursuing."""

    if not (invite_text or "").strip():
        # Warm/never-invited contacts have no prior framing to preserve. The
        # campaign's default role family is product, not an unspecified role.
        return ROLE_PRODUCT
    if _INVITE_BIZOPS.search(invite_text or ""):
        return ROLE_BIZOPS_STRATEGY
    if _INVITE_PRODUCT.search(invite_text or ""):
        return ROLE_PRODUCT
    if _INVITE_ENGINEERING.search(invite_text or ""):
        return ROLE_ENGINEERING
    return ROLE_GENERAL


def campaign_goal(
    facts: CompanyFacts,
    *,
    role_family: str,
    season: str = "fall",
) -> str:
    labels = {
        ROLE_PRODUCT: "product",
        ROLE_BIZOPS_STRATEGY: "BizOps or strategy",
        ROLE_ENGINEERING: "engineering",
        ROLE_GENERAL: "relevant",
    }
    label = labels.get(role_family, "relevant")
    if facts.is_large_company:
        return (
            f"full-time {label} roles and the 2027 new-grad cycle at {facts.name}; "
            "a referral or routing path is the primary outcome"
        )
    return f"a {season} {label} internship or co-op at {facts.name}"

_FORMER_AUTHORITY = re.compile(r"(?:\bformer\s+|\bex[- ]?)$", re.I)
_AUTHORITY_ORG_BINDING = re.compile(
    r"(?:\bat\s+|@\s*)(?P<organization>[^|;,\u2014\u2013]+)",
    re.I,
)
_AUTHORITY_OF_BINDING = re.compile(
    r"^\s*of\s+(?P<organization>[^|;,\u2014\u2013]+)",
    re.I,
)
_ORG_TOKEN_STOP = {
    "and", "company", "corp", "corporation", "inc", "llc", "ltd", "the",
}


def _organization_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", value.casefold())
        if token not in _ORG_TOKEN_STOP
    }


def _authority_clause(title: str, start: int, end: int) -> tuple[str, str]:
    """Return the authority clause and its suffix after the matched title."""

    boundaries = "|;\u2014\u2013"
    clause_start = max(title.rfind(char, 0, start) for char in boundaries)
    clause_ends = [
        position
        for position in (title.find(char, end) for char in boundaries)
        if position >= 0
    ]
    clause_end = min(clause_ends) if clause_ends else len(title)
    return title[clause_start + 1:clause_end], title[end:clause_end]


def _authority_bound_organization(
    title: str,
    match: re.Match[str],
    *,
    allow_of: bool = False,
) -> str:
    """Find the company after an authority title, allowing a function phrase.

    LinkedIn commonly writes ``SVP Engineering @ Actian``, ``VP Product at X``
    and ``Head of Data @ Y``. The function words are part of the role, not the
    employer, so the explicit ``at``/``@`` binding later in the same clause
    wins. ``Founder of X`` remains supported as a direct binding.
    """

    _clause, suffix = _authority_clause(title, match.start(), match.end())
    if binding := _AUTHORITY_ORG_BINDING.search(suffix):
        return binding.group("organization").strip()
    if allow_of and (binding := _AUTHORITY_OF_BINDING.match(suffix)):
        return binding.group("organization").strip()
    return ""


def _has_target_routing_authority(title: str, company: str) -> bool:
    """Whether a manager/executive role is bound to the target organization."""

    target_tokens = _organization_tokens(company)
    for match in _ROUTING_AUTHORITY.finditer(title):
        if _FORMER_AUTHORITY.search(title[max(0, match.start() - 10):match.start()]):
            continue
        clause, _suffix = _authority_clause(title, match.start(), match.end())
        organization = _authority_bound_organization(title, match)
        if organization:
            bound_tokens = _organization_tokens(organization)
            if target_tokens and target_tokens & bound_tokens:
                return True
            continue
        if re.search(r"\b(?:at)\s+[a-z0-9]|@[a-z0-9]", clause, re.I):
            continue
        # A bare current authority title belongs to the filed company.
        return True
    return False


def _has_target_create_authority(title: str, company: str) -> bool:
    """Bind founder/executive authority to the organization being pursued.

    LinkedIn headlines routinely contain former roles, side projects, and
    unrelated founder identities.  They also use ``founding engineer`` for an
    early IC.  None of those establishes authority to create a role at the
    target company.
    """

    target_tokens = _organization_tokens(company)
    for match in _FOUNDER_EXEC.finditer(title):
        if _FORMER_AUTHORITY.search(title[max(0, match.start() - 10):match.start()]):
            continue
        bound_organization = _authority_bound_organization(
            title,
            match,
            allow_of=True,
        )
        if bound_organization:
            bound_tokens = _organization_tokens(bound_organization)
            if target_tokens and target_tokens & bound_tokens:
                return True
            continue
        clause, _suffix = _authority_clause(title, match.start(), match.end())
        if re.search(r"\b(?:at|of)\s+[a-z0-9]|@[a-z0-9]", clause, re.I):
            # An explicit organization binding elsewhere in the same clause
            # owns the unbound companion title ("CEO/Co-Founder @Kyndoo").
            continue
        # A bare "Founder"/"CEO" title belongs to the contact's filed company.
        # Explicit bindings ("Founder of X") are handled above and must match.
        return True
    return False


def resolve_capability(
    contact: ContactRecord,
    facts: CompanyFacts,
    *,
    declared: Capability | None = None,
    state: ThreadState | None = None,
) -> Capability:
    """Authority x company size.

    ``declared`` comes from the thread read and always wins - if somebody says
    they left the company or cannot help, no title heuristic overrides that.
    """

    if declared in {
        Capability.DECLINED_REFERRAL,
        Capability.CANNOT_HELP,
        Capability.NO_LONGER_THERE,
    }:
        return declared

    title = f"{contact.title or ''} {contact.contact_type or ''}"
    authority_title = (contact.title or "").strip() or (contact.contact_type or "")

    if _RECRUITING_AUTHORITY.search(title):
        return Capability.CAN_REFER
    # An explicit current authority title bound to the target wins over
    # education tokens elsewhere in the headline.  LinkedIn commonly appends
    # "MBA", "MS" or "PhD" to CTO/CEO/Lead titles; those tokens do not turn an
    # executive into a junior contact.
    if _has_target_create_authority(authority_title, facts.name):
        return Capability.CAN_CREATE if facts.is_small else Capability.CAN_REFER
    if _FOUNDING_ENGINEER.search(title):
        # At a tiny team the useful question is whether approaching the
        # founders directly is sensible, not who owns hiring.  This remains
        # an IC-level INTEL move.  At larger startups a founding engineer is
        # senior enough to route toward a real requisition.
        return (
            Capability.CAN_OPINE
            if facts.needs_approach_recommendation
            else Capability.CAN_REFER
        )
    if _JUNIOR.search(title) or _NON_FTE.search(title):
        return Capability.CAN_OPINE
    if _has_target_routing_authority(authority_title, facts.name):
        return Capability.CAN_REFER
    if facts.is_large_company:
        # Track changes the campaign goal, not an individual's authority. An
        # ordinary large-company IC can report what they have seen from their
        # own seat; silence does not make them an org mapper.
        return Capability.CAN_OPINE
    if _SENIOR.search(title) and not _ROUTING_AUTHORITY.search(title):
        return Capability.CAN_REFER
    if state in {ThreadState.NO_CONTEXT, ThreadState.OUTBOUND_UNANSWERED}:
        # Silence is not evidence of routing authority.  NAME must be earned
        # by a relevant function or by a company small enough that most
        # employees plausibly know the organization.  An ordinary IC can
        # still answer one low-cost question from their own seat.
        if facts.needs_approach_recommendation:
            return Capability.CAN_OPINE
        if _NAME_FUNCTION.search(title) or facts.is_tight_knit:
            return Capability.CAN_NAME
        return Capability.CAN_OPINE
    return Capability.CAN_NAME


def silent_intel_allowed(contact: ContactRecord) -> bool:
    """Whether a silent contact can reasonably answer one inside-view question."""

    title = f"{contact.title or ''} {contact.contact_type or ''}"
    return not (_JUNIOR.search(title) or _NON_FTE.search(title))


def select_ask(
    capability: Capability,
    *,
    has_citable_req: bool,
    req_actionability: str = "not_actionable",
) -> Ask:
    """The ask ladder.

    ``NAME`` was the previous engine's only ask - used 103 times out of 185 -
    and it is the worst rung: it costs the recipient real thought and yields
    the least.  It is now the fallback, not the default.
    """

    if capability is Capability.DECLINED_REFERRAL:
        return Ask.NAME
    if req_actionability == APPLY_NOW:
        if capability in {Capability.CAN_CREATE, Capability.CAN_REFER}:
            return Ask.REFER
        if capability is Capability.CAN_NAME:
            return Ask.NAME
        if capability is Capability.CAN_OPINE:
            return Ask.INTEL
        return Ask.NONE
    if req_actionability == PIPELINE_SIGNAL:
        if capability in {
            Capability.CAN_CREATE,
            Capability.CAN_REFER,
            Capability.CAN_NAME,
        }:
            return Ask.NAME
        if capability is Capability.CAN_OPINE:
            return Ask.INTEL
        return Ask.NONE
    if capability is Capability.CAN_CREATE:
        return Ask.CREATE
    if capability is Capability.CAN_REFER:
        return Ask.REFER if has_citable_req else Ask.NAME
    if capability is Capability.CAN_NAME:
        return Ask.NAME
    if capability is Capability.CAN_OPINE:
        # No authority to refer, but an IC can usually name the person who owns
        # product hiring. The writer keeps this to one casual routing question.
        return Ask.INTEL
    return Ask.NONE


# --------------------------------------------------------------------------
# Requisition freshness
# --------------------------------------------------------------------------

CITABLE = "citable"
NEEDS_VERIFICATION = "needs_verification"
STALE = "stale"

APPLY_NOW = "apply_now"
CREATE_WEDGE = "create_wedge"
PIPELINE_SIGNAL = "pipeline_signal"
NOT_ACTIONABLE = "not_actionable"

_SEASON = re.compile(r"\b(fall|summer|spring|winter)\b", re.I)
_YEAR = re.compile(r"\b(20\d{2})\b")
_DEAD_STATUS = {"expired", "closed", "paused_identity_conflict"}
_PRODUCT_FUNCTION = re.compile(
    r"\bproduct(?:\s+(?:manager|management|owner|lead|strategy|operations))?\b|"
    r"\b(?:apm|pm)\b",
    re.I,
)
_WRONG_LEVEL = re.compile(
    r"\b(?:chief|cpo|vice president|vp|head of product|director|principal|staff|"
    r"senior product manager|sr\.? product manager)\b",
    re.I,
)
_INTERNSHIP = re.compile(r"\b(?:intern(?:ship)?|co[- ]?op)\b", re.I)
_NEW_GRAD_MBA = re.compile(
    r"\b(?:new[- ]?grad(?:uate)?|graduate programme|graduate program|mba)\b",
    re.I,
)


def requisition_state(
    opportunity: OpportunityRecord,
    *,
    pursuit_season: str = "fall",
    now: datetime | None = None,
    max_age_days: int = 45,
) -> str:
    """Whether a requisition may be named in an outbound message.

    Of 161 product-ish rows in ``opportunities.csv`` only 9 are fall roles and
    27 are explicitly Summer 2026 - dead as of August 2026 - while just 5 rows
    anywhere are marked expired.  The previous engine cited a dead summer
    requisition to Raymond Chan at Shield AI.
    """

    now = now or datetime.now(UTC)
    if (opportunity.status or "").strip().lower() in _DEAD_STATUS:
        return STALE

    haystack = f"{opportunity.title or ''} {opportunity.notes or ''}"

    season_match = _SEASON.search(haystack)
    if season_match and season_match.group(1).lower() != pursuit_season.lower():
        return STALE

    if year_match := _YEAR.search(haystack):
        if int(year_match.group(1)) < now.year:
            return STALE

    if discovered := (opportunity.discovered_at or "").strip():
        try:
            seen = datetime.fromisoformat(discovered.replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=UTC)
            if (now - seen).days > max_age_days:
                return NEEDS_VERIFICATION
        except ValueError:
            pass

    opportunity_type = str(
        getattr(opportunity.opportunity_type, "value", opportunity.opportunity_type)
    ).casefold()
    # A fresh full-time role is not seasonal, so absence of "fall" is not a
    # reason to distrust it.  Internship/co-op claims are seasonal and must
    # name the campaign season before they may be cited.
    if season_match or opportunity_type == OpportunityType.FULL_TIME.value:
        return CITABLE
    return NEEDS_VERIFICATION


def requisition_actionability(
    opportunity: OpportunityRecord,
    facts: CompanyFacts,
    *,
    pursuit_season: str = "fall",
    now: datetime | None = None,
) -> str:
    """How a real product requisition can be used in this campaign.

    Freshness answers whether the posting is trustworthy.  Actionability is a
    separate question: whether Akshat can apply now, use approved headcount as
    a small-company creation wedge, or treat it only as a large-company
    pipeline signal.
    """

    now = now or datetime.now(UTC)
    if requisition_state(
        opportunity, pursuit_season=pursuit_season, now=now
    ) != CITABLE:
        return NOT_ACTIONABLE

    title = opportunity.title or ""
    haystack = f"{title} {opportunity.notes or ''}"
    # Fit rationales and source notes routinely contain the word "product"
    # even for strategy, engineering, and operations jobs.  Function and level
    # therefore come from the authoritative title only.
    if not _PRODUCT_FUNCTION.search(title) or _WRONG_LEVEL.search(title):
        return NOT_ACTIONABLE

    opportunity_type = str(
        getattr(opportunity.opportunity_type, "value", opportunity.opportunity_type)
    ).casefold()
    is_internship = (
        opportunity_type == OpportunityType.INTERNSHIP.value
        or bool(_INTERNSHIP.search(haystack))
    )
    season = _SEASON.search(haystack)
    season_matches = bool(
        season and season.group(1).casefold() == pursuit_season.casefold()
    )

    year = int(match.group(1)) if (match := _YEAR.search(haystack)) else None
    is_next_cycle_grad = bool(
        year == now.year + 1 and _NEW_GRAD_MBA.search(haystack)
    )
    if (is_internship and season_matches) or is_next_cycle_grad:
        return APPLY_NOW

    is_full_time = opportunity_type == OpportunityType.FULL_TIME.value or (
        opportunity_type == OpportunityType.OTHER.value and not is_internship
    )
    if not is_full_time:
        return NOT_ACTIONABLE
    return CREATE_WEDGE if facts.is_small else PIPELINE_SIGNAL


def pick_actionable_requisition(
    opportunities: list[OpportunityRecord],
    facts: CompanyFacts,
    *,
    pursuit_season: str = "fall",
    now: datetime | None = None,
) -> tuple[OpportunityRecord | None, str]:
    """Pick the strongest actionable product requisition, if one exists."""

    ranked: list[tuple[int, OpportunityRecord, str]] = []
    priority = {APPLY_NOW: 3, CREATE_WEDGE: 2, PIPELINE_SIGNAL: 1}
    for opportunity in opportunities:
        actionability = requisition_actionability(
            opportunity,
            facts,
            pursuit_season=pursuit_season,
            now=now,
        )
        if actionability != NOT_ACTIONABLE:
            ranked.append((priority[actionability], opportunity, actionability))
    if not ranked:
        return None, NOT_ACTIONABLE
    _, opportunity, actionability = max(
        ranked,
        key=lambda item: (item[0], item[1].discovered_at or ""),
    )
    return opportunity, actionability


def pick_citable_requisition(
    opportunities: list[OpportunityRecord],
    *,
    pursuit_season: str = "fall",
    now: datetime | None = None,
) -> OpportunityRecord | None:
    for opportunity in opportunities:
        if requisition_state(
            opportunity, pursuit_season=pursuit_season, now=now
        ) == CITABLE:
            return opportunity
    return None

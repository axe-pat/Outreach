"""Deterministic organization-identity contradiction audit.

Name-string joins are not identity.  This module never repairs workbook data;
it emits evidence for a human when contact titles or geography point at a
different company than the organization row.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from ..tracking import ContactRecord, OrganizationRecord, TouchpointRecord


_EMPLOYER = re.compile(
    r"(?:@|\bat\b)\s*([A-Z][A-Za-z0-9&.'’() -]{1,80}?)"
    r"(?=\s+(?:@|\bat\b)\s+|\s*[|•·;,]|$)",
    re.I,
)
_PAST_EMPLOYER_QUALIFIER = re.compile(
    r"\b(?:ex|prev|previously|former|formerly|alum|alumni)\b", re.I
)
_NON_ROUTING_AFFILIATION = re.compile(
    r"\b(?:creator(?:\s+program)?|ambassador(?:\s+program)?|advisor|advisory|"
    r"community(?:\s+member|\s+membership)|fellow(?:ship)?|mentor)\b",
    re.I,
)
_BOUND_AFFILIATION_NOTE = re.compile(
    r"\bbound_affiliation_type\s*=\s*"
    r"(?P<kind>creator_program|ambassador_program|advisory|community_membership)\b",
    re.I,
)
_TITLE_SEGMENT_BOUNDARY = re.compile(r"[|•·;,]")
_GENERIC_AT_SCALE = re.compile(
    r"\b(?:automation|operate|operating|operations|systems?|production|"
    r"performance|growth)\s+at\s+scale\b",
    re.I,
)
_LEGAL_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "plc",
    "pvt",
    "private",
}
_GENERIC_EMPLOYER_WORDS = {
    "group",
    "global",
    "technologies",
    "technology",
    "tech",
    "labs",
    "systems",
}

CONFIRMED_HERE = "confirmed_here"
WORKS_ELSEWHERE = "works_elsewhere"
UNKNOWN_MEMBERSHIP = "unknown"
NON_PERSON_NAME = "non_person_name"

EXPECTED_REFERRAL_AFFILIATION = "expected_referral_affiliation"
EXPECTED_UNINVITED_AFFILIATION = "expected_uninvited_affiliation"
FLAG_ORG_MEMBERSHIP_CONFLICT = "flag_org_membership_conflict"
FLAG_CONFIRMED_NAME_COLLISION = "flag_confirmed_org_name_collision_for_reassignment"

_EDUCATION_EMPLOYER = re.compile(
    r"\b(?:university|college|school|academy|institute of technology)\b|"
    r"^(?:usc|cmu|ucla|ucr|fiu|rit|sbu|scu|upenn|harvard|"
    r"penn state|ut austin|mt\.? sac|cornell tech)$",
    re.I,
)
_VERB_INITIAL_NAME = re.compile(
    r"^(?:building|hiring|join|joining|meet|introducing|welcome|"
    r"looking|seeking|exploring|helping|connecting)\b",
    re.I,
)
_NAME_TRAILING_PUNCTUATION = re.compile(r"[.!?,;:]$")

# Explicit aliases only.  Fuzzy token overlap is precisely what made Clara
# match Santa Clara University and Mount match Iron Mountain.
_EMPLOYER_ALIASES: tuple[frozenset[str], ...] = (
    frozenset({"anam", "anam ai"}),
    frozenset({"cisco", "cisco systems"}),
    frozenset({"gen auto ai", "general autonomy"}),
    frozenset({"hebbia", "hebbia ai"}),
    frozenset({"invisible", "invisible tech", "invisible technology", "invisible technologies"}),
    frozenset({"jobright", "jobright ai"}),
    frozenset({"keck", "keck medicine", "keck medicine of usc"}),
    frozenset({"mercor", "mercor applied ai"}),
    frozenset({"opto", "opto investments"}),
    frozenset({"outset", "outset ai"}),
    frozenset({"voker", "voker ai"}),
    frozenset({"yondu", "yondu ai"}),
)

_EDUCATION_ACRONYMS = {
    "asu", "csu", "fiu", "lpu", "ncsu", "rit", "sbu", "scu", "ucr",
    "ucla", "upenn", "usc",
}
_EMPLOYER_TRAILING_CONTEXT = re.compile(
    r"(?:\.\s*(?:ex|former|formerly|previously)\b.*|"
    r"\s+[-–—]\s+(?:building|focused|helping|working)\b.*)$",
    re.I,
)

_REGION_PATTERNS: dict[str, re.Pattern[str]] = {
    "india": re.compile(
        r"\bindia\b|\bmumbai\b|\bthane\b|\bbengaluru\b|\bbangalore\b|"
        r"\bhyderabad\b|\bpune\b|\bdelhi\b|\bnoida\b|\bgurugram\b|\bchennai\b",
        re.I,
    ),
    "united_states": re.compile(
        r"\bunited states\b|\bu\.?s\.?a?\b|\bsan francisco\b|\bsan diego\b|"
        r"\blos angeles\b|\bnew york\b|\bseattle\b|\baustin\b|"
        r"\bcalifornia\b|\bwashington,? d\.?c\.?\b",
        re.I,
    ),
    "canada": re.compile(r"\bcanada\b|\btoronto\b|\bvancouver\b|\bmontreal\b", re.I),
    "united_kingdom": re.compile(r"\bunited kingdom\b|\bu\.?k\.?\b|\blondon\b", re.I),
}


@dataclass(frozen=True)
class IdentityFinding:
    organization_id: str
    organization_name: str
    kind: str
    evidence: str
    contact_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ContactMembership:
    """One contact's organization membership, with no workbook mutation."""

    contact_id: str
    organization_id: str
    organization_name: str
    full_name: str
    title: str
    linkedin_url: str
    classification: str
    # Evidence only. Never use this value as a reassignment destination: one
    # person may have several simultaneous current affiliations.
    named_employer: str = ""
    source: str = "workbook_title"
    reason: str = ""
    proposed_action: str = "none"
    garbage_reasons: tuple[str, ...] = ()
    routing_employers: tuple[str, ...] = ()
    non_routing_affiliations: tuple[str, ...] = ()
    bound_affiliation_type: str = "none"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _tokens(value: str) -> list[str]:
    # Parenthetical batch/team metadata is decoration, not identity: "Icarus
    # (YC F25)" and "Cisco (Webex)" still name Icarus and Cisco.
    value = re.sub(r"\([^)]{1,80}\)", " ", value or "")
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").casefold())
        if token not in _LEGAL_SUFFIXES
    ]


def _normalized_employer(value: str) -> str:
    tokens = _tokens(value)
    return " ".join(tokens)


def employers_match(left: str, right: str) -> bool:
    """Exact identity plus a small reviewed alias set; never substring match."""

    left_key = _normalized_employer(left)
    right_key = _normalized_employer(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    return any(left_key in aliases and right_key in aliases for aliases in _EMPLOYER_ALIASES)


def _aliases_for(organization_name: str) -> set[str]:
    key = _normalized_employer(organization_name)
    aliases = {key} if key else set()
    for group in _EMPLOYER_ALIASES:
        if key in group:
            aliases.update(group)
    return aliases


def _match_segment_prefix(title: str, start: int) -> str:
    segment_start = 0
    for boundary in _TITLE_SEGMENT_BOUNDARY.finditer((title or "")[:start]):
        segment_start = boundary.end()
    return (title or "")[segment_start:start]


def _education_mention(employer: str, prefix: str) -> bool:
    del prefix
    key = _normalized_employer(employer)
    return bool(
        _EDUCATION_EMPLOYER.search(employer)
        or key in _EDUCATION_ACRONYMS
        or any(key.startswith(f"{acronym} ") for acronym in _EDUCATION_ACRONYMS)
    )


def _non_routing_affiliation_kind(prefix: str) -> str:
    value = (prefix or "").casefold()
    if "creator" in value:
        return "creator_program"
    if "ambassador" in value:
        return "ambassador_program"
    if "advisor" in value or "advisory" in value:
        return "advisory"
    if re.search(
        r"\b(?:community\s+(?:member|membership)|fellow|fellowship|mentor)\b",
        value,
    ):
        return "community_membership"
    return ""


def current_title_non_routing_affiliations(title: str) -> tuple[str, ...]:
    """Return explicit affiliations that do not establish employee routing."""

    affiliations: list[str] = []
    for match in _EMPLOYER.finditer(title or ""):
        prefix = _match_segment_prefix(title, match.start())
        kind = _non_routing_affiliation_kind(prefix)
        if not kind:
            continue
        organization = " ".join(match.group(1).split()).strip()
        organization = _EMPLOYER_TRAILING_CONTEXT.sub("", organization).strip(
            " .-–—"
        )
        value = f"{organization} ({kind})"
        if organization and value.casefold() not in {
            item.casefold() for item in affiliations
        }:
            affiliations.append(value)
    return tuple(affiliations)


def title_names_organization(title: str, organization_name: str) -> bool:
    """Catch exact employer segments that omit ``at``/``@`` syntax."""

    aliases = _aliases_for(organization_name)
    if not aliases:
        return False
    for segment in re.split(r"[|•·;,]", title or ""):
        if _PAST_EMPLOYER_QUALIFIER.search(segment):
            continue
        normalized = _normalized_employer(segment.strip(" -–—"))
        if normalized in aliases:
            return True
        if re.search(r"\b(?:of|via)\b", segment, re.I):
            for alias in aliases:
                if normalized.endswith(f" {alias}"):
                    return True
        if re.search(r"\s[-–—]\s", segment):
            for alias in aliases:
                if normalized.endswith(f" {alias}"):
                    return True
    return False


def current_title_employers(title: str) -> tuple[str, ...]:
    """Return employment/internship affiliations with routing value."""

    employers: list[str] = []
    for match in _EMPLOYER.finditer(title or ""):
        prefix = _match_segment_prefix(title, match.start())
        if _PAST_EMPLOYER_QUALIFIER.search(prefix):
            continue
        if _NON_ROUTING_AFFILIATION.search(prefix):
            continue
        employer = " ".join(match.group(1).split()).strip()
        employer = _EMPLOYER_TRAILING_CONTEXT.sub("", employer).strip(" .-–—")
        if employer.casefold() == "scale" and _GENERIC_AT_SCALE.search(title or ""):
            continue
        if _education_mention(employer, prefix):
            continue
        if employer and employer.casefold() not in {item.casefold() for item in employers}:
            employers.append(employer)
    return tuple(employers)


def non_person_name_reasons(full_name: str, organization_name: str = "") -> tuple[str, ...]:
    """High-precision garbage-row signals; a flag is report-only."""

    name = " ".join((full_name or "").split()).strip()
    reasons: list[str] = []
    if not name:
        reasons.append("empty_name")
        return tuple(reasons)
    if _VERB_INITIAL_NAME.search(name):
        reasons.append("verb_initial_phrase")
    if _NAME_TRAILING_PUNCTUATION.search(name):
        final_token = re.findall(r"[A-Za-z]+", name[-4:])
        is_person_initial = bool(final_token and len(final_token[-1]) == 1)
        if not is_person_initial:
            reasons.append("trailing_punctuation")
    name_key = _normalized_employer(name)
    org_key = _normalized_employer(organization_name)
    if org_key and (name_key == org_key or org_key in name_key.split()):
        reasons.append("organization_name_in_person_field")
    return tuple(dict.fromkeys(reasons))


def classify_contact_membership(
    contact: ContactRecord,
    organization: OrganizationRecord,
) -> ContactMembership:
    """Classify one contact using only workbook evidence."""

    garbage_reasons = non_person_name_reasons(contact.full_name, organization.name)
    if garbage_reasons:
        return ContactMembership(
            contact_id=contact.contact_id,
            organization_id=organization.organization_id,
            organization_name=organization.name,
            full_name=contact.full_name,
            title=contact.title,
            linkedin_url=contact.linkedin_url,
            classification=NON_PERSON_NAME,
            reason="contact row does not look like a person",
            proposed_action="review_delete_garbage_row",
            garbage_reasons=garbage_reasons,
        )

    employers = current_title_employers(contact.title)
    non_routing = current_title_non_routing_affiliations(contact.title)
    note_match = _BOUND_AFFILIATION_NOTE.search(contact.notes or "")
    noted_bound_type = note_match.group("kind").casefold() if note_match else ""
    target_non_routing = next(
        (
            value
            for value in non_routing
            if employers_match(organization.name, value.rsplit(" (", 1)[0])
        ),
        "",
    )
    target_non_routing_type = (
        target_non_routing.rsplit(" (", 1)[-1].removesuffix(")")
        if target_non_routing
        else ""
    )
    if any(employers_match(organization.name, employer) for employer in employers):
        matched = next(
            employer
            for employer in employers
            if employers_match(organization.name, employer)
        )
        return ContactMembership(
            contact_id=contact.contact_id,
            organization_id=organization.organization_id,
            organization_name=organization.name,
            full_name=contact.full_name,
            title=contact.title,
            linkedin_url=contact.linkedin_url,
            classification=CONFIRMED_HERE,
            named_employer=matched,
            reason="title explicitly names the workbook employer",
            routing_employers=employers,
            non_routing_affiliations=non_routing,
            bound_affiliation_type="employment_or_internship",
        )
    if employers:
        target_is_bare_affiliation = title_names_organization(
            contact.title,
            organization.name,
        )
        return ContactMembership(
            contact_id=contact.contact_id,
            organization_id=organization.organization_id,
            organization_name=organization.name,
            full_name=contact.full_name,
            title=contact.title,
            linkedin_url=contact.linkedin_url,
            classification=WORKS_ELSEWHERE,
            named_employer=employers[0],
            reason=(
                "title names a current affiliation but not the workbook organization"
            ),
            proposed_action="evaluate_outreach_relationship_context",
            routing_employers=employers,
            non_routing_affiliations=non_routing,
            bound_affiliation_type=(
                noted_bound_type
                or target_non_routing_type
                or ("untyped_non_employment_affiliation" if target_is_bare_affiliation else "none")
            ),
        )
    if target_non_routing or noted_bound_type:
        return ContactMembership(
            contact_id=contact.contact_id,
            organization_id=organization.organization_id,
            organization_name=organization.name,
            full_name=contact.full_name,
            title=contact.title,
            linkedin_url=contact.linkedin_url,
            classification=UNKNOWN_MEMBERSHIP,
            reason=(
                "bound affiliation has no employee or internship routing value"
            ),
            proposed_action="hold_if_live_and_verify_employment",
            non_routing_affiliations=non_routing,
            bound_affiliation_type=(
                noted_bound_type
                or target_non_routing_type
            ),
        )
    if title_names_organization(contact.title, organization.name):
        return ContactMembership(
            contact_id=contact.contact_id,
            organization_id=organization.organization_id,
            organization_name=organization.name,
            full_name=contact.full_name,
            title=contact.title,
            linkedin_url=contact.linkedin_url,
            classification=CONFIRMED_HERE,
            named_employer=organization.name,
            reason="title contains an exact current-employer segment",
            routing_employers=(organization.name,),
            bound_affiliation_type="untyped_current_affiliation",
        )
    return ContactMembership(
        contact_id=contact.contact_id,
        organization_id=organization.organization_id,
        organization_name=organization.name,
        full_name=contact.full_name,
        title=contact.title,
        linkedin_url=contact.linkedin_url,
        classification=UNKNOWN_MEMBERSHIP,
        reason=(
            "no title available"
            if not (contact.title or "").strip()
            else "title names no current employer"
        ),
        proposed_action=(
            "defer_profile_read_until_live"
            if contact.linkedin_url
            else "human_review_if_live"
        ),
        non_routing_affiliations=non_routing,
        bound_affiliation_type=noted_bound_type or "none",
    )


def needs_membership_verification(
    membership: ContactMembership,
    *,
    is_live: bool,
    is_diagnostic_sample: bool = False,
) -> bool:
    """Gate profile reads at use time instead of sweeping dormant contacts.

    A workbook-unknown employer is not itself permission to visit a profile.
    The contact must either be about to receive a draft or belong to an
    explicitly bounded diagnostic sample.
    """

    return bool(
        membership.classification == UNKNOWN_MEMBERSHIP
        and membership.linkedin_url
        and (is_live or is_diagnostic_sample)
    )


def resolve_membership_from_profile(
    membership: ContactMembership,
    *,
    current_employer: str,
    current_title: str = "",
) -> ContactMembership:
    """Resolve a workbook-unknown contact from a read-only profile observation."""

    if membership.classification != UNKNOWN_MEMBERSHIP:
        return membership
    employer = " ".join((current_employer or "").split()).strip()
    if not employer:
        return ContactMembership(
            **{
                **membership.as_dict(),
                "source": "linkedin_profile",
                "reason": "profile read did not expose a current employer",
                "proposed_action": "human_review",
            }
        )
    confirmed = employers_match(membership.organization_name, employer)
    return ContactMembership(
        **{
            **membership.as_dict(),
            "title": current_title or membership.title,
            "classification": CONFIRMED_HERE if confirmed else WORKS_ELSEWHERE,
            "named_employer": employer,
            "source": "linkedin_profile",
            "reason": (
                "profile current experience names the workbook employer"
                if confirmed
                else (
                    "profile names a current affiliation but not the workbook "
                    "organization"
                )
            ),
            "proposed_action": (
                "none" if confirmed else "evaluate_outreach_relationship_context"
            ),
        }
    )


def membership_conflict_disposition(
    membership: ContactMembership,
    contact: ContactRecord,
    touchpoints: list[TouchpointRecord],
    *,
    confirmed_name_collision: bool = False,
) -> tuple[str, str]:
    """Interpret an affiliation mismatch using how the contact was mapped.

    ``organization_id`` serves both employee membership and referral routing in
    the workbook. A title mismatch alone therefore never authorizes a detach or
    reassignment, and every non-action remains visible with a reason.
    """

    if membership.classification != WORKS_ELSEWHERE:
        return "none", "no conflicting current affiliation"
    if confirmed_name_collision:
        return (
            FLAG_CONFIRMED_NAME_COLLISION,
            "confirmed Mount/Iron Mountain name collision; review reassignment",
        )
    target_lists = {
        token.strip().casefold()
        for token in re.split(r"[;,]", contact.target_lists or "")
        if token.strip()
    }
    if "referrals" in target_lists:
        return (
            EXPECTED_REFERRAL_AFFILIATION,
            "contact is intentionally mapped as a referral path",
        )
    sent_invite = any(
        re.sub(r"[\s-]+", "_", touchpoint.message_kind.casefold())
        == "linkedin_invite"
        and (
            touchpoint.status.casefold() in {"sent", "delivered", "completed"}
            or bool((touchpoint.sent_at or "").strip())
        )
        for touchpoint in touchpoints
        if touchpoint.contact_id == contact.contact_id
    )
    if not sent_invite:
        return (
            EXPECTED_UNINVITED_AFFILIATION,
            "no sent LinkedIn invite treated the contact as an employee",
        )
    return (
        FLAG_ORG_MEMBERSHIP_CONFLICT,
        "sent LinkedIn invite treated the contact as an employee of this organization",
    )


def parse_current_experience_lines(lines: list[str]) -> tuple[str, str, list[str]]:
    """Parse LinkedIn's stable title/company/date text triplet for a current role."""

    cleaned = [" ".join(str(line).split()).strip() for line in lines]
    cleaned = [line for line in cleaned if line]
    for index, line in enumerate(cleaned):
        if not re.search(r"\b(?:Present|Current)\b", line, re.I) or index < 2:
            continue
        company_line = cleaned[index - 1]
        company = company_line.split(" · ", maxsplit=1)[0].strip()
        title = cleaned[index - 2].removesuffix(" logo").strip()
        if not company or company.casefold() in {"experience", "education"}:
            continue
        evidence = cleaned[max(0, index - 2) : index + 1]
        return company, title, evidence
    return "", "", []


def title_employer(title: str) -> str:
    """Return the explicitly named current employer in a LinkedIn headline."""

    for match in _EMPLOYER.finditer(title or ""):
        # LinkedIn headlines often put the former-role qualifier before the
        # role rather than beside the company: "Ex Data Scientist at X".
        # Treat the whole delimited segment as historical in that case.
        segment_start = 0
        for boundary in _TITLE_SEGMENT_BOUNDARY.finditer((title or "")[: match.start()]):
            segment_start = boundary.end()
        prefix = (title or "")[segment_start : match.start()]
        if _PAST_EMPLOYER_QUALIFIER.search(prefix):
            continue

        employer = " ".join(match.group(1).split()).strip()
        # "Automation at Scale" is an idiom, not employment at Scale.  Keep
        # the exception narrow: explicit @Scale and other "at <brand>"
        # headlines still count as employer evidence.
        if employer.casefold() == "scale" and _GENERIC_AT_SCALE.search(title or ""):
            continue
        return employer
    return ""


def title_entity_conflict(organization_name: str, title: str) -> str:
    """Return contradictory employer evidence, or an empty string."""

    employer = title_employer(title)
    org_tokens = _tokens(organization_name)
    employer_tokens = _tokens(employer)
    if not employer or not org_tokens or not employer_tokens:
        return ""
    if org_tokens == employer_tokens:
        return ""

    org_set = set(org_tokens)
    employer_set = set(employer_tokens)
    extra = employer_set - org_set - _GENERIC_EMPLOYER_WORDS
    raw_employer_tokens = set(re.findall(r"[a-z0-9]+", employer.casefold()))
    has_legal_suffix = bool(raw_employer_tokens & _LEGAL_SUFFIXES)
    # "Ventura Securities Ltd" is not the two-person YC company "Ventura".
    # Conversely, expanded brand/subteam wording such as "DoorDash Ads" is
    # only a weak signal and stays out of this high-recall report unless the
    # title explicitly names a legal entity.
    if org_set <= employer_set and extra:
        return employer if has_legal_suffix else ""

    overlap = len(org_set & employer_set)
    if overlap == 0:
        return employer
    return ""


def infer_region(text: str) -> str:
    matches = [name for name, pattern in _REGION_PATTERNS.items() if pattern.search(text or "")]
    return matches[0] if len(matches) == 1 else ""


def audit_org_identities(
    organizations: list[OrganizationRecord],
    contacts: list[ContactRecord],
) -> list[IdentityFinding]:
    """Flag contradictions without mutating any workbook row."""

    contacts_by_org: dict[str, list[ContactRecord]] = defaultdict(list)
    for contact in contacts:
        contacts_by_org[contact.organization_id].append(contact)

    findings: list[IdentityFinding] = []
    for organization in organizations:
        org_contacts = contacts_by_org.get(organization.organization_id, [])

        named_entities: dict[str, list[str]] = defaultdict(list)
        for contact in org_contacts:
            conflict = title_entity_conflict(organization.name, contact.title)
            if conflict:
                named_entities[conflict].append(contact.contact_id)
        for entity, contact_ids in sorted(named_entities.items()):
            findings.append(
                IdentityFinding(
                    organization_id=organization.organization_id,
                    organization_name=organization.name,
                    kind="title_names_different_entity",
                    evidence=f'{len(contact_ids)} contact title(s) name "{entity}"',
                    contact_ids=tuple(contact_ids),
                )
            )

        org_region = infer_region(f"{organization.city} {organization.notes}")
        contact_regions: dict[str, list[str]] = defaultdict(list)
        for contact in org_contacts:
            region = infer_region(f"{contact.title} {contact.notes}")
            if region:
                contact_regions[region].append(contact.contact_id)
        if org_region and contact_regions:
            located_count = sum(len(ids) for ids in contact_regions.values())
            for region, contact_ids in sorted(contact_regions.items()):
                if region == org_region:
                    continue
                if len(contact_ids) >= 2 and len(contact_ids) * 2 >= located_count:
                    findings.append(
                        IdentityFinding(
                            organization_id=organization.organization_id,
                            organization_name=organization.name,
                            kind="contact_geography_conflicts_with_org",
                            evidence=(
                                f"organization region={org_region}; "
                                f"{len(contact_ids)}/{located_count} located contacts={region}"
                            ),
                            contact_ids=tuple(contact_ids),
                        )
                    )

    return findings


def render_identity_report(findings: list[IdentityFinding]) -> str:
    counts = Counter(finding.kind for finding in findings)
    grouped: dict[tuple[str, str], list[IdentityFinding]] = defaultdict(list)
    for finding in findings:
        grouped[(finding.organization_id, finding.organization_name)].append(finding)

    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            -len(item[1]),
            item[0][1].casefold(),
            item[0][0],
        ),
    )
    likely = [item for item in ranked if len(item[1]) >= 2]
    low_signal = [item for item in ranked if item not in likely]
    lines = [
        "# Organization identity audit",
        "",
        "Report only: no workbook rows were changed by this sweep.",
        "",
        f"Human-review queue (`likely_collision`): **{len(likely)} organizations**",
        f"Low-signal appendix: **{len(low_signal)} organizations**",
        f"Findings: **{len(findings)}**",
        "",
    ]
    if counts:
        lines.extend(["## Summary", ""])
        lines.extend(f"- `{kind}`: {count}" for kind, count in sorted(counts.items()))
        lines.append("")
    if not findings:
        lines.append("No contradictions found.")
        return "\n".join(lines) + "\n"

    for category, organizations in (
        ("likely_collision", likely),
        ("low_signal", low_signal),
    ):
        lines.extend([f"## `{category}`", ""])
        if category == "likely_collision":
            lines.extend(
                [
                    "Review these first. Two or more distinct contradictions",
                    "inside one organization are treated as concentrated evidence.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "One-off signals are retained for later corroboration, not placed",
                    "in the current human-review queue.",
                    "",
                ]
            )
        for (organization_id, organization_name), org_findings in organizations:
            finding_count = len(org_findings)
            lines.extend(
                [
                    f"### {organization_name} (`{organization_id}`) — "
                    f"{finding_count} finding(s)",
                ]
            )
            for finding in org_findings:
                lines.extend(
                    [
                        f"- `{finding.kind}`: {finding.evidence}",
                        f"  - Contacts: {', '.join(finding.contact_ids) or '(none)'}",
                    ]
                )
            lines.append("")
    return "\n".join(lines)

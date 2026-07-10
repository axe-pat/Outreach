from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable, Mapping

from outreach.role_surface_monitor import RoleFamily, classify_role_title


class TargetRoleFamily(str, Enum):
    """Role families used to aim outreach copy, independent of contact type."""

    PRODUCT_PM = "product_pm"
    PRODUCT_STRATEGY = "product_strategy"
    BIZOPS_STRATEGY = "bizops_strategy"
    PROGRAM_OPERATIONS = "program_operations"
    GROWTH_GTM = "growth_gtm"
    GENERAL_BUSINESS = "general_business"


@dataclass(frozen=True)
class TargetRoleContext:
    family: TargetRoleFamily
    label: str
    role_phrase: str
    path_phrase: str
    work_phrase: str
    team_area: str
    subject_label: str
    source: str
    matched_text: str
    matched_rule: str
    is_concrete: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "label": self.label,
            "source": self.source,
            "matched_text": self.matched_text,
            "matched_rule": self.matched_rule,
            "is_concrete": self.is_concrete,
        }


@dataclass(frozen=True)
class _RoleCopy:
    label: str
    role_phrase: str
    path_phrase: str
    work_phrase: str
    team_area: str
    subject_label: str


_ROLE_COPY = {
    TargetRoleFamily.PRODUCT_PM: _RoleCopy(
        label="Product / PM",
        role_phrase="PM/product roles",
        path_phrase="PM/product paths",
        work_phrase="product work",
        team_area="product",
        subject_label="Product fit",
    ),
    TargetRoleFamily.PRODUCT_STRATEGY: _RoleCopy(
        label="Product Strategy",
        role_phrase="Product Strategy roles",
        path_phrase="Product Strategy paths",
        work_phrase="product strategy work",
        team_area="product strategy",
        subject_label="Product Strategy fit",
    ),
    TargetRoleFamily.BIZOPS_STRATEGY: _RoleCopy(
        label="BizOps / Strategy",
        role_phrase="BizOps/Strategy roles",
        path_phrase="BizOps/Strategy paths",
        work_phrase="business operations and strategy work",
        team_area="strategy or business operations",
        subject_label="BizOps / Strategy fit",
    ),
    TargetRoleFamily.PROGRAM_OPERATIONS: _RoleCopy(
        label="Program / Operations",
        role_phrase="Program/Operations roles",
        path_phrase="Program/Operations paths",
        work_phrase="program and operations work",
        team_area="program or operations",
        subject_label="Program / Operations fit",
    ),
    TargetRoleFamily.GROWTH_GTM: _RoleCopy(
        label="Narrow Growth / GTM",
        role_phrase="select Growth/GTM strategy roles",
        path_phrase="select Growth/GTM strategy paths",
        work_phrase="growth and go-to-market strategy work",
        team_area="growth or go-to-market strategy",
        subject_label="Growth / GTM strategy fit",
    ),
    TargetRoleFamily.GENERAL_BUSINESS: _RoleCopy(
        label="General business",
        role_phrase="business-side roles",
        path_phrase="business-side paths",
        work_phrase="business-side work",
        team_area="the relevant business side",
        subject_label="Business-role fit",
    ),
}


_EXPLICIT_FAMILY_ALIASES = {
    "product": TargetRoleFamily.PRODUCT_PM,
    "pm": TargetRoleFamily.PRODUCT_PM,
    "product pm": TargetRoleFamily.PRODUCT_PM,
    "product_pm": TargetRoleFamily.PRODUCT_PM,
    "product management": TargetRoleFamily.PRODUCT_PM,
    "product strategy": TargetRoleFamily.PRODUCT_STRATEGY,
    "product_strategy": TargetRoleFamily.PRODUCT_STRATEGY,
    "bizops": TargetRoleFamily.BIZOPS_STRATEGY,
    "business operations": TargetRoleFamily.BIZOPS_STRATEGY,
    "bizops strategy": TargetRoleFamily.BIZOPS_STRATEGY,
    "bizops_strategy": TargetRoleFamily.BIZOPS_STRATEGY,
    "strategy": TargetRoleFamily.BIZOPS_STRATEGY,
    "program": TargetRoleFamily.PROGRAM_OPERATIONS,
    "program operations": TargetRoleFamily.PROGRAM_OPERATIONS,
    "program_operations": TargetRoleFamily.PROGRAM_OPERATIONS,
    "operations": TargetRoleFamily.PROGRAM_OPERATIONS,
    "growth": TargetRoleFamily.GROWTH_GTM,
    "growth gtm": TargetRoleFamily.GROWTH_GTM,
    "growth_gtm": TargetRoleFamily.GROWTH_GTM,
    "growth adjacent": TargetRoleFamily.GROWTH_GTM,
    "growth_adjacent": TargetRoleFamily.GROWTH_GTM,
    "gtm": TargetRoleFamily.GROWTH_GTM,
    "general": TargetRoleFamily.GENERAL_BUSINESS,
    "general business": TargetRoleFamily.GENERAL_BUSINESS,
    "general_business": TargetRoleFamily.GENERAL_BUSINESS,
    "business": TargetRoleFamily.GENERAL_BUSINESS,
}

_ROLE_FAMILY_MAP = {
    RoleFamily.PRODUCT_PM: TargetRoleFamily.PRODUCT_PM,
    RoleFamily.PRODUCT_STRATEGY: TargetRoleFamily.PRODUCT_STRATEGY,
    RoleFamily.BIZOPS_STRATEGY: TargetRoleFamily.BIZOPS_STRATEGY,
    RoleFamily.PROGRAM_OPERATIONS: TargetRoleFamily.PROGRAM_OPERATIONS,
    RoleFamily.GROWTH_ADJACENT: TargetRoleFamily.GROWTH_GTM,
}

_NARROW_GTM_PATTERN = re.compile(
    r"(?:\b(?:gtm|go[ -]to[ -]market)\b.*\b(?:strategy|strategic|operations|ops|planning|analytics)\b|"
    r"\b(?:strategy|strategic|operations|ops|planning|analytics)\b.*\b(?:gtm|go[ -]to[ -]market)\b)",
    flags=re.I,
)
_GENERAL_BUSINESS_PATTERN = re.compile(
    r"\b(?:business analyst|business generalist|general manager|founder'?s (?:associate|office)|"
    r"corporate development|strategic partnerships|commercial (?:strategy|operations)|"
    r"business planning|business transformation|management consultant)\b",
    flags=re.I,
)
_GENERAL_BUSINESS_EXCLUSIONS = re.compile(
    r"\b(?:software|engineering|engineer|developer|data scientist|designer|recruit|talent|"
    r"accounting|legal|account executive|sales representative|customer support)\b",
    flags=re.I,
)


def infer_target_role_context(
    *,
    explicit_family: str = "",
    explicit_title: str = "",
    opportunity_titles: Iterable[str] | str = (),
    note_context: Mapping[str, object] | None = None,
    organization_notes: str = "",
) -> TargetRoleContext:
    """Infer the role being pursued without treating the recipient's job as the target role.

    Product/PM remains primary within each evidence tier. Precedence is explicit
    family/title, then concrete opportunities, then structured target-role notes,
    then Product default. Product wins a mixed set only inside the same tier.
    """

    context = dict(note_context or {})
    explicit_family_hint = _clean(explicit_family)
    context_family_hint = _clean(context.get("target_role_family"))
    family_hint = explicit_family_hint or context_family_hint
    explicit = _family_from_alias(family_hint)
    if explicit is not None:
        if explicit_family_hint:
            source = "explicit_family"
            matched_text = family_hint
            matched_rule = "explicit target role family"
            is_concrete = True
        else:
            source = _clean(context.get("target_role_source")) or "note_context.target_role_family"
            matched_text = _clean(context.get("target_role_matched_text")) or family_hint
            matched_rule = (
                _clean(context.get("target_role_matched_rule"))
                or "derived target role family from note context"
            )
            is_concrete = _bool_value(
                context.get("target_role_is_concrete"),
                default=source != "product_primary_default",
            )
        return _context(
            explicit,
            source=source,
            matched_text=matched_text,
            matched_rule=matched_rule,
            is_concrete=is_concrete,
        )

    title_hint = _clean(explicit_title) or _clean(context.get("target_role_title"))
    title_inference = _infer_family_from_text(title_hint) if title_hint else None
    if title_inference is not None:
        title_family, title_rule = title_inference
        return _context(
            title_family,
            source=("explicit_title" if _clean(explicit_title) else "note_context.target_role_title"),
            matched_text=title_hint,
            matched_rule=title_rule,
            is_concrete=True,
        )

    opportunity_candidates: list[tuple[str, str]] = []
    _extend_candidates(opportunity_candidates, "opportunity_title", opportunity_titles)
    _extend_candidates(
        opportunity_candidates,
        "note_context.opportunity_title",
        context.get("opportunity_titles"),
    )
    _extend_candidates(
        opportunity_candidates,
        "note_context.latest_opportunity_title",
        context.get("latest_opportunity_titles"),
    )
    opportunity_target = _resolve_candidate_group(opportunity_candidates)
    if opportunity_target is not None:
        return opportunity_target

    structured_target_candidates: list[tuple[str, str]] = []
    _extend_candidates(
        structured_target_candidates,
        "note_context.target_roles",
        context.get("target_roles"),
        split=True,
    )
    _extend_candidates(
        structured_target_candidates,
        "organization_notes.target_roles",
        _structured_note_values(organization_notes, "target_roles"),
        split=True,
    )
    structured_target = _resolve_candidate_group(structured_target_candidates)
    if structured_target is not None:
        return structured_target

    return _context(
        TargetRoleFamily.PRODUCT_PM,
        source="product_primary_default",
        matched_text="",
        matched_rule="no concrete target role found; Product/PM primary fallback",
        is_concrete=False,
    )


def _resolve_candidate_group(
    candidates: list[tuple[str, str]],
) -> TargetRoleContext | None:
    seen: set[str] = set()
    product_target: TargetRoleContext | None = None
    adjacent_target: TargetRoleContext | None = None
    for source, raw_text in candidates:
        text = _clean(raw_text)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        inferred = _infer_family_from_text(text)
        if inferred is None:
            continue
        family, rule = inferred
        inferred_context = _context(
            family,
            source=source,
            matched_text=text,
            matched_rule=rule,
            is_concrete=True,
        )
        if family == TargetRoleFamily.PRODUCT_PM:
            if product_target is None:
                product_target = inferred_context
        elif adjacent_target is None:
            adjacent_target = inferred_context

    if product_target is not None:
        return product_target
    if adjacent_target is not None:
        return adjacent_target
    return None


def target_role_context_from_family(
    family: str | TargetRoleFamily,
    *,
    source: str = "artifact",
    matched_text: str = "",
) -> TargetRoleContext:
    if isinstance(family, TargetRoleFamily):
        normalized = family
    else:
        normalized = _family_from_alias(str(family)) or TargetRoleFamily.PRODUCT_PM
    return _context(
        normalized,
        source=source,
        matched_text=matched_text,
        matched_rule="serialized target role family",
        is_concrete=normalized != TargetRoleFamily.PRODUCT_PM or bool(matched_text),
    )


def rewrite_message_for_target_role(message: str, target: TargetRoleContext) -> str:
    """Replace Product-pivot language while preserving company/contact facts."""

    if target.family == TargetRoleFamily.PRODUCT_PM:
        return message

    role = target.role_phrase
    path = target.path_phrase
    work = target.work_phrase
    team = target.team_area
    subject = target.subject_label
    singular = target.label
    role_or_internship = f"{target.label} role or internship path"

    replacements: tuple[tuple[str, str], ...] = (
        (r"\btechnical\s+PM/product\s+paths?\b", path),
        (r"\bPM/product\s+internship\s+path\b", f"{singular} internship path"),
        (r"\bPM/product\s+internship\s+paths\b", f"{singular} internship paths"),
        (r"\bPM/product\s+paths?\b", path),
        (r"\bPM/product\s+roles?\b", role),
        (r"\bPM/product\s+fit\b", subject),
        (r"\bPM/product,\s*product ops,\s*or strategy paths\b", path),
        (r"\btechnical\s+PM\s+fit\b", subject),
        (r"\ba\s+technical\s+PM\s+candidate\b", "a candidate with a technical + MBA background"),
        (r"\btechnical\s+PM\s+candidates\b", "candidates with technical + MBA backgrounds"),
        (r"\btechnical\s+PM\s+candidate\b", "a candidate with a technical + MBA background"),
        (r"\btechnical\s+PM/product\b", singular),
        (r"\bPM\s+internship\s+path\b", f"{singular} internship path"),
        (r"\bPM\s+internship\s+paths\b", f"{singular} internship paths"),
        (r"\bPM\s+opportunities\b", role),
        (r"\bPM\s+roles?\b", role),
        (r"\bengineering-to-PM\s+path\b", f"engineering + MBA path into {work}"),
        (
            r"\b(?:pivoting|transitioning|making the shift) from engineering into PM\b",
            f"bringing my engineering background into {work}",
        ),
        (
            r"\bmove from data/platform engineering into PM work\b",
            f"apply my data/platform engineering background to {work}",
        ),
        (r"\bengineering \+ PM background\b", "engineering + MBA background"),
        (r"\bproduct/strategy\s+roles?\b", role),
        (r"\bproduct-adjacent\s+paths?\b", path),
        (r"\btechnical\s+product\s+paths?\b", path),
        (r"\btechnical/product\s+overlap\b", f"technical background + {target.label} fit"),
        (r"\bproduct\s+or\s+internship\s+path\b", role_or_internship),
        (r"\bproduct\s+opportunities\b", role),
        (r"\bproduct\s+roles?\b", role),
        (r"\bexploring\s+product\s+work\b", f"exploring {work}"),
        (r"\bfit\s+product\s+work\b", f"fit {work}"),
        (r"\brelevant\s+to\s+product\s+work\b", f"relevant to {work}"),
        (r"\buseful\s+for\s+product\s+work\b", f"useful for {work}"),
        (r"\bproduct\s+paths?\b", path),
        (r"\bproduct\s+fit\b", subject),
        (r"\btechnical\s+MBA\s+could be useful\b", "engineering + MBA background could be useful"),
        (r"\bsomeone on product\b", f"someone in {team}"),
        (r"\bproduct, recruiting\b", f"{team}, recruiting"),
        (r"\bproduct team\b", f"{team} team"),
        (r"\bhow builders work with product there\b", f"how technical operators work with {team} there"),
        (r"\bPM\b", singular),
    )
    rewritten = message
    for pattern, replacement in replacements:
        rewritten = re.sub(pattern, replacement, rewritten, flags=re.I)
    return rewritten


def _infer_family_from_text(text: str) -> tuple[TargetRoleFamily, str] | None:
    alias = _family_from_alias(text)
    if alias is not None:
        return alias, "target role alias"
    if _NARROW_GTM_PATTERN.search(text):
        return TargetRoleFamily.GROWTH_GTM, "narrow GTM strategy/operations"
    classification = classify_role_title(text)
    mapped = _ROLE_FAMILY_MAP.get(classification.family)
    if mapped is not None:
        return mapped, classification.matched_rule
    if not _GENERAL_BUSINESS_EXCLUSIONS.search(text) and _GENERAL_BUSINESS_PATTERN.search(text):
        return TargetRoleFamily.GENERAL_BUSINESS, "general business role"
    return None


def _family_from_alias(value: str) -> TargetRoleFamily | None:
    normalized = re.sub(r"[\s/-]+", " ", _clean(value).casefold()).strip()
    return _EXPLICIT_FAMILY_ALIASES.get(normalized) or _EXPLICIT_FAMILY_ALIASES.get(
        _clean(value).casefold()
    )


def _context(
    family: TargetRoleFamily,
    *,
    source: str,
    matched_text: str,
    matched_rule: str,
    is_concrete: bool,
) -> TargetRoleContext:
    copy = _ROLE_COPY[family]
    return TargetRoleContext(
        family=family,
        label=copy.label,
        role_phrase=copy.role_phrase,
        path_phrase=copy.path_phrase,
        work_phrase=copy.work_phrase,
        team_area=copy.team_area,
        subject_label=copy.subject_label,
        source=source,
        matched_text=matched_text,
        matched_rule=matched_rule,
        is_concrete=is_concrete,
    )


def _structured_note_values(notes: str, key: str) -> list[str]:
    values: list[str] = []
    pattern = re.compile(
        rf"(?:^|\|)\s*{re.escape(key)}\s*=\s*(.*?)(?=\s*\|\s*[a-zA-Z0-9_ -]+\s*=|$)",
        flags=re.I,
    )
    for match in pattern.finditer(notes or ""):
        value = _clean(match.group(1))
        if value:
            values.append(value)
    return values


def _append_candidate(candidates: list[tuple[str, str]], source: str, value: object) -> None:
    text = _clean(value)
    if text:
        candidates.append((source, text))


def _extend_candidates(
    candidates: list[tuple[str, str]],
    source: str,
    value: object,
    *,
    split: bool = False,
) -> None:
    if value is None:
        return
    values: Iterable[object]
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable):
        values = value
    else:
        values = [value]
    for item in values:
        text = _clean(item)
        if not text:
            continue
        parts = re.split(r"\s*(?:,|;|\n)\s*", text) if split else [text]
        for part in parts:
            _append_candidate(candidates, source, part)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _bool_value(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _clean(value).casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return default

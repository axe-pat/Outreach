"""Plan bounded, high-affinity LinkedIn people-search expansions.

The ordinary LinkedIn pipeline already filters every people search to the exact
company.  This module decides when a high-priority account deserves additional
keyword/school passes inside that company, and keeps the decision independent
from browser automation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


DEFAULT_SHARED_HISTORY_TERMS = (
    "Intuit",
    "Gojek",
    "USC",
    "Marshall",
    "Hevo",
    "Optum",
)

_APPLICATION_TAGS = {
    "application-plus-outreach",
    "application_plus_outreach",
    "jobs",
    "pre-apply",
    "pre_apply",
    "post-apply-catchup",
    "post_apply_catchup",
    "resume-generator",
    "resume_generator",
}
_STRATEGIC_TAGS = {
    "core",
    "dream",
    "priority",
    "relationship",
    "story-fit",
    "tier-a",
    "track-2",
}
_ROLE_QUERY_TERMS = {
    "product_pm": ("product", "hiring", "head of product"),
    "product_strategy": ("product strategy", "hiring", "head of product"),
    "bizops_strategy": ("strategy", "hiring", "chief of staff"),
    "program_operations": ("operations", "hiring", "program manager"),
    "growth_gtm": ("growth strategy", "hiring", "growth leadership"),
    "general_business": ("business operations", "hiring", "leadership"),
}
_RELEVANT_ROLE_PATTERN = re.compile(
    r"\b(?:"
    r"(?:technical\s+)?product\s+(?:manager|management|operations?|ops|strategy|lead)|"
    r"(?:head|director|vp)(?:\s+of|[,\s-]+)\s*product|pm|apm|"
    r"business\s+operations?|bizops|strategy|chief\s+of\s+staff|"
    r"program\s+manager|program\s+operations?|operations?\s+manager|"
    r"growth(?:\s+(?:strategy|operations?|ops))?|gtm(?:\s+(?:strategy|operations?|ops))?"
    r")\b",
    flags=re.I,
)


@dataclass(frozen=True)
class AffinitySearchPass:
    name: str
    query: str = ""
    school: str = ""
    signal: str = ""
    limit: int = 6
    max_pages: int = 1

    def as_pass_definition(self) -> dict[str, str | int | bool]:
        definition: dict[str, str | int | bool] = {
            "query": self.query,
            "limit": self.limit,
            "max_pages": self.max_pages,
            # Tied with existing-connections so stable sorting runs the base
            # connection pass first, then affinity, before broad role passes.
            "priority": 1,
            "use_us_location": True,
            "enabled": True,
            "affinity_signal": self.signal,
            # Once the pool is already broad enough, another expansion is not
            # worth extra browser work.
            "run_if_below_pool_size": 36,
        }
        if self.school:
            definition["school"] = self.school
        if self.signal == "shared_history" and self.query:
            # LinkedIn keyword matches can come from profile sections that are
            # not repeated on the compact result card. Preserve why this
            # targeted pass surfaced the person for downstream scoring.
            definition["shared_history_term"] = self.query
        return definition


@dataclass(frozen=True)
class AffinityExpansionPlan:
    eligible: bool
    reasons: tuple[str, ...]
    target_role_family: str
    passes: tuple[AffinitySearchPass, ...] = ()

    @property
    def pass_definitions(self) -> dict[str, dict[str, str | int | bool]]:
        return {item.name: item.as_pass_definition() for item in self.passes}

    def as_dict(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "target_role_family": self.target_role_family,
            "pass_count": len(self.passes),
            "passes": [
                {
                    "name": item.name,
                    "query": item.query,
                    "school": item.school,
                    "signal": item.signal,
                    "limit": item.limit,
                    "max_pages": item.max_pages,
                }
                for item in self.passes
            ],
        }


def plan_high_affinity_expansion(
    context: Mapping[str, object] | None,
    *,
    ex_companies: Iterable[str] = (),
    shared_history_keywords: Iterable[str] = (),
    max_passes: int = 10,
    per_pass_limit: int = 6,
) -> AffinityExpansionPlan:
    """Return extra exact-company search passes for a top, role-backed account.

    Eligibility deliberately requires both a useful target-role signal and a
    top-account signal.  ResumeGenerator-backed application companies count as
    top accounts because they carry both application and outreach intent;
    manually tagged strategic accounts and callers supplying ``priority_score``
    are also supported.
    """

    payload = dict(context or {})
    target_lists = _split_tags(str(payload.get("target_lists") or ""))
    opportunity_titles = _string_list(payload.get("opportunity_titles"))
    explicit_title = str(payload.get("target_role_title") or "").strip()
    target_role_family = str(payload.get("target_role_family") or "").strip().lower()
    concrete_target = _as_bool(payload.get("target_role_is_concrete"))
    role_evidence = [*opportunity_titles, *([explicit_title] if explicit_title else [])]
    strong_role_fit = any(_RELEVANT_ROLE_PATTERN.search(title) for title in role_evidence)
    if concrete_target and target_role_family in _ROLE_QUERY_TERMS:
        strong_role_fit = True

    priority_score = _optional_int(payload.get("priority_score"))
    application_backed = bool(target_lists.intersection(_APPLICATION_TAGS))
    strategic = bool(target_lists.intersection(_STRATEGIC_TAGS))
    priority_backed = priority_score is not None and priority_score >= 70

    reasons: list[str] = []
    if application_backed:
        reasons.append("application plus outreach context")
    if strategic:
        reasons.append("strategic account tag")
    if priority_backed:
        reasons.append(f"LinkedIn company priority score {priority_score}")
    if strong_role_fit:
        reasons.append("concrete target-role evidence")

    top_account = application_backed or strategic or priority_backed
    if not top_account or not strong_role_fit:
        if not top_account:
            reasons.append("no top-account evidence")
        if not strong_role_fit:
            reasons.append("no concrete relevant role evidence")
        return AffinityExpansionPlan(
            eligible=False,
            reasons=tuple(reasons),
            target_role_family=target_role_family,
        )

    role_family = target_role_family or _infer_role_family(role_evidence)
    history_passes = _history_passes(
        ex_companies=ex_companies,
        shared_history_keywords=shared_history_keywords,
        per_pass_limit=per_pass_limit,
    )
    role_passes = _role_passes(role_family, per_pass_limit=per_pass_limit)
    pass_budget = max(0, max_passes)
    # Preserve role targeting even when a caller configures many employer
    # aliases. Four core warm paths run first, then the role searches, then any
    # remaining history budget. This prevents a broad exact-company preflight
    # from starving all role-specific expansion passes.
    role_budget = min(len(role_passes), pass_budget)
    history_budget = max(0, pass_budget - role_budget)
    core_history_count = min(4, history_budget)
    passes = [
        *history_passes[:core_history_count],
        *role_passes[:role_budget],
        *history_passes[core_history_count:history_budget],
    ]
    passes = passes[:pass_budget]
    return AffinityExpansionPlan(
        eligible=True,
        reasons=tuple(reasons),
        target_role_family=role_family,
        passes=tuple(passes),
    )


def filter_affinity_pass_definitions(
    plan: AffinityExpansionPlan,
    *,
    include_passes: Sequence[str] = (),
    exclude_passes: Sequence[str] = (),
) -> dict[str, dict[str, str | int | bool]]:
    """Apply existing CLI include/exclude semantics to an affinity plan."""

    included = {item.strip() for item in include_passes if item.strip()}
    excluded = {item.strip() for item in exclude_passes if item.strip()}
    return {
        name: definition
        for name, definition in plan.pass_definitions.items()
        if name not in excluded and (not included or name in included)
    }


def affinity_pass_candidate_relevant(
    pass_name: str,
    *,
    role_bucket: str,
    title: str,
    raw_text: str = "",
) -> bool:
    """Keep relevant leadership/adjacent cards missed by the legacy role bucketer.

    This helper is intentionally scoped to generated ``affinity_*`` passes.
    Ordinary product/engineering passes retain their existing filtering rules.
    """

    if not pass_name.startswith("affinity_"):
        return False
    if role_bucket != "Other":
        return True
    return _RELEVANT_ROLE_PATTERN.search(f"{title} {raw_text}") is not None


def high_affinity_candidate_signals(candidate: Mapping[str, object]) -> tuple[str, ...]:
    """Return the concrete warm-path signals found on a candidate card."""

    signals: list[str] = []
    if _as_bool(candidate.get("usc_marshall")):
        signals.append("USC Marshall")
    elif _as_bool(candidate.get("usc")):
        signals.append("USC")
    for item in _string_list(candidate.get("shared_history_signals")):
        cleaned = item.strip()
        if cleaned:
            signals.append(cleaned)
    return tuple(_dedupe_terms(signals))


def recommend_affinity_send_cap(
    candidates: Iterable[Mapping[str, object]],
    *,
    plan: AffinityExpansionPlan,
    base_cap: int = 3,
    max_cap: int = 5,
    min_score: int = 35,
) -> int:
    """Bound a send-cap lift to actual, sendable affinity results.

    The planner never raises volume merely because affinity searches ran.  It
    requires at least two non-connected candidates whose cards contain a real
    USC/Marshall or shared-history signal and whose existing score clears the
    normal send floor.
    """

    base = max(0, base_cap)
    ceiling = max(base, max_cap)
    if not plan.eligible:
        return base
    qualified = 0
    for candidate in candidates:
        if _as_bool(candidate.get("existing_connection")):
            continue
        if not high_affinity_candidate_signals(candidate):
            continue
        score = _optional_int(candidate.get("score"))
        if score is None or score < min_score:
            continue
        qualified += 1
    if qualified < 2:
        return base
    return min(ceiling, max(base, qualified))


def allocate_affinity_invite_cap(
    *,
    planned_cap: int,
    recommended_cap: int,
    remaining_invites: int,
    affinity_headroom: int,
) -> tuple[int, int, int]:
    """Allocate a per-company lift without consuming another account's base slots.

    ``remaining_invites`` starts as the sum of already planned invite slots.
    Only unused space under the global daily LinkedIn budget is supplied as
    ``affinity_headroom``.  The returned tuple is the company cap, updated
    remaining slots, and updated headroom.
    """

    base = max(0, planned_cap)
    remaining = max(0, remaining_invites)
    headroom = max(0, affinity_headroom)
    extra = min(max(0, recommended_cap - base), headroom)
    remaining += extra
    return min(base + extra, remaining), remaining, headroom - extra


def _history_passes(
    *,
    ex_companies: Iterable[str],
    shared_history_keywords: Iterable[str],
    per_pass_limit: int,
) -> list[AffinitySearchPass]:
    # Keep the four explicitly requested core paths first, then honor configured
    # personal-history keywords before filling remaining space with other past
    # employers. This means a real configured signal such as Thapar is not
    # silently crowded out by defaults.
    ordered_terms = _dedupe_terms(
        [
            *DEFAULT_SHARED_HISTORY_TERMS[:4],
            *shared_history_keywords,
            *DEFAULT_SHARED_HISTORY_TERMS[4:],
            *ex_companies,
        ]
    )
    passes: list[AffinitySearchPass] = []
    for term in ordered_terms:
        normalized = term.casefold()
        slug = _slug(term)
        if normalized == "usc":
            passes.append(
                AffinitySearchPass(
                    name="affinity_history_usc",
                    school="University of Southern California",
                    signal="shared_school",
                    limit=per_pass_limit,
                )
            )
        elif normalized == "marshall":
            passes.append(
                AffinitySearchPass(
                    name="affinity_history_marshall",
                    school="USC Marshall School of Business",
                    signal="shared_school",
                    limit=per_pass_limit,
                )
            )
        else:
            passes.append(
                AffinitySearchPass(
                    name=f"affinity_history_{slug}",
                    query=term,
                    signal="shared_history",
                    limit=per_pass_limit,
                )
            )
    return passes


def _role_passes(role_family: str, *, per_pass_limit: int) -> list[AffinitySearchPass]:
    queries = _ROLE_QUERY_TERMS.get(role_family)
    if queries is None:
        queries = _ROLE_QUERY_TERMS["product_pm"]
    return [
        AffinitySearchPass(
            name=f"affinity_role_{_slug(query)}",
            query=query,
            signal="target_role",
            limit=per_pass_limit,
        )
        for query in queries
    ]


def _infer_role_family(role_evidence: Iterable[str]) -> str:
    text = " ".join(role_evidence).casefold()
    if re.search(r"\b(?:product strategy|product operations?|product ops)\b", text):
        return "product_strategy"
    if re.search(r"\b(?:product|pm|apm)\b", text):
        return "product_pm"
    if re.search(r"\b(?:bizops|business operations?|strategy|chief of staff)\b", text):
        return "bizops_strategy"
    if re.search(r"\b(?:program|operations?)\b", text):
        return "program_operations"
    if re.search(r"\b(?:growth|gtm|go.to.market)\b", text):
        return "growth_gtm"
    return "general_business"


def _split_tags(value: str) -> set[str]:
    return {
        item.strip().casefold()
        for item in re.split(r"[;,|]", value)
        if item.strip()
    }


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Iterable) or isinstance(value, Mapping):
        return []
    return [str(item) for item in value if str(item).strip()]


def _dedupe_terms(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(str(value).split()).strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        # A broad employer keyword already covers its longer legal/product
        # alias (for example, Hevo also covers Hevo Data).
        if any(
            key.startswith(existing + " ")
            or (len(existing) >= 5 and key.startswith(existing))
            for existing in seen
        ):
            continue
        result.append(cleaned)
        seen.add(key)
    return result


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "signal"


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y"}


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None

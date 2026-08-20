"""Durable parking conditions and the opportunity-triggered reopen watcher."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from ..tracking import ContactRecord, OpportunityRecord, OrganizationRecord, OutreachWorkbook
from .context import (
    APPLY_NOW,
    CREATE_WEDGE,
    CompanyFacts,
    company_facts,
    requisition_actionability,
)


@dataclass(frozen=True)
class ReopenAssessment:
    contact_id: str
    name: str
    company: str
    condition: str
    status: str
    reason: str
    opportunity_title: str = ""
    opportunity_url: str = ""
    req_actionability: str = ""


def _value(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name) or "")
    return str(getattr(item, name, "") or "")


def persist_reopen_conditions(
    workbook: OutreachWorkbook,
    results: Iterable[Any],
) -> int:
    """Persist only newly observed conditions; never clear one implicitly."""

    updated = 0
    existing = {
        contact.contact_id: contact for contact in workbook.list_contacts()
    }
    for item in results:
        contact_id = _value(item, "contact_id").strip()
        condition = _value(item, "reopen_condition").strip()
        if not condition and hasattr(item, "decision"):
            condition = _value(item.decision, "reopen_condition").strip()
        if not contact_id or not condition or contact_id not in existing:
            continue
        if existing[contact_id].reopen_condition == condition:
            continue
        if workbook.update_contact(contact_id, reopen_condition=condition) is not None:
            updated += 1
            existing[contact_id].reopen_condition = condition
    return updated


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def _condition_names_company(condition: str, company: str) -> bool:
    normalized_condition = _normalized(condition)
    normalized_company = _normalized(company)
    return bool(normalized_company and normalized_company in normalized_condition)


def evaluate_reopen_conditions(
    *,
    contacts: list[ContactRecord],
    organizations: list[OrganizationRecord],
    opportunities: list[OpportunityRecord],
    pursuit_season: str = "fall",
    now: datetime | None = None,
) -> list[ReopenAssessment]:
    """Evaluate parked conditions without drafting or mutating contact state."""

    now = now or datetime.now(UTC)
    org_by_id = {organization.organization_id: organization for organization in organizations}
    opportunities_by_org: dict[str, list[OpportunityRecord]] = {}
    for opportunity in opportunities:
        opportunities_by_org.setdefault(opportunity.organization_id, []).append(opportunity)

    assessments: list[ReopenAssessment] = []
    priority = {APPLY_NOW: 2, CREATE_WEDGE: 1}
    for contact in contacts:
        condition = (contact.reopen_condition or "").strip()
        if not condition:
            continue
        organization = org_by_id.get(contact.organization_id)
        company = organization.name if organization else contact.organization_id
        if not organization or not _condition_names_company(condition, company):
            assessments.append(
                ReopenAssessment(
                    contact_id=contact.contact_id,
                    name=contact.full_name,
                    company=company,
                    condition=condition,
                    status="still_parked",
                    reason="condition does not name the linked company",
                )
            )
            continue

        facts: CompanyFacts = company_facts(organization)
        candidates: list[tuple[int, OpportunityRecord, str]] = []
        for opportunity in opportunities_by_org.get(contact.organization_id, []):
            actionability = requisition_actionability(
                opportunity,
                facts,
                pursuit_season=pursuit_season,
                now=now,
            )
            if actionability in priority:
                candidates.append((priority[actionability], opportunity, actionability))

        if not candidates:
            assessments.append(
                ReopenAssessment(
                    contact_id=contact.contact_id,
                    name=contact.full_name,
                    company=company,
                    condition=condition,
                    status="still_parked",
                    reason="no apply_now or create_wedge product requisition",
                )
            )
            continue

        _, opportunity, actionability = max(
            candidates,
            key=lambda item: (item[0], item[1].discovered_at or ""),
        )
        assessments.append(
            ReopenAssessment(
                contact_id=contact.contact_id,
                name=contact.full_name,
                company=company,
                condition=condition,
                status="reopen_candidate",
                reason=f"matching {actionability} requisition appeared",
                opportunity_title=opportunity.title,
                opportunity_url=opportunity.source_url,
                req_actionability=actionability,
            )
        )
    return assessments


def render_reopen_report(assessments: list[ReopenAssessment]) -> str:
    triggered = sum(item.status == "reopen_candidate" for item in assessments)
    lines = [
        "# Reopen candidates",
        "",
        f"Parked conditions checked: **{len(assessments)}** · candidates: **{triggered}**",
        "",
        "This pass does not unpark contacts or draft messages.",
        "",
    ]
    for item in assessments:
        lines.extend(
            [
                f"## {item.name} — {item.company}",
                f"- Status: `{item.status}` — {item.reason}",
                f"- Reopen when: {item.condition}",
                f"- Opportunity: {item.opportunity_title or '(none)'}",
                f"- Actionability: `{item.req_actionability or 'not_actionable'}`",
                f"- URL: {item.opportunity_url or '(none)'}",
                "",
            ]
        )
    return "\n".join(lines)


def check_reopen_conditions(
    *,
    workbook: OutreachWorkbook,
    artifacts_dir: Path,
    pursuit_season: str = "fall",
    now: datetime | None = None,
) -> tuple[Path, list[ReopenAssessment]]:
    assessments = evaluate_reopen_conditions(
        contacts=workbook.list_contacts(),
        organizations=workbook.list_organizations(),
        opportunities=workbook.list_opportunities(),
        pursuit_season=pursuit_season,
        now=now,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = artifacts_dir / f"{stamp}-reopen-candidates.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_reopen_report(assessments), encoding="utf-8")
    return output, assessments

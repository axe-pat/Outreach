#!/usr/bin/env python3
"""Resolve P2-9 contact membership without mutating the Outreach workbook.

The default pass uses workbook titles only. ``--live`` may visit an unknown
contact only when the accepted-silent dry run says a message would be drafted,
or when the contact belongs to the bounded diagnostic sample below. Dormant
unknowns stay deferred. There is deliberately no apply flag in this script.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from outreach.config import OutreachSettings  # noqa: E402
from outreach.reply_engine.org_identity import (  # noqa: E402
    CONFIRMED_HERE,
    EXPECTED_REFERRAL_AFFILIATION,
    EXPECTED_UNINVITED_AFFILIATION,
    FLAG_CONFIRMED_NAME_COLLISION,
    FLAG_ORG_MEMBERSHIP_CONFLICT,
    NON_PERSON_NAME,
    UNKNOWN_MEMBERSHIP,
    WORKS_ELSEWHERE,
    ContactMembership,
    classify_contact_membership,
    membership_conflict_disposition,
    needs_membership_verification,
    parse_current_experience_lines,
    resolve_membership_from_profile,
)
from outreach.services.linkedin import LinkedInScraper  # noqa: E402
from outreach.tracking import (  # noqa: E402
    ContactRecord,
    OrganizationRecord,
    TouchpointRecord,
)


# These are evidence samples, not a repair queue. Clara samples three of its
# person-shaped Founder rows; Yondu and Anthropic sample role-only headlines
# whose workbook text cannot establish a current employer.
DIAGNOSTIC_SAMPLE_IDS: dict[str, tuple[str, ...]] = {
    "org-clara": (
        "ct-org-clara-https-www-linkedin-com-in-cuauhtli-padilla",
        "ct-org-clara-https-www-linkedin-com-in-georgefavvas",
        "ct-org-clara-https-www-linkedin-com-in-zeeshan1293",
    ),
    "org-yondu": (
        "ct-org-yondu-https-www-linkedin-com-in-derek-tran-331b81285",
        "ct-org-yondu-https-www-linkedin-com-in-ryanstonick",
        "ct-org-yondu-https-www-linkedin-com-in-takatoshi-soeda-25b48a20a",
    ),
    "org-anthropic": (
        "ct-org-anthropic-https-www-linkedin-com-in-dzmitry-k-0729581",
        "ct-org-anthropic-https-www-linkedin-com-in-jaymin-desai",
        "ct-org-anthropic-https-www-linkedin-com-in-lelandrichardson",
    ),
}

CONFIRMED_NAME_COLLISION_CONTACT_IDS = frozenset(
    {
        "ct-org-mount-https-www-linkedin-com-in-sidharthmenon22",
        "ct-org-mount-https-www-linkedin-com-in-lorenapelegrinalvarez",
        "ct-org-mount-https-www-linkedin-com-in-vijayaganeshvm",
        "ct-org-mount-https-www-linkedin-com-in-chethanbn",
    }
)


def _load(path: Path, model):
    with path.open(newline="", encoding="utf-8") as handle:
        fields = set(model.model_fields)
        return [
            model(
                **{
                    key: value
                    for key, value in row.items()
                    if key in fields
                    and (
                        value != ""
                        or model.model_fields[key].is_required()
                    )
                }
            )
            for row in csv.DictReader(handle)
        ]


def likely_collision_org_ids(path: Path) -> list[str]:
    """Read only the P2-2 likely-collision section, never its appendix."""

    ids: list[str] = []
    in_likely = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "## `likely_collision`":
            in_likely = True
            continue
        if in_likely and line.startswith("## `"):
            break
        if not in_likely:
            continue
        match = re.match(r"### .+ \(`([^`]+)`\) —", line)
        if match:
            ids.append(match.group(1))
    return ids


def build_title_memberships(
    organizations: list[OrganizationRecord],
    contacts: list[ContactRecord],
    organization_ids: set[str],
) -> list[ContactMembership]:
    organizations_by_id = {item.organization_id: item for item in organizations}
    memberships = [
        classify_contact_membership(contact, organizations_by_id[contact.organization_id])
        for contact in contacts
        if contact.organization_id in organization_ids
        and contact.organization_id in organizations_by_id
    ]
    return sorted(
        memberships,
        key=lambda item: (
            item.organization_name.casefold(),
            item.full_name.casefold(),
            item.contact_id,
        ),
    )


def would_draft_contact_ids(path: Path) -> set[str]:
    """Read the deterministic dry-run artifact; ``ask`` is the live boundary."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item.get("contact_id") or "")
        for item in list(payload.get("results") or [])
        if isinstance(item, dict)
        and item.get("action") == "ask"
        and str(item.get("contact_id") or "")
    }


def select_profile_candidates(
    memberships: list[ContactMembership],
    *,
    live_contact_ids: set[str],
    diagnostic_sample_ids: set[str],
) -> tuple[list[ContactMembership], list[ContactMembership], list[ContactMembership]]:
    """Apply the lazy identity gate and keep suppressed unknowns deferred."""

    live: list[ContactMembership] = []
    diagnostic: list[ContactMembership] = []
    deferred: list[ContactMembership] = []
    for membership in memberships:
        if membership.classification != UNKNOWN_MEMBERSHIP:
            continue
        is_live = membership.contact_id in live_contact_ids
        is_diagnostic = membership.contact_id in diagnostic_sample_ids
        if needs_membership_verification(
            membership,
            is_live=is_live,
            is_diagnostic_sample=is_diagnostic,
        ):
            if is_live:
                live.append(membership)
            if is_diagnostic and not is_live:
                diagnostic.append(membership)
            continue
        deferred.append(membership)
    return live, diagnostic, deferred


def _experience_url(profile_url: str) -> str:
    parsed = urlsplit(profile_url)
    path = parsed.path.rstrip("/")
    if "/details/experience" not in path:
        path = f"{path}/details/experience"
    return urlunsplit((parsed.scheme or "https", parsed.netloc, f"{path}/", "", ""))


def _extract_experience_payload(page) -> dict[str, object]:
    script = r"""
    () => {
      const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
      const unique = (values) => {
        const seen = new Set();
        return values.filter((value) => {
          const key = value.toLowerCase();
          if (!value || seen.has(key)) return false;
          seen.add(key);
          return true;
        });
      };
      const main = document.querySelector('main') || document;
      const candidates = Array.from(main.querySelectorAll(
        'li.pvs-list__paged-list-item, li.artdeco-list__item, section li'
      ));
      const items = [];
      const seen = new Set();
      for (const item of candidates) {
        const text = clean(item.innerText || item.textContent);
        if (!text || seen.has(text)) continue;
        const rawLines = (item.innerText || item.textContent || '')
          .split('\n').map(clean).filter(Boolean);
        const lines = unique(rawLines);
        if (!lines.some((line) => /\b(?:Present|Current)\b/i.test(line))) continue;
        const companyAnchor = Array.from(item.querySelectorAll('a[href*="/company/"]'))
          .find((anchor) => clean(anchor.innerText || anchor.textContent));
        let company = companyAnchor
          ? clean(companyAnchor.innerText || companyAnchor.textContent)
          : '';
        company = company.replace(/\s+logo$/i, '').trim();
        items.push({
          lines,
          company,
          company_url: companyAnchor ? companyAnchor.href : '',
        });
        seen.add(text);
      }
      const headlineNode = document.querySelector(
        'main .text-body-medium.break-words, main [data-generated-suggestion-target]'
      );
      const bodyLines = (document.body ? document.body.innerText : '')
        .split('\n').map(clean).filter(Boolean);
      const experienceIndex = bodyLines.findIndex((line) => line === 'Experience');
      return {
        headline: clean(headlineNode ? headlineNode.textContent : ''),
        items,
        body_lines: bodyLines.slice(Math.max(0, experienceIndex), Math.max(0, experienceIndex) + 300),
        body_preview: clean(document.body ? document.body.innerText : '').slice(0, 500),
      };
    }
    """
    payload = page.evaluate(script)
    return payload if isinstance(payload, dict) else {"headline": "", "items": []}


def _line_company(lines: list[str]) -> str:
    for line in lines:
        if " · " not in line:
            continue
        candidate = line.split(" · ", maxsplit=1)[0].strip()
        if candidate and not re.search(r"\b(?:Present|Current)\b", line, re.I):
            return candidate
    return ""


def _line_title(lines: list[str], company: str) -> str:
    ignored = {"experience", company.casefold(), f"{company.casefold()} logo"}
    for line in lines:
        lowered = line.casefold()
        if lowered in ignored:
            continue
        if re.match(
            r"^(?:Full-time|Part-time|Self-employed|Contract|Internship)\s*·",
            line,
            re.I,
        ):
            continue
        if re.search(r"\s·\s(?:On-site|Remote|Hybrid)$", line, re.I):
            continue
        if re.search(r"\b(?:Present|Current)\b", line, re.I):
            continue
        if " · " in line and line.split(" · ", maxsplit=1)[0].strip().casefold() == company.casefold():
            continue
        return line
    return ""


def parse_current_experience(payload: dict[str, object]) -> tuple[str, str, list[str]]:
    """Choose the first explicit current experience from captured DOM evidence."""

    items = [raw for raw in list(payload.get("items") or []) if isinstance(raw, dict)]
    # LinkedIn nests a role inside a company group. Prefer the outer item with
    # its /company/ anchor; otherwise a role/date line can be mistaken for the
    # employer (Ryan Stonick's Yondu experience exposed this DOM shape).
    for require_company_anchor in (True, False):
        for raw in items:
            anchored_company = str(raw.get("company") or "").strip()
            if require_company_anchor and not anchored_company:
                continue
            lines = [
                str(line).strip()
                for line in list(raw.get("lines") or [])
                if str(line).strip()
            ]
            company = anchored_company or _line_company(lines)
            if not company:
                continue
            return company, _line_title(lines, company), lines
    return parse_current_experience_lines(
        [str(line) for line in list(payload.get("body_lines") or [])]
    )


def normalized_observation_employer(observation: dict[str, object]) -> str:
    """Repair a known nested-role parse from evidence already captured."""

    employer = str(observation.get("current_employer") or "").strip()
    if not re.search(r"\b(?:Present|Current)\b", employer, re.I):
        return employer
    preview = str(observation.get("body_preview") or "")
    match = re.search(
        r"\bExperience\s+(.{1,80}?)\s+"
        r"(?:Full-time|Part-time|Self-employed|Contract|Internship)\s+·",
        preview,
        re.I,
    )
    return match.group(1).strip() if match else ""


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_observations(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(item.get("contact_id") or ""): dict(item)
        for item in list(payload.get("results") or [])
        if isinstance(item, dict) and str(item.get("contact_id") or "")
    }


def pull_selected_profiles(
    candidates: list[ContactMembership],
    *,
    observations_path: Path,
    max_profiles: int = 0,
) -> dict[str, dict[str, object]]:
    """Read one LinkedIn experience page per unresolved contact; no clicks or writes."""

    observations = load_observations(observations_path)
    pending = [
        membership
        for membership in candidates
        if observations.get(membership.contact_id, {}).get("status") != "resolved"
    ]
    if max_profiles > 0:
        pending = pending[:max_profiles]

    settings = OutreachSettings()
    scraper = LinkedInScraper(settings)
    scraper.require_live_cdp_session()
    browser: Any = None
    with sync_playwright() as playwright:
        browser = scraper._connect_over_cdp(playwright)
        context = browser.contexts[0]
        preflight = scraper._session_preflight(context)
        if not preflight.get("ok"):
            raise RuntimeError(f"LinkedIn session preflight failed: {preflight}")
        page = context.new_page()
        page.set_default_timeout(15000)
        try:
            total = len(pending)
            for index, membership in enumerate(pending, start=1):
                target = _experience_url(membership.linkedin_url)
                observed_at = datetime.now(UTC).isoformat()
                observation: dict[str, object] = {
                    "contact_id": membership.contact_id,
                    "organization_id": membership.organization_id,
                    "organization_name": membership.organization_name,
                    "name": membership.full_name,
                    "profile_url": membership.linkedin_url,
                    "experience_url": target,
                    "observed_at": observed_at,
                    "status": "failed",
                    "current_employer": "",
                    "current_title": "",
                    "current_experience_lines": [],
                    "headline": "",
                    "final_url": "",
                    "read_only": True,
                }
                try:
                    loaded = scraper._safe_goto(page, target, timeout_ms=20000)
                    observation["final_url"] = page.url
                    if not loaded or scraper._is_authwall_or_login(page):
                        observation["status"] = "auth_or_navigation_failed"
                    else:
                        page.wait_for_timeout(700)
                        page.mouse.wheel(0, 1200)
                        page.wait_for_timeout(500)
                        payload = _extract_experience_payload(page)
                        employer, title, lines = parse_current_experience(payload)
                        observation.update(
                            {
                                "headline": str(payload.get("headline") or ""),
                                "current_employer": employer,
                                "current_title": title,
                                "current_experience_lines": lines,
                                "status": "resolved" if employer else "no_current_experience_exposed",
                                "body_preview": str(payload.get("body_preview") or ""),
                            }
                        )
                except (PlaywrightError, RuntimeError) as exc:
                    observation["status"] = "read_error"
                    observation["error"] = str(exc)
                observations[membership.contact_id] = observation
                _write_json(
                    observations_path,
                    {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "read_only": True,
                        "results": list(observations.values()),
                    },
                )
                print(
                    f"profile {index}/{total}: {membership.full_name} -> "
                    f"{observation['status']} {observation['current_employer']}"
                )
                if observation["status"] == "auth_or_navigation_failed":
                    break
        finally:
            scraper._close_page_safely(page)
            if browser is not None:
                browser.close()
    return observations


def merge_observations(
    memberships: list[ContactMembership],
    observations: dict[str, dict[str, object]],
) -> list[ContactMembership]:
    merged: list[ContactMembership] = []
    for membership in memberships:
        observation = observations.get(membership.contact_id, {})
        if observation.get("status") == "resolved":
            merged.append(
                resolve_membership_from_profile(
                    membership,
                    current_employer=normalized_observation_employer(observation),
                    current_title=str(observation.get("current_title") or ""),
                )
            )
        else:
            merged.append(membership)
    return merged


def _org_summaries(memberships: list[ContactMembership]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[ContactMembership]] = defaultdict(list)
    for membership in memberships:
        grouped[(membership.organization_id, membership.organization_name)].append(membership)
    summaries: list[dict[str, object]] = []
    for (organization_id, organization_name), rows in grouped.items():
        counts = Counter(item.classification for item in rows)
        employers = Counter(
            item.named_employer
            for item in rows
            if item.classification == WORKS_ELSEWHERE and item.named_employer
        )
        review = bool(
            counts[CONFIRMED_HERE] == 0
            and counts[UNKNOWN_MEMBERSHIP] == 0
            and counts[WORKS_ELSEWHERE] > 0
        )
        summaries.append(
            {
                "organization_id": organization_id,
                "organization_name": organization_name,
                "contact_count": len(rows),
                "classification_counts": dict(sorted(counts.items())),
                "other_employers": dict(employers.most_common()),
                "human_review": review,
                "proposed_action": "review_split_or_delete_org" if review else "none",
            }
        )
    return sorted(summaries, key=lambda item: str(item["organization_name"]).casefold())


def diagnostic_org_verdicts(
    memberships: list[ContactMembership],
    observations: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """Summarize only the reviewed three-contact samples, never the whole org."""

    by_id = {membership.contact_id: membership for membership in memberships}
    verdicts: list[dict[str, object]] = []
    for organization_id, contact_ids in DIAGNOSTIC_SAMPLE_IDS.items():
        samples = [by_id[contact_id] for contact_id in contact_ids if contact_id in by_id]
        resolved = [
            membership
            for membership in samples
            if observations.get(membership.contact_id, {}).get("status") == "resolved"
        ]
        counts = Counter(membership.classification for membership in resolved)
        if counts[CONFIRMED_HERE] >= 2:
            verdict = "supported_real_company"
        elif counts[WORKS_ELSEWHERE] >= 2:
            verdict = "likely_discovery_artifact"
        else:
            verdict = "inconclusive"
        verdicts.append(
            {
                "organization_id": organization_id,
                "organization_name": samples[0].organization_name if samples else "",
                "sample_size": len(samples),
                "resolved_count": len(resolved),
                "classification_counts": dict(sorted(counts.items())),
                "verdict": verdict,
                "sample_contacts": [
                    {
                        **membership.as_dict(),
                        "profile_read_status": observations.get(
                            membership.contact_id, {}
                        ).get("status", "not_attempted"),
                    }
                    for membership in samples
                ],
            }
        )
    return verdicts


def human_queue(
    memberships: list[ContactMembership],
    observations: dict[str, dict[str, object]],
    selected_contact_ids: set[str],
    diagnostic_verdicts: list[dict[str, object]],
    membership_context: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return only still-actionable exceptions, never deferred dormant rows."""

    queue: list[dict[str, object]] = []
    for membership in memberships:
        if membership.classification == NON_PERSON_NAME:
            queue.append(
                {
                    "kind": "garbage_contact",
                    **membership.as_dict(),
                }
            )
        elif (
            membership.classification == UNKNOWN_MEMBERSHIP
            and membership.contact_id in selected_contact_ids
        ):
            observation = observations.get(membership.contact_id, {})
            queue.append(
                {
                    "kind": "unresolved_contact",
                    **membership.as_dict(),
                    "profile_read_status": observation.get("status", "not_attempted"),
                    "profile_read_error": observation.get("error", ""),
                }
            )
    for verdict in diagnostic_verdicts:
        if verdict["verdict"] == "inconclusive":
            queue.append({"kind": "diagnostic_org_inconclusive", **verdict})
    for item in membership_context:
        if item["context_action"] in {
            FLAG_CONFIRMED_NAME_COLLISION,
            FLAG_ORG_MEMBERSHIP_CONFLICT,
        }:
            queue.append({"kind": "membership_conflict", **item})
    return queue


def render_markdown(payload: dict[str, object]) -> str:
    before = payload["before_profile_pull"]
    after = payload["after_profile_pull"]
    queue = list(payload["human_queue"])
    lines = [
        "# Contact organization-membership resolution",
        "",
        "Report only: LinkedIn reads were non-interactive and no workbook row was changed.",
        "",
        "## Counts",
        "",
        f"- Contacts in the 17-org input: **{payload['contact_count']}**",
        f"- Before profile pull: `{before}`",
        f"- After profile pull: `{after}`",
        f"- Auto-classified from workbook titles: **{payload['title_auto_classified_count']}**",
        f"- Live identity-gate candidates: **{payload['live_candidate_count']}**",
        f"- Diagnostic samples: **{payload['diagnostic_candidate_count']}**",
        f"- Deferred unknown contacts: **{payload['deferred_unknown_count']}**",
        f"- Expected referral/uninvited affiliations: **{payload['expected_affiliation_count']}**",
        f"- Visible membership-conflict flags: **{payload['membership_conflict_flag_count']}**",
        "- Automatic detach/reassignment proposals: **0**",
        f"- Human queue: **{len(queue)} rows**",
        "",
        "## Lazy verification scope",
        "",
        "Only would-draft contacts and the explicit 3x3 diagnostic sample are eligible for a profile read. Suppressed contacts remain deferred until they become live.",
        "",
        "### Live contacts",
        "",
    ]
    for item in payload["live_candidates"]:
        lines.append(
            f"- {item['full_name']} — {item['organization_name']} (`{item['contact_id']}`)"
        )
    lines.extend(["", "### Live contact results", ""])
    for item in payload["live_results"]:
        lines.append(
            f"- {item['full_name']} — `{item['classification']}` at "
            f"**{item['named_employer'] or '(unresolved)'}**; current title: "
            f"{item['title'] or '(none)'}"
        )
    lines.extend(["", "### Diagnostic samples", ""])
    for item in payload["diagnostic_candidates"]:
        lines.append(
            f"- {item['full_name']} — {item['organization_name']} (`{item['contact_id']}`)"
        )
    lines.extend(["", "## Diagnostic organization verdicts", ""])
    for verdict in payload["diagnostic_org_verdicts"]:
        lines.append(
            f"- **{verdict['organization_name']}**: `{verdict['verdict']}` "
            f"({verdict['resolved_count']}/{verdict['sample_size']} resolved; "
            f"`{verdict['classification_counts']}`)"
        )
    lines.extend(["", "## Garbage-row flags", ""])
    garbage = list(payload["garbage_rows"])
    if not garbage:
        lines.append("No garbage rows flagged.")
    for item in garbage:
        lines.append(
            f"- {item['full_name']} (`{item['contact_id']}`): `{item['garbage_reasons']}`"
        )
    lines.extend([
        "",
        "## Human queue",
        "",
    ])
    if not queue:
        lines.append("No unresolved rows.")
    for item in queue:
        kind = str(item.get("kind") or "")
        if kind == "diagnostic_org_inconclusive":
            lines.extend(
                [
                    f"### {item['organization_name']} — diagnostic sample inconclusive",
                    f"- Counts: `{item['classification_counts']}`",
                    "",
                ]
            )
            continue
        lines.extend(
            [
                f"### {item.get('full_name') or '(missing name)'} — {item.get('organization_name')}",
                f"- Kind: `{kind}`",
                f"- Contact: `{item.get('contact_id')}`",
                f"- Title: {item.get('title') or '(none)'}",
                f"- Profile: {item.get('linkedin_url') or '(none)'}",
                f"- Reason: {item.get('reason')}",
                f"- Context: {item.get('context_reason', '(not applicable)')}",
                f"- Profile read: `{item.get('profile_read_status', 'not_applicable')}`",
                "",
            ]
        )
    return "\n".join(lines)


def render_auto_classifications(payload: dict[str, object]) -> str:
    rows = list(payload["title_auto_classifications"])
    lines = [
        "# P2-9 workbook-title auto-classifications",
        "",
        "Report only: these are proposed classifications; no workbook row was changed.",
        "",
        f"The spec's measured reference was **142** rows. The current workbook and the specified current-employer filters produce **{len(rows)}** rows; this report preserves that discrepancy instead of tuning classifications to a target count.",
        "",
        "| Contact | Workbook org | Classification | Named employer | Proposed action |",
        "|---|---|---|---|---|",
    ]
    for item in rows:
        values = [
            str(item.get("full_name") or ""),
            str(item.get("organization_name") or ""),
            str(item.get("classification") or ""),
            str(item.get("named_employer") or ""),
            str(item.get("proposed_action") or ""),
        ]
        escaped = [value.replace("|", "\\|").replace("\n", " ") for value in values]
        lines.append("| " + " | ".join(escaped) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=REPO / "workspace")
    parser.add_argument(
        "--org-audit",
        type=Path,
        default=REPO / "artifacts" / "20260815-round2-org-identity-audit.md",
    )
    parser.add_argument(
        "--accepted-silent-backlog",
        type=Path,
        default=(
            REPO
            / "artifacts"
            / "20260816-round2-final-accepted-silent-dry-run.json"
        ),
    )
    parser.add_argument(
        "--observations",
        type=Path,
        default=(
            REPO
            / "artifacts"
            / "20260816-p2-9-contact-membership-profile-observations.json"
        ),
    )
    parser.add_argument("--out-prefix", type=Path, default=None)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-profiles", type=int, default=0)
    args = parser.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    prefix = args.out_prefix or REPO / "artifacts" / f"{stamp}-contact-membership"
    observations_path = args.observations

    organizations = _load(args.workspace / "organizations.csv", OrganizationRecord)
    contacts = _load(args.workspace / "contacts.csv", ContactRecord)
    touchpoints = _load(args.workspace / "touchpoints.csv", TouchpointRecord)
    org_ids = likely_collision_org_ids(args.org_audit)
    memberships = build_title_memberships(organizations, contacts, set(org_ids))
    before = Counter(item.classification for item in memberships)
    live_ids = would_draft_contact_ids(args.accepted_silent_backlog)
    diagnostic_ids = {
        contact_id
        for contact_ids in DIAGNOSTIC_SAMPLE_IDS.values()
        for contact_id in contact_ids
    }
    live_candidates, diagnostic_candidates, deferred = select_profile_candidates(
        memberships,
        live_contact_ids=live_ids,
        diagnostic_sample_ids=diagnostic_ids,
    )
    selected_candidates = live_candidates + diagnostic_candidates
    selected_ids = {item.contact_id for item in selected_candidates}

    observations = load_observations(observations_path)
    if args.live:
        observations = pull_selected_profiles(
            selected_candidates,
            observations_path=observations_path,
            max_profiles=max(0, args.max_profiles),
        )
    merged = merge_observations(memberships, observations)
    after = Counter(item.classification for item in merged)
    summaries = _org_summaries(merged)
    verdicts = diagnostic_org_verdicts(merged, observations)
    contacts_by_id = {contact.contact_id: contact for contact in contacts}
    membership_context: list[dict[str, object]] = []
    for membership in merged:
        if membership.classification != WORKS_ELSEWHERE:
            continue
        contact = contacts_by_id[membership.contact_id]
        action, reason = membership_conflict_disposition(
            membership,
            contact,
            touchpoints,
            confirmed_name_collision=(
                membership.contact_id in CONFIRMED_NAME_COLLISION_CONTACT_IDS
            ),
        )
        membership_context.append(
            {
                **membership.as_dict(),
                "context_action": action,
                "context_reason": reason,
            }
        )
    queue = human_queue(
        merged,
        observations,
        selected_ids,
        verdicts,
        membership_context,
    )
    garbage = [
        item.as_dict() for item in memberships if item.classification == NON_PERSON_NAME
    ]
    context_action_counts = Counter(
        str(item["context_action"]) for item in membership_context
    )
    title_auto_classifications = [
        item.as_dict()
        for item in memberships
        if item.classification in {CONFIRMED_HERE, WORKS_ELSEWHERE}
    ]
    merged_by_id = {item.contact_id: item for item in merged}
    live_results = [merged_by_id[item.contact_id].as_dict() for item in live_candidates]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "report_only": True,
        "linkedin_read_only": True,
        "workbook_mutations": 0,
        "selection_principle": "verify employer only when contact becomes live",
        "source_org_audit": str(args.org_audit),
        "source_accepted_silent_backlog": str(args.accepted_silent_backlog),
        "organization_ids": org_ids,
        "organization_count": len(org_ids),
        "contact_count": len(memberships),
        "before_profile_pull": dict(sorted(before.items())),
        "title_auto_classified_count": before[CONFIRMED_HERE] + before[WORKS_ELSEWHERE],
        "title_auto_classifications": title_auto_classifications,
        "title_auto_classification_reference_count": 142,
        "title_auto_classification_count_discrepancy": (
            len(title_auto_classifications) - 142
        ),
        "profile_pull_candidates": len(selected_candidates),
        "live_candidate_count": len(live_candidates),
        "live_candidates": [item.as_dict() for item in live_candidates],
        "live_results": live_results,
        "diagnostic_candidate_count": len(diagnostic_candidates),
        "diagnostic_candidates": [item.as_dict() for item in diagnostic_candidates],
        "deferred_unknown_count": len(deferred),
        "deferred_unknowns": [item.as_dict() for item in deferred],
        "unknown_without_profile_url": sum(
            item.classification == UNKNOWN_MEMBERSHIP and not item.linkedin_url
            for item in memberships
        ),
        "profile_observation_count": len(observations),
        "profile_reads_pending": sum(
            observations.get(item.contact_id, {}).get("status") != "resolved"
            for item in selected_candidates
        ),
        "profile_observation_statuses": dict(
            sorted(Counter(str(item.get("status") or "") for item in observations.values()).items())
        ),
        "after_profile_pull": dict(sorted(after.items())),
        "memberships": [item.as_dict() for item in merged],
        "organization_summaries": summaries,
        "diagnostic_org_verdicts": verdicts,
        "garbage_rows": garbage,
        "garbage_row_count": len(garbage),
        "membership_context": membership_context,
        "membership_context_action_counts": dict(sorted(context_action_counts.items())),
        "expected_affiliation_count": (
            context_action_counts[EXPECTED_REFERRAL_AFFILIATION]
            + context_action_counts[EXPECTED_UNINVITED_AFFILIATION]
        ),
        "membership_conflict_flag_count": (
            context_action_counts[FLAG_CONFIRMED_NAME_COLLISION]
            + context_action_counts[FLAG_ORG_MEMBERSHIP_CONFLICT]
        ),
        "proposed_workbook_changes": garbage,
        "proposed_workbook_change_count": len(garbage),
        "automatic_detach_or_reassignment_count": 0,
        "human_queue": queue,
        "human_queue_count": len(queue),
    }
    json_path = prefix.with_suffix(".json")
    markdown_path = prefix.with_suffix(".md")
    auto_classifications_path = prefix.with_name(
        f"{prefix.name}-auto-classifications"
    ).with_suffix(".md")
    _write_json(json_path, payload)
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    auto_classifications_path.write_text(
        render_auto_classifications(payload), encoding="utf-8"
    )

    print(json.dumps({key: payload[key] for key in (
        "organization_count",
        "contact_count",
        "before_profile_pull",
        "profile_pull_candidates",
        "live_candidate_count",
        "diagnostic_candidate_count",
        "deferred_unknown_count",
        "unknown_without_profile_url",
        "profile_reads_pending",
        "profile_observation_statuses",
        "after_profile_pull",
        "garbage_row_count",
        "expected_affiliation_count",
        "membership_conflict_flag_count",
        "proposed_workbook_change_count",
        "automatic_detach_or_reassignment_count",
        "human_queue_count",
    )}, indent=2))
    print(f"wrote {json_path}")
    print(f"wrote {markdown_path}")
    print(f"wrote {auto_classifications_path}")
    print("workbook_mutations=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

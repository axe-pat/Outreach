#!/usr/bin/env python3
"""Run the reply engine over a reconciled follow-up backlog.

Usage:
    python scripts/run_reply_engine.py --backlog artifacts/<...>-backlog.json
    python scripts/run_reply_engine.py --backlog <...> --live   # calls the model

Without ``--live`` the model is never called: the structured read falls back to
regex extraction and no copy is composed.  That still exercises thread
ordering, state resolution, the decision table and the collision policy, which
is the fastest way to see what the engine decided and why.  Copy-level critic
checks require candidate copy, so they run in focused tests and live composition.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from outreach.reply_engine import Action, ThreadInput, run, summarize  # noqa: E402
from outreach.reply_engine.proof import load_proof_beats  # noqa: E402
from outreach.reply_engine.reopen import persist_reopen_conditions  # noqa: E402
from outreach.reply_engine.touches import outbound_followup_touch_counts  # noqa: E402
from outreach.tracking import (  # noqa: E402
    ContactRecord,
    OpportunityRecord,
    OrganizationRecord,
    OutreachWorkbook,
    TouchpointRecord,
)


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _model(cls, row: dict):
    fields = cls.model_fields
    payload: dict = {}
    for key, value in row.items():
        if key not in fields:
            continue
        if value == "":
            # Keep empties for required fields (e.g. message_text on
            # withdrawals); let pydantic defaults fill optional ones.
            if fields[key].is_required():
                payload[key] = value
            continue
        payload[key] = value
    return cls(**payload)


def load_workbook(workspace: Path):
    organizations = {
        row["organization_id"]: _model(OrganizationRecord, row)
        for row in _rows(workspace / "organizations.csv")
    }
    contacts = {
        row["contact_id"]: _model(ContactRecord, row)
        for row in _rows(workspace / "contacts.csv")
    }
    opportunities: dict[str, list[OpportunityRecord]] = defaultdict(list)
    for row in _rows(workspace / "opportunities.csv"):
        opportunities[row["organization_id"]].append(_model(OpportunityRecord, row))
    return organizations, contacts, opportunities


def load_invite_timestamps(workspace: Path) -> dict[str, datetime]:
    """When each invite was sent, so the undated invite row can be placed.

    Without this the invite has no timestamp and threads that begin with an
    inbound message cannot be ordered - which is the Sandeep P. failure.

    Takes the EARLIEST invite touchpoint per contact.  Reconcile passes re-log
    the same invite at a later date, and taking the last one places the invite
    after replies it actually preceded.
    """

    stamps: dict[str, datetime] = {}
    for row in _rows(workspace / "touchpoints.csv"):
        if row.get("message_kind") != "linkedin_invite":
            continue
        raw = (row.get("sent_at") or row.get("recorded_at") or "").strip()
        contact_id = row.get("contact_id") or ""
        if not raw or not contact_id:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        existing = stamps.get(contact_id)
        if existing is None or parsed < existing:
            stamps[contact_id] = parsed
    return stamps


def load_touch_counts(workspace: Path) -> dict[str, int]:
    touchpoints = [
        _model(TouchpointRecord, row)
        for row in _rows(workspace / "touchpoints.csv")
    ]
    return outbound_followup_touch_counts(touchpoints)


def _window(value) -> list[dict]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = ast.literal_eval(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, SyntaxError):
            return []
    return []


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "fired"}


def build_inputs(
    backlog: dict,
    organizations,
    contacts,
    opportunities,
    invite_stamps,
    touch_counts,
):
    inputs = []
    for row in backlog.get("results", []):
        contact = contacts.get(row.get("contact_id", ""))
        if contact is None:
            contact = ContactRecord(
                contact_id=row.get("contact_id", ""),
                organization_id=row.get("organization_id", ""),
                full_name=row.get("name", ""),
                title=row.get("title", ""),
                contact_type=row.get("contact_type", ""),
            )
        # Workbook identity repairs outrank stale artifact organization IDs.
        # The reviewed Ventura artifact still says org-ventura, but those ten
        # contacts now correctly belong to org-ventura-securities.
        organization_id = contact.organization_id or row.get("organization_id", "")
        inputs.append(
            ThreadInput(
                contact=contact,
                organization=organizations.get(organization_id),
                raw_window=_window(row.get("message_window")),
                opportunities=opportunities.get(organization_id, []),
                segment=str(row.get("segment") or "reply"),
                relationship_context=str(row.get("relationship_context") or ""),
                band=row.get("band", "") or "",
                invite_sent_at=invite_stamps.get(row.get("contact_id", "")),
                # Artifacts created before full-thread capture carried no
                # confidence marker. Treat them as partial rather than silently
                # drafting against what may only be an inbox preview line.
                capture_confidence=str(row.get("capture_confidence") or "partial"),
                captured_message_count=int(row.get("captured_message_count") or 0),
                expected_message_count=(
                    int(row["expected_message_count"])
                    if str(row.get("expected_message_count") or "").isdigit()
                    else None
                ),
                touch_count=touch_counts.get(contact.contact_id, 0),
                reopen_condition_fired=_truthy(
                    row.get("reopen_condition_fired")
                ),
                hold_reason=str(row.get("hold_reason") or ""),
            )
        )
    return inputs


def render_markdown(drafts, stats) -> str:
    lines = [
        f"# Reply engine run — {datetime.now(UTC).date()}",
        "",
        f"Threads: **{stats['total']}** · messages: **{stats['with_message']}** · "
        f"suppressed: **{stats['suppressed']}** · held: **{stats['held']}**",
        f"Mean length: **{stats['mean_words']} words** · "
        f"max sentence reuse: **{stats['max_sentence_reuse']}**",
        f"Contacts to create: **{stats['contacts_to_create']}** · "
        f"human tasks: **{stats['human_tasks']}**",
        "",
    ]
    by_action = defaultdict(list)
    for draft in drafts:
        by_action[draft.decision.action].append(draft)

    for action in Action:
        group = by_action.get(action)
        if not group:
            continue
        lines.append(f"## {action.value} ({len(group)})")
        lines.append("")
        for draft in group:
            lines.append(f"### {draft.name} — {draft.company}")
            lines.append(f"- **Title:** {draft.title[:90]}")
            lines.append(
                f"- **State:** {draft.thread_state.value} · **rule {draft.decision.rule}** · "
                f"ask={draft.decision.ask.value} · {draft.decision.reason}"
            )
            capture_total = (
                str(draft.expected_message_count)
                if draft.expected_message_count is not None
                else "unknown"
            )
            lines.append(
                f"- **Capture:** {draft.capture_confidence} · "
                f"{draft.captured_message_count}/{capture_total} messages"
            )
            lines.append(
                f"- **Touches:** {draft.touch_count} prior follow-up(s) · "
                f"cap_reached={str(draft.touch_cap_reached).lower()} · "
                f"reopen_fired={str(draft.reopen_condition_fired).lower()}"
            )
            if draft.last_message:
                lines.append(f"- **Last:** {draft.last_message[:180]}")
            if draft.decision.contacts_to_create:
                names = ", ".join(p.name for p in draft.decision.contacts_to_create)
                lines.append(f"- **Create contacts:** {names}")
            if draft.decision.human_tasks:
                lines.append(f"- **Human task:** {'; '.join(draft.decision.human_tasks)}")
            if draft.usable_proof:
                lines.append(f"- **Usable proof:** {', '.join(draft.usable_proof)}")
            if draft.decision.reopen_condition:
                lines.append(f"- **Reopen when:** {draft.decision.reopen_condition}")
            if draft.decision.availability_qualifier:
                lines.append(
                    f"- **Required availability:** {draft.decision.availability_qualifier}"
                )
            if draft.critic_flags:
                lines.append(f"- **Critic:** {', '.join(draft.critic_flags)}")
            if draft.message:
                lines.append("")
                lines.append("```")
                lines.append(draft.message)
                lines.append("```")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backlog", required=True)
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--live", action="store_true", help="call the model")
    parser.add_argument("--season", default="fall")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out-prefix", default="")
    args = parser.parse_args()

    backlog = json.loads(Path(args.backlog).read_text(encoding="utf-8"))
    workspace = REPO / args.workspace
    organizations, contacts, opportunities = load_workbook(workspace)
    proof_path = workspace / "proof_beats.yml"
    proof_beats = load_proof_beats(proof_path) if proof_path.exists() else []
    profile_path = REPO / "Profile" / "profile.md"
    profile_text = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""
    invite_stamps = load_invite_timestamps(workspace)
    touch_counts = load_touch_counts(workspace)
    inputs = build_inputs(
        backlog,
        organizations,
        contacts,
        opportunities,
        invite_stamps,
        touch_counts,
    )
    if args.limit:
        inputs = inputs[: args.limit]

    client = None
    if args.live:
        import anthropic

        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            print("ANTHROPIC_API_KEY not set; refusing to run --live", file=sys.stderr)
            return 2
        client = anthropic.Anthropic(api_key=key, timeout=60.0)

    drafts = run(
        inputs,
        client=client,
        pursuit_season=args.season,
        proof_beats=proof_beats,
        profile_text=profile_text,
    )
    persisted_reopen_conditions = (
        persist_reopen_conditions(
            OutreachWorkbook(workspace),
            drafts,
        )
        if args.live
        else 0
    )
    stats = summarize(drafts)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    prefix = args.out_prefix or f"artifacts/{stamp}-reply-engine"
    Path(f"{prefix}.md").write_text(render_markdown(drafts, stats), encoding="utf-8")
    Path(f"{prefix}.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "source_backlog": args.backlog,
                "live": args.live,
                "reopen_conditions_persisted": persisted_reopen_conditions,
                "summary": stats,
                "results": [
                    {
                        "contact_id": d.contact_id,
                        "name": d.name,
                        "company": d.company,
                        "title": d.title,
                        "state": d.thread_state.value,
                        "action": d.decision.action.value,
                        "ask": d.decision.ask.value,
                        "rule": d.decision.rule,
                        "reason": d.decision.reason,
                        "req_actionability": d.decision.req_actionability,
                        "requisition": d.decision.citable_req,
                        "capability": d.capability.value,
                        "read_capability": d.read.capability.value,
                        "question_kind": d.read.question_kind,
                        "offer_made": d.read.offer_made,
                        "offer_target": d.read.offer_target,
                        "read_source": d.read.source,
                        "message": d.message,
                        "critic_flags": d.critic_flags,
                        "contacts_to_create": [p.name for p in d.decision.contacts_to_create],
                        "human_tasks": d.decision.human_tasks,
                        "reopen_condition": d.decision.reopen_condition,
                        "availability_qualifier": d.decision.availability_qualifier,
                        "capture_confidence": d.capture_confidence,
                        "captured_message_count": d.captured_message_count,
                        "expected_message_count": d.expected_message_count,
                        "touch_count": d.touch_count,
                        "touch_cap_reached": d.touch_cap_reached,
                        "reopen_condition_fired": d.reopen_condition_fired,
                        "usable_proof": d.usable_proof,
                    }
                    for d in drafts
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps(stats, indent=2))
    if args.live:
        print(f"persisted reopen conditions: {persisted_reopen_conditions}")
    print(f"\nwrote {prefix}.md and {prefix}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

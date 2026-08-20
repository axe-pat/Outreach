#!/usr/bin/env python3
"""Draft every LinkedIn follow-up lane into one human review artifact.

The run is artifact-first: it may read the latest reconciled ledger and call
the configured models, but it never sends or creates a send queue. The single
Markdown output is deliberately copy-first and contains no decision traces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.build_accepted_silent_backlog import (  # noqa: E402
    build_backlog,
    normalized,
    write_first_outreach_review,
)
from scripts.run_reply_engine import (  # noqa: E402
    build_inputs,
    load_invite_timestamps,
    load_touch_counts,
    load_workbook,
)
from outreach.reply_engine import (  # noqa: E402
    Action,
    Ask,
    persist_reopen_conditions,
    run,
    summarize,
)
from outreach.reply_engine.critic import batch_repetition_sentences  # noqa: E402
from outreach.reply_engine.proof import load_proof_beats  # noqa: E402
from outreach.tracking import OutreachWorkbook  # noqa: E402

ASK_ORDER = (Ask.CREATE, Ask.REFER, Ask.NAME, Ask.INTEL)
_FROM_US = {"you", "me", "akshat", "akshat pathak"}


def _has_prior_outbound_linkedin(row: dict) -> bool:
    """Defense-in-depth gate shared by every follow-up lane."""

    if str(row.get("original_invite_note") or "").strip():
        return True
    for item in row.get("message_window") or []:
        if not isinstance(item, dict):
            continue
        if normalized(str(item.get("sender") or "")) not in _FROM_US:
            continue
        if str(item.get("message") or "").strip():
            return True
    return False


def _load_env_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    path = REPO / ".env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("ANTHROPIC_API_KEY="):
            continue
        return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _latest_reconcile_artifact() -> Path:
    candidates = sorted(
        (REPO / "artifacts").glob("*-linkedin-message-reconcile.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("no linkedin-message-reconcile artifact found")
    return candidates[0]


def _locked_names(path: Path) -> set[str]:
    """Parse every named thread in the locked approved-sends document."""

    names: set[str] = set()
    in_already_suppressed = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Already suppressed"):
            in_already_suppressed = True
            continue
        if in_already_suppressed:
            if line.startswith("## "):
                in_already_suppressed = False
            elif line.strip():
                for value in line.split("·"):
                    cleaned = value.strip().strip("* .")
                    if cleaned:
                        names.add(normalized(cleaned))
        patterns = (
            r"^###\s+(?:\d+(?:–\d+)?\.\s+)?(.+?)\s+—\s+",
            r"^\*\*(.+?)\s+—\s+",
            r"^##\s+New outreach\s+—\s+(.+?)\s*$",
            r"^\|\s*\*\*(.+?)\*\*\s*\|",
        )
        for pattern in patterns:
            match = re.match(pattern, line)
            if match:
                names.add(normalized(match.group(1)))
                break
    return {name for name in names if name}


def _reply_rows(reconcile_path: Path) -> tuple[list[dict], list[str]]:
    payload = json.loads(reconcile_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    human_tasks: list[str] = []
    for item in list(payload.get("results") or []):
        action = str(item.get("action") or "")
        if action == "ambiguous_contact_match":
            human_tasks.append(
                f"Resolve ambiguous LinkedIn thread for {item.get('name') or '(unknown name)'}"
            )
            continue
        if not item.get("contact_id") or str(item.get("normalized_status")) != "replied":
            continue
        rows.append(
            {
                **item,
                "segment": "reply",
                "relationship_context": "existing_conversation",
                "capture_confidence": str(item.get("capture_confidence") or "partial"),
            }
        )
    return rows, human_tasks


def _unmatched_created_rows(workspace: Path) -> list[dict]:
    workbook = OutreachWorkbook(workspace)
    contacts_by_name: dict[str, list] = defaultdict(list)
    for contact in workbook.list_contacts():
        if "created from unmatched thread" in (contact.notes or "").casefold():
            contacts_by_name[normalized(contact.full_name)].append(contact)

    state_path = workspace / "linkedin_message_state.json"
    if not state_path.exists():
        return []
    states = json.loads(state_path.read_text(encoding="utf-8")).get("thread_states") or {}
    rows: list[dict] = []
    for state in states.values():
        if not isinstance(state, dict):
            continue
        matches = contacts_by_name.get(normalized(str(state.get("name") or "")), [])
        if len(matches) != 1:
            continue
        window = list(state.get("message_window") or [])
        distinct_messages = {
            str(item.get("message") or "").strip().casefold()
            for item in window
            if isinstance(item, dict) and str(item.get("message") or "").strip()
        }
        has_inbound = any(
            normalized(str(item.get("sender") or "")) not in _FROM_US
            for item in window
            if isinstance(item, dict) and str(item.get("sender") or "").strip()
        )
        if len(distinct_messages) < 2 or not has_inbound:
            continue
        contact = matches[0]
        unresolved_org = contact.organization_id.startswith("org-unresolved-")
        rows.append(
            {
                "contact_id": contact.contact_id,
                "organization_id": contact.organization_id,
                "name": contact.full_name,
                "title": contact.title,
                "segment": "unmatched_created",
                "relationship_context": "existing_conversation",
                "message_window": window,
                "capture_confidence": str(state.get("capture_confidence") or "partial"),
                "captured_message_count": len(window),
                "expected_message_count": state.get("expected_message_count"),
                "hold_reason": (
                    "unmatched-created contact still has unresolved organization identity"
                    if unresolved_org
                    else ""
                ),
            }
        )
    return rows


def build_combined_backlog(
    *,
    workspace: Path,
    reconcile_path: Path,
    approved_sends: Path,
    season: str,
    first_outreach_review_output: Path | None = None,
) -> tuple[dict, list[str], dict[str, int]]:
    silent = build_backlog(workspace=workspace, pursuit_season=season)
    if first_outreach_review_output is not None:
        write_first_outreach_review(
            list(silent.get("first_outreach_review") or []),
            first_outreach_review_output,
        )
    reply_rows, preflight_tasks = _reply_rows(reconcile_path)
    unmatched_rows = _unmatched_created_rows(workspace)
    locked = _locked_names(approved_sends)

    by_contact: dict[str, dict] = {
        str(row["contact_id"]): dict(row) for row in silent["results"]
    }
    for row in reply_rows + unmatched_rows:
        by_contact[str(row["contact_id"])] = row

    locked_count = 0
    no_prior_outbound_count = 0
    rows: list[dict] = []
    for row in by_contact.values():
        if normalized(str(row.get("name") or "")) in locked:
            locked_count += 1
            continue
        if not _has_prior_outbound_linkedin(row):
            no_prior_outbound_count += 1
            continue
        rows.append(row)
    rows.sort(
        key=lambda row: (
            {"reply": 0, "unmatched_created": 1, "accepted_silent": 2}.get(
                str(row.get("segment") or ""), 9
            ),
            str(row.get("company") or "").casefold(),
            str(row.get("name") or "").casefold(),
        )
    )
    source_counts = Counter(str(row.get("segment") or "unknown") for row in rows)
    meta = {
        "accepted_silent_source": int(silent["summary"]["by_segment"].get("accepted_silent", 0)),
        "moved_to_first_outreach_review": int(
            silent["summary"].get("moved_to_first_outreach_review", 0)
        ),
        "reply_candidates": len(reply_rows),
        "unmatched_created_candidates": len(unmatched_rows),
        "locked_approved_threads_excluded": locked_count,
        "no_prior_outbound_gate_excluded": no_prior_outbound_count,
        **{f"combined_{key}": value for key, value in source_counts.items()},
    }
    return {"results": rows}, preflight_tasks, meta


def _last_thing(draft) -> str:
    if draft.last_message:
        speaker = "You" if normalized(draft.last_sender) in _FROM_US else draft.last_sender or "Recipient"
        return f"{speaker}: {draft.last_message}"
    if draft.segment == "warm_uninvited":
        return "Existing warm connection / PeopleGrove contact; no prior LinkedIn message."
    return "Invite accepted; no reply yet."


def _review_entry(draft) -> list[str]:
    lines = [
        f"### {draft.name} — {draft.company}",
        f"- **Title:** {draft.title or '(missing title)'}",
        f"- **Last thing:** {_last_thing(draft)}",
    ]
    if not draft.critic_passed:
        lines.append(f"- **HELD — critic:** {', '.join(draft.critic_flags)}")
    lines.extend(["", draft.message.strip(), ""])
    return lines


def render_review(
    drafts,
    stats,
    meta: dict[str, int],
    preflight_tasks: list[str],
    *,
    additional_2027_suppressions=(),
) -> str:
    messages = [draft for draft in drafts if draft.message]
    sendable = [
        draft
        for draft in messages
        if draft.critic_passed and draft.decision.action is not Action.HOLD
    ]
    held = [draft for draft in drafts if draft.decision.action is Action.HOLD]
    suppressed_for_2027_by_contact = {
        draft.contact_id: draft
        for draft in [*drafts, *additional_2027_suppressions]
        if draft.decision.action is Action.SUPPRESS
        and "preserve for 2027 re-entry" in draft.decision.reason
    }
    suppressed_for_2027 = list(suppressed_for_2027_by_contact.values())
    critic_counts = Counter(flag for draft in drafts for flag in draft.critic_flags)
    ask_counts = Counter(draft.decision.ask for draft in messages)
    sentence_counts = Counter(
        sentence.casefold()
        for draft in messages
        for sentence in batch_repetition_sentences(
            draft.message, draft.decision
        )
    )
    mean_words = round(
        sum(len(draft.message.split()) for draft in messages) / len(messages), 1
    ) if messages else 0
    max_reuse = max(sentence_counts.values(), default=0)

    lines = [
        f"# LinkedIn follow-up review — {datetime.now().astimezone().date()}",
        "",
        "Artifact only. Nothing in this file has been sent or added to a send queue.",
        "",
        "## Batch summary",
        "",
        f"- **Total drafts:** {len(messages)}",
        f"- **Sendable:** {len(sendable)}",
        f"- **Held:** {len(held)}",
        f"- **Suppressed for 2027:** {len(suppressed_for_2027)}",
        "- **Ask split:** " + ", ".join(
            f"{ask.value.upper()} {ask_counts[ask]}" for ask in ASK_ORDER
        ),
        f"- **Direct replies / no ask:** {ask_counts[Ask.NONE]}",
        f"- **Mean word count:** {mean_words}",
        f"- **Maximum sentence reuse:** {max_reuse}",
        "- **Critic flags:** " + (
            ", ".join(f"{flag} ({count})" for flag, count in sorted(critic_counts.items()))
            if critic_counts else "none"
        ),
        f"- **Source lanes:** accepted-silent {meta.get('combined_accepted_silent', 0)}, "
        f"warm/never-invited {meta.get('combined_warm_uninvited', 0)}, "
        f"reply {meta.get('combined_reply', 0)}, unmatched-created {meta.get('combined_unmatched_created', 0)}",
        f"- **Locked approved threads excluded:** {meta.get('locked_approved_threads_excluded', 0)}",
        "",
    ]

    grouped: dict[Ask, list] = defaultdict(list)
    direct: list = []
    for draft in messages:
        if draft.decision.ask is Ask.NONE:
            direct.append(draft)
        else:
            grouped[draft.decision.ask].append(draft)

    for ask in (Ask.CREATE, Ask.REFER):
        lines.extend([f"## {ask.value.upper()} ({len(grouped[ask])})", ""])
        for draft in grouped[ask]:
            lines.extend(_review_entry(draft))

    if direct:
        lines.extend([f"## DIRECT REPLIES — NO ASK ({len(direct)})", ""])
        for draft in direct:
            lines.extend(_review_entry(draft))

    lines.extend([f"## NAME ({len(grouped[Ask.NAME])})", ""])
    for draft in grouped[Ask.NAME]:
        lines.extend(_review_entry(draft))

    lines.extend([
        f"## INTEL ({len(grouped[Ask.INTEL])})",
        "",
        "Compact review block: one low-cost question per recipient.",
        "",
    ])
    for draft in grouped[Ask.INTEL]:
        lines.extend(
            [
                f"### {draft.name} — {draft.company}",
                f"**{draft.title or '(missing title)'}** · {_last_thing(draft)}",
                "",
                draft.message.strip(),
                "",
            ]
        )
        if not draft.critic_passed:
            lines.insert(len(lines) - 2, f"**HELD — critic:** {', '.join(draft.critic_flags)}")

    deterministic_holds = [draft for draft in held if not draft.message]
    lines.extend(["## HELD — NO DRAFT", ""])
    if deterministic_holds:
        for draft in deterministic_holds:
            lines.extend(
                [
                    f"### {draft.name} — {draft.company}",
                    f"- **Title:** {draft.title or '(missing title)'}",
                    f"- **Last thing:** {_last_thing(draft)}",
                    f"- **HELD:** {draft.decision.reason}",
                    "",
                ]
            )
    else:
        lines.extend(["None.", ""])

    lines.extend(["## Suppressed for 2027 re-entry", ""])
    if suppressed_for_2027:
        for draft in sorted(
            suppressed_for_2027,
            key=lambda item: (item.company.casefold(), item.name.casefold()),
        ):
            lines.append(
                f"- **{draft.name} — {draft.company}** ({draft.title or 'missing title'}): "
                f"{draft.decision.reopen_condition or '2027 full-time or new-grad recruiting opens'}"
            )
    else:
        lines.append("- None.")
    lines.append("")

    contacts_to_create = [
        (draft, person)
        for draft in drafts
        for person in draft.decision.contacts_to_create
    ]
    lines.extend(["## Contact rows to create", ""])
    if contacts_to_create:
        for draft, person in contacts_to_create:
            lines.append(f"- **{person.name}** — named by {draft.name} at {draft.company}")
    else:
        lines.append("- None.")

    human_tasks = list(preflight_tasks)
    for draft in drafts:
        human_tasks.extend(
            f"{draft.name} — {task}" for task in draft.decision.human_tasks
        )
    lines.extend(["", "## Human tasks", ""])
    if human_tasks:
        lines.extend(f"- {task}" for task in dict.fromkeys(human_tasks))
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--reconcile-artifact", default="")
    parser.add_argument(
        "--approved-sends",
        default="artifacts/20260814-approved-sends.md",
    )
    parser.add_argument("--season", default="fall")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    workspace = REPO / args.workspace
    reconcile_path = (
        (REPO / args.reconcile_artifact)
        if args.reconcile_artifact
        else _latest_reconcile_artifact()
    )
    approved_sends = REPO / args.approved_sends
    output = (
        REPO / args.output
        if args.output
        else REPO / "artifacts" / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-reply-engine-review.md"
    )
    first_outreach_review_output = output.with_name(
        f"{output.stem}-warm-never-invited-manual-review.md"
    )
    backlog, preflight_tasks, meta = build_combined_backlog(
        workspace=workspace,
        reconcile_path=reconcile_path,
        approved_sends=approved_sends,
        season=args.season,
        first_outreach_review_output=first_outreach_review_output,
    )
    organizations, contacts, opportunities = load_workbook(workspace)
    inputs = build_inputs(
        backlog,
        organizations,
        contacts,
        opportunities,
        load_invite_timestamps(workspace),
        load_touch_counts(workspace),
    )
    if args.limit:
        inputs = inputs[: args.limit]

    client = None
    if args.live:
        import anthropic

        key = _load_env_key()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for --live")
        client = anthropic.Anthropic(api_key=key, timeout=60.0)

    proof_path = workspace / "proof_beats.yml"
    profile_path = REPO / "Profile" / "profile.md"
    drafts = run(
        inputs,
        client=client,
        pursuit_season=args.season,
        proof_beats=load_proof_beats(proof_path) if proof_path.exists() else [],
        profile_text=profile_path.read_text(encoding="utf-8") if profile_path.exists() else "",
    )
    persisted_reopen_conditions = (
        persist_reopen_conditions(OutreachWorkbook(workspace), drafts)
        if args.live
        else 0
    )
    stats = summarize(drafts)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_review(drafts, stats, meta, preflight_tasks), encoding="utf-8")

    print(
        json.dumps(
            {
                "summary": stats,
                "lanes": meta,
                "reopen_conditions_persisted": persisted_reopen_conditions,
                "output": str(output),
            },
            indent=2,
        )
    )
    print("send_actions=0")
    print("json_review_artifacts=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

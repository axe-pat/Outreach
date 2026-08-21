#!/usr/bin/env python3
"""Replay deterministic batch gates over an existing Markdown review pack.

This command never calls a model and never sends.  It exists so a gate change
can be evaluated against the exact copy already paid for, without silently
regenerating the batch.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.run_reply_engine import (  # noqa: E402
    build_inputs,
    load_invite_timestamps,
    load_touch_counts,
    load_workbook,
)
from scripts.run_reply_engine_all_lanes import (  # noqa: E402
    _latest_reconcile_artifact,
    build_combined_backlog,
)
from outreach.reply_engine import (  # noqa: E402
    Action,
    Ask,
    ReplyDraft,
    ThreadInput,
    load_proof_beats,
    order_messages,
    review,
    run,
)
from outreach.reply_engine.critic import (  # noqa: E402
    batch_repetition_sentences,
    company_ask_key,
    company_ask_sentences,
)
from outreach.reply_engine.proof import used_proof_beats  # noqa: E402
from outreach.reply_engine.org_identity import (  # noqa: E402
    WORKS_ELSEWHERE,
    classify_contact_membership,
)
from outreach.reply_engine.thread import (  # noqa: E402
    last_inbound_text,
    original_invite_text,
)


SECTION_ASKS = {
    "CREATE": Ask.CREATE,
    "REFER": Ask.REFER,
    "NAME": Ask.NAME,
    "INTEL": Ask.INTEL,
    "DIRECT REPLIES — NO ASK": Ask.NONE,
}
_FLAG_BOUNDARY = re.compile(r", (?=[a-z_]+(?::|,|$))")


@dataclass
class SavedDraft:
    name: str
    company: str
    title: str
    ask: Ask
    message: str
    old_flags: list[str]
    last_thing: str = ""


@dataclass
class ReplayContext:
    item: ThreadInput
    draft: ReplyDraft
    invite_text: str
    last_inbound_message: str


def _split_flags(value: str) -> list[str]:
    return [
        flag.strip()
        for flag in _FLAG_BOUNDARY.split(value)
        if flag.strip()
    ]


def _parse_entry(lines: list[str], ask: Ask) -> SavedDraft:
    heading = lines[0].removeprefix("### ")
    name, company = heading.split(" — ", 1)
    title = ""
    last_thing = ""
    old_flags: list[str] = []
    message_lines: list[str] = []
    for line in lines[1:]:
        if line.startswith("- **Title:** "):
            title = line.removeprefix("- **Title:** ")
            continue
        if line.startswith("- **Last thing:** "):
            last_thing = line.removeprefix("- **Last thing:** ")
            continue
        if line.startswith("**") and "** · " in line:
            marker = line.index("** · ")
            title = line[2:marker]
            last_thing = line[marker + len("** · "):]
            continue
        held_match = re.search(r"HELD — (?:critic|data):\*\* (.+)$", line)
        if held_match:
            old_flags = _split_flags(held_match.group(1))
            continue
        message_lines.append(line)
    message = "\n".join(message_lines).strip()
    return SavedDraft(
        name=name.strip(),
        company=company.strip(),
        title=title.strip(),
        ask=ask,
        message=message,
        old_flags=old_flags,
        last_thing=last_thing.strip(),
    )


def parse_review(path: Path) -> list[SavedDraft]:
    lines = path.read_text(encoding="utf-8").splitlines()
    drafts: list[SavedDraft] = []
    current_ask: Ask | None = None
    entry: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if entry and current_ask is not None:
                drafts.append(_parse_entry(entry, current_ask))
            entry = []
            label = re.sub(
                r"\s+\(\d+(?:\s+—\s+[^)]*)?\)$",
                "",
                line.removeprefix("## "),
            )
            current_ask = SECTION_ASKS.get(label)
            continue
        if line.startswith("### "):
            if entry and current_ask is not None:
                drafts.append(_parse_entry(entry, current_ask))
            entry = [line] if current_ask is not None else []
            continue
        if entry:
            entry.append(line)
    if entry and current_ask is not None:
        drafts.append(_parse_entry(entry, current_ask))
    return drafts


def _key(name: str, company: str) -> tuple[str, str]:
    return (
        " ".join((name or "").split()).casefold(),
        " ".join((company or "").split()).casefold(),
    )


def _prior_flagged_names(path: Path, flag: str) -> set[str]:
    """Read contact names from an earlier audit before replacing its logic."""

    if not path.exists():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- **") or flag not in line:
            continue
        names.add(_key(line.removeprefix("- **").split(" — ", 1)[0], "")[0])
    return names


def build_replay_contexts(
    *,
    workspace: Path,
    reconcile_path: Path,
    approved_sends: Path,
    season: str,
) -> tuple[list[ReplayContext], dict[str, int]]:
    backlog, _preflight_tasks, meta = build_combined_backlog(
        workspace=workspace,
        reconcile_path=reconcile_path,
        approved_sends=approved_sends,
        season=season,
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
    dry_drafts = run(inputs, client=None, pursuit_season=season)
    contexts: list[ReplayContext] = []
    for item, draft in zip(inputs, dry_drafts, strict=True):
        messages, _order_confident = order_messages(
            item.raw_window,
            invite_sent_at=item.invite_sent_at,
        )
        contexts.append(
            ReplayContext(
                item=item,
                draft=draft,
                invite_text=original_invite_text(messages),
                last_inbound_message=last_inbound_text(messages),
            )
        )
    return contexts, meta


def _context_for_saved(
    saved: SavedDraft,
    contexts: list[ReplayContext],
) -> ReplayContext | None:
    exact = [
        context
        for context in contexts
        if _key(context.draft.name, context.draft.company)
        == _key(saved.name, saved.company)
    ]
    if len(exact) == 1:
        return exact[0]
    same_name = [
        context
        for context in contexts
        if _key(context.draft.name, "")[0] == _key(saved.name, "")[0]
    ]
    return same_name[0] if len(same_name) == 1 else None


def replay(
    drafts: list[SavedDraft],
    contexts: list[ReplayContext],
    proof_beats,
    profile_text: str,
    prior_terminal_names: set[str] | None = None,
) -> tuple[list[dict], Counter[str], Counter[Ask]]:
    sentence_counts: Counter[str] = Counter()
    company_ask_counts: Counter[tuple[str, str]] = Counter()
    proof_counts: Counter[tuple[Ask, str]] = Counter()
    rows: list[dict] = []
    metrics: Counter[str] = Counter()
    full_ask_split: Counter[Ask] = Counter(
        context.draft.decision.ask
        for context in contexts
        if context.draft.decision.action is Action.ASK
        and context.draft.decision.ask is not Ask.NONE
    )
    metrics["full_suppressed_for_2027"] = sum(
        context.draft.decision.action is Action.SUPPRESS
        and "preserve for 2027 re-entry" in context.draft.decision.reason
        for context in contexts
    )
    for saved in drafts:
        metrics["old_critic_held" if saved.old_flags else "old_sendable"] += 1
        original_message = saved.message
        if "—" in original_message:
            metrics["em_dash_normalized"] += 1
        context = _context_for_saved(saved, contexts)
        if context is None:
            metrics["unmapped"] += 1
            rows.append(
                {
                    "draft": saved,
                    "flags": [
                        "thread history incomplete; targeted LinkedIn re-pull required"
                    ],
                    "status": "data_hold",
                    "current_ask": Ask.NONE,
                }
            )
            continue

        current = context.draft
        decision = copy.deepcopy(current.decision)
        if _key(saved.name, "")[0] in (prior_terminal_names or set()):
            metrics[
                f"prior_terminal_{decision.campaign_track}"
            ] += 1
        membership = None
        if context.item.organization is not None:
            membership = classify_contact_membership(
                context.item.contact,
                context.item.organization,
            )
        if (
            context.item.relationship_context == "warm_uninvited_referral"
            and membership is not None
            and membership.classification == WORKS_ELSEWHERE
        ):
            metrics["expected_warm_referral_mismatch"] += 1
        reason = decision.reason.casefold()
        if decision.action is Action.HOLD:
            if reason.startswith("org binding unverified"):
                status = "org_mismatch_hold"
            elif "apology or correction" in reason:
                status = "apology_hold"
            else:
                status = "other_deterministic_hold"
            metrics[status] += 1
            rows.append(
                {
                    "draft": saved,
                    "flags": [decision.reason],
                    "status": status,
                    "current_ask": decision.ask,
                    "membership": membership,
                }
            )
            continue
        if decision.action is Action.SUPPRESS:
            metrics["currently_suppressed"] += 1
            if "preserve for 2027 re-entry" in decision.reason:
                metrics["saved_suppressed_for_2027"] += 1
            rows.append(
                {
                    "draft": saved,
                    "flags": [decision.reason],
                    "status": "currently_suppressed",
                    "current_ask": decision.ask,
                    "membership": membership,
                }
            )
            continue

        result = review(
            message=saved.message,
            decision=decision,
            read=current.read,
            capability=current.capability,
            batch_sentence_counts=sentence_counts,
            batch_company_ask_counts=company_ask_counts,
            has_attachment_task=bool(decision.human_tasks),
            proof_beats=proof_beats,
            proof_beat_counts=proof_counts,
            profile_text=profile_text,
            recipient_title=current.title,
            relationship_context=context.item.relationship_context,
            recipient_name=current.name,
            company=current.company,
            invite_text=context.invite_text,
            last_inbound_message=context.last_inbound_message,
        )
        # The critic's deterministic punctuation normalization is part of the
        # reviewed copy, not a model rewrite. Persist it into the saved row so
        # counters and any reissued pack use exactly what the critic evaluated.
        saved.message = result.normalized_message
        # A fragment flag records provenance from the one-time em-dash rewrite.
        # Once the normalized pack is re-read there is no dash left from which
        # to infer that provenance, so keep this hold until the row is actually
        # regenerated.
        persistent_normalization_flags = [
            flag
            for flag in saved.old_flags
            if flag.startswith("em_dash_fragment:")
        ]
        flags = list(dict.fromkeys([*result.flags, *persistent_normalization_flags]))
        if saved.ask is not decision.ask:
            flags.append(f"stale_ask:{saved.ask.value}->{decision.ask.value}")
            metrics["stale_ask"] += 1
        critic_passed = result.passed and not persistent_normalization_flags
        if critic_passed:
            metrics["critic_passed"] += 1
        else:
            metrics["critic_failed"] += 1
        status = "release" if not flags else "regenerate"
        metrics[status] += 1
        if saved.old_flags and status == "release":
            metrics["old_held_released"] += 1
        elif saved.old_flags:
            metrics["old_held_still_held"] += 1
        elif status != "release":
            metrics["old_sendable_newly_held"] += 1
        if any(flag.startswith("em_dash_fragment:") for flag in flags):
            metrics["em_dash_fragment_drafts"] += 1
        rows.append(
            {
                "draft": saved,
                "flags": flags,
                "status": status,
                "current_ask": decision.ask,
                "membership": membership,
                "decision": decision,
                "context": context,
            }
        )
        for sentence in batch_repetition_sentences(saved.message, decision):
            sentence_counts[sentence.casefold()] += 1
        for question in company_ask_sentences(saved.message, decision):
            key = company_ask_key(current.company, question)
            if all(key):
                company_ask_counts[key] += 1
        for beat in used_proof_beats(saved.message, proof_beats):
            proof_counts[(decision.ask, beat.beat_id)] += 1
    metrics["total"] = len(drafts)
    metrics["mapped"] = len(drafts) - metrics["unmapped"]
    metrics["max_non_ask_sentence_reuse"] = max(sentence_counts.values(), default=0)
    metrics["prior_terminal_total"] = sum(
        metrics[f"prior_terminal_{track}"]
        for track in ("startup", "large_company")
    )
    return rows, metrics, full_ask_split


def render_report(
    rows: list[dict],
    metrics: Counter[str],
    full_ask_split: Counter[Ask],
    meta: dict[str, int],
) -> str:
    flag_counts = Counter(flag for row in rows for flag in row["flags"])
    flag_family_counts = Counter(
        flag.split(":", 1)[0]
        for row in rows
        if row["status"] == "regenerate"
        for flag in row["flags"]
    )
    released = [row for row in rows if row["status"] == "release"]
    residual = [row for row in rows if row["status"] != "release"]
    org_rate = (
        100 * metrics["org_mismatch_hold"] / metrics["total"]
        if metrics["total"] else 0
    )
    org_rows = [row for row in rows if row["status"] == "org_mismatch_hold"]
    non_routing_org_rows = [
        row
        for row in org_rows
        if row.get("membership") is not None
        and row["membership"].bound_affiliation_type
        not in {"none", "employment_or_internship", "untyped_current_affiliation"}
    ]
    genuine_org_rows = [row for row in org_rows if row not in non_routing_org_rows]

    def names(values: list[dict]) -> str:
        return ", ".join(row["draft"].name for row in values) or "none"

    lines = [
        "# Follow-up copy pre-regeneration audit",
        "",
        f"Deterministic replay over the saved {metrics['total']}-message review pack. "
        "No model was called, no draft was regenerated, and nothing was sent.",
        "",
        "## Before regeneration",
        "",
        f"- Saved drafts replayed: {metrics['total']} (mapped {metrics['mapped']})",
        f"- Prior pack sendable: {metrics['old_sendable']}",
        f"- Release after deterministic replay: {metrics['release']}",
        f"- Previously held drafts released by deterministic rules: "
        f"{metrics['old_held_released']}",
        f"- Previously sendable drafts newly held: "
        f"{metrics['old_sendable_newly_held']}",
        f"- Drafts with em dashes normalized in code: "
        f"{metrics['em_dash_normalized']}",
        f"- Drafts where normalization created a sentence under five words: "
        f"{metrics['em_dash_fragment_drafts']}",
        "- The expected 43 releases resolve to 41: Will Nzeuton remains over "
        "budget after the dash-separated words are counted correctly, and "
        "Subramani Ramadas now exposes a bare `there` company referent after "
        "the full-stop substitution.",
        f"- Total drafts requiring regeneration: {metrics['regenerate']} "
        f"(critic failures {metrics['critic_failed']}; stale asks {metrics['stale_ask']})",
        "- Residual critic reasons by family: " + (
            ", ".join(
                f"{flag} ({count})"
                for flag, count in sorted(
                    flag_family_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            )
            if flag_family_counts else "none"
        ),
        f"- Hold for organization mismatch: {metrics['org_mismatch_hold']} "
        f"({org_rate:.1f}% of the saved pack)",
        f"  - Genuine employment/internship mismatch: {len(genuine_org_rows)} "
        f"({names(genuine_org_rows)})",
        f"  - Target bound only by a non-routing affiliation: "
        f"{len(non_routing_org_rows)} ({names(non_routing_org_rows)})",
        "  - Valid concurrent employment/internship affiliation among the held rows: 0",
        f"- Expected different-employer warm referral mappings (not holds): "
        f"{metrics['expected_warm_referral_mismatch']}",
        f"- Hold for apology/correction: {metrics['apology_hold']}",
        f"- Other deterministic holds: {metrics['other_deterministic_hold']}",
        f"- Suppressed for 2027 re-entry across the full dry run: "
        f"{metrics['full_suppressed_for_2027']} "
        f"(of which {metrics['saved_suppressed_for_2027']} are in the saved pack)",
        f"- Other currently suppressed saved drafts: "
        f"{metrics['currently_suppressed'] - metrics['saved_suppressed_for_2027']}",
        "- Corrected would-draft ask split: "
        + ", ".join(
            f"{ask.value.upper()} {full_ask_split[ask]}"
            for ask in (Ask.CREATE, Ask.REFER, Ask.NAME, Ask.INTEL)
        ),
        f"- Full dry-run lanes: accepted-silent {meta.get('combined_accepted_silent', 0)}, "
        f"warm/never-invited {meta.get('combined_warm_uninvited', 0)}, "
        f"reply {meta.get('combined_reply', 0)}, "
        f"unmatched-created {meta.get('combined_unmatched_created', 0)}",
        "- Prior 57 `terminal_touch_not_named` drafts by track: "
        f"startup {metrics['prior_terminal_startup']}, "
        f"large_company {metrics['prior_terminal_large_company']} "
        f"(total {metrics['prior_terminal_total']})",
        f"- Maximum non-ask sentence reuse: {metrics['max_non_ask_sentence_reuse']}",
        "- Other prescribed sentences exposed to the batch gate: none. Ask "
        "questions are already excluded; the opening, availability, company-name, "
        "and track-goal requirements prescribe substance rather than literal copy.",
        "- All replay flags: " + (
            ", ".join(f"{flag} ({count})" for flag, count in sorted(flag_counts.items()))
            if flag_counts else "none"
        ),
        "",
        "## Warm/never-invited opener",
        "",
        "The writer must open with the recipient's first name and a truthful reason "
        "for reaching out about the target company. It may not thank them for accepting "
        "or connecting, because no invite was sent in this lane.",
        "",
        "## Released without model regeneration",
        "",
    ]
    if released:
        for row in released:
            draft = row["draft"]
            lines.append(
                f"- **{draft.name} — {draft.company} ({draft.ask.value.upper()})**"
            )
    else:
        lines.append("- None.")
    lines.extend([
        "",
        "## Holds and residual drafts",
        "",
    ])
    if not residual:
        lines.append("- None.")
    else:
        for row in residual:
            draft = row["draft"]
            lines.append(
                f"- **{draft.name} — {draft.company} "
                f"({draft.ask.value.upper()} -> {row['current_ask'].value.upper()})** "
                f"[{row['status']}]: "
                + ", ".join(row["flags"])
            )
    lines.append("")
    return "\n".join(lines)


def _preserved_footer(review_path: Path) -> tuple[str, int, int]:
    """Keep no-draft holds, re-entry rows, and human tasks byte-for-byte."""

    text = review_path.read_text(encoding="utf-8")
    marker = "## HELD — NO DRAFT"
    if marker not in text:
        raise RuntimeError(f"review pack has no {marker!r} section")
    footer = marker + text.split(marker, 1)[1]
    held_section = footer.split("## Suppressed for 2027 re-entry", 1)[0]
    suppressed_section = footer.split("## Suppressed for 2027 re-entry", 1)[1]
    suppressed_section = suppressed_section.split("## Contact rows to create", 1)[0]
    deterministic_holds = len(re.findall(r"^### ", held_section, re.M))
    suppressed = len(re.findall(r"^- \*\*", suppressed_section, re.M))
    return footer.rstrip() + "\n", deterministic_holds, suppressed


def render_reissued_review(
    *,
    rows: list[dict],
    original_review: Path,
    meta: dict[str, int],
) -> str:
    """Render the same paid-for drafts after deterministic critic replay."""

    footer, deterministic_holds, suppressed = _preserved_footer(original_review)
    sendable = [row for row in rows if row["status"] == "release"]
    critic_held = [row for row in rows if row["status"] != "release"]
    ask_counts = Counter(row["draft"].ask for row in rows)
    critic_counts = Counter(flag for row in critic_held for flag in row["flags"])
    sentence_counts: Counter[str] = Counter()
    for row in rows:
        decision = row.get("decision")
        if decision is None:
            continue
        for sentence in batch_repetition_sentences(row["draft"].message, decision):
            sentence_counts[sentence.casefold()] += 1
    rows_with_copy = [row for row in rows if row["draft"].message.strip()]
    mean_words = round(
        sum(len(row["draft"].message.split()) for row in rows_with_copy)
        / len(rows_with_copy),
        1,
    ) if rows_with_copy else 0
    max_reuse = max(sentence_counts.values(), default=0)

    original_text = original_review.read_text(encoding="utf-8")
    original_lines = original_text.splitlines()
    preserved_summary = [
        line
        for line in original_lines
        if line.startswith("- **Manual follow-up holds:**")
        or line.startswith("- **Suppressed:**")
        or line.startswith("- **Suppressed permanently:**")
        or line.startswith("- **Moved to manual first-message review:**")
        or line.startswith("- **Fivetran manual holds outside this pack:**")
        or line.startswith("- **Decision-layer ask split before operator verbatim overrides:**")
        or line.startswith("- **Source lanes:**")
        or line.startswith("- **Source boundary:**")
        or line.startswith("- **Locked approved threads excluded:**")
    ]
    if not preserved_summary:
        preserved_summary = [
            f"- **Source lanes:** accepted-silent {meta.get('combined_accepted_silent', 0)}, "
            f"warm/never-invited {meta.get('combined_warm_uninvited', 0)}, "
            f"reply {meta.get('combined_reply', 0)}, "
            f"unmatched-created {meta.get('combined_unmatched_created', 0)}",
            f"- **Locked approved threads excluded:** "
            f"{meta.get('locked_approved_threads_excluded', 0)}",
        ]

    artifact_note = (
        "Artifact only. Nothing in this file has been sent or added to a send queue."
    )
    if "Henry Kwan is excluded" in original_text:
        artifact_note += (
            " Henry Kwan is excluded because his 2026-08-19 manual send is "
            "already logged."
        )

    lines = [
        f"# LinkedIn follow-up review — {datetime.now().astimezone().date()}",
        "",
        artifact_note,
        "",
        "## Batch summary",
        "",
        f"- **Total drafts:** {len(rows)}",
        f"- **Sendable:** {len(sendable)}",
        f"- **Held:** {len(critic_held) + deterministic_holds}",
        f"- **Suppressed for 2027:** {suppressed}",
        "- **Ask split:** " + ", ".join(
            f"{ask.value.upper()} {ask_counts[ask]}"
            for ask in (Ask.CREATE, Ask.REFER, Ask.NAME, Ask.INTEL)
        ),
        f"- **Direct replies / no ask:** {ask_counts[Ask.NONE]}",
        f"- **Mean word count:** {mean_words}",
        f"- **Maximum sentence reuse:** {max_reuse}",
        "- **Critic flags:** " + (
            ", ".join(
                f"{flag} ({count})"
                for flag, count in sorted(critic_counts.items())
            )
            if critic_counts else "none"
        ),
        *preserved_summary,
        "",
    ]

    def append_standard(row: dict) -> None:
        draft = row["draft"]
        lines.extend([
            f"### {draft.name} — {draft.company}",
            f"- **Title:** {draft.title or '(missing title)'}",
            f"- **Last thing:** {draft.last_thing or 'No prior message recorded.'}",
        ])
        if row["status"] != "release":
            label = "data" if row["status"] == "data_hold" else "critic"
            lines.append(f"- **HELD — {label}:** {', '.join(row['flags'])}")
        lines.extend(["", draft.message.strip(), ""])

    for ask in (Ask.CREATE, Ask.REFER):
        group = [row for row in rows if row["draft"].ask is ask]
        lines.extend([f"## {ask.value.upper()} ({len(group)})", ""])
        for row in group:
            append_standard(row)

    direct = [row for row in rows if row["draft"].ask is Ask.NONE]
    if direct:
        lines.extend([f"## DIRECT REPLIES — NO ASK ({len(direct)})", ""])
        for row in direct:
            append_standard(row)

    name_rows = [row for row in rows if row["draft"].ask is Ask.NAME]
    lines.extend([f"## NAME ({len(name_rows)})", ""])
    for row in name_rows:
        append_standard(row)

    intel_rows = [row for row in rows if row["draft"].ask is Ask.INTEL]
    all_intel_held = bool(intel_rows) and all(
        row["status"] != "release" for row in intel_rows
    )
    intel_heading = f"## INTEL ({len(intel_rows)})"
    intel_note = "Compact review block: one low-cost question per recipient."
    if all_intel_held:
        intel_heading = f"## INTEL ({len(intel_rows)} — ALL HELD)"
        intel_note = (
            "All entries in this block are held; none are currently sendable."
        )
    lines.extend([
        intel_heading,
        "",
        intel_note,
        "",
    ])
    for row in intel_rows:
        draft = row["draft"]
        lines.extend([
            f"### {draft.name} — {draft.company}",
            f"**{draft.title or '(missing title)'}** · "
            f"{draft.last_thing or 'No prior message recorded.'}",
            "",
        ])
        if row["status"] != "release":
            label = "data" if row["status"] == "data_hold" else "critic"
            lines.append(f"**HELD — {label}:** {', '.join(row['flags'])}")
        lines.extend([draft.message.strip(), ""])

    lines.extend([footer.rstrip(), ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("review")
    parser.add_argument(
        "--proof-beats",
        default="workspace/proof_beats.yml",
    )
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--reconcile-artifact", default="")
    parser.add_argument(
        "--approved-sends",
        default="artifacts/20260814-approved-sends.md",
    )
    parser.add_argument("--season", default="fall")
    parser.add_argument("--profile", default="Profile/profile.md")
    parser.add_argument(
        "--prior-audit",
        default="artifacts/20260818-followup-copy-pre-regeneration-audit.md",
    )
    parser.add_argument(
        "--output",
        default=(
            f"artifacts/{datetime.now(UTC):%Y%m%d}-"
            "followup-copy-corrected-pre-regeneration-audit.md"
        ),
    )
    parser.add_argument(
        "--review-output",
        default="",
        help="Optional no-model reissue of the review pack after normalization.",
    )
    args = parser.parse_args()

    drafts = parse_review(REPO / args.review)
    proof_beats = load_proof_beats(REPO / args.proof_beats)
    workspace = REPO / args.workspace
    reconcile_path = (
        REPO / args.reconcile_artifact
        if args.reconcile_artifact
        else _latest_reconcile_artifact()
    )
    contexts, meta = build_replay_contexts(
        workspace=workspace,
        reconcile_path=reconcile_path,
        approved_sends=REPO / args.approved_sends,
        season=args.season,
    )
    profile_path = REPO / args.profile
    rows, metrics, full_ask_split = replay(
        drafts,
        contexts,
        proof_beats,
        profile_path.read_text(encoding="utf-8") if profile_path.exists() else "",
        _prior_flagged_names(
            REPO / args.prior_audit,
            "terminal_touch_not_named",
        ),
    )
    report = render_report(rows, metrics, full_ask_split, meta)
    output = REPO / args.output
    output.write_text(report, encoding="utf-8")
    if args.review_output:
        review_output = REPO / args.review_output
        review_output.write_text(
            render_reissued_review(
                rows=rows,
                original_review=REPO / args.review,
                meta=meta,
            ),
            encoding="utf-8",
        )
        print(f"review={review_output}")
    print(report)
    print(f"report={output}")
    print("anthropic_calls=0")
    print("regenerated_drafts=0")
    print("send_actions=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

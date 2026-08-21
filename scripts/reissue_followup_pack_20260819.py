#!/usr/bin/env python3
"""Reissue the Aug 19 follow-up pack after the first-message boundary fix.

This command is artifact-only.  It removes every contact without prior
outbound LinkedIn evidence, preserves operator-finalized copy verbatim, and
regenerates only drafts whose ask changed or whose INTEL focus changed.  It
has no send integration.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.build_accepted_silent_backlog import (  # noqa: E402
    build_backlog,
    normalized,
    write_first_outreach_review,
)
from scripts.recritic_reply_review import (  # noqa: E402
    SavedDraft,
    _context_for_saved,
    build_replay_contexts,
    parse_review,
    render_reissued_review,
)
from scripts.run_reply_engine_all_lanes import (  # noqa: E402
    _latest_reconcile_artifact,
    _load_env_key,
    _locked_names,
)
from outreach.reply_engine import Action, Ask, Capability, run  # noqa: E402
from outreach.reply_engine.critic import (  # noqa: E402
    batch_repetition_sentences,
    company_ask_key,
    company_ask_sentences,
    review,
)
from outreach.reply_engine.proof import (  # noqa: E402
    load_proof_beats,
    used_proof_beats,
)
from outreach.tracking import OutreachWorkbook  # noqa: E402


MANUAL_FOLLOWUP_HOLD_MARKER = "manual_followup_hold"


def _sent_from_review_names(
    workbook: OutreachWorkbook,
    review_path: Path,
) -> set[str]:
    """Return contacts whose exact reviewed follow-up is already durable."""

    contacts = {contact.contact_id: contact for contact in workbook.list_contacts()}
    names: set[str] = set()
    for touchpoint in workbook.list_touchpoints():
        if normalized(touchpoint.status) != "sent":
            continue
        if normalized(touchpoint.message_kind) != "linkedin followup":
            continue
        if Path(touchpoint.source_artifact or "").name != review_path.name:
            continue
        contact = contacts.get(touchpoint.contact_id)
        if contact is not None:
            names.add(normalized(contact.full_name))
    return names


VERBATIM_DRAFTS: dict[str, tuple[Ask, str]] = {
    "ryan samadi": (
        Ask.CREATE,
        "Hi Ryan, last note from me on this. More directly: would you be open to "
        "bringing on a part-time product intern at Alt-X this fall? Either way, "
        "wishing you the best with Alt-X!",
    ),
    "andrew pekin": (
        Ask.CREATE,
        "Hi Andrew, last note from me on this. Simple ask rather than another pitch: "
        "would you be open to bringing on a part-time product intern at Bellagent this "
        "fall? Either way, best of luck with Bellagent!",
    ),
    "daichi hiraoka": (
        Ask.CREATE,
        "Hi Daichi, last note from me on this. Would you consider taking on a fall "
        "product intern at Korso? Happy to start on whatever's most annoying right now. "
        "Either way, best of luck!",
    ),
    "sean wu": (
        Ask.CREATE,
        "Hi Sean, thanks for connecting. Synphony's bed-level analytics and pipeline "
        "integration is close to what I spent five years on at Hevo, diagnosing "
        "reliability across 120K+ pipelines. Would love to talk about a fall product "
        "internship there if you're open to it!",
    ),
    "ryan liu": (
        Ask.REFER,
        "Hi Ryan, thanks for connecting. I built an AI agent that runs my whole job "
        "search: sourcing, ranking, outreach, follow-ups. Which is to say I've built a "
        "worse version of Jobright for an audience of one. I saw the Product Manager "
        "Intern role and it's exactly what I'm after this fall. Would you be open to "
        "referring me?",
    ),
    "kelly mcdonald": (
        Ask.NAME,
        "Hi Kelly, thanks for connecting. I'm exploring a fall product internship or "
        "co-op at Abridge and would find it really helpful to know who I should be "
        "talking to about that. I'll stop bugging you after this one, promise!",
    ),
}


def _existing_no_draft_details(review_text: str, name: str) -> dict[str, str]:
    """Recover display context when a durable hold no longer parses as a draft."""

    match = re.search(
        rf"^### {re.escape(name)} — (?P<company>[^\n]+)\n"
        rf"- \*\*Title:\*\* (?P<title>[^\n]*)\n"
        rf"- \*\*Last thing:\*\* (?P<last_thing>[^\n]*)$",
        review_text,
        re.M,
    )
    return match.groupdict() if match else {}


def _last_thing(context) -> str:
    draft = context.draft
    if draft.last_message:
        sender = normalized(draft.last_sender)
        speaker = "You" if sender in {"you", "me", "akshat", "akshat pathak"} else (
            draft.last_sender or "Recipient"
        )
        return f"{speaker}: {draft.last_message}"
    return "Invite accepted; no reply yet."


def _review_one(draft, context, proof_beats, profile_text):
    decision = draft.decision
    result = review(
        message=draft.message,
        decision=decision,
        read=draft.read,
        capability=draft.capability,
        has_attachment_task=bool(decision.human_tasks),
        proof_beats=proof_beats,
        profile_text=profile_text,
        recipient_title=draft.title,
        relationship_context=context.item.relationship_context,
        recipient_name=draft.name,
        company=draft.company,
        invite_text=context.invite_text,
        last_inbound_message=context.last_inbound_message,
    )
    draft.message = result.normalized_message
    draft.critic_flags = result.flags
    draft.critic_passed = result.passed


def _seed_batch(drafts_and_contexts, proof_beats):
    sentence_counts: Counter[str] = Counter()
    company_ask_counts: Counter[tuple[str, str]] = Counter()
    proof_counts: Counter[tuple[Ask, str]] = Counter()
    banned: list[str] = []
    for draft, _context in drafts_and_contexts:
        sentences = batch_repetition_sentences(draft.message, draft.decision)
        banned.extend(sentences)
        sentence_counts.update(sentence.casefold() for sentence in sentences)
        for question in company_ask_sentences(draft.message, draft.decision):
            key = company_ask_key(draft.company, question)
            if all(key):
                company_ask_counts[key] += 1
        for beat in used_proof_beats(draft.message, proof_beats):
            proof_counts[(draft.decision.ask, beat.beat_id)] += 1
    return sentence_counts, company_ask_counts, proof_counts, banned


def _final_critic_pass(
    drafts_and_contexts,
    operator_names: set[str],
    proof_beats,
    profile_text: str,
) -> None:
    sentence_counts: Counter[str] = Counter()
    company_ask_counts: Counter[tuple[str, str]] = Counter()
    proof_counts: Counter[tuple[Ask, str]] = Counter()
    for draft, context in drafts_and_contexts:
        operator_final = normalized(draft.name) in operator_names
        if not operator_final:
            result = review(
                message=draft.message,
                decision=draft.decision,
                read=draft.read,
                capability=draft.capability,
                batch_sentence_counts=sentence_counts,
                batch_company_ask_counts=company_ask_counts,
                has_attachment_task=bool(draft.decision.human_tasks),
                proof_beats=proof_beats,
                proof_beat_counts=proof_counts,
                profile_text=profile_text,
                recipient_title=draft.title,
                relationship_context=context.item.relationship_context,
                recipient_name=draft.name,
                company=draft.company,
                invite_text=context.invite_text,
                last_inbound_message=context.last_inbound_message,
            )
            draft.message = result.normalized_message
            draft.critic_flags = result.flags
            draft.critic_passed = result.passed
        else:
            draft.critic_flags = []
            draft.critic_passed = True
            draft.compose_source = "operator_verbatim_20260819"

        sentence_counts.update(
            sentence.casefold()
            for sentence in batch_repetition_sentences(
                draft.message,
                draft.decision,
            )
        )
        for question in company_ask_sentences(draft.message, draft.decision):
            key = company_ask_key(draft.company, question)
            if all(key):
                company_ask_counts[key] += 1
        for beat in used_proof_beats(draft.message, proof_beats):
            proof_counts[(draft.decision.ask, beat.beat_id)] += 1


def _render_rows(drafts_and_contexts) -> list[dict]:
    rows: list[dict] = []
    for draft, context in drafts_and_contexts:
        saved = SavedDraft(
            name=draft.name,
            company=draft.company,
            title=draft.title,
            ask=draft.decision.ask,
            message=draft.message,
            old_flags=[],
            last_thing=_last_thing(context),
        )
        rows.append(
            {
                "draft": saved,
                "flags": list(draft.critic_flags),
                "status": "release" if draft.critic_passed else "regenerate",
                "decision": draft.decision,
            }
        )
    return rows


def _decorate_review(
    review_text: str,
    *,
    permanent_contacts,
    organization_names: dict[str, str],
    fivetran_rows,
    manual_hold_rows,
    moved_count: int,
    current_decision_split: Counter[Ask],
) -> str:
    permanent_count = len(permanent_contacts)
    suppressed_2027_match = re.search(
        r"^- \*\*Suppressed for 2027:\*\* (\d+)$",
        review_text,
        re.M,
    )
    suppressed_2027 = int(suppressed_2027_match.group(1)) if suppressed_2027_match else 0
    replacement = (
        f"- **Suppressed:** {permanent_count + suppressed_2027}\n"
        f"- **Suppressed permanently:** {permanent_count}\n"
        f"- **Suppressed for 2027:** {suppressed_2027}\n"
        f"- **Moved to manual first-message review:** {moved_count}\n"
        f"- **Fivetran manual holds outside this pack:** {len(fivetran_rows)}\n"
        "- **Decision-layer ask split before operator verbatim overrides:** "
        + ", ".join(
            f"{ask.value.upper()} {current_decision_split[ask]}"
            for ask in (Ask.CREATE, Ask.REFER, Ask.NAME, Ask.INTEL)
        )
    )
    review_text = re.sub(
        r"^- \*\*Suppressed for 2027:\*\* \d+$",
        replacement,
        review_text,
        count=1,
        flags=re.M,
    )
    review_text = re.sub(
        r"^- \*\*Source lanes:\*\*.*$",
        "- **Source boundary:** first-message contacts are excluded; this pack contains follow-ups only",
        review_text,
        count=1,
        flags=re.M,
    )
    if "- **Source boundary:**" not in review_text:
        review_text = review_text.replace(
            "- **Locked approved threads excluded:**",
            "- **Source boundary:** first-message contacts are excluded; this pack contains follow-ups only\n"
            "- **Locked approved threads excluded:**",
            1,
        )
    review_text = review_text.replace(
        "Artifact only. Nothing in this file has been sent or added to a send queue.",
        "Artifact only. Nothing in this file has been sent or added to a send queue. "
        "Henry Kwan is excluded because his 2026-08-19 manual send is already logged.",
        1,
    )

    existing_manual_count = sum(
        bool(
            re.search(
                rf"^### {re.escape(row['name'])} — ",
                review_text,
                re.M,
            )
        )
        for row in manual_hold_rows
    )
    for row in manual_hold_rows:
        review_text = re.sub(
            rf"\n### {re.escape(row['name'])} — [^\n]+\n.*?"
            rf"(?=\n### |\n## Permanently suppressed)",
            "",
            review_text,
            flags=re.S,
        )

    manual_lines: list[str] = []
    for row in sorted(manual_hold_rows, key=lambda item: item["name"].casefold()):
        manual_lines.extend(
            [
                f"### {row['name']} — {row['company']}",
                f"- **Title:** {row['title'] or '(missing title)'}",
                f"- **Last thing:** {row['last_thing'] or 'No prior message recorded.'}",
                "- **HELD:** Akshat will reply manually later; the engine must not draft or send.",
                "",
            ]
        )
    if manual_lines:
        review_text = review_text.replace(
            "## HELD — NO DRAFT",
            "## HELD — NO DRAFT\n\n" + "\n".join(manual_lines).rstrip(),
            1,
        )

    newly_counted_manual = len(manual_hold_rows) - existing_manual_count
    if newly_counted_manual:
        review_text = re.sub(
            r"^(- \*\*Held:\*\* )(\d+)$",
            lambda match: (
                match.group(1)
                + str(int(match.group(2)) + newly_counted_manual)
            ),
            review_text,
            count=1,
            flags=re.M,
        )
    review_text = re.sub(
        r"^- \*\*Manual follow-up holds:\*\* \d+\n?",
        "",
        review_text,
        flags=re.M,
    )
    review_text = re.sub(
        r"^(- \*\*Held:\*\* \d+)$",
        rf"\1\n- **Manual follow-up holds:** {len(manual_hold_rows)}",
        review_text,
        count=1,
        flags=re.M,
    )

    # A resume/recovery run may use an already decorated review as its source.
    # Strip the old block before inserting the current durable suppression set.
    review_text = re.sub(
        r"\n## Permanently suppressed\n.*?(?=\n## Suppressed for 2027 re-entry)",
        "",
        review_text,
        flags=re.S,
    )
    permanent_lines = [
        "## Permanently suppressed",
        "",
        "These are not sends and carry a durable ledger marker.",
        "",
    ]
    for contact in sorted(permanent_contacts, key=lambda item: item.full_name.casefold()):
        company = organization_names.get(contact.organization_id, contact.organization_id)
        permanent_lines.append(
            f"- **{contact.full_name} — {company}** "
            f"({contact.title or 'missing title'})"
        )
    permanent_lines.append("")
    review_text = review_text.replace(
        "## Suppressed for 2027 re-entry",
        "\n".join(permanent_lines) + "\n## Suppressed for 2027 re-entry",
        1,
    )

    fivetran_lines = [
        "## Fivetran — held for Akshat's manual first outreach",
        "",
        "No copy was generated. These rows also appear in the separate first-message artifact.",
        "",
    ]
    for row in sorted(fivetran_rows, key=lambda item: item["name"].casefold()):
        fivetran_lines.append(
            f"- **{row['name']}** ({row.get('title') or 'missing title'})"
        )
    fivetran_lines.append("")
    review_text = review_text.replace(
        "## HELD — NO DRAFT",
        "\n".join(fivetran_lines) + "\n## HELD — NO DRAFT",
        1,
    )
    suresh_task = (
        "- Suresh Mergu — Reply manually later; durable engine hold is active."
    )
    if re.search(r"^- Suresh Mergu —.*$", review_text, re.M):
        review_text = re.sub(
            r"^- Suresh Mergu —.*$",
            suresh_task,
            review_text,
            count=1,
            flags=re.M,
        )
    elif "## Human tasks\n" in review_text:
        review_text = review_text.replace(
            "## Human tasks\n",
            "## Human tasks\n\n" + suresh_task,
            1,
        )
    return review_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--review",
        default="artifacts/20260819-linkedin-followup-review-critic-replay.md",
    )
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument(
        "--approved-sends",
        default="artifacts/20260814-approved-sends.md",
    )
    parser.add_argument("--reconcile-artifact", default="")
    parser.add_argument("--season", default="fall")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--resume-empty",
        action="store_true",
        help="Reuse every non-empty draft and retry composer-error rows only.",
    )
    parser.add_argument(
        "--limit-targets",
        type=int,
        default=0,
        help="Bound an incremental composer-error retry; zero means all.",
    )
    parser.add_argument(
        "--recover-empty-from",
        default="",
        help=(
            "No-model recovery: restore prior paid-for text for empty composer-error "
            "rows and keep each one held."
        ),
    )
    parser.add_argument(
        "--output",
        default="artifacts/20260819-linkedin-followup-review-reissued.md",
    )
    parser.add_argument(
        "--warm-review-output",
        default="artifacts/20260819-warm-never-invited-manual-review.md",
    )
    args = parser.parse_args()

    workspace = REPO / args.workspace
    review_path = REPO / args.review
    approved_sends = REPO / args.approved_sends
    reconcile_path = (
        REPO / args.reconcile_artifact
        if args.reconcile_artifact
        else _latest_reconcile_artifact()
    )
    workbook = OutreachWorkbook(workspace)
    contacts = workbook.list_contacts()
    organization_names = {
        organization.organization_id: organization.name
        for organization in workbook.list_organizations()
    }
    permanent_contacts = [
        contact
        for contact in contacts
        if "suppress follow up permanently" in (contact.notes or "").casefold()
    ]
    permanent_names = {normalized(contact.full_name) for contact in permanent_contacts}
    manual_hold_contacts = [
        contact
        for contact in contacts
        if MANUAL_FOLLOWUP_HOLD_MARKER in (contact.notes or "").casefold()
    ]
    manual_hold_names = {
        normalized(contact.full_name) for contact in manual_hold_contacts
    }
    locked_names = _locked_names(approved_sends)
    already_sent_names = _sent_from_review_names(workbook, review_path)

    silent = build_backlog(workspace=workspace, pursuit_season=args.season)
    warm_rows = list(silent.get("first_outreach_review") or [])
    warm_names = {normalized(row["name"]) for row in warm_rows}
    write_first_outreach_review(warm_rows, REPO / args.warm_review_output)
    fivetran_rows = [
        row
        for row in warm_rows
        if normalized(row.get("company") or "") == "fivetran"
        and normalized(row.get("name") or "") not in permanent_names
    ]

    source_review_text = review_path.read_text(encoding="utf-8")
    saved_all = parse_review(review_path)
    saved_by_name = {normalized(item.name): item for item in saved_all}
    manual_hold_rows = []
    for contact in manual_hold_contacts:
        saved_item = saved_by_name.get(normalized(contact.full_name))
        existing = _existing_no_draft_details(
            source_review_text,
            contact.full_name,
        )
        manual_hold_rows.append(
            {
                "name": contact.full_name,
                "company": existing.get("company") or organization_names.get(
                    contact.organization_id,
                    contact.organization_id,
                ),
                "title": (
                    contact.title
                    or (saved_item.title if saved_item else "")
                    or existing.get("title", "")
                ),
                "last_thing": (
                    saved_item.last_thing
                    if saved_item
                    else existing.get("last_thing", "")
                ),
            }
        )
    recovery_by_key: dict[tuple[str, str], SavedDraft] = {}
    if args.recover_empty_from:
        recovery_by_key = {
            (normalized(item.name), normalized(item.company)): item
            for item in parse_review(REPO / args.recover_empty_from)
        }
    excluded_names = (
        warm_names
        | permanent_names
        | manual_hold_names
        | locked_names
        | already_sent_names
    )
    saved = [
        item for item in saved_all if normalized(item.name) not in excluded_names
    ]
    contexts, meta = build_replay_contexts(
        workspace=workspace,
        reconcile_path=reconcile_path,
        approved_sends=approved_sends,
        season=args.season,
    )
    mapped = [(item, _context_for_saved(item, contexts)) for item in saved]
    missing = [item.name for item, context in mapped if context is None]
    if missing:
        raise RuntimeError(f"saved drafts no longer map to current backlog: {missing}")

    proof_beats = load_proof_beats(workspace / "proof_beats.yml")
    profile_path = REPO / "Profile" / "profile.md"
    profile_text = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""

    reused: dict[str, tuple] = {}
    targets: list[tuple] = []
    recovered_failed_targets = 0
    forced_composer_hold_ids: set[str] = set()
    operator_names = set(VERBATIM_DRAFTS)
    current_decision_split: Counter[Ask] = Counter()
    target_candidates_seen = 0
    for saved_item, context in mapped:
        assert context is not None
        current = copy.deepcopy(context.draft)
        if current.decision.action is not Action.ASK and current.decision.ask is Ask.NONE:
            pass
        elif current.decision.ask is not Ask.NONE:
            current_decision_split[current.decision.ask] += 1

        key = normalized(saved_item.name)
        if key in VERBATIM_DRAFTS:
            ask, message = VERBATIM_DRAFTS[key]
            current.decision.ask = ask
            if ask is Ask.NAME:
                current.capability = Capability.CAN_NAME
                current.decision.create_direct_ask = False
            current.message = message
            current.compose_source = "operator_verbatim_20260819"
            reused[current.contact_id] = (current, context)
            continue

        prior = recovery_by_key.get(
            (normalized(saved_item.name), normalized(saved_item.company))
        )
        recover_failed_target = bool(
            args.recover_empty_from
            and prior is not None
            and (
                current.decision.ask is Ask.INTEL
                or prior.ask is not current.decision.ask
            )
        )
        if recover_failed_target:
            if prior is None or not prior.message.strip():
                raise RuntimeError(
                    f"no prior paid-for copy available for {saved_item.name}"
                )
            current.message = prior.message
            current.compose_source = "saved_after_composer_connection_error"
            _review_one(current, context, proof_beats, profile_text)
            current.critic_flags = list(
                dict.fromkeys(
                    [*current.critic_flags, "composer_unavailable:APIConnectionError"]
                )
            )
            current.critic_passed = False
            reused[current.contact_id] = (current, context)
            forced_composer_hold_ids.add(current.contact_id)
            recovered_failed_targets += 1
            continue

        if not current.decision.emits_message:
            reused[current.contact_id] = (current, context)
            continue
        needs_regeneration = (
            not saved_item.message.strip()
            if args.resume_empty
            else (
                current.decision.ask is Ask.INTEL
                or saved_item.ask is not current.decision.ask
            )
        )
        if needs_regeneration:
            target_candidates_seen += 1
            if not args.limit_targets or len(targets) < args.limit_targets:
                targets.append((saved_item, context))
                continue

        current.message = saved_item.message
        current.compose_source = "saved_unaffected_20260819"
        _review_one(current, context, proof_beats, profile_text)
        prior_composer_flags = [
            flag
            for flag in saved_item.old_flags
            if flag.startswith("composer_unavailable:")
        ]
        if prior_composer_flags:
            current.critic_flags = list(
                dict.fromkeys([*current.critic_flags, *prior_composer_flags])
            )
            current.critic_passed = False
            forced_composer_hold_ids.add(current.contact_id)
        reused[current.contact_id] = (current, context)

    seed_rows = list(reused.values())
    sentence_counts, company_ask_counts, proof_counts, banned = _seed_batch(
        seed_rows,
        proof_beats,
    )

    print(f"source_saved_drafts={len(saved_all)}")
    print(f"moved_to_first_message_review={len(warm_rows)}")
    print(f"permanently_suppressed={len(permanent_contacts)}")
    print(f"manual_followup_holds={len(manual_hold_contacts)}")
    print(f"already_sent_from_review={len(already_sent_names)}")
    print(f"locked_approved_excluded={len([s for s in saved_all if normalized(s.name) in locked_names])}")
    print(f"followup_drafts_remaining={len(saved)}")
    print(f"operator_verbatim={len(VERBATIM_DRAFTS)}")
    print(f"targeted_regeneration={len(targets)}")
    print(f"target_candidates_total={target_candidates_seen}")
    print(f"failed_targets_restored_and_held={recovered_failed_targets}")
    print(
        "current_decision_split="
        + ",".join(
            f"{ask.value}:{current_decision_split[ask]}"
            for ask in (Ask.CREATE, Ask.REFER, Ask.NAME, Ask.INTEL)
        )
    )
    print("send_actions=0")
    sys.stdout.flush()
    if not args.live and not args.recover_empty_from:
        print("anthropic_calls=0 (add --live for the targeted regeneration)")
        return 0

    regenerated = []
    if args.live:
        import anthropic

        api_key = _load_env_key()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for --live")
        client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
        regenerated = run(
            [context.item for _saved, context in targets],
            client=client,
            pursuit_season=args.season,
            proof_beats=proof_beats,
            profile_text=profile_text,
            initial_batch_sentence_counts=sentence_counts,
            initial_company_ask_counts=company_ask_counts,
            initial_proof_beat_counts=proof_counts,
            initial_banned_sentences=banned,
        )
    regenerated_by_contact = {draft.contact_id: draft for draft in regenerated}
    print(
        "composer_sources="
        + str(dict(Counter(draft.compose_source for draft in regenerated)))
    )

    merged: list[tuple] = []
    for saved_item, context in mapped:
        assert context is not None
        contact_id = context.draft.contact_id
        if contact_id in reused:
            merged.append(reused[contact_id])
        else:
            merged.append((regenerated_by_contact[contact_id], context))

    _final_critic_pass(merged, operator_names, proof_beats, profile_text)
    if forced_composer_hold_ids:
        for draft, _context in merged:
            if draft.contact_id not in forced_composer_hold_ids:
                continue
            if not any(
                flag.startswith("composer_unavailable:")
                for flag in draft.critic_flags
            ):
                draft.critic_flags.append("composer_unavailable:APIConnectionError")
            draft.critic_passed = False
    render_rows = _render_rows(merged)
    review_text = render_reissued_review(
        rows=render_rows,
        original_review=review_path,
        meta=meta,
    )
    review_text = _decorate_review(
        review_text,
        permanent_contacts=permanent_contacts,
        organization_names=organization_names,
        fivetran_rows=fivetran_rows,
        manual_hold_rows=manual_hold_rows,
        moved_count=len(warm_rows),
        current_decision_split=current_decision_split,
    )
    output = REPO / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(review_text, encoding="utf-8")

    sendable = sum(row["status"] == "release" for row in render_rows)
    critic_held = len(render_rows) - sendable
    # The preserved footer contains the prior eight no-draft deterministic holds.
    deterministic_held = len(
        re.findall(
            r"^### ",
            review_text.split("## HELD — NO DRAFT", 1)[1].split(
                "## Permanently suppressed", 1
            )[0],
            re.M,
        )
    )
    flag_counts = Counter(flag for row in render_rows for flag in row["flags"])
    print(f"drafts_regenerated={len(regenerated)}")
    print(f"sendable={sendable}")
    print(f"critic_held={critic_held}")
    print(f"deterministic_held={deterministic_held}")
    print(f"manual_followup_holds={len(manual_hold_contacts)}")
    print(f"suppressed={len(permanent_contacts) + 10}")
    print(f"fivetran_manual_holds={len(fivetran_rows)}")
    print(f"critic_flags={dict(sorted(flag_counts.items()))}")
    print(f"review={output}")
    print(f"warm_review={REPO / args.warm_review_output}")
    print("send_actions=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

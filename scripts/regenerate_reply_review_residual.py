#!/usr/bin/env python3
"""Regenerate only the deterministic residual from a saved review pack.

The command replays the current critic and decision layer before it permits a
model call. Released copy is reused byte-for-byte; deterministic holds and
2027 suppressions never reach the writer. Nothing in this command can send a
LinkedIn message or create a send queue.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.recritic_reply_review import (  # noqa: E402
    _context_for_saved,
    _prior_flagged_names,
    build_replay_contexts,
    parse_review,
    replay,
)
from scripts.run_reply_engine_all_lanes import (  # noqa: E402
    _latest_reconcile_artifact,
    _load_env_key,
    build_combined_backlog,
    render_review,
)
from outreach.reply_engine import (  # noqa: E402
    Action,
    Ask,
    ThreadState,
    persist_reopen_conditions,
    run,
    summarize,
)
from outreach.reply_engine.critic import (  # noqa: E402
    batch_repetition_sentences,
    company_ask_key,
    company_ask_sentences,
)
from outreach.reply_engine.models import requires_availability_qualifier  # noqa: E402
from outreach.reply_engine.proof import load_proof_beats, used_proof_beats  # noqa: E402
from outreach.reply_engine.touches import effective_touch_cap  # noqa: E402
from outreach.tracking import OutreachWorkbook  # noqa: E402


def _preflight_contract(
    *,
    rows: list[dict],
    contexts,
    expected_residual: int,
    expected_company_ask_flags: int,
) -> tuple[list[dict], Counter[Ask]]:
    residual = [row for row in rows if row["status"] == "regenerate"]
    if len(residual) != expected_residual:
        raise RuntimeError(
            f"refusing transmission: expected {expected_residual} residual rows, "
            f"found {len(residual)}"
        )

    repeated_company_asks = sum(
        flag.startswith("repeated_company_ask:")
        for row in residual
        for flag in row["flags"]
    )
    if repeated_company_asks != expected_company_ask_flags:
        raise RuntimeError(
            "refusing transmission: expected "
            f"{expected_company_ask_flags} repeated-company-ask flags, "
            f"found {repeated_company_asks}"
        )

    failures: list[str] = []
    ask_split: Counter[Ask] = Counter()
    vamshi_ask = Ask.NONE
    cap = effective_touch_cap(reopen_condition_fired=False)
    for context in contexts:
        draft = context.draft
        decision = draft.decision
        if decision.action is Action.ASK and decision.ask is not Ask.NONE:
            ask_split[decision.ask] += 1
        if draft.name == "Vamshi Ramarapu":
            vamshi_ask = decision.ask

        if decision.emits_message and requires_availability_qualifier(
            decision, draft.read
        ) and not decision.availability_qualifier:
            failures.append(f"{draft.name}: missing structured availability qualifier")

        if decision.emits_message and decision.campaign_track == "large_company":
            normalized_goal = decision.goal.casefold()
            if not (
                "full-time" in normalized_goal
                or "2027" in normalized_goal
                or "new-grad" in normalized_goal
            ):
                failures.append(f"{draft.name}: large-company goal did not shift")
            if "fall" in normalized_goal and "intern" in normalized_goal:
                failures.append(f"{draft.name}: large-company goal still targets fall internship")

        if (
            decision.emits_message
            and decision.campaign_track == "startup"
            and decision.ask is Ask.CREATE
            and context.item.touch_count > 0
            and not decision.create_direct_ask
        ):
            failures.append(f"{draft.name}: repeated startup CREATE is not a direct proposal")

        if (
            draft.thread_state is ThreadState.NO_CONTEXT
            and decision.campaign_track == "large_company"
            and context.item.touch_count + 1 >= cap
            and decision.action is not Action.SUPPRESS
        ):
            failures.append(f"{draft.name}: capped large-company contact was not suppressed")

    if vamshi_ask is not Ask.NAME:
        failures.append(f"Vamshi Ramarapu: expected NAME, found {vamshi_ask.value}")
    if failures:
        raise RuntimeError("preflight contract failed:\n- " + "\n- ".join(failures))
    return residual, ask_split


def _seed_batch_state(released_rows: list[dict], proof_beats):
    sentence_counts: Counter[str] = Counter()
    company_ask_counts: Counter[tuple[str, str]] = Counter()
    proof_counts: Counter[tuple[Ask, str]] = Counter()
    banned: list[str] = []
    for row in released_rows:
        saved = row["draft"]
        context = row["context"]
        decision = context.draft.decision
        non_ask_sentences = batch_repetition_sentences(saved.message, decision)
        for sentence in non_ask_sentences:
            sentence_counts[sentence.casefold()] += 1
        banned.extend(non_ask_sentences)
        for question in company_ask_sentences(saved.message, decision):
            key = company_ask_key(context.draft.company, question)
            if all(key):
                company_ask_counts[key] += 1
        for beat in used_proof_beats(saved.message, proof_beats):
            proof_counts[(decision.ask, beat.beat_id)] += 1
    return sentence_counts, company_ask_counts, proof_counts, banned


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("review")
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument(
        "--approved-sends",
        default="artifacts/20260814-approved-sends.md",
    )
    parser.add_argument("--reconcile-artifact", default="")
    parser.add_argument("--season", default="fall")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--expect-residual", type=int, default=295)
    parser.add_argument("--expect-company-ask-flags", type=int, default=74)
    parser.add_argument(
        "--prior-audit",
        default="artifacts/20260818-followup-copy-pre-regeneration-audit.md",
    )
    parser.add_argument(
        "--output",
        default="artifacts/20260818-linkedin-followup-review-final.md",
    )
    args = parser.parse_args()

    workspace = REPO / args.workspace
    proof_beats = load_proof_beats(workspace / "proof_beats.yml")
    profile_path = REPO / "Profile" / "profile.md"
    profile_text = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""
    saved = parse_review(REPO / args.review)
    reconcile_path = (
        REPO / args.reconcile_artifact
        if args.reconcile_artifact
        else _latest_reconcile_artifact()
    )
    approved_sends = REPO / args.approved_sends
    contexts, full_meta = build_replay_contexts(
        workspace=workspace,
        reconcile_path=reconcile_path,
        approved_sends=approved_sends,
        season=args.season,
    )
    _backlog, preflight_tasks, _ = build_combined_backlog(
        workspace=workspace,
        reconcile_path=reconcile_path,
        approved_sends=approved_sends,
        season=args.season,
    )
    rows, replay_metrics, _full_ask_split = replay(
        saved,
        contexts,
        proof_beats,
        profile_text,
        _prior_flagged_names(REPO / args.prior_audit, "terminal_touch_not_named"),
    )

    for row in rows:
        row["context"] = _context_for_saved(row["draft"], contexts)
    missing = [row["draft"].name for row in rows if row["context"] is None]
    if missing:
        raise RuntimeError(f"saved review rows no longer map to the backlog: {missing}")

    residual_rows, ask_split = _preflight_contract(
        rows=rows,
        contexts=contexts,
        expected_residual=args.expect_residual,
        expected_company_ask_flags=args.expect_company_ask_flags,
    )
    released_rows = [row for row in rows if row["status"] == "release"]
    sentence_counts, company_ask_counts, proof_counts, banned = _seed_batch_state(
        released_rows,
        proof_beats,
    )

    residual_inputs = [row["context"].item for row in residual_rows]
    if len({item.contact.contact_id for item in residual_inputs}) != len(residual_inputs):
        raise RuntimeError("refusing transmission: duplicate contact in residual inputs")

    print(f"saved_drafts={replay_metrics['total']}")
    print(f"released_reused={len(released_rows)}")
    print(f"residual_to_regenerate={len(residual_inputs)}")
    print(f"repeated_company_ask_rows={args.expect_company_ask_flags}")
    print(
        "corrected_ask_split="
        + ",".join(
            f"{ask.value}:{ask_split[ask]}"
            for ask in (Ask.CREATE, Ask.REFER, Ask.NAME, Ask.INTEL)
        )
    )
    print("contract=availability,individual_authority,large_company_goal")
    print("contract=company_ask_variation,startup_create_direct,large_cap_suppress")
    print("validation_batch=0")
    print("send_actions=0")
    sys.stdout.flush()
    if not args.live:
        print("model_calls=0 (add --live to regenerate the audited residual)")
        return 0

    import anthropic

    api_key = _load_env_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required for --live")
    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
    regenerated = run(
        residual_inputs,
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

    merged = []
    merged_contact_ids: set[str] = set()
    for row in rows:
        context = row["context"]
        current = context.draft
        if row["status"] == "regenerate":
            current = regenerated_by_contact[context.item.contact.contact_id]
        elif row["status"] == "release":
            saved_draft = row["draft"]
            current.message = saved_draft.message
            current.compose_source = "saved_release"
            current.critic_flags = []
            current.critic_passed = True
        merged.append(current)
        merged_contact_ids.add(current.contact_id)

    additional_2027 = [
        context.draft
        for context in contexts
        if context.draft.contact_id not in merged_contact_ids
        and context.draft.decision.action is Action.SUPPRESS
        and "preserve for 2027 re-entry" in context.draft.decision.reason
    ]
    reopen_persisted = persist_reopen_conditions(
        OutreachWorkbook(workspace),
        [context.draft for context in contexts],
    )

    lane_counts = Counter(draft.segment for draft in merged)
    review_meta = {
        "locked_approved_threads_excluded": full_meta.get(
            "locked_approved_threads_excluded", 0
        ),
        **{f"combined_{lane}": count for lane, count in lane_counts.items()},
    }
    output = REPO / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_review(
            merged,
            summarize(merged),
            review_meta,
            preflight_tasks,
            additional_2027_suppressions=additional_2027,
        ),
        encoding="utf-8",
    )

    critic_held = [
        draft for draft in regenerated if draft.message and not draft.critic_passed
    ]
    deterministic_held = [
        draft for draft in merged if not draft.message and draft.decision.action is Action.HOLD
    ]
    sendable = [
        draft
        for draft in merged
        if draft.message
        and draft.critic_passed
        and draft.decision.action is not Action.HOLD
    ]
    suppressed_count = sum(
        draft.decision.action is Action.SUPPRESS
        and "preserve for 2027 re-entry" in draft.decision.reason
        for draft in [*merged, *additional_2027]
    )
    print(f"drafts_regenerated={len(regenerated)}")
    print(f"sendable={len(sendable)}")
    print(f"critic_held={len(critic_held)}")
    print(f"deterministic_held={len(deterministic_held)}")
    print(f"suppressed_for_2027={suppressed_count}")
    print(f"reopen_conditions_persisted={reopen_persisted}")
    for draft in [*critic_held, *deterministic_held]:
        reason = ", ".join(draft.critic_flags) or draft.decision.reason
        print(f"held={draft.name} — {reason}")
    print(f"review={output}")
    print(f"completed_at={datetime.now(UTC).isoformat()}")
    print("send_actions=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

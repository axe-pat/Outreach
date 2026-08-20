#!/usr/bin/env python3
"""Transmit only unheld messages from an operator-reviewed reply-engine pack.

This is an explicit, one-time execution boundary.  It does not reopen the
legacy follow-up writer and does not change the recurring reply engine's
artifact-only behavior.  Every live send is gated on an exact recipient name
and an unchanged latest LinkedIn message, then persisted immediately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.recritic_reply_review import (  # noqa: E402
    _context_for_saved,
    build_replay_contexts,
    parse_review,
)
from scripts.reissue_followup_pack_20260819 import VERBATIM_DRAFTS  # noqa: E402
from scripts.run_reply_engine_all_lanes import _locked_names  # noqa: E402
from outreach.cli import persist_linkedin_followup_send_result  # noqa: E402
from outreach.config import OutreachSettings  # noqa: E402
from outreach.reply_engine import Action, ThreadState  # noqa: E402
from outreach.services.linkedin import LinkedInScraper  # noqa: E402
from outreach.tracking import OutreachWorkbook  # noqa: E402


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().casefold()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def build_batch(
    *,
    review_path: Path,
    reconcile_path: Path,
    workspace: Path,
    approved_sends: Path,
    season: str,
) -> tuple[list[dict], list[dict], dict]:
    saved_drafts = parse_review(review_path)
    contexts, meta = build_replay_contexts(
        workspace=workspace,
        reconcile_path=reconcile_path,
        approved_sends=approved_sends,
        season=season,
    )
    workbook = OutreachWorkbook(workspace)
    sent_messages = {
        (touchpoint.contact_id, normalized(touchpoint.message_text))
        for touchpoint in workbook.list_touchpoints()
        if normalized(touchpoint.status) == "sent"
        and normalized(touchpoint.channel.value) == "linkedin"
    }
    locked_names = _locked_names(approved_sends)
    operator_names = set(VERBATIM_DRAFTS)
    selected: list[dict] = []
    excluded: list[dict] = []

    for saved in saved_drafts:
        reason = ""
        context = _context_for_saved(saved, contexts)
        if saved.old_flags:
            reason = "held_in_review_pack"
        elif normalized(saved.name) in locked_names:
            reason = "approved_send_thread_locked"
        elif context is None:
            reason = "missing_current_context"
        elif context.draft.thread_state is not ThreadState.NO_CONTEXT:
            reason = f"current_thread_state:{context.draft.thread_state.value}"
        elif context.draft.decision.action is not Action.ASK:
            reason = f"current_action:{context.draft.decision.action.value}"
        elif (
            context.draft.decision.ask is not saved.ask
            and normalized(saved.name) not in operator_names
        ):
            reason = (
                f"current_ask_changed:{saved.ask.value}->"
                f"{context.draft.decision.ask.value}"
            )
        elif not saved.message.strip():
            reason = "empty_message"
        elif (
            context.draft.contact_id,
            normalized(saved.message),
        ) in sent_messages:
            reason = "exact_message_already_sent"

        if reason:
            excluded.append(
                {
                    "name": saved.name,
                    "company": saved.company,
                    "ask": saved.ask.value,
                    "reason": reason,
                }
            )
            continue

        assert context is not None
        expected_latest = context.draft.last_message.strip()
        if not expected_latest:
            reason = "missing_expected_latest_message"
            excluded.append(
                {
                    "name": saved.name,
                    "company": saved.company,
                    "ask": saved.ask.value,
                    "reason": reason,
                }
            )
            continue
        selected.append(
            {
                "contact_id": context.draft.contact_id,
                "organization_id": context.draft.organization_id,
                "name": saved.name,
                "company": saved.company,
                "draft_kind": f"reply_engine_{saved.ask.value}",
                "send_recommendation": "safe_to_send",
                "draft_message": saved.message,
                "latest_message": expected_latest,
                "last_sender": context.draft.last_sender,
                "thread_id": "",
                "thread_url": "",
                "source_status": "reply_engine_reviewed",
                "_reviewed_require_exact_name": True,
            }
        )

    return selected, excluded, meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--review",
        default="artifacts/20260819-linkedin-followup-review-reissued.md",
    )
    parser.add_argument(
        "--reconcile-artifact",
        default="artifacts/20260820-073852-linkedin-message-reconcile.json",
    )
    parser.add_argument(
        "--approved-sends",
        default="artifacts/20260814-approved-sends.md",
    )
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--season", default="fall")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    settings = OutreachSettings()
    review_path = (REPO / args.review).resolve()
    reconcile_path = (REPO / args.reconcile_artifact).resolve()
    approved_sends = (REPO / args.approved_sends).resolve()
    workspace = (REPO / args.workspace).resolve()
    selected, excluded, meta = build_batch(
        review_path=review_path,
        reconcile_path=reconcile_path,
        workspace=workspace,
        approved_sends=approved_sends,
        season=args.season,
    )

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    batch_path = settings.artifacts_dir / f"{stamp}-reply-engine-reviewed-send-batch.json"
    results_path = settings.artifacts_dir / f"{stamp}-reply-engine-reviewed-send-results.json"
    base_payload = {
        "schema": "reply-engine-reviewed-send-v1",
        "execute": args.execute,
        "review_artifact": str(review_path),
        "review_sha256": sha256(review_path),
        "reconcile_artifact": str(reconcile_path),
        "reconcile_sha256": sha256(reconcile_path),
        "approved_sends_artifact": str(approved_sends),
        "selected_count": len(selected),
        "selected_ask_split": dict(
            Counter(row["draft_kind"].removeprefix("reply_engine_") for row in selected)
        ),
        "excluded_count": len(excluded),
        "excluded_reason_counts": dict(Counter(row["reason"] for row in excluded)),
        "excluded": excluded,
        "backlog_meta": meta,
        "drafts": selected,
    }
    write_json(batch_path, base_payload)
    print(f"batch={batch_path}", flush=True)
    print(f"selected={len(selected)} excluded={len(excluded)}", flush=True)
    print(f"ask_split={base_payload['selected_ask_split']}", flush=True)

    if not args.execute:
        print("dry_run_only=true", flush=True)
        return 0

    progress = {
        **{key: value for key, value in base_payload.items() if key != "drafts"},
        "status": "running",
        "results": [],
        "status_counts": {},
        "touchpoints_added": 0,
    }
    write_json(results_path, progress)
    workbook = OutreachWorkbook(workspace)

    def on_result(_draft, result, _results) -> None:
        created = persist_linkedin_followup_send_result(
            workbook=workbook,
            result=result,
            source_artifact=review_path,
            send_artifact=results_path,
        )
        progress["results"].append(asdict(result))
        progress["touchpoints_added"] += int(created)
        progress["status_counts"] = dict(
            Counter(row["status"] for row in progress["results"])
        )
        write_json(results_path, progress)
        print(
            f"[{len(progress['results'])}/{len(selected)}] "
            f"{result.status}: {result.name} — {result.company}",
            flush=True,
        )

    try:
        LinkedInScraper(settings).send_followup_messages(
            selected,
            execute=True,
            limit=len(selected),
            include_optional=True,
            max_scrolls=60,
            on_result=on_result,
        )
    except Exception as exc:
        progress["status"] = "interrupted"
        progress["error"] = f"{type(exc).__name__}: {exc}"
        write_json(results_path, progress)
        raise

    progress["status"] = "complete"
    progress["completed_at"] = datetime.now(UTC).isoformat()
    write_json(results_path, progress)
    print(f"results={results_path}", flush=True)
    print(f"status_counts={progress['status_counts']}", flush=True)
    print(f"touchpoints_added={progress['touchpoints_added']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
